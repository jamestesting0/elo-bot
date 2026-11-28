from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import OperationFailure

from elo_bot.config.emojis import IMPORTANT_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FinalError
from elo_bot.db import Match
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.finals.dashboard import DashboardState, available_actions
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.publication import publish_bracket
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.lifecycle import set_nickname
from elo_bot.features.tokens.bets import open_market
from elo_bot.features.tokens.round_betting import publish_betting_round, round_post_pending
from tests.integration._final_ui_fixture import FinalUICase, round_select


class TestFinalGateRecovery(FinalUICase):
    async def test_recovery_keeps_an_unconfirmed_setup_private(self):
        await set_nickname(self.final, "1", "Changed")
        ctx = self.context()
        await self.cog.final_recover.callback.func(self.cog, ctx)
        self.assertEqual((await final_for_view()).state, "REGISTRATION")
        self.assertEqual(self.posts, {})
        self.bot.fetch_channel.assert_not_awaited()

    async def test_initial_bracket_channel_failure_stays_pending_until_recovery_publishes(self):
        self.final = await confirm_bracket(self.final, "9")
        before_nodes = self.final.nodes
        self.bot.fetch_channel.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Initial fetch"))
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        failed = await final_for_view()
        self.assertFalse(failed.bracket_send_attempted)
        self.assertTrue(failed.publication_pending)
        ctx = self.context()
        await self.cog.final_recover.callback.func(self.cog, ctx)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(IMPORTANT_EMOJI)))
        self.assertIn("`/final recover`", ctx.send.await_args.args[0])
        self.assertIsNone((await final_for_view()).bracket_message_id)
        self.bot.fetch_channel.side_effect = None
        retry = self.context()
        await self.cog.final_recover.callback.func(self.cog, retry)
        current = await final_for_view()
        self.assertEqual(current.nodes, before_nodes)
        self.assertFalse(current.publication_pending)
        self.assertIsNone(current.publication_error)
        self.assertTrue(retry.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(len(self.posts), 1)
        post = self.posts[current.bracket_message_id]
        post.pin.assert_awaited_once()
        self.assertEqual(len(post.payload["components"]), 0)

    async def test_initial_market_channel_failure_recovers_its_public_bet_control(self):
        self.final = await confirm_bracket(self.final, "9")
        await publish_bracket(self.bot, self.final)
        market = await open_market(self.final, "QF1")
        self.bot.fetch_channel.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Initial fetch"))
        with self.assertRaises(FinalError):
            await publish_betting_round(self.bot, self.final, "Quarterfinals")
        failed = (await final_for_view()).betting_posts["Quarterfinals"]
        self.assertFalse(failed.attempted)
        self.assertIsNotNone(failed.error)
        ctx = self.context()
        await self.cog.final_recover.callback.func(self.cog, ctx)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(IMPORTANT_EMOJI)))
        self.assertIsNone((await final_for_view()).betting_posts["Quarterfinals"].message_id)
        self.bot.fetch_channel.side_effect = None
        retry = self.context()
        await self.cog.final_recover.callback.func(self.cog, retry)
        current = await FinalBettingMarket.get(market.id)
        self.assertEqual(current.state, "OPEN")
        self.assertFalse(round_post_pending(await final_for_view(), "Quarterfinals", [current]))
        control = round_select((await self.betting_message()).payload)
        self.assertEqual(control.options[0].value, market.id)
        self.assertFalse(control.disabled)
        self.assertTrue(retry.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(len(self.posts), 2)

    async def test_existing_threads_keep_missing_markets_and_posts_actionable(self):
        self.final = await confirm_bracket(self.final, "9")

        async def create_thread(bot, token):
            source = await Match.find_one({"token": token})
            await source.set({"thread": str(700 + int(source.final_node_id[-1]))})
            return source.thread

        creator = AsyncMock(side_effect=create_thread)
        with patch("elo_bot.extensions.final.rounds.lifecycle_module.confirm_match", creator):
            with patch("elo_bot.extensions.final.rounds.open_market", new=AsyncMock(
                    side_effect=OperationFailure("Temporary market failure"))):
                first = self.context()
                await self.cog.final_round_start.callback.func(self.cog, first)
            current = await final_for_view()
            bindings = {key: node.thread_id for key, node in current.nodes.items()}
            self.assertTrue(all(bindings[key] for key in ("QF1", "QF2", "QF3", "QF4")))
            self.assertEqual(await FinalBettingMarket.count(), 0)
            self.assertTrue(first.send.await_args.args[0].startswith(str(IMPORTANT_EMOJI)))
            self.assertIn("round", dict(available_actions(DashboardState(current))))
            self.bot.fetch_channel.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Post unavailable"))
            second = self.context()
            await self.cog.final_round_start.callback.func(self.cog, second)
            markets = await FinalBettingMarket.find_all().to_list()
            self.assertEqual(len(markets), 4)
            self.assertTrue(all(not market.announcement_id for market in markets))
            self.assertIn("round", dict(available_actions(DashboardState(await final_for_view(), markets))))
            self.bot.fetch_channel.side_effect = None
            retry = self.context(component=True)
            retry.custom_id = f"finaldash|round|{self.final.id}|9"
            await self.cog.final_dashboard_action.callback.func(self.cog, retry)
        current = await final_for_view()
        self.assertEqual({key: node.thread_id for key, node in current.nodes.items()}, bindings)
        self.assertEqual(creator.await_count, 4)
        self.assertEqual(await Match.count(), 4)
        markets = await FinalBettingMarket.find_all().to_list()
        self.assertFalse(round_post_pending(current, "Quarterfinals", markets))
        self.assertTrue(retry.edit_origin.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))
        menu = round_select((await self.betting_message()).payload)
        self.assertEqual({option.value for option in menu.options}, {market.id for market in markets})
        self.assertEqual(len(self.posts), 1)

    async def test_round_publication_resumes_after_accepted_hosting_without_reopening_betting(self):
        self.final = await confirm_bracket(self.final, "9")
        for node in ("QF1", "QF2", "QF3", "QF4"):
            source = await self.scheduled_pairing(await final_for_view(), node)
            await source.set({"thread": str(700 + int(node[-1]))})
            await bind_thread(await final_for_view(), source, source.thread)
            await open_market(await final_for_view(), node)
            await accept_final_start(source, "9")
        ctx = self.context()
        await self.cog.final_round_start.callback.func(self.cog, ctx)
        markets = await FinalBettingMarket.find_all().to_list()
        self.assertEqual(len(markets), 4)
        self.assertTrue(all(market.state == "CLOSED" for market in markets))
        board = await self.betting_message()
        self.assertIsNone(round_select(board.payload))
        self.assertEqual(board.payload["components"][0][1].content.count(" · Closed · "), 4)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(await Match.count(), 4)

    async def test_betting_closure_publication_failure_has_one_warning_and_recovers(self):
        self.final = await confirm_bracket(self.final, "9")
        market = await open_market(self.final, "QF1")
        await publish_betting_round(self.bot, self.final, "Quarterfinals")
        post = await self.betting_message()
        post.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Edit denied"))
        ctx = self.context()
        await self.cog.final_betting_close.callback.func(self.cog, ctx, match="QF1")
        current = await FinalBettingMarket.get(market.id)
        self.assertEqual(current.state, "CLOSED")
        reference = (await final_for_view()).betting_posts["Quarterfinals"]
        self.assertEqual(reference.message_id, str(post.id))
        self.assertIsNotNone(reference.error)
        feedback = ctx.send.await_args.args[0]
        self.assertTrue(feedback.startswith(str(IMPORTANT_EMOJI)))
        self.assertNotIn(str(SUCCESS_EMOJI), feedback)
        post.edit.side_effect = None
        retry = self.context()
        await self.cog.final_recover.callback.func(self.cog, retry)
        self.assertIsNone((await final_for_view()).betting_posts["Quarterfinals"].error)
        self.assertIsNone(round_select(post.edit.await_args.kwargs))
