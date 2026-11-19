import re

import interactions as ipy
from interactions import OptionType, SlashContext, slash_option
from pydantic import ValidationError
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, SUCCESS_EMOJI, TICK_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final, match_group
from elo_bot.features.finals import lifecycle
from elo_bot.features.finals.result_drafts import (
    ResultDraftExpired,
    create_result_draft,
    finish_result_draft,
    result_draft,
)
from elo_bot.features.finals.result_receipts import result_receipt
from elo_bot.features.finals.result_ui import require_result_role, result_embed, result_form, score_embed
from elo_bot.features.finals.results import FinalResultSubmission


class Results:
    final = final
    match_group = match_group

    async def open_result_form(self, ctx, edition, node_id, *, winner=None, outcome="Played", reason=None):
        node = edition.nodes.get(node_id)
        if node is None or node.bye or not all(node.contestants):
            raise FinalError("Choose a playable match from `/final bracket view`.")
        require_result_role(ctx, node.result_id is not None or winner is not None or outcome == "Forfeit")
        previous = await FinalMatchResult.get(node.result_id) if node.result_id else None
        await ctx.send_modal(result_form(edition, node, str(ctx.author.id), previous,
                                         winner=winner, outcome=outcome, reason=reason))

    @match_group.subcommand(sub_cmd_name="result", sub_cmd_description="Submit or correct all seven district scores")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    @slash_option(name="winner", description="Explicit tie or forfeit winner", opt_type=OptionType.USER)
    @slash_option(name="reason", description="Correction, tie or forfeit reason", opt_type=OptionType.STRING)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_match_result(self, ctx: SlashContext, match: str, winner: ipy.User = None,
                                 reason: str = None):
        edition = await self._ongoing_edition(ctx, access="manager")
        await self.open_result_form(ctx, edition, match, winner=str(winner.id) if winner else None,
                                    reason=reason)

    @match_group.subcommand(sub_cmd_name="forfeit", sub_cmd_description="Record an unplayed forfeit with zero scores")
    @slash_option(name="match", description="Match, e.g. QF1", opt_type=OptionType.STRING, required=True,
                  autocomplete=True)
    @slash_option(name="winner", description="Advancing entrant", opt_type=OptionType.USER, required=True)
    @slash_option(name="reason", description="Forfeit reason", opt_type=OptionType.STRING, required=True)
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_match_forfeit(self, ctx: SlashContext, match: str, winner: ipy.User, reason: str):
        edition = await self._ongoing_edition(ctx, access="manager")
        await self.open_result_form(ctx, edition, match, winner=str(winner.id), outcome="Forfeit", reason=reason)

    @ipy.modal_callback(re.compile(r"^finalres\|.+$"))
    @final_action
    async def final_result_modal(self, ctx: ipy.ModalContext, **response):
        _, final_id, node_id, owner, version, revision, attempt, winner, forfeit = ctx.custom_id.split("|")
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
            request = FinalResultSubmission(
                final_id=edition.id, node_id=node_id, actor_id=owner, operation_key=f"final-modal:{ctx.id}",
                expected_version=int(version), contestant_revision=int(revision), attempt=int(attempt),
                scores={user: response[f"player{side + 1}"].splitlines()
                        for side, user in enumerate(node.contestants)},
                winner=None if winner == "-" else winner, outcome="Forfeit" if forfeit == "1" else "Played",
                reason=response.get("reason"),
            )
            draft = await create_result_draft(edition, request)
            snapshot = draft.snapshot
            guild = await member_guild(ctx)
            names = await member_names(guild, snapshot.contestants)
            preview = score_embed(edition, node_id, snapshot, names)
            if request.reason:
                preview.add_field(name="**Reason**", value=request.reason)
        except (FinalError, ValidationError) as error:
            await ctx.send(f"{ERROR_EMOJI} " + (str(error) if isinstance(error, FinalError)
                                               else "Enter seven valid stars.destruction lines per player."), ephemeral=True)
            return
        except PyMongoError:
            await ctx.send(f"{ERROR_EMOJI} Could not create confirmation. Submit the result again.", ephemeral=True)
            return
        controls = ipy.ActionRow(
            ipy.Button(label="Cancel", emoji=CROSS_EMOJI, style=ipy.ButtonStyle.DANGER, custom_id=f"finalcancel|{draft.id}"),
            ipy.Button(label="Confirm", emoji=TICK_EMOJI, style=ipy.ButtonStyle.SUCCESS, custom_id=f"finalconfirm|{draft.id}"),
        )
        await ctx.send("Confirm this result?", embed=preview, components=controls, ephemeral=True)

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
        names = await member_names(guild, result.snapshot.contestants)
        await ctx.edit_origin(content=result_receipt(edition, result, names, pending=bool(pending),
                                                    round_completed=round_completed),
                              embed=result_embed(edition, result, names), components=[])
