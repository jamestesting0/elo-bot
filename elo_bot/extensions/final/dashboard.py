import re
from typing import assert_never

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI, render_emoji_markup
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import fetch_members, member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals.dashboard import (
    FINAL_CHANGED, DashboardState, available_actions, dashboard_controls, dashboard_embed,
)
from elo_bot.features.finals.invitations import pending_invitations
from elo_bot.features.finals.lifecycle import confirm_lineup, refresh_display_names, reload_final
from elo_bot.features.finals.registration_posts import sync_registration_posts


class Dashboard:
    async def _dashboard_state(self, edition) -> DashboardState:
        markets = await FinalBettingMarket.find({"final_id": str(edition.id)}).to_list()
        submitted = await FinalPrediction.find({"final_id": str(edition.id), "submitted_at": {"$ne": None}}).count()
        invitations = await pending_invitations(edition.id) if edition.state == "REGISTRATION" else []
        return DashboardState(edition, markets, submitted, invitations)

    async def _show_dashboard(self, ctx, edition, *, outcome=None, edit=False):
        if outcome:
            outcome = render_emoji_markup(outcome)
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

    async def _confirm_lineup(self, ctx, edition):
        members = await fetch_members(await member_guild(ctx), edition.entrant_ids)
        refreshed = await refresh_display_names(edition, members)
        if refreshed.setup_revision != edition.setup_revision:
            await self._show_dashboard(ctx, refreshed, outcome=FINAL_CHANGED, edit=True)
            return
        confirmed = await confirm_lineup(refreshed)
        pending = await sync_registration_posts(self.bot, confirmed)
        current = await reload_final(confirmed)
        if (not current.active or current.state == "CANCELLING" or current.opponent_picks is None
                or current.opponent_picks.generation != refreshed.setup_revision + 1):
            outcome = FINAL_CHANGED
        elif pending:
            outcome = f"{IMPORTANT_EMOJI} Lineup confirmed. " + " ".join(pending)
        else:
            outcome = f"{SUCCESS_EMOJI} Lineup confirmed."
        await self._show_dashboard(ctx, current, outcome=outcome, edit=True)

    @ipy.component_callback(re.compile(
        r"^finaldash\|(lineup|bracket|open|close|round|recover|end|refresh)\|[a-f0-9]{24}\|\d+(?:\|\d+)?$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_dashboard_action(self, ctx: ipy.ComponentContext):
        _, action, final_id, owner, *revision = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        if action == "refresh":
            await self._show_dashboard(ctx, edition, edit=True)
            return
        state = await self._dashboard_state(edition)
        if action not in dict(available_actions(state)) or (action == "lineup" and revision != [str(edition.setup_revision)]):
            # Another action or the background recovery already moved the Final on; show where it stands now.
            await self._show_dashboard(ctx, edition, edit=True, outcome=FINAL_CHANGED)
            return
        try:
            match action:
                case "lineup":
                    await self._confirm_lineup(ctx, edition)
                    return
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
