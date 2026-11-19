import re
from typing import assert_never

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals.dashboard import (
    FINAL_CHANGED, DashboardState, available_actions, dashboard_controls, dashboard_embed,
)
from elo_bot.features.finals.invitations import pending_invitations
from elo_bot.features.finals.lifecycle import reload_final


class Dashboard:
    async def _dashboard_state(self, edition) -> DashboardState:
        markets = await FinalBettingMarket.find({"final_id": str(edition.id)}).to_list()
        submitted = await FinalPrediction.find({"final_id": str(edition.id), "submitted_at": {"$ne": None}}).count()
        invitations = await pending_invitations(edition.id) if edition.state == "REGISTRATION" else []
        return DashboardState(edition, markets, submitted, invitations)

    async def _show_dashboard(self, ctx, edition, *, outcome=None, edit=False):
        manager = any(ctx.author.has_role(role) for role in LVL1_ROLES)
        state = await self._dashboard_state(edition)
        names = await member_names(await member_guild(ctx), [
            *edition.entrant_ids, *(invitation.user_id for invitation in state.invitations)])
        embed = dashboard_embed(state, names, manager=manager)
        controls = dashboard_controls(state, str(ctx.author.id)) if manager else []
        if edit and isinstance(ctx, ipy.ModalContext):
            await ctx.edit("@original", content=outcome or "", embed=embed, components=controls)
        elif edit:
            await ctx.edit_origin(content=outcome or "", embed=embed, components=controls)
        else:
            await ctx.send(content=outcome, embed=embed, components=controls, ephemeral=True)

    @ipy.component_callback(re.compile(r"^finaldash\|(bracket|open|close|round|recover|end|refresh)\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_dashboard_action(self, ctx: ipy.ComponentContext):
        _, action, final_id, owner = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        if action == "refresh":
            await self._show_dashboard(ctx, edition, edit=True)
            return
        state = await self._dashboard_state(edition)
        if action not in dict(available_actions(state)):
            # Another action or the background recovery already moved the Final on; show where it stands now.
            await self._show_dashboard(ctx, edition, edit=True, outcome=FINAL_CHANGED)
            return
        try:
            match action:
                case "bracket":
                    await self._open_bracket_editor(ctx, edition, edit=True)
                    return
                case "open" | "close":
                    outcome = await self._set_predictions(edition, action == "open")
                case "round":
                    outcome = await self._start_round(edition, await member_guild(ctx))
                case "recover":
                    pending = await self._recover(edition)
                    outcome = (f"{IMPORTANT_EMOJI} " + "\n".join(pending) if pending
                               else f"{SUCCESS_EMOJI} Final updates are complete.")
                case "end":
                    await self._confirm_final_action(ctx, edition, "end", edit=True)
                    return
                case unreachable:
                    assert_never(unreachable)
        except FinalError as error:
            # A refused action keeps the dashboard, so the manager sees why next to what they can do instead.
            outcome = f"{ERROR_EMOJI} {error}"
        await self._show_dashboard(ctx, await reload_final(edition), outcome=outcome, edit=True)
