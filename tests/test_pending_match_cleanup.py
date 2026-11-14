import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch


from elobot.extensions import thread as thread_commands
from elobot.extensions import match as match_commands
from elobot.features.results.autowin import claim_autowin
from elobot.config.ids import LVL2_ROLES, MATCH_CHANNEL
from elobot.features.matches.cleanup import delete_unreserved_match
from elobot.features.results.operations import begin_result_operation
from elobot.db import Match
from elobot.db.result_models import MatchResultOperation
from tests import _result_finalization_fixture as result_fixture


from tests._match_cleanup_fixture import PendingMatchStateMixin
import elobot.extensions.match.lifecycle as match_lifecycle_module

class TestPendingMatchCleanup(PendingMatchStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_thread_event_preserves_pending_second_player_write(self) -> None:
        await self.exercise_thread_event("finalizer_player2")

    async def test_thread_event_preserves_pending_record_insert(self) -> None:
        await self.exercise_thread_event("finalizer_insert")

    async def test_result_claim_after_cleanup_read_defeats_deletion(self) -> None:
        stale = await Match.find_one({"token": "TOKEN"})
        await begin_result_operation(await Match.find_one({"token": "TOKEN"}), result_fixture.RESULTS)
        self.assertFalse(await delete_unreserved_match(stale))
        self.assertTrue(self.runtime.alive)

    async def test_cleanup_winning_before_result_claim_prevents_all_recording(self) -> None:
        snapshot = await Match.find_one({"token": "TOKEN"})
        collection = Match.get_pymongo_collection()
        update = collection.update_one.side_effect
        async def delete_before_claim(query, updates, **kwargs):
            if "result_operation" in updates.get("$set", {}):
                self.assertTrue(await delete_unreserved_match(snapshot))
            return await update(query, updates, **kwargs)
        collection.update_one.side_effect = delete_before_claim
        finalized = await self.runtime.fixture.cog._finalize_match_results(
            "TOKEN", result_fixture.RESULTS, self.runtime.ctx)
        self.assertFalse(finalized)
        self.assertEqual(set(self.runtime.fixture.state.records), {"10"})
        self.runtime.fixture.state.player_collection.update_one.assert_not_awaited()

    async def test_staff_thread_command_retains_a_pending_result_checkpoint(self) -> None:
        await begin_result_operation(await Match.find_one({"token": "TOKEN"}), result_fixture.RESULTS)
        channel = MagicMock(id=321, parent_channel=MagicMock(id=MATCH_CHANNEL), delete=AsyncMock())
        channel.name = "🔴 Match Thread"
        ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(return_value=MagicMock(id=123)), channel=channel,
            author=MagicMock(id=999))
        confirm = MagicMock(custom_id="confirm", channel=channel, edit_origin=AsyncMock())
        self.runtime.fixture.state.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=confirm))
        self.enterContext(patch.object(thread_commands.asyncio, "sleep", new=AsyncMock()))
        await self.thread.thread_delete.callback.func(self.thread, ctx)
        channel.delete.assert_not_awaited()
        self.assertTrue(self.runtime.alive)
        self.free.assert_not_awaited()
        self.assertIn("unfinished", ctx.send.await_args.args[0])

    async def test_delete_button_keeps_the_thread_for_a_pending_result_retry(self) -> None:
        await begin_result_operation(await Match.find_one({"token": "TOKEN"}), result_fixture.RESULTS)
        channel = MagicMock(id=321, delete=AsyncMock())
        ctx = MagicMock(custom_id="delete_thread|TOKEN", defer=AsyncMock(),
            send=AsyncMock(return_value=MagicMock(id=123)), channel=channel,
            author=MagicMock(id=999, roles=[MagicMock(id=LVL2_ROLES[0])]))
        confirm = MagicMock(custom_id="confirm", channel=channel, edit_origin=AsyncMock())
        self.runtime.fixture.state.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=confirm))
        self.enterContext(patch.object(match_commands.asyncio, "sleep", new=AsyncMock()))
        await self.runtime.fixture.cog.delete_thread_button.callback.func(self.runtime.fixture.cog, ctx)
        channel.delete.assert_not_awaited()
        self.assertIn("unfinished", ctx.send.await_args.args[0])

    async def test_expiry_task_keeps_the_thread_for_a_pending_result_retry(self) -> None:
        await begin_result_operation(await Match.find_one({"token": "TOKEN"}), result_fixture.RESULTS)
        thread = MagicMock(id=321, delete=AsyncMock(), fetch_messages=AsyncMock(return_value=[
            MagicMock(created_at=datetime.now() - timedelta(days=2))]))
        thread.name = "🔴 Match Thread"
        self.runtime.fixture.state.bot.fetch_channel = AsyncMock(return_value=MagicMock(
            fetch_all_threads=AsyncMock(return_value=MagicMock(threads=[thread]))))
        await self.runtime.fixture.cog.inactive_threads_delete.callback.func(self.runtime.fixture.cog)
        thread.delete.assert_not_awaited()
        self.assertTrue(self.runtime.alive)

    async def test_missing_thread_cleanup_retains_a_pending_result_checkpoint(self) -> None:
        match = await Match.find_one({"token": "TOKEN"})
        await begin_result_operation(match, result_fixture.RESULTS)
        self.enterContext(patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))))
        self.runtime.fixture.state.bot.fetch_guild = AsyncMock(return_value=MagicMock(
            fetch_thread=AsyncMock(return_value=None)))
        await self.runtime.fixture.cog.match_data_clean_up.callback.func(self.runtime.fixture.cog)
        self.assertTrue(self.runtime.alive)
        match_lifecycle_module.free_match_clans.assert_not_awaited()

    async def test_autowin_claim_after_cleanup_read_defeats_deletion(self) -> None:
        stale = await Match.find_one({"token": "TOKEN"})
        await claim_autowin(await Match.find_one({"token": "TOKEN"}), "111", "999", "submitted correction")
        self.assertFalse(await delete_unreserved_match(stale))
        self.assertTrue(self.runtime.alive)

    async def test_unpublished_completed_result_retains_its_checkpoint(self) -> None:
        self.runtime.fixture.match["result_operation"] = MatchResultOperation(
            key="result", response=result_fixture.RESULTS, created_at=1, completed=True,
        ).model_dump()
        self.assertFalse(await delete_unreserved_match(await Match.find_one({"token": "TOKEN"})))
        self.assertTrue(self.runtime.alive)

    async def test_published_cancelled_and_legacy_results_remain_deletable(self) -> None:
        states = [None] + [MatchResultOperation(
            key="result", response=result_fixture.RESULTS, created_at=1, **flags,
        ).model_dump() for flags in ({"completed": True, "published": True}, {"cancelled": True})]
        for operation in states:
            with self.subTest(operation=operation):
                self.runtime.alive = True
                self.runtime.fixture.match["result_operation"] = operation
                self.assertTrue(await delete_unreserved_match(await Match.find_one({"token": "TOKEN"})))
                self.assertFalse(self.runtime.alive)

    async def test_completed_autowin_remains_deletable(self) -> None:
        self.runtime.fixture.match.update(autowin_winner_id="111", autowin_completed=True)
        self.assertTrue(await delete_unreserved_match(await Match.find_one({"token": "TOKEN"})))
        self.assertFalse(self.runtime.alive)

    async def test_exclusive_full_reset_can_remove_an_unfinished_checkpoint(self) -> None:
        await begin_result_operation(await Match.find_one({"token": "TOKEN"}), result_fixture.RESULTS)
        await self.runtime.run_reset()
        self.assertFalse(self.runtime.alive)
        self.assertEqual(self.runtime.fixture.state.records, {})
        self.assertEqual([row["elo"] for row in self.runtime.fixture.state.players.values()], [1000, 1000])
