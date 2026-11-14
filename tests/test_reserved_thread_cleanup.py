import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from elo_bot.db import Match


from tests._match_cleanup_fixture import ReservedThreadStateMixin

class TestReservedThreadCleanup(ReservedThreadStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_staff_thread_delete_refuses_a_reserved_slot(self) -> None:
        await self.fixture.thread.thread_delete.callback.func(self.fixture.thread, self.ctx)
        self.channel.delete.assert_not_awaited()
        self.assertIn("reserved bases", self.ctx.send.await_args.args[0])
        self.assert_held()

    async def test_delete_button_refuses_a_reserved_slot(self) -> None:
        await self.runtime.fixture.cog.delete_thread_button.callback.func(self.runtime.fixture.cog, self.ctx)
        self.channel.delete.assert_not_awaited()
        self.assertIn("reserved bases", self.ctx.send.await_args.args[0])
        self.assert_held()

    async def test_expiry_task_retains_a_reserved_thread(self) -> None:
        self.channel.fetch_messages = AsyncMock(return_value=[MagicMock(
            created_at=datetime.now() - timedelta(days=2))])
        self.runtime.fixture.state.bot.fetch_channel = AsyncMock(return_value=MagicMock(
            fetch_all_threads=AsyncMock(return_value=MagicMock(threads=[self.channel]))))
        await self.runtime.fixture.cog.inactive_threads_delete.callback.func(self.runtime.fixture.cog)
        self.channel.delete.assert_not_awaited()
        self.assert_held()

    async def test_external_thread_delete_releases_the_reserved_match(self) -> None:
        await self.fixture.thread.on_thread_delete.callback(self.fixture.thread, MagicMock(thread=self.channel))
        self.fixture.free.assert_awaited_once()
        self.assertFalse(self.runtime.alive)
        self.assertIsNone(self.runtime.fixture.match["participants"][0]["base_store"])
        self.assertFalse(self.runtime.fixture.match["participants"][0]["opponent_bases_stored"])

    async def test_confirmed_missing_thread_cleanup_releases_the_reserved_match(self) -> None:
        match = await Match.find_one({"token": "TOKEN"})
        self.enterContext(patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[match]))))
        self.runtime.fixture.state.bot.fetch_guild = AsyncMock(return_value=MagicMock(
            fetch_thread=AsyncMock(return_value=None)))
        await self.runtime.fixture.cog.match_data_clean_up.callback.func(self.runtime.fixture.cog)
        self.assertFalse(self.runtime.alive)
        self.assertIsNone(self.runtime.fixture.match["participants"][0]["base_store"])
        self.assertFalse(self.runtime.fixture.match["participants"][0]["opponent_bases_stored"])
