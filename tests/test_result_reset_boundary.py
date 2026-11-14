import unittest
from collections.abc import Awaitable, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import anyio

from cogs import data_commands, match_commands
from elobot.features.hosting.war import result_store
from elobot.features.hosting.war.helper_store import HelperScoreStoreOutcome
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.players.data_boundary import PlayerDataBoundary
from elobot.db import Match
from tests import _result_retry_store as retry_fixture


class TestResultResetBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        self.runtime = retry_fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.boundary = PlayerDataBoundary()
        for module in (data_commands, match_commands, result_store):
            self.enterContext(patch.object(module, "PLAYER_DATA_BOUNDARY", self.boundary))
        self.ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(), message=None, author=MagicMock(id=999))
        self.data = data_commands.DataCommands(self.runtime.state.bot)
        self.runtime.state.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>", send=AsyncMock()))
        self.enterContext(patch.object(self.runtime.cog, "_finish_stored_result_side", new=AsyncMock()))
        self.enterContext(patch.object(result_store.result_store_ui, "notify_stored_result", new=AsyncMock()))

    async def resume_after_reset(self, callback: Callable[[], Awaitable[None | bool]]) -> None:
        started = anyio.Event()
        async def run() -> None:
            started.set()
            await callback()
        lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault("TOKEN", anyio.Lock())
        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                async with lock:
                    tasks.start_soon(run)
                    await started.wait()
                    async with self.boundary.reset():
                        pass

    def assert_reset_rejection(self) -> None:
        self.assertIn("Player data was reset or is being reset", self.ctx.send.await_args.args[0])
        self.assertFalse(any(call.args[0].startswith(str(SUCCESS_EMOJI)) for call in self.ctx.send.await_args_list))
        self.runtime.state.player_collection.update_one.assert_not_awaited()

    async def test_finalizer_queued_before_reset_cannot_start_afterward(self) -> None:
        await self.resume_after_reset(lambda: self.runtime.cog._finalize_match_results(
            "TOKEN", retry_fixture.RESULTS, self.ctx))
        self.assert_reset_rejection()
        self.assertIsNone(self.runtime.match["result_operation"])
        self.assertEqual(set(self.runtime.state.records), {"10"})

    async def test_resubmit_queued_before_reset_cannot_start_afterward(self) -> None:
        self.ctx.custom_id = "resubmit_modal|TOKEN"
        await self.resume_after_reset(lambda: self.runtime.cog.resubmit_modal.callback.func(
            self.runtime.cog, self.ctx, **retry_fixture.RESULTS))
        self.assert_reset_rejection()
        self.assertIsNone(self.runtime.match["result_operation"])
        self.assertEqual(set(self.runtime.state.records), {"10"})

    async def test_operation_record_deletion_queued_before_reset_is_rejected(self) -> None:
        await self.runtime.invoke_record()
        self.runtime.state.player_collection.update_one.reset_mock()
        await self.resume_after_reset(lambda: self.data.data_delete_match.callback.func(self.data, self.ctx, "11"))
        self.assert_reset_rejection()
        self.assertFalse(self.runtime.match["result_operation"]["cancelled"])
        self.assertEqual(set(self.runtime.state.records), {"10", "11"})

    async def test_helper_job_started_before_reset_cannot_store_or_finalize_afterward(self) -> None:
        self.runtime.match["is_war"] = True
        self.runtime.match["participants"][0]["result"] = [None, None, None]
        match = await Match.find_one({"token": "TOKEN"})
        async def reset_during_helper(*_args):
            async with self.boundary.reset():
                pass
            return HelperScoreStoreOutcome(results=(None, None, None), changed=False, skipped=(), job_id="job",
                status="succeeded", summary="complete", trusted=tuple(zip(retry_fixture.DISTRICTS, ["3.100"] * 3)))
        self.enterContext(patch.object(result_store.helper_store, "store_helper_score", new=AsyncMock(
            side_effect=reset_during_helper)))
        await result_store.store_match_result_automatically(self.runtime.cog, self.ctx,
            result_store.AutomaticResultStoreRequest(match=match, player=0))
        self.assert_reset_rejection()
        self.assertEqual(self.runtime.match["participants"][0]["result"], [None, None, None])
        self.assertIsNone(self.runtime.match["result_operation"])

    async def test_result_modal_captures_reset_generation_before_discord_defer(self) -> None:
        self.ctx.custom_id = "result_modal|TOKEN"
        async def reset_while_deferring(**_kwargs) -> None:
            async with self.boundary.reset():
                pass
        self.ctx.defer.side_effect = reset_while_deferring
        await self.runtime.cog.result_submit_modal.callback.func(self.runtime.cog, self.ctx, **retry_fixture.RESULTS)
        self.assert_reset_rejection()
        self.assertEqual(set(self.runtime.state.records), {"10"})
