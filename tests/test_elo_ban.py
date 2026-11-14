import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.app.client import create_client
from elo_bot.features.admin import commands as admin_commands
from elo_bot.config.ids import ELO_BLACKLIST_ROLE


class TestEloBan(unittest.IsolatedAsyncioTestCase):
    async def test_blacklisted_interactions_never_reach_callbacks_or_waiters(self):
        for profile in ("main", "beta"):
            bot = create_client(profile)
            for kind in (2, 3, 4, 5):
                with self.subTest(module=profile, kind=kind):
                    event = ipy.events.RawGatewayEvent(data={
                        "type": kind, "id": "123", "token": "test-token",
                        "member": {"roles": [str(ELO_BLACKLIST_ROLE)]},
                    })
                    with patch.object(bot.http, "post_initial_response", new_callable=AsyncMock) as respond:
                        with patch.object(ipy.Client._dispatch_interaction, "callback", new_callable=AsyncMock) as dispatch:
                            await bot.processors["raw_interaction_create"](event)
                    dispatch.assert_not_awaited()
                    payload = respond.await_args.args[0]
                    if kind == 4:
                        self.assertEqual(payload, {"type": 8, "data": {"choices": []}})
                    else:
                        self.assertEqual(payload["type"], 4)
                        self.assertEqual(payload["data"]["flags"], 64)
                        self.assertIn("banned", payload["data"]["content"])

    async def test_unbanned_interactions_pass_through(self):
        for profile in ("main", "beta"):
            bot = create_client(profile)
            for member in ({"roles": []}, None):
                event = ipy.events.RawGatewayEvent(data={"type": 2})
                if member is not None:
                    event.data["member"] = member
                with patch.object(ipy.Client._dispatch_interaction, "callback", new_callable=AsyncMock) as dispatch:
                    await bot.processors["raw_interaction_create"](event)
                dispatch.assert_awaited_once_with(bot, event)

    async def test_role_changes_require_staff_and_confirm_after_persistence(self):
        for module in (admin_commands,):
            for command_name, method in (("ban", "add_role"), ("unban", "remove_role")):
                with self.subTest(module=module.__name__, command=command_name):
                    ctx = MagicMock(spec=ipy.SlashContext)
                    ctx.author = MagicMock(spec=ipy.Member)
                    ctx.defer = AsyncMock()
                    ctx.send = AsyncMock()
                    user = MagicMock(spec=ipy.Member, mention="<@123>")

                    async def change_role(role):
                        self.assertEqual(role, module.ELO_BLACKLIST_ROLE)
                        ctx.send.assert_not_awaited()

                    role_change = AsyncMock(side_effect=change_role)
                    setattr(user, method, role_change)
                    command = getattr(module, command_name)
                    ctx.author.has_role.return_value = False
                    self.assertFalse(await command.checks[0](ctx))
                    ctx.author.has_role.return_value = True
                    self.assertTrue(await command.checks[0](ctx))
                    await command.callback(ctx, user)
                    role_change.assert_awaited_once_with(module.ELO_BLACKLIST_ROLE)
                    ctx.send.assert_awaited_once()
                    self.assertTrue(ctx.send.await_args.args[0].startswith(str(module.SUCCESS_EMOJI)))
                    self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_failed_role_change_does_not_announce_success(self):
        for module in (admin_commands,):
            for command_name, method in (("ban", "add_role"), ("unban", "remove_role")):
                with self.subTest(module=module.__name__, command=command_name):
                    ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
                    user = MagicMock()
                    setattr(user, method, AsyncMock(side_effect=RuntimeError("role failed")))
                    with self.assertRaisesRegex(RuntimeError, "role failed"):
                        await getattr(module, command_name).callback(ctx, user)
                    ctx.send.assert_not_awaited()
