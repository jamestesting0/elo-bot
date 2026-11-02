import unittest
from unittest.mock import AsyncMock, MagicMock

import anyio

from cogs import data_commands
from common.constants import SUCCESS_EMOJI
from tests import _result_retry_store as retry_fixture


class TestResultDeleteIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        self.runtime = retry_fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.data = data_commands.DataCommands(self.runtime.state.bot)

    async def test_two_legacy_deletes_cannot_remove_a_replacement_with_the_reused_id(self) -> None:
        first_paused, release_first, second_started, first_completed = (anyio.Event() for _ in range(4))
        callers: dict[int, str] = {}
        original_update = self.runtime.state.update_player
        async def pause_updates(query, updates, **kwargs):
            if callers[anyio.get_current_task().id] == "first" and query.get("user_id") == "111":
                first_paused.set()
                await release_first.wait()
            if callers[anyio.get_current_task().id] == "second":
                await first_completed.wait()
            return await original_update(query, updates, **kwargs)
        self.runtime.state.player_collection.update_one.side_effect = pause_updates
        def replace_deleted_record(*_args, **_kwargs) -> None:
            replacement = self.runtime.state.make_record("10")
            replacement.operation_key = "replacement-match:new-operation"
            self.runtime.state.records["10"] = replacement
            for player in self.runtime.state.players.values():
                player["matches"] = [replacement.to_ref()]
                player["elo"] = 1100
            first_completed.set()
        first_ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(side_effect=replace_deleted_record))
        second_ctx = MagicMock(defer=AsyncMock(side_effect=lambda **_kwargs: second_started.set()), send=AsyncMock())
        async def first() -> None:
            callers[anyio.get_current_task().id] = "first"
            await self.data.data_delete_match.callback.func(self.data, first_ctx, "10")
        async def second() -> None:
            callers[anyio.get_current_task().id] = "second"
            await self.data.data_delete_match.callback.func(self.data, second_ctx, "10")
        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(first)
                await first_paused.wait()
                tasks.start_soon(second)
                await second_started.wait()
                release_first.set()
        self.assertEqual(set(self.runtime.state.records), {"10"})
        self.assertEqual(self.runtime.state.records["10"].operation_key, "replacement-match:new-operation")
        self.assertEqual([player["elo"] for player in self.runtime.state.players.values()], [1100, 1100])
        self.assertFalse(second_ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertIn("already deleted or replaced", second_ctx.send.await_args.args[0])
