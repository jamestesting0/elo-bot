import unittest

import interactions as ipy

from elo_bot.config.presentation import FOOTER_IMAGE
from elo_bot.features.finals.bracket import automatic_pairs, build_bracket
from elo_bot.features.finals.prediction_ui import prediction_view
from elo_bot.core.finals import NODE_IDS
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalPrediction


class TestFinalPredictionNavigation(unittest.TestCase):
    def fixture(self, count):
        final = EloFinal.model_construct(season_number=13, entrant_ids=[str(i) for i in range(count)],
            player_nicknames={str(i): f"Player {i}" for i in range(count)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        prediction = FinalPrediction.model_construct(user_id="viewer", season_number=13,
            draft={key: node.winner for key, node in final.nodes.items() if node.bye})
        return final, prediction

    def next_button(self, final, prediction):
        _, rows = prediction_view(final, prediction)
        return next(button for row in rows for button in row.components if getattr(button, "label", None) == "Next")

    def test_next_waits_for_every_winner_on_the_current_page(self):
        final, prediction = self.fixture(8)
        for stage, nodes in enumerate((NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:])):
            with self.subTest(stage=stage):
                prediction.stage = stage
                self.assertTrue(self.next_button(final, prediction).disabled)
                for node in nodes:
                    prediction.draft[node] = "0"
                self.assertFalse(self.next_button(final, prediction).disabled)

    def test_byes_are_complete_without_an_extra_choice(self):
        final, prediction = self.fixture(5)
        played = next(key for key in NODE_IDS[:4] if not final.nodes[key].bye)
        self.assertTrue(self.next_button(final, prediction).disabled)
        prediction.draft[played] = final.nodes[played].contestants[0]
        self.assertFalse(self.next_button(final, prediction).disabled)

    def test_submit_appears_only_on_the_review_step(self):
        final, prediction = self.fixture(8)
        for stage in range(3):
            with self.subTest(stage=stage):
                prediction.stage = stage
                _, rows = prediction_view(final, prediction)
                self.assertNotIn("Submit", [button.label for button in rows[-1].components])

    def test_review_cannot_advance_even_with_complete_draft(self):
        final, prediction = self.fixture(8)
        prediction.stage = 3
        prediction.draft = dict.fromkeys(NODE_IDS, "0")
        _, rows = prediction_view(final, prediction)
        self.assertEqual([button.label for button in rows[-1].components], ["Back", "Submit"])
        prediction.draft["QF2"] = "1"
        embed, _ = prediction_view(final, prediction, {"0": "**[Player Zero](<https://discordapp.com/users/0?tab=wishlist>)**"})
        text = embed.description
        picks = text.split("\n\n", 1)[1].split("\n")
        self.assertEqual([line.split(" ", 1)[0] for line in picks],
                         ["`QF1`", "`QF2`", "`QF3`", "`QF4`", "`SF1`", "`SF2`", "🏆"])
        self.assertEqual(picks[0], "`QF1` **[Player Zero](<https://discordapp.com/users/0?tab=wishlist>)**")
        self.assertEqual(picks[1], "`QF2` **[❌Invalid Player](<https://discordapp.com/users/1?tab=wishlist>)**")

    def test_wizard_embed_names_every_select_and_fits_five_rows(self):
        final, prediction = self.fixture(8)
        prediction.draft = dict.fromkeys(NODE_IDS, "0")
        prediction.submitted = dict(prediction.draft)
        prediction.submitted_at = 1
        for stage in range(4):
            prediction.stage = stage
            embed, rows = prediction_view(final, prediction)
            self.assertLessEqual(len(rows), 5)
            self.assertEqual(embed.footer.icon_url, FOOTER_IMAGE)
            menus = [row.components[0] for row in rows if isinstance(row.components[0], ipy.StringSelectMenu)]
            self.assertEqual(len(menus), 0 if stage == 3 else len((NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:])[stage]))
            for menu in menus:
                self.assertIn(f"**{menu.custom_id.split('|')[-1]}** · ", embed.description)
                self.assertTrue(menu.placeholder.startswith("🏆 Choose the "))
            self.assertIn("Editing your submitted picks", embed.description)
            self.assertEqual(embed.image.url if stage == 3 else embed.image, "attachment://prediction.png" if stage == 3 else None)
        embed, rows = prediction_view(final, prediction, submitted=True)
        self.assertEqual(rows[-1].components[0].label, "Edit Picks")
        self.assertEqual(embed.footer.icon_url, FOOTER_IMAGE)
