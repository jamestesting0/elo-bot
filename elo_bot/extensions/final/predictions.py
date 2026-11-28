import re
from io import BytesIO

import anyio
import interactions as ipy
from interactions import OptionType, SlashContext, slash_option
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.discord.members import fetch_member_otherwise_user, member_guild
from elo_bot.discord.mentions import fetch_members, member_usernames
from elo_bot.features.finals.labels import final_member_names, final_player_link
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final, predictions_group
from elo_bot.features.finals.prediction_board import (
    prediction_board,
    prediction_card,
    predictions_public,
    predictions_visible,
    submitted_prediction,
)
from elo_bot.features.finals.prediction_ui import prediction_view
from elo_bot.features.finals.predictions import (
    PredictionAction,
    apply_prediction_action,
    begin_prediction,
    change_prediction_state,
)
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.rendering import render_bracket


class Predictions:
    final = final
    predictions_group = predictions_group

    @predictions_group.subcommand(sub_cmd_name="open", sub_cmd_description="Open bracket predictions before play")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def final_predictions_open(self, ctx: SlashContext):
        await self._prediction_state(ctx, True)

    @predictions_group.subcommand(sub_cmd_name="close", sub_cmd_description="Manually close prediction submissions")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    async def final_predictions_close(self, ctx: SlashContext):
        await self._prediction_state(ctx, False)

    @final_action
    async def _prediction_state(self, ctx, open_predictions):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        await ctx.send(await self._set_predictions(edition, open_predictions), ephemeral=True)

    async def _set_predictions(self, edition, open_predictions):
        edition = await change_prediction_state(edition, open_predictions)
        content = f"{SUCCESS_EMOJI} Predictions {'opened' if open_predictions else 'closed'}."
        try:
            await publish_bracket(self.bot, edition)
        except (FinalError, PyMongoError):
            content += f" {IMPORTANT_EMOJI} The bracket post update is pending and will retry."
        return content

    @final.subcommand(sub_cmd_name="predict", sub_cmd_description="Create or resume your full bracket prediction")
    @slash_option(name="final_season", description="Final season", opt_type=OptionType.INTEGER, autocomplete=True)
    @final_action
    async def final_predict(self, ctx: SlashContext, final_season: int = None):
        await ctx.defer(ephemeral=True)
        edition = await self._edition(ctx, final_season, access="player")
        if edition.prediction_state != "OPEN" or not edition.active:
            await self._show_prediction(ctx, edition)
            return
        edition = await self._ongoing_edition(ctx, str(edition.id), access="player")
        prediction = await begin_prediction(edition, str(ctx.author.id))
        guild = await member_guild(ctx)
        names = await final_member_names(edition, guild, edition.entrant_ids)
        embed, components = prediction_view(edition, prediction, names)
        await ctx.send(embed=embed, components=components)

    @ipy.component_callback(re.compile(r"^finalpredict\|[a-f0-9]{24}$"))
    @final_action
    async def final_prediction_open(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, ctx.custom_id.split("|")[1], access="player")
        prediction = await begin_prediction(edition, str(ctx.author.id))
        names = await final_member_names(edition, await member_guild(ctx), edition.entrant_ids)
        embed, components = prediction_view(edition, prediction, names)
        await ctx.send(embed=embed, components=components)

    @ipy.component_callback(re.compile(r"^finalpred\|.+$"))
    @final_action
    async def final_prediction_action(self, ctx: ipy.ComponentContext):
        _, final_id, owner, revision, action, node = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            await ctx.send(f"{ERROR_EMOJI} You cannot interact with another user's components.", ephemeral=True)
            return
        await ctx.defer(edit_origin=True)
        try:
            edition = await self._ongoing_edition(ctx, final_id, access="player")
            prediction = await apply_prediction_action(PredictionAction(
                final_id=edition.id, user_id=owner, session_revision=int(revision), action=action,
                node_id=None if node == "-" else node, choice=ctx.values[0] if action == "pick" else None,
            ))
            guild = await member_guild(ctx)
            members = await fetch_members(guild, edition.entrant_ids)
            names = {user: final_player_link(edition, user, member) for user, member in members.items()}
            outcome = ""
            if action == "submit":
                try:
                    await publish_bracket(self.bot, edition)
                except (FinalError, PyMongoError):
                    outcome = f"{IMPORTANT_EMOJI} The bracket post update is pending and will retry."
            embed, components = prediction_view(edition, prediction, names, submitted=action == "submit",
                                                outcome=outcome)
            if prediction.stage == 3 or action == "submit":
                try:
                    picks = prediction.submitted if action == "submit" else prediction.draft
                    rendered = await anyio.to_thread.run_sync(render_bracket, edition, picks, None,
                                                              member_usernames(members))
                except FinalError as error:
                    embed, components = prediction_view(edition, prediction, names, submitted=action == "submit",
                        outcome=f"{IMPORTANT_EMOJI} Image unavailable: {error}", image_available=False)
                    await ctx.edit("@original", embed=embed, components=components, attachments=[])
                    return
                with BytesIO(rendered) as image:
                    await ctx.edit("@original", embed=embed, components=components, attachments=[],
                                   file=ipy.File(image, file_name="prediction.png"))
            else:
                await ctx.edit("@original", embed=embed, components=components, attachments=[])
        except FinalError as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}\nOpen `/final predict` to resume.",
                                  embeds=[], components=[], attachments=[])

    async def _show_prediction(self, ctx, edition):
        prediction = await submitted_prediction(edition, str(ctx.author.id))
        if prediction is None:
            raise FinalError("Predictions are closed. You didn't submit one.")
        await self._send_card(ctx, await prediction_card(edition, prediction, await member_guild(ctx), user=ctx.author),
                              ephemeral=True)

    async def _send_card(self, ctx, card, *, ephemeral):
        if card.image is None:
            await ctx.send(embed=card.embed, ephemeral=ephemeral)
            return
        with BytesIO(card.image) as image:
            await ctx.send(embed=card.embed, ephemeral=ephemeral,
                           file=ipy.File(image, file_name="prediction.png", description=card.alt_text))

    async def _visible_edition(self, ctx, season):
        edition = await self._edition(ctx, season)
        if not predictions_visible(edition, any(ctx.author.has_role(role) for role in LVL1_ROLES)):
            raise FinalError("Predictions are visible once they close.")
        return edition

    @predictions_group.subcommand(sub_cmd_name="view", sub_cmd_description="View submitted bracket predictions")
    @slash_option(name="player", description="Show this player's prediction", opt_type=OptionType.USER)
    @slash_option(name="final_season", description="Final season", opt_type=OptionType.INTEGER, autocomplete=True)
    @slash_option(name="hidden", description="Only show this to you", opt_type=OptionType.BOOLEAN)
    @final_action
    async def final_predictions_view(self, ctx: SlashContext, player: ipy.User = None, final_season: int = None,
                                     hidden: bool = False):
        edition = await self._visible_edition(ctx, final_season)
        # Before closure only managers get here, and the picks must not reach the channel.
        private = hidden or not predictions_public(edition)
        await ctx.defer(ephemeral=private)
        guild = await member_guild(ctx)
        if player is None:
            embed, components = await prediction_board(edition, guild, str(ctx.author.id), 0)
            await ctx.send(embed=embed, components=components, ephemeral=private)
            return
        prediction = await submitted_prediction(edition, str(player.id))
        if prediction is None:
            raise FinalError("This player didn't submit a prediction.")
        await self._send_card(ctx, await prediction_card(edition, prediction, guild, user=player), ephemeral=private)

    @ipy.component_callback(re.compile(r"^finalpredpage\|\d+\|\d+\|\d+$"))
    @final_action
    async def final_predictions_page(self, ctx: ipy.ComponentContext):
        _, season, owner, page = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        edition = await self._visible_edition(ctx, int(season))
        await ctx.defer(edit_origin=True)
        embed, components = await prediction_board(edition, await member_guild(ctx), owner, int(page))
        await ctx.edit("@original", embed=embed, components=components)

    @ipy.component_callback(re.compile(r"^finalpredpick\|\d+$"))
    @final_action
    async def final_predictions_pick(self, ctx: ipy.ComponentContext):
        # Anyone may open a card from a shared list; it answers privately so the list stays as it is.
        edition = await self._visible_edition(ctx, int(ctx.custom_id.split("|")[1]))
        await ctx.defer(ephemeral=True)
        prediction = await submitted_prediction(edition, ctx.values[0])
        if prediction is None:
            raise FinalError("This prediction is no longer available.")
        guild = await member_guild(ctx)
        user = await fetch_member_otherwise_user(guild, self.bot, prediction.user_id)
        await self._send_card(ctx, await prediction_card(edition, prediction, guild, user=user), ephemeral=True)
