import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie.odm.settings.document import DocumentSettings

from cogs.giveaway_commands import GiveawayCommands
from elobot.db import Giveaway


class TestGiveawayTerminalRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(Giveaway, "_document_settings", DocumentSettings(
            name="giveaways", use_state_management=True,
        )))
        self.row = {
            "_id": "42", "end_timestamp": 1, "ended": True, "entries": ["100", "200"],
            "sponsor_id": "300", "channel_id": 50, "winner_id": "100", "draw_id": "ORIGINAL",
        }
        self.message = MagicMock(embeds=[ipy.Embed(
            title="Prize Giveaway`(Ending)`", description="Sponsor terms.", fields=[
                ipy.EmbedField(name="Details", value="Ends: earlier\nEntries: 2"),
                ipy.EmbedField(name="Status", value="Ending in progress."),
            ],
        )], components=[], edit=AsyncMock(), reply=AsyncMock())

        async def edit(**kwargs):
            self.message.embeds = [deepcopy(kwargs["embed"])]
            self.message.components = deepcopy(kwargs["components"])

        self.message.edit.side_effect = edit
        self.bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=MagicMock(
            fetch_message=AsyncMock(return_value=self.message),
        )))
        self.bot.fetch_channel.return_value.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Member {user_id}"),
        )
        self.cog = GiveawayCommands(self.bot)
        self.get = self.enterContext(patch.object(Giveaway, "get", new=AsyncMock(side_effect=self.load)))
        self.collection = MagicMock(update_one=AsyncMock())
        self.enterContext(patch.object(Giveaway, "get_pymongo_collection", return_value=self.collection))

    def load(self, *_args):
        document = Giveaway.model_validate(deepcopy(self.row))
        document._save_state()
        return document

    async def test_retry_repaints_the_saved_winner_without_redraw_or_announcement(self) -> None:
        expected = deepcopy(self.row)
        result = await self.cog.end_giveaway("42")

        self.assertTrue(result)
        self.assertEqual(self.row, expected)
        self.assertEqual(self.message.embeds[0].title, "Prize Giveaway `(Ended)`")
        self.assertIn("Winner: **[Member 100](<https://discordapp.com/users/100>)**",
                      self.message.embeds[0].fields[0].value)
        self.assertEqual(self.message.embeds[0].description, "Sponsor terms.")
        self.assertEqual(len(self.message.embeds[0].fields), 1)
        self.assertEqual(self.message.components, [])
        self.collection.update_one.assert_not_awaited()
        self.message.reply.assert_not_awaited()

        self.assertFalse(await self.cog.end_giveaway("42"))
        self.message.edit.assert_awaited_once()
        self.message.reply.assert_not_awaited()

    async def test_delayed_repaint_cannot_replace_a_newer_reroll(self) -> None:
        reads = 0

        def get(*_args):
            nonlocal reads
            reads += 1
            if reads == 2:
                self.row.update(draw_id="NEWER", winner_id="200")
                self.message.embeds = [ipy.Embed(
                    title="Prize Giveaway`(Ended)`", fields=[ipy.EmbedField(name="Details", value="Winner: <@200>")],
                )]
            return self.load()

        self.get.side_effect = get
        result = await self.cog.end_giveaway("42")

        self.assertFalse(result)
        self.assertEqual(self.row["draw_id"], "NEWER")
        self.assertEqual(self.message.embeds[0].fields[0].value, "Winner: <@200>")
        self.message.edit.assert_not_awaited()
        self.message.reply.assert_not_awaited()

    async def test_ordinary_end_command_can_recover_the_terminal_message(self) -> None:
        ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        await self.cog.giveaway_end.callback.func(self.cog, ctx, "42")

        self.assertEqual(self.message.embeds[0].title, "Prize Giveaway `(Ended)`")
        self.assertIn("has been ended", ctx.send.await_args.args[0])
        self.collection.update_one.assert_not_awaited()
        self.message.reply.assert_not_awaited()
