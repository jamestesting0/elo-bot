import unittest

from elo_bot.features.finals.inventory import inventory


class TestFinalInventory(unittest.TestCase):
    def test_legacy_season_is_never_guessed_from_latest_plus_one(self):
        report = inventory([{"_id": "legacy", "season_number": 8, "player_nicknames": {"1": "One"}}],
                           [{"_id": "archive", "season_number": 7, "end": 2}])
        self.assertEqual(report["proposals"], [])
        self.assertEqual(len(report["blocked"]), 1)

    def test_explicit_mapping_proposes_captured_archive_without_writes(self):
        record = {"_id": "legacy", "season_number": 8, "player_nicknames": {"1": "One"}}
        report = inventory([record], [{"_id": "archive", "season_number": 7, "end": 2}], {"legacy": "archive"})
        self.assertEqual(report["proposals"][0]["set"]["season_number"], 7)
        self.assertEqual(record["season_number"], 8)
        self.assertFalse(report["proposals"][0]["set"]["active"])

    def test_duplicate_and_legacy_score_conflicts_block_activation(self):
        records = [{"_id": "a", "season_number": 8, "quarter_finals": [{"id": "QF1"}]},
                   {"_id": "b", "season_number": 8}]
        report = inventory(records, [{"_id": "s", "season_number": 7, "end": 2}], {"a": "s", "b": "s"})
        self.assertEqual(report["duplicates"], [8])
        self.assertEqual(report["legacy_score_records"], ["a"])
        self.assertTrue(any(row["id"] == "a" for row in report["blocked"]))
