import unittest

import interactions as ipy
from interactions.models.discord.message import process_message_payload

from common.discord.embeds import embed_edit_payload


class TestEmbedEditPayload(unittest.TestCase):
    def test_multiple_images_keep_images_and_clear_description(self) -> None:
        embed = ipy.Embed(title="Match", url="https://example.com/match")
        embed.set_images("https://example.com/one.png", "https://example.com/two.png")

        payload = process_message_payload(embeds=embed_edit_payload(embed))

        self.assertEqual(payload["embeds"][0]["description"], "")
        self.assertEqual([item["image"]["url"] for item in payload["embeds"]],
                         ["https://example.com/one.png", "https://example.com/two.png"])
        self.assertIsNone(embed.description)
