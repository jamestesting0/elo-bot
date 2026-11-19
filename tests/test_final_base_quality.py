import unittest

from elo_bot.config.game import BASE_TIERS
from elo_bot.db import Match, Player
from elo_bot.features.matches.base_selection import get_base_quality


class TestFinalBaseQuality(unittest.TestCase):
    def test_final_matches_draw_only_excellent_bases_whatever_the_players_elo(self):
        lowest = min(lower for lower, _ in BASE_TIERS.values())
        players = [Player.model_construct(elo=lowest), Player.model_construct(elo=lowest)]
        ranked = Match.model_construct(format=None, primary_base_quality=None, secondary_base_quality=None)
        final = Match.model_construct(format="elo_final_seven", primary_base_quality="Average",
                                      secondary_base_quality="Average")
        self.assertNotEqual(get_base_quality(*players, ranked), ("Excellent", "Excellent"))
        self.assertEqual(get_base_quality(*players, final), ("Excellent", "Excellent"))
