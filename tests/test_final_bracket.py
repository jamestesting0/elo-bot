import unittest

from common.finals.bracket import advance_nodes, automatic_pairs, build_bracket, require_round_ready
from common.finals.types import FinalError
from elobot.db import EloFinal


class TestFinalBracket(unittest.TestCase):
    def edition(self, count):
        return EloFinal.model_construct(season_number=1, active=True, entrant_ids=[str(i) for i in range(count)],
                                        archived_elo={str(i): 1000 - i for i in range(count)})

    def test_five_to_eight_entrants_have_exactly_the_required_byes_and_graph(self):
        for count in range(5, 9):
            with self.subTest(count=count):
                final = self.edition(count)
                nodes = build_bracket(final, automatic_pairs(final))
                self.assertEqual(set(nodes), {"QF1", "QF2", "QF3", "QF4", "SF1", "SF2", "F1"})
                self.assertEqual(sum(node.bye for node in nodes.values()), 8 - count)
                self.assertEqual(nodes["SF1"].feeders, ["QF1", "QF2"])
                self.assertEqual(nodes["F1"].feeders, ["SF1", "SF2"])

    def test_explicit_lower_seed_bye_is_preserved(self):
        final = self.edition(5)
        final.byes = ["4"]
        nodes = build_bracket(final, automatic_pairs(final))
        self.assertTrue(any(node.bye and node.winner == "4" for node in nodes.values()))

    def test_manual_arrangement_is_exact_and_duplicate_players_are_rejected(self):
        final = self.edition(8)
        pairs = {"QF1": ["7", "2"], "QF2": ["4", "1"], "QF3": ["0", "6"], "QF4": ["5", "3"]}
        nodes = build_bracket(final, pairs)
        self.assertEqual(nodes["QF1"].contestants, ["7", "2"])
        pairs["QF4"] = ["5", "7"]
        with self.assertRaises(FinalError):
            build_bracket(final, pairs)

    def test_early_semifinal_pair_does_not_authorize_round_start(self):
        final = self.edition(5)
        final.nodes = build_bracket(final, automatic_pairs(final))
        with self.assertRaises(FinalError):
            require_round_ready(final, "Semifinals")

    def test_changed_winner_invalidates_only_its_played_descendants(self):
        final = self.edition(8)
        nodes = build_bracket(final, automatic_pairs(final))
        for node_id in ("QF1", "QF2", "QF3", "QF4"):
            nodes[node_id].winner = nodes[node_id].contestants[0]
        advance_nodes(nodes)
        nodes["SF1"].winner = nodes["SF1"].contestants[0]
        nodes["SF1"].result_id = "old-sf1"
        nodes["SF2"].winner = nodes["SF2"].contestants[0]
        nodes["SF2"].result_id = "keep-sf2"
        advance_nodes(nodes)
        nodes["F1"].winner = nodes["F1"].contestants[0]
        nodes["F1"].result_id = "old-final"
        nodes["QF1"].winner = nodes["QF1"].contestants[1]
        changed = advance_nodes(nodes)
        self.assertEqual(changed, ["SF1", "F1"])
        self.assertEqual(nodes["SF1"].state, "NEEDS_MANAGER")
        self.assertIsNone(nodes["F1"].winner)
        self.assertEqual(nodes["SF2"].result_id, "keep-sf2")
