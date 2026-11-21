import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image, ImageChops, ImageDraw

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.final_models import FinalResultSnapshot
from elo_bot.features.finals.rendering import render_scoreboard


# Interiors measured independently on the supplied clean artwork.
NAME_PLATES = ((434, 214, 640, 258), (1032, 214, 1238, 258))
ROW_Y = (342, 409, 476, 543, 610, 677, 744)
SCORE_PLATES = tuple((x1, y - 22, x2, y + 22) for x1, x2 in
    ((448, 532), (552, 704), (1076, 1160), (1180, 1332)) for y in ROW_Y)
TOTAL_PLATES = ((425, 802, 509, 856), (533, 802, 685, 856),
                (1102, 802, 1186, 856), (1204, 802, 1352, 856))
AVATAR_BOXES = ((286, 150, 416, 280), (1255, 150, 1385, 280))


class TestFinalScoreboard(unittest.TestCase):
    def setUp(self):
        self.final = EloFinal.model_construct(season_number=15, player_nicknames={"1": "SenFGr", "2": "Kaito"})
        self.snapshot = FinalResultSnapshot(contestants=["1", "2"], winner="2", scores={
            "1": ["2.83", "3.100", "2.71", "3.100", "3.100", "2.58", "3.100"], "2": ["3.100"] * 7})

    def test_text_fits_every_measured_plate_and_preserves_the_artwork_for_long_and_cjk_names(self):
        for names in ({"1": "SenFGr", "2": "Kaito"},
                      {"1": "Long contestant " * 5, "2": "王者" * 15}):
            with self.subTest(names=names):
                self.final.player_nicknames = names
                with Image.open("assets/finals/elo_final_scoreboard.png") as template, \
                        Image.open(BytesIO(render_scoreboard(self.final, self.snapshot, {}))) as rendered:
                    self.assertEqual(rendered.size, (1672, 941))
                    difference = ImageChops.difference(template.convert("RGB"), rendered).convert("L")
                    remaining = difference.copy()
                    for plate in (*NAME_PLATES, *SCORE_PLATES, *TOTAL_PLATES):
                        with self.subTest(plate=plate):
                            bounds = difference.crop(plate).getbbox()
                            self.assertIsNotNone(bounds)
                            left, top, right, bottom = bounds
                            self.assertLessEqual(abs((left + right) / 2 - (plate[2] - plate[0]) / 2), 1)
                            self.assertLessEqual(abs((top + bottom) / 2 - (plate[3] - plate[1]) / 2), 1)
                            remaining.paste(0, plate)
                    self.assertIsNone(remaining.getbbox(), "Ink must stay inside the measured plate interiors")

    def test_each_district_and_total_uses_saved_scores_in_the_correct_side_and_row(self):
        drawn = []
        draw_text = ImageDraw.ImageDraw.text

        def record(draw, position, text, *args, **kwargs):
            drawn.append((draw.textbbox(position, text, font=kwargs["font"], stroke_width=1), text))
            return draw_text(draw, position, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_scoreboard(self.final, self.snapshot, {})
        expected = (
            ("SenFGr", *("2", "83%", "3", "100%", "2", "71%", "3", "100%", "3", "100%",
                         "2", "58%", "3", "100%"), "18", "612%"),
            ("Kaito", *("3", "100%") * 7, "21", "700%"),
        )
        self.assertEqual([text for _, text in drawn], [text for side in expected for text in side])
        for side, offset in enumerate((0, 17)):
            for row, y in enumerate(ROW_Y):
                x, top, _, _ = drawn[offset + 1 + row * 2][0]
                self.assertTrue((448 if side == 0 else 1076) <= x < (532 if side == 0 else 1160))
                self.assertTrue(y - 22 <= top <= y + 22)

    def test_winner_and_loser_colors_apply_to_names_scores_and_totals(self):
        for winner in ("1", "2"):
            self.snapshot.winner = winner
            with self.subTest(winner=winner), Image.open(BytesIO(
                    render_scoreboard(self.final, self.snapshot, {}))) as image:
                for side, user in enumerate(("1", "2")):
                    muted = (226, 207, 173)
                    normal = (255, 240, 213)
                    plates = (NAME_PLATES[side], *SCORE_PLATES[side * 14:side * 14 + 14],
                              *TOTAL_PLATES[side * 2:side * 2 + 2])
                    for index, plate in enumerate(plates):
                        active = (255, 240, 215) if index == 0 else normal
                        color, other = (active, muted) if user == winner else (muted, active)
                        colors = set(image.crop(plate).getdata())
                        self.assertIn(color, colors)
                        self.assertNotIn(other, colors)

    def test_avatar_is_center_cropped_and_circle_edges_are_antialiased_without_overpainting_frames(self):
        with Image.new("RGB", (390, 130), "red") as source, BytesIO() as output:
            ImageDraw.Draw(source).rectangle((130, 0, 259, 129), fill="#32a783")
            source.save(output, format="PNG")
            data = output.getvalue()
        with Image.open("assets/finals/elo_final_scoreboard.png") as template, Image.open(BytesIO(
                render_scoreboard(self.final, self.snapshot, {"1": data, "2": b"invalid-avatar"}))) as image:
            self.assertEqual(image.getpixel((351, 215)), (50, 167, 131))
            self.assertEqual(image.getpixel((286, 150)), template.getpixel((286, 150)))
            self.assertEqual(image.crop(AVATAR_BOXES[1]).tobytes(), template.crop(AVATAR_BOXES[1]).tobytes())
            difference = ImageChops.difference(template.convert("RGB"), image)
            self.assertEqual(difference.crop((270, 134, 432, 296)).getbbox(), (16, 16, 146, 146))
            edge = image.crop((286, 150, 416, 151))
            self.assertTrue(any(pixel != (50, 167, 131) and pixel != template.getpixel((286 + x, 150))
                                for x, pixel in enumerate(edge.getdata())))

    def test_missing_or_wrong_sized_templates_report_image_unavailability(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "scoreboard.png"
            for size in (None, (100, 100)):
                with self.subTest(size=size):
                    if size:
                        with Image.new("RGB", size) as image:
                            image.save(path)
                    with patch("elo_bot.features.finals.rendering.SCOREBOARD_TEMPLATE", path):
                        with self.assertRaises(FinalError):
                            render_scoreboard(self.final, self.snapshot, {})
