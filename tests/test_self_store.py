import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions import self_service as self_commands
from elobot.extensions.self_service import SelfCommands
from elobot.config.ids import BOT_ID
from elobot.config.emojis import ERROR_EMOJI


CALLER_ID = "123456789012345678"


class TestSelfStoreBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.user.id = BOT_ID
        self.match_commands = MagicMock()
        self.match_commands.store_self_result = AsyncMock()
        self.match_commands.store_self_bases = AsyncMock()
        self.bot.get_ext.return_value = self.match_commands
        self.cog = SelfCommands(self.bot)

    @staticmethod
    def make_context(author_id: str = CALLER_ID) -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=author_id)
        ctx.channel = MagicMock(id=900)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def make_match() -> SimpleNamespace:
        return SimpleNamespace(
            participants=[
                SimpleNamespace(user_id=CALLER_ID),
                SimpleNamespace(user_id="200"),
            ],
            is_war=True,
            times=[100, 200],
            host_clans=["#FIRST", "#SECOND"],
            final_districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
        )

    async def invoke(
            self,
            ctx: ipy.SlashContext,
            match: SimpleNamespace | None,
            *,
            available: bool = True,
    ) -> MagicMock:
        with (
            patch("elobot.extensions.self_service.is_self_store_available", return_value=available, create=True),
            patch("elobot.extensions.self_service.Match") as match_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.self_store_base.callback.func(self.cog, ctx)
            return match_model

    def test_self_store_window_boundaries(self) -> None:
        for now, expected in (
            (datetime(2026, 9, 3, 6, 59, 59, tzinfo=UTC), False),
            (datetime(2026, 9, 3, 7, 0, 0, tzinfo=UTC), True),
            (datetime(2026, 9, 4, 6, 59, 59, tzinfo=UTC), True),
            (datetime(2026, 9, 4, 7, 0, 0, tzinfo=UTC), False),
        ):
            with self.subTest(now=now):
                self.assertEqual(self_commands.is_self_store_available(now), expected)

    def test_self_store_registration_has_no_options_and_one_user_limit(self) -> None:
        # Given the registered self-store command.
        command = self.cog.self_store_base

        # When its machine-consumed registration metadata is inspected.
        max_concurrency = command.max_concurrency

        # Then its route, empty option list, and one-per-user concurrency remain intact.
        self.assertEqual(command.resolved_name, "self store base")
        self.assertFalse(command.options)
        self.assertEqual(max_concurrency.bucket, ipy.Buckets.USER)
        self.assertEqual(max_concurrency.concurrent, 1)

    async def test_self_store_infers_participant_and_delegates(self) -> None:
        for player, author in ((0, CALLER_ID), (1, "200")):
            with self.subTest(player=player):
                self.bot.reset_mock()
                ctx = self.make_context(author)
                match = self.make_match()

                await self.invoke(ctx, match)

                ctx.defer.assert_awaited_once_with(ephemeral=True)
                self.bot.get_ext.assert_called_once_with("MatchCommands")
                self.match_commands.store_self_bases.assert_awaited_once_with(ctx, match, player)
                self.match_commands.store_self_result.assert_not_awaited()
                ctx.send.assert_not_awaited()

    async def test_self_store_rejects_outside_window_before_match_lookup(self) -> None:
        # Given a participant invoking the command outside the storage window.
        ctx = self.make_context()

        # When they invoke self store.
        match_model = await self.invoke(ctx, self.make_match(), available=False)

        # Then the command rejects before querying the match or resolving its lifecycle.
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        match_model.find_one.assert_not_awaited()
        self.bot.get_ext.assert_not_called()
        self.match_commands.store_self_bases.assert_not_awaited()
        self.match_commands.store_self_result.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_self_store_rejects_missing_match_and_outsider(self) -> None:
        for author, match in (("300", self.make_match()), (CALLER_ID, None)):
            with self.subTest(author=author, match_present=match is not None):
                self.bot.reset_mock()
                ctx = self.make_context(author)

                await self.invoke(ctx, match)

                ctx.defer.assert_awaited_once_with(ephemeral=True)
                self.bot.get_ext.assert_not_called()
                self.match_commands.store_self_bases.assert_not_awaited()
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_self_store_delegates_match_validity_to_lifecycle(self) -> None:
        # Given a participant match without result-storage configuration fields.
        ctx = self.make_context()
        match = SimpleNamespace(participants=self.make_match().participants)

        # When they invoke self store during the storage window.
        await self.invoke(ctx, match)

        # Then the command delegates validity decisions to the base-storage lifecycle.
        self.bot.get_ext.assert_called_once_with("MatchCommands")
        self.match_commands.store_self_bases.assert_awaited_once_with(ctx, match, 0)
        self.match_commands.store_self_result.assert_not_awaited()
        ctx.send.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
