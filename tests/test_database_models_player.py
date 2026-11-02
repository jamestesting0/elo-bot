import unittest

from database_models import player_models


class TestDatabasePlayerModels(unittest.TestCase):
    def test_player_model_contracts(self) -> None:
        self.assertEqual(player_models.BaseLayout.Settings.name, "base_layouts")
        self.assertEqual(player_models.RecordedMatch.Settings.name, "recorded_matches")
        self.assertEqual(player_models.RecordedMatch.Settings.indexes, ["id", "time"])
        self.assertEqual(player_models.Player.Settings.name, "players")
        self.assertEqual(player_models.Queue.Settings.name, "queue")
        self.assertTrue(player_models.Queue.Settings.use_state_management)
        self.assertEqual(set(player_models.QueueDistrictSelection.model_fields), {"attempt_id", "districts", "confirmed"})


if __name__ == "__main__":
    unittest.main()
