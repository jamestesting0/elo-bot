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


NAME_PLATES = ((453, 336, 739, 390), (933, 336, 1219, 390))
ROW_Y = (472, 535, 596, 659, 721, 783, 846)
STAR_RIGHTS = (441, 1005)
TOTAL_STAR_RIGHTS = (451.5, 1015.5)
STAR_PLATES = tuple((right - 80, y - 22, right + 3, y + 22) for right in STAR_RIGHTS for y in ROW_Y)
DESTRUCTION_PLATES = tuple((center - 80, y - 22, center + 81, y + 22)
                           for center in (655, 1219) for y in ROW_Y)
TOTAL_STAR_PLATES = ((371, 892, 455, 948), (935, 892, 1020, 948))
TOTAL_DESTRUCTION_PLATES = ((565, 892, 746, 948), (1129, 892, 1310, 948))
AVATAR_BOXES = ((359, 325, 431, 397), (1241, 325, 1313, 397))


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
                    self.assertEqual(rendered.size, (1672, 1033))
                    difference = ImageChops.difference(template.convert("RGB"), rendered).convert("L")
                    remaining = difference.copy()
                    for plate in (*NAME_PLATES, *DESTRUCTION_PLATES, *TOTAL_DESTRUCTION_PLATES):
                        with self.subTest(plate=plate):
                            bounds = difference.crop(plate).getbbox()
                            self.assertIsNotNone(bounds)
                            left, top, right, bottom = bounds
                            self.assertLessEqual(abs((left + right) / 2 - (plate[2] - plate[0]) / 2), 1)
                            self.assertLessEqual(abs((top + bottom) / 2 - (plate[3] - plate[1]) / 2), 1)
                            remaining.paste(0, plate)
                    for plate in (*STAR_PLATES, *TOTAL_STAR_PLATES):
                        with self.subTest(plate=plate):
                            bounds = difference.crop(plate).getbbox()
                            self.assertIsNotNone(bounds)
                            _left, top, _right, bottom = bounds
                            self.assertLessEqual(abs((top + bottom) / 2 - (plate[3] - plate[1]) / 2), 1)
                            remaining.paste(0, plate)
                    self.assertIsNone(remaining.getbbox(), "Ink must stay inside the measured plate interiors")

    def test_each_district_and_total_uses_saved_scores_in_the_correct_side_and_row(self):
        drawn = []
        draw_text = ImageDraw.ImageDraw.text

        def record(draw, position, text, *args, **kwargs):
            drawn.append((draw.textbbox(position, text, font=kwargs["font"], stroke_width=1,
                                        anchor=kwargs.get("anchor")), text))
            return draw_text(draw, position, text, *args, **kwargs)

        with patch.object(ImageDraw.ImageDraw, "text", record):
            render_scoreboard(self.final, self.snapshot, {})
        expected = (("2", "3", "2", "3", "3", "2", "3"),
                    ("3", "3", "3", "3", "3", "3", "3"))
        for side, values in enumerate(expected):
            stars = sorted((bounds, text) for bounds, text in drawn
                           if text.isdecimal() and abs(bounds[2] - STAR_RIGHTS[side]) <= 1)
            self.assertEqual([text for _bounds, text in stars], list(values))
            for (left, top, right, bottom), _text in stars[:-1]:
                self.assertTrue(any(y - 22 <= top and bottom <= y + 22 for y in ROW_Y))
            total = next((bounds for bounds, text in drawn if text == str((18, 21)[side])
                          and abs(bounds[2] - TOTAL_STAR_RIGHTS[side]) <= 1), None)
            self.assertIsNotNone(total)

    def test_winner_and_loser_colors_apply_to_names_scores_and_totals(self):
        for winner in ("1", "2"):
            self.snapshot.winner = winner
            with self.subTest(winner=winner), Image.open(BytesIO(
                    render_scoreboard(self.final, self.snapshot, {}))) as image:
                for side, user in enumerate(("1", "2")):
                    muted = (226, 207, 173)
                    normal = (255, 240, 213)
                    score_plates = (*STAR_PLATES[side * 7:side * 7 + 7],
                                    *DESTRUCTION_PLATES[side * 7:side * 7 + 7])
                    total_plates = (TOTAL_STAR_PLATES[side], TOTAL_DESTRUCTION_PLATES[side])
                    plates = (NAME_PLATES[side], *score_plates, *total_plates)
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
            self.assertEqual(image.getpixel((395, 361)), (50, 167, 131))
            self.assertEqual(image.getpixel((359, 325)), template.getpixel((359, 325)))
            self.assertEqual(image.crop(AVATAR_BOXES[1]).tobytes(), template.crop(AVATAR_BOXES[1]).tobytes())
            difference = ImageChops.difference(template.convert("RGB"), image)
            self.assertEqual(difference.crop((343, 309, 447, 413)).getbbox(), (16, 16, 88, 88))
            edge = image.crop((359, 325, 431, 326))
            self.assertTrue(any(pixel != (50, 167, 131) and pixel != template.getpixel((359 + x, 325))
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
