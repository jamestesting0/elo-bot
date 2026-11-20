import os
import unittest
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit
from uuid import uuid4
from unittest.mock import patch

from beanie import init_beanie
from pymongo import AsyncMongoClient

from elo_bot.app.backups import backup_database1, backup_database2, sync_database
from elo_bot.config.settings import DATA_BASES
from elo_bot.db.timestamp_models import TIMESTAMP_POST_COLLECTION, TIMESTAMP_POST_INDEXES, TimestampPost


class TestTimestampRetention(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        uri = os.environ["INTEGRATION_MONGO_URI"]
        self.assertIn(urlsplit(uri).hostname, {"localhost", "127.0.0.1", "::1"})
        self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=3000)
        self.addAsyncCleanup(self.client.close)
        self.source = self.new_database()
        await init_beanie(database=self.source, document_models=[TimestampPost], skip_indexes=True)
        await TimestampPost.get_pymongo_collection().create_indexes(TIMESTAMP_POST_INDEXES)
        self.expiry = datetime.now(UTC).replace(microsecond=0) + timedelta(days=7)
        await TimestampPost(id=123, user_id=456, expires_at=self.expiry).insert()

    def new_database(self):
        database = self.client[f"elo_timestamp_retention_it_{uuid4().hex}"]
        self.addAsyncCleanup(self.client.drop_database, database.name)
        return database

    async def test_attribution_lookup_expires_even_before_mongodb_removes_the_row(self):
        with patch("elo_bot.db.timestamp_models.datetime", wraps=datetime) as clock:
            clock.now.return_value = self.expiry - timedelta(milliseconds=1)
            self.assertEqual(await TimestampPost.owner_for_message(123), 456)
            clock.now.return_value = self.expiry
            self.assertIsNone(await TimestampPost.owner_for_message(123))
        self.assertIsNotNone(await TimestampPost.get(123))
        indices = await self.source[TIMESTAMP_POST_COLLECTION].index_information()
        self.assertEqual(indices["timestamp_post_expiry"]["key"], [("expires_at", 1)])
        self.assertEqual(indices["timestamp_post_expiry"]["expireAfterSeconds"], 0)

    async def test_scheduled_backups_and_beta_sync_copy_ordinary_data_without_attribution(self):
        await self.source["ordinary"].insert_one({"_id": 7, "value": "kept"})
        first, second, beta = (self.new_database() for _ in range(3))
        database_routes = {DATA_BASES["main"]: self.source, "elo_backup1": first, "elo_backup2": second}
        with patch("elo_bot.app.backups.runtime.VERSION", "main", create=True):
            await backup_database1.callback(database_routes)
            await backup_database2.callback(database_routes)
        await sync_database(self.source, beta)

        for destination in (first, second, beta):
            with self.subTest(destination=destination.name):
                self.assertNotIn(TIMESTAMP_POST_COLLECTION, await destination.list_collection_names())
                self.assertEqual(await destination["ordinary"].find_one({"_id": 7}), {"_id": 7, "value": "kept"})
