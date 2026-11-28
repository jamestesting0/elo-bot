import json
from pathlib import Path
import unittest

from elo_bot.config.emojis import TOKEN_EMOJI, configure_application_emojis, render_emoji_markup
from elo_bot.config.presentation import FOOTER_TEXT
from elo_bot.db.token_models import TokenWallet
from elo_bot.discord.embeds import v2_footer_text
from elo_bot.features.embeds.readme import readme_page
from elo_bot.features.tokens.item_view import item_panel


class TestApplicationEmojis(unittest.IsolatedAsyncioTestCase):
    async def test_shop_readme_and_footer_resolve_emojis_from_each_application(self):
        self.addCleanup(configure_application_emojis, "test-fallback")
        files = Path(__file__).resolve().parents[1] / "assets" / "emojis"
        versions = json.loads((files / "application_ids.json").read_text())
        sources = json.loads((files / "application_sources.json").read_text())
        self.assertEqual(set(versions["beta"]), set(sources))
        self.assertEqual(set(versions["main"]), set(sources))
        self.assertTrue(set(versions["beta"].values()).isdisjoint(versions["main"].values()))

        wallet = TokenWallet.model_construct(id="1", user_id="1", balance=5000)
        for version, ids in versions.items():
            with self.subTest(application=version):
                configure_application_emojis(version)
                self.assertEqual(str(TOKEN_EMOJI.id), ids["token"])
                self.assertEqual(v2_footer_text(), f"-# <:elo_footer:{ids['elo_footer']}> {FOOTER_TEXT}")
                for page in range(2):
                    embed, rows = await item_panel(wallet, "shop", page)
                    for option in rows[0].to_dict()["components"][0]["options"]:
                        self.assertEqual(str(option["emoji"]["id"]), ids[option["value"]])
                        self.assertTrue(any(ids[option["value"]] in field.name for field in embed.fields))
                self.assertIn(str(TOKEN_EMOJI), readme_page("elo_final", 1)[0].description)
                self.assertEqual(render_emoji_markup("<:external:123456789>"), "<:external:123456789>")
