import unittest
from functools import lru_cache, partialmethod
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageChops

from elo_bot.features.finals.bracket import advance_nodes, automatic_pairs, build_bracket
from elo_bot.features.finals.graphic_labels import fitted_label
from elo_bot.features.finals.rendering import bracket_labels, render_bracket, render_elo_kings
from elo_bot.features.finals.prediction_display import prediction_statuses
from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalPrediction


NAME_PLATES = (
    (132, 502, 315, 536), (132, 551, 315, 585), (132, 600, 315, 635), (132, 650, 315, 684),
    (132, 700, 315, 735), (132, 752, 315, 786), (132, 800, 315, 834), (132, 849, 315, 883),
    (421, 535, 601, 572), (421, 632, 600, 669), (421, 725, 601, 761), (421, 820, 600, 856),
    (705, 594, 883, 630), (705, 766, 883, 802), (977, 636, 1180, 691),
)
PLATE_MIDLINES = (520, 569, 619, 668, 719, 770, 818, 867, 555, 652, 744, 839, 613, 785, 666)


class TestFinalRendering(unittest.TestCase):
    def edition(self, count):
        final = EloFinal.model_construct(season_number=1, active=True,
                                         entrant_ids=[str(i) for i in range(count)],
                                         player_nicknames={str(i): f"Player {i}" for i in range(count)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        return final

    def test_occupied_plates_have_centered_text_and_empty_plates_keep_the_artwork(self):
        for count in (5, 8):
            with self.subTest(count=count):
                final = self.edition(count)
                labels = bracket_labels(final)
                self.assertEqual(len(labels), 15)
                self.assertEqual(labels.count("BYE"), 8 - count)
        final = self.edition(8)
        for names in ({str(i): f"Player {i}" for i in range(8)},
                      {str(i): "Long contestant " * 3 for i in range(8)},
                      {str(i): "王者" * 5 if i % 2 == 0 else "Ελληνικά" * 3 for i in range(8)}):
            final.player_nicknames = names
            final.champion = "0"
            with self.subTest(names=names), \
                    patch("elo_bot.features.finals.rendering.fitted_label", lru_cache(maxsize=None)(fitted_label)), \
                    patch.object(Image.Image, "save", partialmethod(Image.Image.save, compress_level=0)), \
                    Image.open("assets/finals/elo_final_bracket.png") as template, \
                    Image.open(BytesIO(render_bracket(final))) as rendered:
                self.assertEqual(rendered.size, (1312, 941))
                difference = ImageChops.difference(template.crop((180, 0, 1492, 941)).convert("RGB"), rendered).convert("L")
                remaining = difference.copy()
                for index, (plate, center_y) in enumerate(zip(NAME_PLATES, PLATE_MIDLINES, strict=True)):
                    with self.subTest(plate=plate):
                        bounds = difference.crop(plate).getbbox()
                        if 8 <= index < 14:
                            self.assertIsNone(bounds, "Unresolved advancement slots should be blank")
                            continue
                        self.assertIsNotNone(bounds, "Occupied name plates must contain rendered text")
                        left, top, right, bottom = bounds
                        self.assertLessEqual(abs((left + right) / 2 - (plate[2] - plate[0]) / 2), 1.5)
                        self.assertLessEqual(abs(plate[1] + (top + bottom) / 2 - center_y), 1)
                        remaining.paste(0, plate)
                self.assertIsNone(remaining.getbbox(), "Text must not overlap frames, connectors or artwork")

    def test_leaderboard_capitals_and_scores_center_in_their_respective_fields(self):
        rows = (("Hera | S11 King", 1824, None), ("Héloïse | S12 King", 1751, None),
                ("Hugo | S13 King", 1678, None), ("Hank | S14 King", 1605, None))
        with Image.open("assets/finals/elo_kings_leaderboard.png") as template, \
                Image.open(BytesIO(render_elo_kings(rows))) as rendered:
            difference = ImageChops.difference(template.convert("RGB"), rendered).convert("L")
            for name_y, score_y in ((744, 743), (788, 788), (833, 833), (878, 878)):
                for left, right, center_y in ((519, 541, name_y), (1086, 1182, score_y), (1182, 1215, score_y)):
                    with self.subTest(center_y=center_y, column=left):
                        bounds = difference.crop((left, center_y - 20, right, center_y + 20)).getbbox()
                        self.assertIsNotNone(bounds)
                        self.assertLessEqual(abs((bounds[1] + bounds[3]) / 2 - 20), 1)

    def test_leaderboard_accent_does_not_move_the_king_title(self):
        plain = render_elo_kings([("Etoile | S11 King", 1824, None)])
        accented = render_elo_kings([("Étoile | S11 King", 1824, None)])
        with Image.open(BytesIO(plain)) as first, Image.open(BytesIO(accented)) as second:
            difference = ImageChops.difference(first, second)
            self.assertIsNotNone(difference.crop((519, 723, 630, 765)).getbbox())
            self.assertIsNone(difference.crop((650, 723, 810, 765)).getbbox())

    def test_completed_bracket_labels_follow_each_round_in_plate_order(self):
        final = self.edition(8)
        final.nodes = build_bracket(final, {f"QF{i + 1}": [str(2 * i), str(2 * i + 1)] for i in range(4)})
        for i in range(4):
            final.nodes[f"QF{i + 1}"].winner = str(2 * i)
        advance_nodes(final.nodes)
        final.nodes["SF1"].winner = "0"
        final.nodes["SF2"].winner = "4"
        advance_nodes(final.nodes)
        final.nodes["F1"].winner = final.champion = "0"
        self.assertEqual(bracket_labels(final), [f"Player {i}" for i in range(8)]
                         + ["Player 0", "Player 2", "Player 4", "Player 6", "Player 0", "Player 4", "Player 0"])

    def test_prediction_labels_use_submitted_picks_without_changing_official_bracket(self):
        final = self.edition(5)
        final.nodes["QF2"].winner = "4"
        final.champion = "4"
        before = final.model_dump()
        picks = {"QF1": "0", "QF2": "3", "QF3": "1", "QF4": "2",
                 "SF1": "3", "SF2": "2", "F1": "2"}
        labels = bracket_labels(final, picks)
        self.assertEqual(labels[:8].count("BYE"), 3)
        self.assertEqual(labels[8:], ["Player 0", "Player 3", "Player 1", "Player 2",
                                      "Player 3", "Player 2", "Player 2"])
        with Image.open(BytesIO(render_bracket(final, picks))) as rendered:
            self.assertEqual(rendered.size, (1312, 941))
        self.assertEqual(final.model_dump(), before)

    def test_prediction_markers_follow_submitted_picks_without_changing_official_image(self):
        final = self.edition(8)
        picks = {"QF1": "0", "QF2": "3", "QF3": "1", "QF4": "2"}
        for node_id, winner, outcome in (("QF1", "0", "Played"), ("QF2", "4", "Played"),
                                         ("QF3", "1", "Forfeit")):
            node = final.nodes[node_id]
            node.winner, node.outcome, node.result_id = winner, outcome, node_id
        prediction = FinalPrediction.model_construct(submitted=picks)
        statuses = prediction_statuses(final, prediction)
        self.assertEqual(statuses, {"QF1": "correct", "QF2": "incorrect", "QF3": "excluded"})
        official = render_bracket(final)
        with Image.open(BytesIO(render_bracket(final, picks))) as plain, \
                Image.open(BytesIO(render_bracket(final, picks, statuses))) as marked:
            diff = ImageChops.difference(plain, marked)
            for x, y in ((613, 554), (613, 651), (613, 743)):
                self.assertIsNotNone(diff.crop((x - 15, y - 15, x + 16, y + 16)).getbbox())
            self.assertIsNone(diff.crop((598, 823, 629, 854)).getbbox())
        self.assertEqual(official, render_bracket(final))

    def test_measured_long_name_is_shortened_to_box_width(self):
        label, font = fitted_label("Long contestant " * 3)
        bounds = font.getbbox(label, stroke_width=1)
        self.assertLessEqual(bounds[2] - bounds[0], 168)
        self.assertTrue(label.endswith("…"))

    def test_bracket_renders_saved_names_when_only_the_bundled_font_is_available(self):
        final = self.edition(8)
        final.player_nicknames.update({"0": "Al Pal | (っ◔ᴗ◔)っ ♥ asmo ♥", "1": "👾\u200d🎮"})
        with patch("elo_bot.features.finals.graphic_labels.FALLBACK_FONTS", ()):
            actual = render_bracket(final, usernames={"0": "al.pall"})

        final.player_nicknames.update({"0": "Al Pal asmo", "1": "1"})
        self.assertEqual(actual, render_bracket(final))

    def test_graphic_labels_normalize_stylized_names_and_replace_invisible_names(self):
        cases = ((" 𝓐𝓵\u202e \nＰａｌ ", "Al Pal"), ("\u200b\u200d\u2800", "al.pal"),
                 ("\u0301" * 20, "al.pal"))
        with patch("elo_bot.features.finals.graphic_labels.FALLBACK_FONTS", ()):
            for name, expected in cases:
                with self.subTest(name=name):
                    label, font = fitted_label(name, username="al.pal")
                    self.assertEqual(label, expected)
                    self.assertIsNotNone(font.getmask(label).getbbox())

    def test_missing_template_reports_truthful_error(self):
        with patch("elo_bot.features.finals.rendering.TEMPLATE", Path("absent-final-template.png")):
            with self.assertRaises(FinalError):
                render_bracket(self.edition(5))

    def test_unmapped_template_size_keeps_text_fallback(self):
        with TemporaryDirectory() as directory:
            template = Path(directory) / "bracket.png"
            with Image.new("RGB", (100, 100)) as image:
                image.save(template)
            with patch("elo_bot.features.finals.rendering.TEMPLATE", template):
                with self.assertRaisesRegex(FinalError, "template dimensions changed"):
                    render_bracket(self.edition(8))

    def test_eliminated_bye_and_champion_text_use_distinct_plate_colors(self):
        final = self.edition(5)
        node = final.nodes["QF2"]
        node.winner = node.contestants[1]
        node.state = "DECIDED"
        final.champion = node.winner
        with Image.open(BytesIO(render_bracket(final))) as image:
            bye_colors = set(image.crop(NAME_PLATES[1]).getdata())
            loser_colors = set(image.crop(NAME_PLATES[2]).getdata())
            winner_colors = set(image.crop(NAME_PLATES[3]).getdata())
            champion_colors = set(image.crop(NAME_PLATES[-1]).getdata())
        self.assertIn((224, 194, 157), bye_colors)
        self.assertIn((224, 194, 157), loser_colors)
        self.assertIn((255, 246, 229), winner_colors)
        self.assertIn((255, 230, 160), champion_colors)

    def test_changed_pairing_leaves_the_advancement_label_blank(self):
        final = self.edition(8)
        final.nodes["SF1"].state = "NEEDS_MANAGER"
        self.assertEqual(bracket_labels(final)[12], "")

    def test_removed_champion_is_not_rendered_after_correction(self):
        final = self.edition(8)
        final.champion = "3"
        self.assertEqual(bracket_labels(final)[-1], "Player 3")
        final.champion = None
        self.assertEqual(bracket_labels(final)[-1], "")
