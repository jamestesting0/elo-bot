import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions.self_service import SelfCommands
from elobot.config.ids import BOT_ID


class TestSelfStoreBaseLive(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.user.id = BOT_ID
        self.extension = MagicMock(store_self_live_bases=AsyncMock())
        self.bot.get_ext.return_value = self.extension
        self.cog = SelfCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author.id = 111
        self.ctx.channel.id = 900
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock()
        self.match = SimpleNamespace(participants=[SimpleNamespace(user_id="111"), SimpleNamespace(user_id="222")])

    async def invoke(self, available=True) -> None:
        with patch("elobot.extensions.self_service.Match") as model, patch(
            "elobot.extensions.self_service.is_self_store_available", return_value=available,
        ):
            model.find_one = AsyncMock(return_value=self.match)
            await self.cog.self_store_base_live.callback.func(self.cog, self.ctx)

    def test_registration_uses_existing_self_store_guards(self) -> None:
        command = self.cog.self_store_base_live
        self.assertEqual(command.resolved_name, "self store base-live")
        self.assertFalse(command.options)
        self.assertEqual(command.max_concurrency.bucket, ipy.Buckets.USER)
        self.assertEqual(command.max_concurrency.concurrent, 1)
        self.assertEqual(len(command.checks), 1)

    async def test_either_participant_can_store_both_sides(self) -> None:
        for player in (111, 222):
            self.ctx.author.id = player
            await self.invoke()
            self.extension.store_self_live_bases.assert_awaited_with(self.ctx, self.match)
        self.assertEqual(self.extension.store_self_live_bases.await_count, 2)

    async def test_missing_match_outsider_and_time_window_reject_before_storage(self) -> None:
        await self.invoke(available=False)
        self.ctx.author.id = 333
        await self.invoke()
        self.match = None
        await self.invoke()
        self.extension.store_self_live_bases.assert_not_awaited()
