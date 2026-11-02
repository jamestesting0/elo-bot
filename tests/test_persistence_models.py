import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError
from pymongo import ReturnDocument

from database_models import AuditLogEntry, Giveaway


class TestAuditLogEntryContract(unittest.TestCase):
    def test_rejects_non_numeric_author_id_and_negative_recorded_time(self):
        # Given: malformed semantic IDs and an impossible audit timestamp
        invalid_entries = (
            {"recorded_at_ns": 1, "command": "/audit", "author_id": "not-a-discord-id"},
            {"recorded_at_ns": -1, "command": "/audit", "author_id": "42"},
        )

        # When: each value crosses the audit-log persistence boundary
        # Then: Pydantic rejects the invalid document
        collection = MagicMock()
        with patch.object(AuditLogEntry, "get_pymongo_collection", return_value=collection):
            for values in invalid_entries:
                with self.subTest(values=values), self.assertRaises(ValidationError):
                    AuditLogEntry(**values)

class TestGiveawayContract(unittest.TestCase):
    def test_rejects_invalid_discord_ids_and_numeric_boundaries(self):
        # Given: malformed giveaway IDs plus negative and zero-only persisted boundaries
        base_fields = {
            "id": "100",
            "end_timestamp": 1.5,
            "sponsor_id": "200",
            "channel_id": 300,
        }
        invalid_giveaways = (
            {**base_fields, "id": "not-a-discord-id"},
            {**base_fields, "sponsor_id": "not-a-discord-id"},
            {**base_fields, "entries": ["not-a-discord-id"]},
            {**base_fields, "end_timestamp": -0.1},
            {**base_fields, "min_elo": -1},
            {**base_fields, "min_matches": -1},
            {**base_fields, "channel_id": 0},
        )

        # When: each invalid value crosses the giveaway persistence boundary
        # Then: Pydantic rejects the document before it can be persisted
        collection = MagicMock()
        with patch.object(Giveaway, "get_pymongo_collection", return_value=collection):
            for fields in invalid_giveaways:
                with self.subTest(fields=fields), self.assertRaises(ValidationError):
                    Giveaway(**fields)


class TestGiveawayAddEntry(unittest.IsolatedAsyncioTestCase):
    async def test_add_entry_uses_one_atomic_update_and_parses_the_result(self):
        # Given: Mongo atomically accepts a previously absent entrant
        raw_giveaway = {
            "_id": "100",
            "end_timestamp": 1.5,
            "ended": False,
            "entries": ["400"],
            "sponsor_id": "200",
            "min_elo": 0,
            "min_matches": 0,
            "channel_id": 300,
        }
        collection = MagicMock()
        collection.find_one_and_update = AsyncMock(return_value=raw_giveaway)

        # When: that entrant is added
        with (
            patch.object(Giveaway, "get_pymongo_collection", return_value=collection),
            patch("database_models.giveaway_models.time", return_value=1.0),
        ):
            result = await Giveaway.add_entry("100", "400")

        # Then: the exact raw atomic operation is used and its document becomes a Giveaway
        collection.find_one_and_update.assert_awaited_once_with(
            {"_id": "100", "entries": {"$ne": "400"}, "ended": {"$ne": True},
             "end_timestamp": {"$gt": 1.0}},
            {"$addToSet": {"entries": "400"}},
            return_document=ReturnDocument.AFTER,
        )
        collection.find_one.assert_not_called()
        self.assertIsInstance(result, Giveaway)
        self.assertEqual(result.id, "100")
        self.assertEqual(result.entries, ["400"])

    async def test_add_entry_returns_none_when_mongo_rejects_missing_or_duplicate_entry(self):
        # Given: Mongo finds no row matching the atomic entry filter
        collection = MagicMock()
        collection.find_one_and_update = AsyncMock(return_value=None)

        # When: adding a missing or duplicate entrant is attempted
        with patch.object(Giveaway, "get_pymongo_collection", return_value=collection):
            result = await Giveaway.add_entry("100", "400")

        # Then: no document is returned and no preliminary read is performed
        self.assertIsNone(result)
        collection.find_one_and_update.assert_awaited_once()
        collection.find_one.assert_not_called()


if __name__ == "__main__":
    unittest.main()
