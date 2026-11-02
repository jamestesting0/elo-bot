import unittest
from unittest.mock import AsyncMock, MagicMock, call

import interactions as ipy

from cogs.self_commands import SelfCommands
from common.constants import AUTOMATIC_MATCH_THREAD, LVL2_ROLES


class TestSelfCommandAccess(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.fetch_channel = AsyncMock()
        self.cog = SelfCommands(self.bot)

    def make_context(self, caller_id: int, role_ids: set[int]) -> MagicMock:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.bot = self.bot
        ctx.guild = MagicMock()
        ctx.author.id = caller_id
        ctx.author.has_role.side_effect = lambda role_id: int(role_id) in role_ids
        return ctx

    async def assert_self_command_access(self, ctx: MagicMock, expected: bool) -> None:
        for command in (self.cog.self_host_war, self.cog.self_store_base, self.cog.self_store_result):
            with self.subTest(command=command.resolved_name):
                self.assertEqual(len(command.checks), 1)
                self.assertIs(await command.checks[0](ctx), expected)

    async def test_self_commands_allow_lvl2_role_without_thread_lookup(self) -> None:
        # Given a caller with one of the permitted LVL2 roles.
        ctx = self.make_context(caller_id=123, role_ids={LVL2_ROLES[-1]})

        # When both self-service command checks run.
        await self.assert_self_command_access(ctx, expected=True)

        # Then role access short-circuits without querying the automatic-match thread.
        self.bot.fetch_channel.assert_not_awaited()

    async def test_self_commands_allow_automatic_match_thread_member_without_lvl2_role(self) -> None:
        # Given a caller without an LVL2 role who belongs to the automatic-match thread.
        ctx = self.make_context(caller_id=123, role_ids=set())
        thread_member = MagicMock()
        thread_member._user_id = ctx.author.id
        thread = MagicMock(spec=ipy.ThreadChannel)
        thread.fetch_members = AsyncMock(return_value=[thread_member])
        self.bot.fetch_channel.return_value = thread

        # When both self-service command checks run.
        await self.assert_self_command_access(ctx, expected=True)

        # Then membership in the configured thread grants access.
        self.bot.fetch_channel.assert_has_awaits([
            call(AUTOMATIC_MATCH_THREAD),
            call(AUTOMATIC_MATCH_THREAD),
            call(AUTOMATIC_MATCH_THREAD),
        ])

    async def test_self_commands_deny_caller_without_lvl2_role_or_thread_membership(self) -> None:
        # Given a caller who has neither an LVL2 role nor automatic-thread membership.
        ctx = self.make_context(caller_id=123, role_ids=set())
        other_member = MagicMock()
        other_member._user_id = 999
        thread = MagicMock(spec=ipy.ThreadChannel)
        thread.fetch_members = AsyncMock(return_value=[other_member])
        self.bot.fetch_channel.return_value = thread

        # When both self-service command checks run.
        await self.assert_self_command_access(ctx, expected=False)

        # Then the lack of both access paths denies both commands.
        self.assertEqual(thread.fetch_members.await_count, 3)


if __name__ == "__main__":
    unittest.main()
