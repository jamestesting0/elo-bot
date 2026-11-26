import unittest
from unittest.mock import patch

import interactions as ipy

from elo_bot.app.client import create_client


class TestDiscordMessageUpdates(unittest.IsolatedAsyncioTestCase):
    async def test_link_preview_updates_the_cached_message_and_dispatches_message_update(self) -> None:
        bot = create_client("beta")
        message = bot.cache.place_message_data({"id": "43", "channel_id": "42", "content": "https://example.com"})
        event = ipy.events.RawGatewayEvent(data={
            "id": "43", "channel_id": "42",
            "embeds": [
                {"type": "components", "components": [{"type": 10, "content": "Link preview"}]},
                {"type": "rich", "title": "Regular embed"},
            ],
        })

        with patch.object(bot, "dispatch") as dispatch:
            await bot.processors["raw_message_update"](event)

        dispatch.assert_called_once()
        update = dispatch.call_args.args[0]
        self.assertIsInstance(update, ipy.events.MessageUpdate)
        self.assertEqual(update.before.embeds, [])
        self.assertIs(update.after, message)
        self.assertEqual(message.content, "https://example.com")
        self.assertEqual([embed.type.value for embed in message.embeds], ["components", "rich"])
        self.assertEqual(message.embeds[1].title, "Regular embed")
