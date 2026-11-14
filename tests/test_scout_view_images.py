import unittest
from types import SimpleNamespace

from elo_bot.config.presentation import LEGACY_UNAVAILABLE_IMAGE, UNAVAILABLE_IMAGE
from elo_bot.features.matches.attack_bases import is_missing_scout_view

LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


def match_with_image(image: str | None) -> SimpleNamespace:
    participant = SimpleNamespace(user_id="111", bases=[SimpleNamespace(link=LINK, image=image)])
    return SimpleNamespace(participants=[participant], final_districts=["Barbarian Camp"])


class TestScoutViewImages(unittest.TestCase):
    def test_placeholder_images_count_as_missing_scout_view(self) -> None:
        cases = {"none": None, "current placeholder": UNAVAILABLE_IMAGE, "placeholder stored before assets": LEGACY_UNAVAILABLE_IMAGE}
        for label, image in cases.items():
            with self.subTest(label):
                self.assertEqual(is_missing_scout_view(match_with_image(image), 111), ("Barbarian Camp", LINK))

    def test_real_screenshot_is_not_missing(self) -> None:
        match = match_with_image("https://cdn.discordapp.com/attachments/1/2/base.png")
        self.assertEqual(is_missing_scout_view(match, 111), (None, None))
