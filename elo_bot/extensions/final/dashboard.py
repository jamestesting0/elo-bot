import re
from typing import assert_never

import interactions as ipy

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import member_names
from elo_bot.extensions.final.checks import final_action
from elo_bot.features.finals.dashboard import DashboardState, available_actions, dashboard_controls, dashboard_embed
from elo_bot.features.finals.lifecycle import reload_final


class Dashboard:
    async def _dashboard_state(self, edition) -> DashboardState:
        markets = await FinalBettingMarket.find({"final_id": str(edition.id)}).to_list()
        submitted = await FinalPrediction.find({"final_id": str(edition.id), "submitted_at": {"$ne": None}}).count()
        return DashboardState(edition, markets, submitted)

    async def _show_dashboard(self, ctx, edition, *, outcome=None, edit=False):
        manager = any(ctx.author.has_role(role) for role in LVL1_ROLES)
        state = await self._dashboard_state(edition)
        names = await member_names(await member_guild(ctx), edition.entrant_ids)
        embed = dashboard_embed(state, names, manager=manager)
        controls = dashboard_controls(state, str(ctx.author.id)) if manager else []
        if edit:
            await ctx.edit_origin(content=outcome or "", embed=embed, components=controls)
        else:
            await ctx.send(content=outcome, embed=embed, components=controls, ephemeral=True)

    @ipy.component_callback(re.compile(r"^finaldash\|(bracket|open|close|round|recover|end)\|[a-f0-9]{24}\|\d+$"))
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_dashboard_action(self, ctx: ipy.ComponentContext):
        _, action, final_id, owner = ctx.custom_id.split("|")
        if owner != str(ctx.author.id):
            raise FinalError("You cannot interact with another user's components.")
        await ctx.defer(edit_origin=True)
        edition = await self._ongoing_edition(ctx, final_id, access="manager")
        state = await self._dashboard_state(edition)
        if action not in dict(available_actions(state)):
            raise FinalError("This view is outdated. Run `/final status` again.")
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
        await self._show_dashboard(ctx, await reload_final(edition), outcome=outcome, edit=True)
