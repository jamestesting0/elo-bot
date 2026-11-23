import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from elo_bot.config.ids import EXP_GRANT_CHANNEL, EXP_GRANT_COMMAND, MAIN_GUILD
from elo_bot.integrations.exp import ExpGrantError, grant_exp


class TestExpGrants(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = {}
        self.client = MagicMock(login=AsyncMock(), wait_until_ready=AsyncMock(), close=AsyncMock())
        def event(callback):
            self.events[callback.__name__] = callback
            return callback
        self.client.event.side_effect = event
        async def connect(**kwargs):
            await asyncio.Event().wait()
        self.client.connect = AsyncMock(side_effect=connect)
        self.channel = MagicMock(spec=discord.TextChannel)
        self.channel.id = EXP_GRANT_CHANNEL
        self.channel.guild.id = MAIN_GUILD
        self.channel.guild.fetch_member = AsyncMock(side_effect=lambda user_id: discord.Object(user_id))
        self.client.fetch_channel = AsyncMock(return_value=self.channel)
        self.add = AsyncMock()
        self.add.name = "add"
        self.command = MagicMock(spec=discord.SlashCommand)
        self.command.id = EXP_GRANT_COMMAND
        self.command.application_id = 894
        self.command.children = [self.add]
        self.channel.application_commands = AsyncMock(return_value=[self.command])
        self.enterContext(patch("elo_bot.integrations.exp.discord.Client", return_value=self.client))
        self.enterContext(patch("elo_bot.integrations.exp.BOT_STAFF_TOKEN", "test-secret"))

    def message(self, user="653894110884921366", *, author=894, interaction=321, title="Success", message_id=456):
        return SimpleNamespace(
            id=message_id, channel=self.channel, author=SimpleNamespace(id=author),
            interaction=SimpleNamespace(id=interaction), embeds=[discord.Embed.from_dict({
                "author": {"name": title}, "description": f"The user <@{user}> has received **3000** exp.",
            })],
        )

    async def test_only_correlated_confirmation_grants_exp_even_when_response_arrives_before_return(self):
        for edited in (False, True):
            with self.subTest(edited=edited):
                granted = {}
                reserved = False

                async def reserve():
                    nonlocal reserved
                    reserved = True

                async def submit(channel, member, amount):
                    self.assertTrue(reserved)
                    self.assertEqual(channel.id, 1329864725831356507)
                    granted[member.id] = amount
                    await self.events["on_message"](self.message(author=999, message_id=457))
                    await self.events["on_message"](self.message(interaction=999, message_id=458))
                    response = self.message()
                    if edited:
                        await self.events["on_raw_message_edit"](SimpleNamespace(message=response))
                    else:
                        await self.events["on_message"](response)
                    return SimpleNamespace(id=321)

                self.add.side_effect = submit
                result = await grant_exp("653894110884921366", reserve)
                self.assertEqual(granted, {653894110884921366: 3000})
                self.assertEqual(result, "456")
                self.client.close.assert_awaited()

    async def test_permission_failure_before_submit_does_not_reserve_or_expose_token(self):
        self.channel.application_commands.side_effect = discord.InvalidData("test-secret")
        reserve = AsyncMock()
        with self.assertRaises(ExpGrantError) as raised:
            await grant_exp("653894110884921366", reserve)
        self.assertFalse(raised.exception.uncertain)
        self.assertNotIn("test-secret", str(raised.exception))
        reserve.assert_not_awaited()
        self.add.assert_not_awaited()

    async def test_timeout_after_submission_stays_uncertain_and_does_not_resubmit(self):
        self.add.side_effect = discord.InvalidData("No response")
        reserve = AsyncMock()
        with self.assertRaises(ExpGrantError) as raised:
            await grant_exp("653894110884921366", reserve)
        self.assertTrue(raised.exception.uncertain)
        reserve.assert_awaited_once()
        self.add.assert_awaited_once()

    async def test_explicit_command_error_is_safe_to_return(self):
        async def submit(*args, **kwargs):
            await self.events["on_message"](self.message(title="Error"))
            return SimpleNamespace(id=321)

        self.add.side_effect = submit
        with self.assertRaises(ExpGrantError) as raised:
            await grant_exp("653894110884921366", AsyncMock())
        self.assertFalse(raised.exception.uncertain)

    async def test_wrong_recipient_or_amount_never_confirms_the_grant(self):
        original_timeout = asyncio.timeout
        for response in (self.message(user="999"), self.message()):
            with self.subTest(recipient=response.embeds[0].description):
                if "999" not in response.embeds[0].description:
                    response.embeds[0].description = "The user <@653894110884921366> has received **30** exp."

                async def submit(*args, **kwargs):
                    await self.events["on_message"](response)
                    return SimpleNamespace(id=321)

                self.add.side_effect = submit
                with patch("elo_bot.integrations.exp.asyncio.timeout", side_effect=lambda _: original_timeout(0.1)):
                    with self.assertRaises(ExpGrantError) as raised:
                        await grant_exp("653894110884921366", AsyncMock())
                self.assertTrue(raised.exception.uncertain)
