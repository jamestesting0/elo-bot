import unittest

from elo_bot.features.base_store.models import numeric_base_store_slot


class TestBaseStoreModels(unittest.TestCase):
    def test_numeric_slot_maps_only_persisted_slots(self) -> None:
        self.assertEqual(numeric_base_store_slot("Slot 1"), 1)
        self.assertEqual(numeric_base_store_slot("Slot 2"), 2)
        self.assertIsNone(numeric_base_store_slot("Slot 3"))



if __name__ == "__main__":
    unittest.main()
