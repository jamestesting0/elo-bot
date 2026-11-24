import re
from io import BytesIO

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from bson.errors import InvalidId
from interactions import OptionType, SlashContext, slash_option

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.members import fetch_member_otherwise_user, member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import bracket_group, final
from elo_bot.features.finals.bracket_ui import bracket_controls, bracket_embed
from elo_bot.features.finals.predictions import submitted_predictions
from elo_bot.features.finals.profile_ui import profile_view
from elo_bot.features.finals.rendering import render_bracket
from elo_bot.features.finals.result_ui import result_embed


class Views:
    final = final
    bracket_group = bracket_group

    @final.subcommand(sub_cmd_name="profile", sub_cmd_description="Show a player's Final statistics")
    @slash_option(name="player", description="Player", opt_type=OptionType.USER)
    @slash_option(name="final_season", description="Final season", opt_type=OptionType.INTEGER, autocomplete=True)
    @slash_option(name="hidden", description="Only show this to you", opt_type=OptionType.BOOLEAN)
    @final_action
    async def final_profile(self, ctx: SlashContext, player: ipy.User = None, final_season: int = None,
                            hidden: bool = False):
        await ctx.defer(ephemeral=hidden)
        edition = await self._edition(ctx, final_season)
        guild = await member_guild(ctx)
        embed, controls = await profile_view(edition, player or ctx.author, str(ctx.author.id), guild=guild)
        await ctx.send(embed=embed, components=controls)

    @ipy.component_callback(re.compile(r"^finalprofile(?:refresh)?\|.+$"))
    @final_action
    async def final_profile_navigation(self, ctx: ipy.ComponentContext):
        parts = ctx.custom_id.split("|")
        if parts[1] != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        season = int(parts[3]) if parts[0] == "finalprofilerefresh" else int(ctx.values[0])
        edition = await self._edition(ctx, season)
        guild = await member_guild(ctx)
        user = await fetch_member_otherwise_user(guild, self.bot, parts[2])
        if user is None:
            raise FinalError("This player is unavailable.")
        embed, controls = await profile_view(edition, user, parts[1], guild=guild)
        await ctx.edit_origin(embed=embed, components=controls)

    @bracket_group.subcommand(sub_cmd_name="view", sub_cmd_description="View the saved Final bracket")
    @slash_option(name="final_season", description="Final season", opt_type=OptionType.INTEGER, autocomplete=True)
    @slash_option(name="hidden", description="Only show this to you", opt_type=OptionType.BOOLEAN)
    @final_action
    async def final_bracket_view(self, ctx: SlashContext, final_season: int = None, hidden: bool = False):
        await ctx.defer(ephemeral=hidden)
        edition = await self._edition(ctx, final_season)
        user_ids = set(edition.entrant_ids)
        guild = await member_guild(ctx)
        names = await member_names(guild, user_ids)
        embed = bracket_embed(edition, names, await submitted_predictions(edition))
        controls = await bracket_controls(edition)
        if not edition.nodes:
            await ctx.send(embed=embed, components=controls)
            return
        try:
            rendered = await anyio.to_thread.run_sync(render_bracket, edition)
        except FinalError as error:
            embed.add_field(name="**Image**", value=str(error))
            await ctx.send(embed=embed, components=controls)
            return
        embed.set_image(url="attachment://bracket.png")
        with BytesIO(rendered) as image:
            await ctx.send(embed=embed, components=controls, file=ipy.File(image, file_name="bracket.png"))

    @ipy.component_callback(re.compile(r"^finalresult\|.+$"))
    @final_action
    async def final_result_select(self, ctx: ipy.ComponentContext):
        await ctx.defer(ephemeral=True)
        try:
            final_id = PydanticObjectId(ctx.custom_id.split("|")[1])
        except InvalidId as error:
            raise FinalError("This Final is unavailable.") from error
        edition = await EloFinal.get(final_id)
        if edition is None:
            raise FinalError("This Final is unavailable.")
        await self._access(ctx, edition)
        node = edition.nodes.get(ctx.values[0])
        if node is None or node.bye or not node.winner:
            raise FinalError("This match has no decided result.")
        result = await FinalMatchResult.get(node.result_id) if node.result_id else None
        if result is None:
            raise FinalError("This match result is unavailable.")
        guild = await member_guild(ctx)
        names = await member_names(guild, result.snapshot.contestants)
        await ctx.send(embed=result_embed(edition, result, names), ephemeral=True)
