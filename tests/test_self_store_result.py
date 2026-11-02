import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.self_commands import SelfCommands
from common.constants import ERROR_EMOJI


CALLER_ID = "123456789012345678"


class TestSelfStoreResult(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
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

    async def invoke(self, ctx: ipy.SlashContext, match: SimpleNamespace | None) -> MagicMock:
        with patch("cogs.self_commands.Match") as match_model:
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.self_store_result.callback.func(self.cog, ctx)
            return match_model

    def test_registration_has_no_options_and_one_user_limit(self) -> None:
        # Given the registered result-storage leaf.
        command = self.cog.self_store_result

        # When its machine-consumed registration metadata is inspected.
        max_concurrency = command.max_concurrency

        # Then it is a zero-option, one-per-user leaf under self store.
        self.assertEqual(command.resolved_name, "self store result")
        self.assertFalse(command.options)
        self.assertEqual(max_concurrency.bucket, ipy.Buckets.USER)
        self.assertEqual(max_concurrency.concurrent, 1)

    async def test_infers_participant_and_delegates(self) -> None:
        for player, author in ((0, CALLER_ID), (1, "200")):
            with self.subTest(player=player):
                self.bot.reset_mock()
                ctx = self.make_context(author)
                match = self.make_match()

                await self.invoke(ctx, match)

                ctx.defer.assert_awaited_once_with(ephemeral=True)
                self.bot.get_ext.assert_called_once_with("MatchCommands")
                self.match_commands.store_self_result.assert_awaited_once_with(ctx, match, player)
                self.match_commands.store_self_bases.assert_not_awaited()
                ctx.send.assert_not_awaited()

    async def test_rejects_missing_match_and_outsider(self) -> None:
        for author, match in (("300", self.make_match()), (CALLER_ID, None)):
            with self.subTest(author=author, match_present=match is not None):
                self.bot.reset_mock()
                ctx = self.make_context(author)

                await self.invoke(ctx, match)

                ctx.defer.assert_awaited_once_with(ephemeral=True)
                self.bot.get_ext.assert_not_called()
                self.match_commands.store_self_result.assert_not_awaited()
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_rejects_incomplete_match_configuration(self) -> None:
        cases = {
            "non-war": lambda match: setattr(match, "is_war", False),
            "unstarted-side": lambda match: match.times.__setitem__(0, None),
            "missing-host-clan": lambda match: match.host_clans.__setitem__(0, None),
            "missing-district": lambda match: match.final_districts.__setitem__(2, None),
            "wrong-district-count": lambda match: setattr(
                match,
                "final_districts",
                ["Barbarian Camp", "Wizard Valley"],
            ),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                self.bot.reset_mock()
                self.match_commands.store_self_result.reset_mock()
                ctx = self.make_context()
                match = self.make_match()
                mutate(match)

                await self.invoke(ctx, match)

                ctx.defer.assert_awaited_once_with(ephemeral=True)
                self.bot.get_ext.assert_not_called()
                self.match_commands.store_self_result.assert_not_awaited()
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_reports_missing_match_extension(self) -> None:
        # Given an eligible participant while MatchCommands is unavailable.
        self.bot.get_ext.return_value = None
        ctx = self.make_context()

        # When they invoke self store result.
        await self.invoke(ctx, self.make_match())

        # Then the result leaf reports unavailability without invoking a lifecycle.
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.bot.get_ext.assert_called_once_with("MatchCommands")
        self.match_commands.store_self_result.assert_not_awaited()
        self.match_commands.store_self_bases.assert_not_awaited()
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])


if __name__ == "__main__":
    unittest.main()
