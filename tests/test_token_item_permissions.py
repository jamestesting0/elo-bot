from unittest.mock import AsyncMock, MagicMock, patch
import unittest

import interactions as ipy

from elo_bot.config.ids import LVL1_ROLES, MAIN_GUILD, STAFF_ROLE
from elo_bot.extensions.tokens import TokenCommands


class TestTokenItemPermissions(unittest.IsolatedAsyncioTestCase):
    async def test_item_adjustment_commands_reject_staff_and_wrong_servers_before_inventory_changes(self):
        cog = TokenCommands(ipy.Client())
        for command in (cog.tokens_give_item, cog.tokens_remove_item):
            with self.subTest(command=command.resolved_name):
                staff = MagicMock(guild_id=MAIN_GUILD, send=AsyncMock(), defer=AsyncMock(), kwargs={})
                staff.author.has_role.side_effect = lambda role: role == STAFF_ROLE
                manager = MagicMock(guild_id=MAIN_GUILD)
                manager.author.has_role.side_effect = lambda role: role in LVL1_ROLES
                self.assertTrue(all([await check(manager) for check in command.checks]))
                with patch("elo_bot.extensions.tokens.adjust_items", new=AsyncMock()) as adjust:
                    with self.assertRaises(ipy.errors.CommandCheckFailure):
                        await command(staff)
                    await command.callback.func(cog, staff, MagicMock(id=2), "exp_potion", 1)
                    self.assertIn("permission", staff.send.await_args.args[0])
                    outside = MagicMock(guild_id=123, send=AsyncMock(), defer=AsyncMock())
                    outside.author.has_role.return_value = True
                    await command.callback.func(cog, outside, MagicMock(id=2), "exp_potion", 1)
                    self.assertIn("server", outside.send.await_args.args[0])
                    adjust.assert_not_awaited()
