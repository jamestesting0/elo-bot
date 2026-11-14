import unittest
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.extensions.self_service import SelfCommands
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import LVL2_ROLES


class TestSelfStoreResultLive(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.extension = MagicMock(store_self_live_result=AsyncMock())
        self.bot.get_ext.return_value = self.extension
        self.cog = SelfCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author.id = 111
        self.ctx.channel.id = 900
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock()
        self.match = SimpleNamespace(
            participants=[SimpleNamespace(user_id="111"), SimpleNamespace(user_id="222")],
            is_war=False,
            times=[100, None],
            host_clans=["#CLAN", None],
            final_districts=["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"],
        )

    async def invoke(self) -> None:
        with patch("elo_bot.extensions.self_service.Match") as model:
            model.find_one = AsyncMock(return_value=self.match)
            await self.cog.self_store_result_live.callback.func(self.cog, self.ctx)

    def test_command_registration(self) -> None:
        command = self.cog.self_store_result_live
        self.assertEqual(command.resolved_name, "self store result-live")
        self.assertFalse(command.options)
        self.assertEqual(command.max_concurrency.bucket, ipy.Buckets.CHANNEL)
        self.assertEqual(command.max_concurrency.concurrent, 1)

    async def test_either_participant_uses_shared_live_match(self) -> None:
        for caller in (111, 222):
            with self.subTest(caller=caller):
                self.ctx.author.id = caller
                await self.invoke()
                self.ctx.defer.assert_awaited_with(ephemeral=False)
                self.extension.store_self_live_result.assert_awaited_with(self.ctx, self.match)
        self.assertEqual(self.extension.store_self_live_result.await_count, 2)

    async def test_rejects_invalid_match_before_delegation(self) -> None:
        cases = (
            ("is_war", True), ("times", [None, 100]), ("host_clans", [None, "#OTHER"]),
            ("final_districts", ["Wizard Valley", None, "Dragon Cliffs"]),
            ("final_districts", ["Wizard Valley"]),
            ("participants", [SimpleNamespace(user_id="111")]),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                previous = getattr(self.match, field)
                setattr(self.match, field, value)
                await self.invoke()
                setattr(self.match, field, previous)
                self.extension.store_self_live_result.assert_not_awaited()
                self.ctx.send.assert_awaited_with(ANY, ephemeral=False)
                self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_rejects_outsider_and_missing_match(self) -> None:
        self.ctx.author.id = 333
        await self.invoke()
        self.match = None
        await self.invoke()
        self.bot.get_ext.assert_not_called()
        self.extension.store_self_live_result.assert_not_awaited()

    async def test_missing_extension_is_visible(self) -> None:
        self.bot.get_ext.return_value = None
        await self.invoke()
        self.ctx.send.assert_awaited_once_with(
            f"{ERROR_EMOJI} Automatic result storage is currently unavailable. Please contact a helper.",
            ephemeral=False,
        )

    async def test_uses_existing_access_check(self) -> None:
        check = self.cog.self_store_result_live.checks[0]
        self.ctx.author.has_role.side_effect = lambda role: role == LVL2_ROLES[0]
        self.assertTrue(await check(self.ctx))
        self.ctx.author.has_role.return_value = False
        self.ctx.author.has_role.side_effect = None
        self.ctx.bot.fetch_channel = AsyncMock(return_value=MagicMock(fetch_members=AsyncMock(return_value=[])))
        self.assertFalse(await check(self.ctx))
