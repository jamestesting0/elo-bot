import unittest

from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import Match
from elo_bot.db.match_models import BaseLink, Participant
from elo_bot.features.hosting.live.helper_live import parse_paired_live_bases, unpaired_bases_message
from elo_bot.features.matches.attack_bases import missing_bases_message


class TestHelperLiveBases(unittest.TestCase):
    def match(self, missing: dict[int, set[str]]) -> Match:
        participants = [Participant.model_construct(user_id=str(side), bases=[
            None if district in missing.get(side, set()) else BaseLink(link=f"https://link/{side}/{index}", image="")
            for index, district in enumerate(FINAL_DISTRICTS)]) for side in (0, 1)]
        return Match.model_construct(final_districts=list(FINAL_DISTRICTS), participants=participants)

    def test_refusal_names_each_district_either_player_lacks(self):
        match = self.match({0: {"Balloon Lagoon"}, 1: {"Goblin Mines", "Balloon Lagoon"}})
        self.assertIsNone(parse_paired_live_bases(match))
        named = {district for district in FINAL_DISTRICTS if f"**{district}**" in unpaired_bases_message(match)}
        self.assertEqual(named, {"Balloon Lagoon", "Goblin Mines"})

    def test_war_hosting_checks_only_the_bases_the_hosted_player_attacks(self):
        match = self.match({0: {"Balloon Lagoon"}, 1: {"Goblin Mines"}})
        named = {district for district in FINAL_DISTRICTS
                 if f"**{district}**" in missing_bases_message(match, (1,))}
        self.assertEqual(named, {"Goblin Mines"})
        self.assertIsNone(missing_bases_message(self.match({}), (0,)))

    def test_complete_pairs_are_paired_for_the_opposite_player(self):
        paired = parse_paired_live_bases(self.match({}))
        self.assertEqual(paired.for_player1[0], "https://link/1/0")
        self.assertEqual(paired.for_player2[0], "https://link/0/0")
