import interactions as ipy
from interactions import SlashContext
from pymongo.errors import PyMongoError

from elo_bot.config.emojis import ERROR_EMOJI, IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.config.ids import LVL1_ROLES
from elo_bot.core.finals import FinalError
from elo_bot.discord.members import member_guild
from elo_bot.discord.mentions import fetch_members
from elo_bot.extensions.final.checks import final_action
from elo_bot.extensions.final.groups import final, round_group
from elo_bot.features.finals.bracket import require_round_ready
from elo_bot.features.finals.dashboard import DashboardState, dashboard_controls, next_round
from elo_bot.features.finals.lifecycle import refresh_display_names, reload_final
from elo_bot.features.finals.matches import bind_thread, prepare_match
from elo_bot.features.matches import lifecycle as lifecycle_module
from elo_bot.features.matches.thread_messages import ThreadMessageBindingError
from elo_bot.features.queue import policy
from elo_bot.features.tokens.bets import open_market
from elo_bot.features.tokens.presentation import market_signature, publish_market


class Rounds:
    final = final
    round_group = round_group

    @round_group.subcommand(sub_cmd_name="start", sub_cmd_description="Create or resume the round's match threads")
    @ipy.check(ipy.has_any_role(*LVL1_ROLES))
    @final_action
    async def final_round_start(self, ctx: SlashContext):
        await ctx.defer(ephemeral=True)
        edition = await self._ongoing_edition(ctx, access="manager")
        content = await self._start_round(edition, await member_guild(ctx))
        current = await reload_final(edition)
        controls = dashboard_controls(DashboardState(current), str(ctx.author.id)) if current.prediction_state == "OPEN" else []
        await ctx.send(content, components=controls, ephemeral=True)

    async def _provision_node(self, edition, state, node_id) -> tuple[str | None, bool, list[str]]:
        """Create the pairing's match thread if missing and open its betting; returns thread, betting open, errors."""
        node = edition.nodes[node_id]
        errors = []
        betting = False
        thread = node.thread_id
        if not thread:
            try:
                async with policy.QUEUE_OPERATION_LOCK:
                    source = await prepare_match(edition, node_id)
                if source is not None:
                    thread = await lifecycle_module.confirm_match(self.bot, source.token)
                    await bind_thread(edition, source, thread)
            except (FinalError, ipy.errors.HTTPException, PyMongoError, ThreadMessageBindingError) as error:
                errors.append(str(error))
        if thread:
            try:
                market = next((market for market in state.markets if market.node_id == node_id
                               and market.contestant_revision == node.contestant_revision), None)
                if node.first_actual_started_at is None:
                    market = await open_market(edition, node_id)
                if market is not None and (not market.announcement_id or market.publication_error
                        or market.announcement_signature != market_signature(market)):
                    await publish_market(self.bot, edition, market)
                betting = market is not None and market.state == "OPEN"
            except (FinalError, ipy.errors.HTTPException, PyMongoError) as error:
                errors.append(f"Betting: {error}")
        return thread, betting, errors

    async def _start_round(self, edition, guild):
        # Thread names and the bracket use bracket names, so pick up display-name changes first.
        edition = await refresh_display_names(edition, await fetch_members(guild, edition.entrant_ids))
        state = await self._dashboard_state(edition)
        round_name = next_round(state)
        if round_name is None:
            raise FinalError("No round is ready to start.")
        nodes = require_round_ready(edition, round_name)
        completed = []
        failed = False
        markets = []
        for node_id in nodes:
            node = edition.nodes[node_id]
            if node.bye or node.winner is not None or node.result_id is not None:
                continue
            thread, betting, errors = await self._provision_node(edition, state, node_id)
            if betting:
                markets.append(node_id)
            completed.append(f"`{node_id}`" + (f" <#{thread}>" if thread else "")
                             + (f" {ERROR_EMOJI} " + " ".join(errors) if errors else ""))
            failed = failed or bool(errors)
        status = (f"{IMPORTANT_EMOJI} {round_name} partly started." if failed else
                  f"{SUCCESS_EMOJI} {round_name} started.")
        if markets:
            market_names = ", ".join(markets[:-1]) + " and " + markets[-1] if len(markets) > 1 else markets[0]
            status += f" Betting is open for {market_names}."
        current = await reload_final(edition)
        if current.prediction_state == "OPEN":
            completed.append(f"{IMPORTANT_EMOJI} Predictions are still open. Hosts can't start matches until they close.")
        return status + "\n" + "\n".join(completed)
