import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from pymongo.errors import OperationFailure

from elo_bot.config.ids import MATCH_CHANNEL
from tests import _result_retry_store as retry_fixture
from tests._patch_sites import patch_sites

RESULTS = retry_fixture.RESULTS


class TestResubmitEmbedPublication(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = retry_fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.enterContext(patch_sites(('elo_bot.extensions.match.results.choose_tip', 'elo_bot.extensions.match.hosting.choose_tip', 'elo_bot.extensions.match.settings.choose_tip'), new=AsyncMock(return_value="tip")))
        self.summary = MagicMock(id=901, edit=AsyncMock())
        self.public = MagicMock(id=902, edit=AsyncMock())
        self.channel = MagicMock(send=AsyncMock(return_value=self.public),
                                 fetch_message=AsyncMock(return_value=self.public))
        self.runtime.thread.send.return_value = self.summary
        self.runtime.thread.fetch_message = AsyncMock(return_value=self.summary)
        self.runtime.state.bot.fetch_channel.side_effect = (
            lambda channel_id: self.channel if channel_id == MATCH_CHANNEL else self.runtime.thread
        )
        self.ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                             guild=self.runtime.state.bot.fetch_guild.return_value)
        self.corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}

    async def prepare_completed_match(self) -> None:
        self.assertTrue(await self.runtime.cog._finalize_match_results_locked("TOKEN", RESULTS))
        self.summary.edit.reset_mock()
        self.public.edit.reset_mock()
        self.runtime.thread.send.reset_mock()
        self.channel.send.reset_mock()
        self.runtime.state.player_collection.update_one.reset_mock()

    async def resubmit(self) -> None:
        await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, self.ctx, **self.corrected)

    async def test_correction_updates_bound_summary_and_public_preserving_controls(self) -> None:
        # Given: both result surfaces already exist.
        await self.prepare_completed_match()
        # When: the result is corrected.
        await self.resubmit()
        # Then: both existing messages show the correction, with components omitted.
        self.assertEqual(self.runtime.match["result_operation"]["summary_message_id"], 901)
        self.summary.edit.assert_awaited_once()
        self.public.edit.assert_awaited_once()
        self.assertEqual(self.summary.edit.await_args.kwargs["embed"].description,
                         self.public.edit.await_args.kwargs["embed"].description)
        self.assertNotIn("components", self.summary.edit.await_args.kwargs)
        self.assertNotIn("components", self.public.edit.await_args.kwargs)
        self.runtime.thread.send.assert_not_awaited()
        self.channel.send.assert_not_awaited()
        self.assertTrue(self.runtime.match["result_operation"]["published"])

    async def test_missing_public_binding_failure_leaves_pending_and_retry_does_not_rerate(self) -> None:
        # Given: the public message is missing and its replacement cannot be bound.
        await self.prepare_completed_match()
        self.runtime.match["msg"] = None
        self.runtime.match["result_operation"]["public_message_id"] = None

        def fail_public_binding(updates) -> None:
            if updates.get("$set", {}).get("msg") == "902":
                raise OperationFailure("injected public binding failure")

        self.runtime.before_match_update = fail_public_binding
        # When: correction publication fails, then the same correction is retried.
        await self.resubmit()
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.assertIsNone(self.runtime.match["msg"])
        self.assertEqual(self.public.edit.await_args.kwargs["embed"].title, "**Result Publication Incomplete**")
        self.assertEqual(self.public.edit.await_args.kwargs["components"], [])
        self.assertIn("incomplete", self.ctx.send.await_args.args[0].lower())
        players = deepcopy(self.runtime.state.players)
        self.runtime.before_match_update = lambda _updates: None
        await self.resubmit()
        # Then: binding is durable and rating state has not changed again.
        self.assertEqual(self.runtime.match["msg"], "902")
        self.assertTrue(self.runtime.match["result_operation"]["published"])
        self.assertEqual(self.runtime.state.players, players)

    async def test_failed_summary_edit_keeps_operation_unpublished(self) -> None:
        # Given: Discord rejects editing the established summary.
        await self.prepare_completed_match()
        self.summary.edit.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        # When: the result is corrected.
        await self.resubmit()
        # Then: correction persists while publication remains retryable.
        self.assertTrue(self.runtime.match["result_operation"]["completed"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.public.edit.assert_not_awaited()
        self.assertIn("incomplete", self.ctx.send.await_args.args[0].lower())

    async def test_new_result_messages_publish_the_finished_embed(self) -> None:
        self.runtime.match["msg"] = None

        async def send_summary(*_args, **kwargs) -> MagicMock:
            self.assertIsNone(self.runtime.match["result_operation"]["summary_message_id"])
            self.assertEqual(kwargs["embed"].title, "**Match has Ended**")
            self.assertEqual(kwargs["components"][0].components[0].custom_id, "delete_thread|TOKEN")
            return self.summary

        async def send_public(*_args, **kwargs) -> MagicMock:
            self.assertEqual(self.runtime.match["result_operation"]["summary_message_id"], 901)
            self.assertIsNone(self.runtime.match["msg"])
            self.assertEqual(kwargs["embed"].title, "**Match has Ended**")
            self.assertEqual(kwargs["components"], [])
            return self.public

        self.runtime.thread.send.side_effect = send_summary
        self.channel.send.side_effect = send_public
        self.assertTrue(await self.runtime.cog._finalize_match_results_locked("TOKEN", RESULTS))
        self.assertEqual(self.runtime.match["result_operation"]["summary_message_id"], 901)
        self.assertEqual(self.runtime.match["msg"], "902")
        self.summary.edit.assert_not_awaited()
        self.public.edit.assert_not_awaited()

    async def test_public_send_failure_stays_unpublished_and_retry_sends_finished_embed(self) -> None:
        await self.prepare_completed_match()
        self.runtime.match["msg"] = None
        self.runtime.match["result_operation"]["public_message_id"] = None
        self.channel.send.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        await self.resubmit()
        self.assertIsNone(self.runtime.match["msg"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        players = deepcopy(self.runtime.state.players)
        self.channel.send.reset_mock(side_effect=True)
        self.channel.send.return_value = self.public
        await self.resubmit()
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].title, "**Match has Ended**")
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.runtime.state.players, players)
        self.assertEqual(self.runtime.match["msg"], "902")
        self.assertTrue(self.runtime.match["result_operation"]["published"])
