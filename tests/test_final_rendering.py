import unittest
from functools import lru_cache, partialmethod
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageChops

from common.finals.bracket import advance_nodes, automatic_pairs, build_bracket
from common.finals.rendering import bracket_labels, fitted_label, render_bracket
from common.finals.types import FinalError
from database_models import EloFinal


# Blue interiors measured from the supplied image, independently of renderer centers.
NAME_PLATES = (
    (312, 502, 495, 536), (312, 551, 495, 585), (312, 600, 495, 635), (312, 650, 495, 684),
    (312, 700, 495, 735), (312, 752, 495, 786), (312, 800, 495, 834), (312, 849, 495, 883),
    (601, 535, 781, 572), (601, 632, 780, 669), (601, 725, 781, 761), (601, 820, 780, 856),
    (885, 594, 1063, 630), (885, 766, 1063, 802), (1157, 636, 1360, 691),
)


class TestFinalRendering(unittest.TestCase):
    def edition(self, count):
        final = EloFinal.model_construct(season_number=1, active=True,
                                         entrant_ids=[str(i) for i in range(count)],
                                         player_nicknames={str(i): f"Player {i}" for i in range(count)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        return final

    def test_ink_is_centered_inside_every_plate_without_changing_the_artwork(self):
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
                    patch("common.finals.rendering.fitted_label", lru_cache(maxsize=None)(fitted_label)), \
                    patch.object(Image.Image, "save", partialmethod(Image.Image.save, compress_level=0)), \
                    Image.open("assets/elo_final_bracket.png") as template, \
                    Image.open(BytesIO(render_bracket(final))) as rendered:
                self.assertEqual(rendered.size, (1672, 941))
                difference = ImageChops.difference(template.convert("RGB"), rendered).convert("L")
                remaining = difference.copy()
                for plate in NAME_PLATES:
                    with self.subTest(plate=plate):
                        bounds = difference.crop(plate).getbbox()
                        self.assertIsNotNone(bounds, "Every name plate must contain rendered text")
                        left, top, right, bottom = bounds
                        self.assertLessEqual(abs((left + right) / 2 - (plate[2] - plate[0]) / 2), 1.5)
                        self.assertLessEqual(abs((top + bottom) / 2 - (plate[3] - plate[1]) / 2), 1.5)
                        remaining.paste(0, plate)
                self.assertIsNone(remaining.getbbox(), "Text must not overlap frames, connectors or artwork")

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

    def test_measured_long_name_is_shortened_to_box_width(self):
        label, font = fitted_label("Long contestant " * 3)
        bounds = font.getbbox(label, stroke_width=1)
        self.assertLessEqual(bounds[2] - bounds[0], 168)
        self.assertTrue(label.endswith("…"))

    def test_missing_template_reports_truthful_error(self):
        with patch("common.finals.rendering.TEMPLATE", Path("absent-final-template.png")):
            with self.assertRaises(FinalError):
                render_bracket(self.edition(5))

    def test_unmapped_template_size_keeps_text_fallback(self):
        with TemporaryDirectory() as directory:
            template = Path(directory) / "bracket.png"
            with Image.new("RGB", (100, 100)) as image:
                image.save(template)
            with patch("common.finals.rendering.TEMPLATE", template):
                with self.assertRaisesRegex(FinalError, "template dimensions changed"):
                    render_bracket(self.edition(8))

    def test_removed_champion_is_not_rendered_after_correction(self):
        final = self.edition(8)
        final.champion = "3"
        self.assertEqual(bracket_labels(final)[-1], "Player 3")
        final.champion = None
        self.assertEqual(bracket_labels(final)[-1], "TBD")
