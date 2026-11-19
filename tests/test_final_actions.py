import unittest
from typing import Literal, assert_never
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.config.ids import MAIN_GUILD
from elo_bot.core.finals import FinalError
from elo_bot.extensions.final.checks import final_action


class TestFinalActionResponses(unittest.IsolatedAsyncioTestCase):
    def context(self, component=False):
        client = MagicMock()
        client.app.id = 42
        user = MagicMock(id=9)
        user.has_role.return_value = False
        client.cache.get_member.return_value = user
        client.cache.place_user_data.return_value = user
        client.cache.get_user.return_value = user
        message = MagicMock(id=9001, flags=ipy.MessageFlags.EPHEMERAL)
        client.cache.place_message_data.return_value = message
        client.http.post_initial_response = AsyncMock()
        client.http.get_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.edit_interaction_message = AsyncMock(return_value={"id": "9001"})
        client.http.post_followup = AsyncMock(return_value={"id": "9002"})
        payload = {"id": "1750000000000000000", "application_id": "42", "type": 3 if component else 2,
                   "token": "offline", "version": 1, "locale": "en-US", "channel_id": "77", "guild_id": str(MAIN_GUILD),
                   "user": {"id": "9", "username": "Player", "discriminator": "0", "avatar": None},
                   "data": {"custom_id": "final-action", "component_type": 2} if component else
                           {"id": "123", "name": "final", "type": 1}}
        if component:
            payload["message"] = {"id": "9001"}
        cls = ipy.ComponentContext if component else ipy.SlashContext
        return cls.from_dict(client, payload), client

    async def test_errors_follow_the_interaction_acknowledgement_without_extra_messages(self):
        failure = FinalError("Fixture rejection")

        @final_action
        async def fail(owner: None, ctx: ipy.InteractionContext) -> None:
            raise failure

        states: tuple[Literal["fresh", "deferred", "responded", "edit_origin"], ...] = (
            "fresh", "deferred", "responded", "edit_origin",
        )
        for state in states:
            with self.subTest(state=state):
                ctx, client = self.context(component=state == "edit_origin")
                match state:
                    case "fresh":
                        pass
                    case "deferred":
                        await ctx.defer(ephemeral=True)
                    case "responded":
                        await ctx.send("Earlier response", ephemeral=True)
                    case "edit_origin":
                        await ctx.defer(edit_origin=True)
                    case unreachable:
                        assert_never(unreachable)
                client.http.post_initial_response.reset_mock()
                client.http.post_followup.reset_mock()
                await fail(None, ctx)
                expected = f"{ERROR_EMOJI} {failure}"
                match state:
                    case "fresh":
                        data = client.http.post_initial_response.await_args.args[0]["data"]
                        self.assertEqual(data["content"], expected)
                        self.assertTrue(data["flags"] & ipy.MessageFlags.EPHEMERAL)
                    case "responded":
                        data = client.http.post_followup.await_args.args[0]
                        self.assertEqual(data["content"], expected)
                        self.assertTrue(data["flags"] & ipy.MessageFlags.EPHEMERAL)
                        client.http.post_initial_response.assert_not_awaited()
                    case "deferred" | "edit_origin":
                        data = client.http.edit_interaction_message.await_args.args[0]
                        self.assertEqual(data["content"], expected)
                        client.http.post_initial_response.assert_not_awaited()
                        client.http.post_followup.assert_not_awaited()
                        if state == "edit_origin":
                            self.assertEqual(data["components"], [])
                            self.assertEqual(data["embeds"], [])
                    case unreachable:
                        assert_never(unreachable)

    async def test_unexpected_errors_reach_the_global_handler(self):
        @final_action
        async def fail(owner: None, ctx: ipy.InteractionContext) -> None:
            raise LookupError("Fixture unexpected failure")

        ctx, client = self.context()
        with self.assertRaises(LookupError):
            await fail(None, ctx)
        client.http.post_initial_response.assert_not_awaited()
        client.http.post_followup.assert_not_awaited()
