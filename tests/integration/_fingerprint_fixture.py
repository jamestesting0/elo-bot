from datetime import timedelta
import os
from uuid import uuid4
from beanie import init_beanie
from bson import ObjectId
from pymongo import AsyncMongoClient
from common.base_fingerprints.jobs import claim_tasks, materialize_outbox
from common.base_fingerprints.types import ExtractionState, utcnow
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout


class FingerprintPersistenceStateMixin:
    async def asyncSetUp(self):
        self.client = AsyncMongoClient(os.environ["INTEGRATION_MONGO_URI"], serverSelectionTimeoutMS=2000, tz_aware=True)
        self.database = self.client[f"fingerprint_test_{uuid4().hex}"]
        await init_beanie(database=self.database, document_models=[BaseLayout, BaseFingerprintTask, BaseExtractionRun])
        self.base = BaseLayout(id=ObjectId.from_datetime(utcnow() - timedelta(days=1)), district="Barbarian Camp",
                               link="base", image_url="https://cdn.example/base.png", quality="Good",
                               expiration_date=1, records=[{"keep": True}],
                               fingerprint_extraction=ExtractionState(source_revision=0))
        await self.base.insert()

    async def asyncTearDown(self):
        await self.client.drop_database(self.database.name)
        await self.client.close()

    async def claim(self):
        await materialize_outbox()
        return (await claim_tasks(None, 1, "test-worker"))[0]
