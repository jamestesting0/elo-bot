from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from common.finals.lifecycle import confirm_bracket, resolve_final
from common.finals.publication import publish_bracket
from common.finals.types import FinalError
from database_models import EloFinal
from common.tokens.bets import close_market, open_market
from common.tokens.presentation import publish_market, market_signature
from common.finals.recovery import recover_final
from common.finals.results import FinalResultSubmission, submit_result
from database_models.token_models import FinalBettingMarket


class TestFinalPublication(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        rendering = patch("common.finals.publication.render_bracket", return_value=b"bracket-image")
        rendering.start()
        self.addCleanup(rendering.stop)
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.message = MagicMock(id=100, pin=AsyncMock(), edit=AsyncMock())
        self.channel = MagicMock(send=AsyncMock(return_value=self.message), fetch_message=AsyncMock(return_value=self.message))
        self.channel.guild.fetch_member = AsyncMock(return_value=None)
        self.bot = MagicMock(fetch_channel=AsyncMock(return_value=self.channel))
        self.bot.user.id = 42
        self.message.author.id = 42

    async def save_result(self):
        node = self.final.nodes["QF1"]
        return await submit_result(FinalResultSubmission(
            final_id=self.final.id, node_id=node.id, actor_id="manager", operation_key="publication-result",
            expected_version=0, contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={user: ["3.100" if side == 0 else "2.80"] * 7
                    for side, user in enumerate(node.contestants)},
        ))

    async def test_completed_recovery_removes_pending_copy_without_duplicate_result_post(self):
        result = await self.save_result()
        self.assertTrue((await resolve_final()).repair_pending)
        self.assertEqual(await recover_final(self.final, bot=self.bot), [])
        current = await resolve_final()
        progress = current.result_progress[result.id]
        self.assertFalse(current.repair_pending)
        self.assertFalse(progress.published_repair_pending)
        self.assertTrue(progress.completed)
        self.channel.send.assert_awaited_once()
        self.assertIn("**Recovery**", [field.name for field in self.channel.send.await_args.kwargs["embed"].fields])
        self.assertNotIn("**Recovery**", [field.name for field in self.message.edit.await_args.kwargs["embed"].fields])
        self.message.edit.assert_awaited_once()
        await recover_final(current, bot=self.bot)
        self.message.edit.assert_awaited_once()

    async def test_legacy_pending_post_edit_failure_retains_result_and_retry_clears_copy(self):
        result = await self.save_result()
        await recover_final(self.final, bot=self.bot)
        path = f"result_progress.{result.id}.published_repair_pending"
        await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id}, {"$unset": {path: ""}})
        self.message.edit.reset_mock()
        self.message.edit.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        pending = await recover_final(await resolve_final(), bot=self.bot)
        self.assertEqual(pending, ["QF1 result saved; publication remains pending."])
        current = await resolve_final()
        self.assertEqual(current.nodes["QF1"].result_id, result.id)
        self.assertFalse(current.repair_pending)
        self.assertIsNotNone(current.result_progress[result.id].error)
        self.message.edit.assert_awaited_once()
        self.message.edit.side_effect = None
        self.assertEqual(await recover_final(current, bot=self.bot), [])
        current = await resolve_final()
        self.assertIsNone(current.result_progress[result.id].error)
        self.assertFalse(current.result_progress[result.id].published_repair_pending)
        self.assertNotIn("**Recovery**", [field.name for field in self.message.edit.await_args.kwargs["embed"].fields])
        self.channel.send.assert_awaited_once()

    async def test_pin_failure_preserves_bound_post_and_retry_does_not_duplicate(self):
        self.message.pin.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await resolve_final()
        self.assertEqual(current.bracket_message_id, "100")
        self.assertTrue(current.publication_pending)
        self.assertIsNotNone(current.publication_error)
        self.message.pin.side_effect = None
        await publish_bracket(self.bot, current)
        self.channel.send.assert_awaited_once()
        self.message.edit.assert_awaited_once()
        self.assertFalse((await resolve_final()).publication_pending)

    async def test_uncertain_send_is_recovered_by_nonce_without_second_post(self):
        async def uncertain(**kwargs):
            self.message.nonce = kwargs["nonce"]
            raise ipy.errors.HTTPException(MagicMock(status=502, reason="Bad Gateway"))
        self.channel.send.side_effect = uncertain
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await resolve_final()
        self.assertIsNone(current.bracket_message_id)
        self.channel.fetch_messages = AsyncMock(return_value=[self.message])
        await publish_bracket(self.bot, current)
        self.channel.send.assert_awaited_once()
        self.assertEqual((await resolve_final()).bracket_message_id, "100")

    async def test_changed_revision_during_send_remains_pending_for_current_render(self):
        async def change(**kwargs):
            await EloFinal.get_pymongo_collection().update_one({"_id": self.final.id},
                {"$set": {"player_nicknames.1": "Changed"}, "$inc": {"revision": 1}})
            return self.message
        self.channel.send.side_effect = change
        with self.assertRaises(FinalError):
            await publish_bracket(self.bot, self.final)
        current = await resolve_final()
        self.assertEqual(current.bracket_message_id, "100")
        self.assertTrue(current.publication_pending)
        self.assertLess(current.bracket_published_revision, current.revision)
        await publish_bracket(self.bot, current)
        self.assertEqual((await resolve_final()).bracket_published_revision, current.revision)

    async def test_closed_market_recovery_disables_the_published_betting_control(self):
        market = await open_market(self.final, "QF1")
        await publish_market(self.bot, self.final, market)
        self.assertFalse(self.channel.send.await_args.kwargs["components"][0].disabled)
        await close_market(market.id)
        await recover_final(self.final, bot=self.bot)
        self.assertTrue(self.message.edit.await_args.kwargs["components"][0].disabled)
        self.assertIn("CLOSED", self.message.edit.await_args.kwargs["embed"].description)
        current = await FinalBettingMarket.get(market.id)
        self.assertEqual(current.announcement_signature, market_signature(current))
        self.channel.send.assert_awaited_once()

    async def test_market_changed_during_send_remains_pending_then_recovers_latest_state(self):
        market = await open_market(self.final, "QF1")
        async def close(**kwargs):
            await close_market(market.id)
            return self.message
        self.channel.send.side_effect = close
        with self.assertRaises(FinalError):
            await publish_market(self.bot, self.final, market)
        await recover_final(self.final, bot=self.bot)
        self.assertTrue(self.message.edit.await_args.kwargs["components"][0].disabled)
        self.channel.send.assert_awaited_once()
