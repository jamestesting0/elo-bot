import unittest

from pymongo import DESCENDING

from elobot.db import match_models


class TestDatabaseMatchModels(unittest.TestCase):
    def test_match_model_contracts(self) -> None:
        self.assertEqual(match_models.Match.Settings.name, "matches")
        self.assertEqual(
            match_models.Match.Settings.indexes,
            ["token", "thread", "participants.user_id", [("is_challenge", 1), ("matched_time", 1)]],
        )
        self.assertTrue(match_models.Match.Settings.use_state_management)
        self.assertEqual(match_models.Clan.Settings.name, "clans")
        self.assertEqual(match_models.HelperTask.Settings.name, "helper_tasks")
        self.assertEqual(match_models.HelperTask.Settings.indexes[-1].document["key"], {"timestamp": DESCENDING})


if __name__ == "__main__":
    unittest.main()
