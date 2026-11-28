import re

import interactions as ipy
from interactions import OptionType, SlashContext, slash_option
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FINAL_DISTRICTS, FinalError, is_final_match
from elo_bot.core.match_results import is_valid_match_result_entry
from elo_bot.db import Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.members import member_guild
from elo_bot.features.finals.labels import final_member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final, match_group
from elo_bot.features.finals import lifecycle
from elo_bot.features.finals.matches import require_current_match
from elo_bot.features.finals.result_drafts import (
    ResultDraftExpired,
    create_result_draft,
    edit_result_draft,
    finish_result_draft,
    result_draft,
)
from elo_bot.features.finals.result_receipts import result_receipt
from elo_bot.features.finals.result_ui import (
    preview_controls,
    require_result_role,
    result_embed,
    result_form,
    score_embed,
    stored_result_fields,
)
from elo_bot.features.finals.results import FinalResultSubmission


class Results:
    final = final
    match_group = match_group

    async def open_result_form(self, ctx, edition, node_id, *, winner=None, outcome="Played", reason=None,
                               source: Match | None = None):
        node = edition.nodes.get(node_id)
        if node is None or node.bye or not all(node.contestants):
            raise FinalError("Choose a playable match from `/final bracket view`.")
        require_result_role(ctx, node.result_id is not None or winner is not None or outcome == "Forfeit")
        previous = await FinalMatchResult.get(node.result_id) if node.result_id else None
        stored = stored_result_fields(source) if source is not None else None
        await ctx.send_modal(result_form(edition, node, str(ctx.author.id), previous,
                                         winner=winner, outcome=outcome, reason=reason, stored=stored,
                                         replay_revision=source.final_replay_revision if source is not None else 0))

    @match_group.subcommand(sub_cmd_name="result", sub_cmd_description="Submit or correct all seven district scores")
    @slash_option(name="winner", description="Forfeit winner", opt_type=OptionType.USER)
    @slash_option(name="reason", description="Correction or forfeit reason", opt_type=OptionType.STRING)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_match_result(self, ctx: SlashContext, winner: ipy.User = None, reason: str = None):
        source = await Match.find_one(Match.thread == str(ctx.channel.id))
        if source is None or not is_final_match(source):
            raise FinalError("Use this command in an Elo Final match thread.")
        edition = await self._ongoing_edition(ctx, source.final_id, access="manager")
        node = require_current_match(edition, source)
        await self.open_result_form(ctx, edition, node.id, winner=str(winner.id) if winner else None,
                                    reason=reason, source=source)

    @match_group.subcommand(sub_cmd_name="forfeit", sub_cmd_description="Forfeit a player's unfinished Final match")
    @slash_option(name="player", description="Player who is forfeiting", opt_type=OptionType.USER, required=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_match_forfeit(self, ctx: SlashContext, player: ipy.User):
        edition = await self._ongoing_edition(ctx, access="manager")
        player_id = str(player.id)
        unfinished = [node for node in edition.nodes.values()
                      if player_id in node.contestants and all(node.contestants)
                      and not node.bye and node.winner is None and node.result_id is None]
        if not unfinished:
            raise FinalError("That player has no unfinished Final match.")
        if len(unfinished) > 1:
            raise FinalError("That player has multiple unfinished Final matches. Check `/final bracket view`.")
        node = unfinished[0]
        opponent = next(user for user in node.contestants if user and user != player_id)
        await self.open_result_form(ctx, edition, node.id, winner=opponent, outcome="Forfeit")

    @ipy.modal_callback(re.compile(r"^finalres\|.+$"))
    @final_action
    async def final_result_modal(self, ctx: ipy.ModalContext, **response):
        _, final_id, node_id, owner, version, revision, attempt, winner, forfeit, *replay = ctx.custom_id.split("|")
        await ctx.defer(ephemeral=True)
        try:
            edition = await self._ongoing_edition(ctx, final_id)
            node = edition.nodes.get(node_id)
            if node is None:
                raise FinalError("The match changed. Open its current result form.")
            needs_manager = node.result_id is not None or winner != "-" or forfeit == "1"
            require_result_role(ctx, needs_manager)
            if owner != str(ctx.author.id):
                raise FinalError("You cannot interact with another user's components.")
            scores = {user: ["0.00"] * len(FINAL_DISTRICTS) if forfeit == "1"
                      else response[f"player{side + 1}"].splitlines()
                      for side, user in enumerate(node.contestants)}
            request = FinalResultSubmission(
                final_id=edition.id, node_id=node_id, actor_id=owner, operation_key=f"final-modal:{ctx.id}",
                expected_version=int(version), contestant_revision=int(revision), attempt=int(attempt),
                scores=scores,
                winner=None if winner == "-" else winner, outcome="Forfeit" if forfeit == "1" else "Played",
                reason=response.get("reason"), replay_revision=int(replay[0]) if replay else None,
            )
            draft = await create_result_draft(edition, request)
            preview, controls = await self._result_preview(ctx, edition, draft)
        except (FinalError, ValidationError) as error:
            await ctx.send(f"{ERROR_EMOJI} " + (str(error) if isinstance(error, FinalError)
                                               else "Enter seven valid stars.destruction lines per player."), ephemeral=True)
            return
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not create confirmation. Submit the result again.", ephemeral=True)
            return
        await ctx.send("Confirm this result?", embed=preview, components=controls, ephemeral=True)

    async def _result_preview(self, ctx, edition, draft):
        names = await final_member_names(edition, await member_guild(ctx), draft.snapshot.contestants)
        preview = score_embed(edition, draft.node_id, draft.snapshot, names)
        if draft.reason:
            preview.add_field(name="**Reason**", value=draft.reason)
        return preview, preview_controls(edition, draft)

    @ipy.component_callback(re.compile(r"^finaledit\|[a-f0-9]{24}$"))
    @final_action
    async def final_result_edit(self, ctx: ipy.ComponentContext):
        try:
            draft = await result_draft(ctx.custom_id.split("|")[1], str(ctx.author.id), ctx.guild_id)
        except ResultDraftExpired as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            return
        edition = await self._ongoing_edition(ctx, str(draft.final_id))
        side, district = ctx.values[0].split("|")
        user = draft.snapshot.contestants[int(side)]
        await ctx.send_modal(ipy.Modal(
            ipy.ShortText(label=f"{edition.player_nicknames[user]} · {FINAL_DISTRICTS[int(district)]}"[:45],
                          custom_id="score", value=draft.snapshot.scores[user][int(district)], max_length=10,
                          placeholder="e.g. 2.83"),
            title=f"Season {edition.season_number} Final · {draft.node_id}",
            custom_id=f"finaleditscore|{draft.id}|{side}|{district}"))

    @ipy.modal_callback(re.compile(r"^finaleditscore\|[a-f0-9]{24}\|[01]\|[0-6]$"))
    @final_action
    async def final_result_edit_score(self, ctx: ipy.ModalContext, score: str):
        _, draft_id, side, district = ctx.custom_id.split("|")
        # Checked before deferring so a typo leaves the preview in place instead of replacing it with the error.
        if not is_valid_match_result_entry(score):
            raise FinalError("Enter the score as stars.destruction, e.g. 2.83.")
        await ctx.defer(edit_origin=True)
        draft = await result_draft(draft_id, str(ctx.author.id), ctx.guild_id)
        draft = await edit_result_draft(draft, draft.snapshot.contestants[int(side)], int(district), score)
        edition = await self._ongoing_edition(ctx, str(draft.final_id))
        preview, controls = await self._result_preview(ctx, edition, draft)
        await ctx.send("Confirm this result?", embed=preview, components=controls)

    @ipy.component_callback(re.compile(r"^final(confirm|cancel)\|[a-f0-9]{24}$"))
    @final_action
    async def final_result_confirmation(self, ctx: ipy.ComponentContext):
        action, draft_id = ctx.custom_id.split("|")
        try:
            draft = await result_draft(draft_id, str(ctx.author.id), ctx.guild_id)
        except ResultDraftExpired as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            return
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, str(draft.final_id))
        node = edition.nodes.get(draft.node_id)
        if node is None:
            raise FinalError("The match changed. Open its current result form.")
        try:
            require_result_role(ctx, draft.expected_version > 0 or draft.explicit_winner is not None
                                or draft.snapshot.outcome == "Forfeit"
                                or node.result_id is not None and node.result_id != draft.result_id)
            result, round_completed = await finish_result_draft(draft, cancel=action == "finalcancel")
        except (FinalError, ValidationError) as error:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} {error}", embeds=[], components=[])
            return
        except ipy.errors.CommandCheckFailure:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} You do not have permission to interact with this component!",
                                           embeds=[], components=[])
            return
        except PyMongoError:
            await ctx.edit_origin(content=f"{ERROR_EMOJI} Could not confirm the save. "
                "Check `/final matches` before retrying.", embeds=[], components=[])
            return
        if result is None:
            await ctx.edit_origin(content=f"{SUCCESS_EMOJI} Submission cancelled.", embeds=[], components=[])
            return
        try:
            pending = await self._recover(edition)
            edition = await lifecycle.reload_final(edition)
        except (FinalError, PyMongoError) as error:
            edition.repair_pending = True
            pending = [str(error)]
        guild = await member_guild(ctx)
        names = await final_member_names(edition, guild, result.snapshot.contestants)
        await ctx.edit_origin(content=result_receipt(edition, result, names, pending=bool(pending),
                                                    round_completed=round_completed),
                              embed=result_embed(edition, result, names), components=[])
