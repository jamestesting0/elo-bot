import io
import os
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from beanie import init_beanie
from bson import ObjectId
from PIL import Image
from pymongo import AsyncMongoClient

from elobot.features.bases.fingerprints import images, service
from elobot.features.bases.fingerprints.ingestion import replace_source
from elobot.features.bases.fingerprints.jobs import claim_tasks, enroll_batch, materialize_outbox, run_counts
from elobot.features.bases.fingerprints.search import eligible
from elobot.features.bases.fingerprints.types import ExtractionResult
from elobot.db.fingerprint_types import ExtractionState, utcnow
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout
from tests._base_fingerprint_fixture import fingerprint
from tests._relative_geometry_fixture import layout
from tests._base_image_fixture import FRESH_URL, OLD_URL, image_response


class TestFingerprintDownloadPersistence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = AsyncMongoClient(os.environ["INTEGRATION_MONGO_URI"], serverSelectionTimeoutMS=2000, tz_aware=True)
        self.database = self.client[f"fingerprint_test_{uuid4().hex}"]
        await init_beanie(database=self.database, document_models=[BaseLayout, BaseFingerprintTask, BaseExtractionRun])
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(service, "SCRATCH", self.directory))
        self.base = BaseLayout(id=ObjectId.from_datetime(utcnow() - timedelta(days=1)), district="Barbarian Camp",
                              link="base", image_url=OLD_URL, quality="Good", expiration_date=0, fingerprint=fingerprint(),
                              fingerprint_extraction=ExtractionState(source_revision=0))
        await self.base.insert()
        self.base = await BaseLayout.get(self.base.id)
        buffer = io.BytesIO()
        Image.new("RGB", (64, 64)).save(buffer, format="PNG")
        self.png = buffer.getvalue()
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.get.side_effect = lambda url, **kwargs: image_response(200 if url == FRESH_URL else 404, self.png)
        self.enterContext(patch.object(images.aiohttp, "ClientSession", return_value=self.session))
        self.enterContext(patch.object(images.aiohttp, "TCPConnector"))
        self.bot = SimpleNamespace(http=SimpleNamespace(request=AsyncMock(return_value={
            "refreshed_urls": [{"original": OLD_URL, "refreshed": FRESH_URL}],
        })))
        self.service = service.FingerprintService(self.bot, "test")
        self.service.cpu.start = AsyncMock()
        self.service.gpu.start = AsyncMock()

    async def asyncTearDown(self):
        await self.client.drop_database(self.database.name)
        await self.client.close()

    async def extract(self, inputs):
        for source in inputs:
            self.assertEqual(Path(source.path).read_bytes(), self.png)
            self.assertEqual(source.source_image_url, FRESH_URL)
        return [ExtractionResult(fingerprint=layout(version=1 if source.pipeline_id == "capital-v1-4d63ddd9"
                                                    else 2).model_copy(update={
            "source_image_url": source.source_image_url, "source_revision": source.source_revision}))
                for source in inputs]

    async def test_single_then_bulk_downloads_skip_saved_fingerprints_and_preserve_failed_single(self):
        await materialize_outbox()
        tasks = await claim_tasks(None, 1, "single")
        self.service.cpu.extract = AsyncMock(side_effect=self.extract)
        await self.service.process(tasks, self.service.cpu)
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint_extraction.status, "ready")
        self.assertEqual(saved.fingerprint.source_image_url, FRESH_URL)
        self.assertEqual(saved.image_url, OLD_URL)
        self.assertEqual(saved.image_revision, 0)
        self.assertEqual((await BaseFingerprintTask.get(tasks[0].id)).state, "succeeded")

        failed = self.base.model_copy(update={"id": ObjectId.from_datetime(utcnow() - timedelta(days=2)),
            "image_url": OLD_URL.replace("/200/", "/201/"), "link": "unavailable"})
        await failed.insert()
        await self.service.request_extraction(failed)
        await materialize_outbox()
        failed_tasks = await claim_tasks(None, 1, "failed-single")
        await self.service.process(failed_tasks, self.service.cpu)
        saved = await BaseLayout.get(failed.id)
        self.assertEqual(saved.fingerprint, failed.fingerprint)
        self.assertEqual(saved.fingerprint_extraction.error_code, "source_unavailable")

        missing = self.base.model_copy(update={"id": ObjectId.from_datetime(utcnow() - timedelta(days=3)),
            "link": "missing", "fingerprint": None, "fingerprint_extraction": None})
        unavailable = failed.model_copy(update={"id": ObjectId.from_datetime(utcnow() - timedelta(days=4)),
            "link": "missing-unavailable", "fingerprint": None, "fingerprint_extraction": None})
        await missing.insert()
        await unavailable.insert()
        run, _ = await self.service.begin_run("123", 8)
        await enroll_batch(run)
        tasks = await claim_tasks(run.id, run.batch_size, "bulk")
        self.assertEqual(len(tasks), 2)
        self.assertEqual({task.base_id for task in tasks}, {missing.id, unavailable.id})
        self.assertEqual(run.actual_device, "cuda:0")
        self.service.gpu.extract = AsyncMock(side_effect=self.extract)
        await self.service.process(tasks, self.service.gpu)
        self.service.gpu.extract.assert_awaited_once()
        self.assertEqual(len(self.service.gpu.extract.await_args.args[0]), 1)
        self.assertEqual(self.service.gpu.device, "cuda:0")
        self.assertEqual(await run_counts(run.id), {"succeeded": 1, "failed": 1})
        status = await self.service.status(run)
        self.assertIn("succeeded: 1, failed: 1", status)
        saved = await BaseLayout.get(failed.id)
        self.assertEqual(saved.fingerprint, failed.fingerprint)
        self.assertEqual(saved.fingerprint_extraction.error_code, "source_unavailable")
        self.assertIsNotNone((await BaseLayout.get(missing.id)).fingerprint)
        self.assertIsNone((await BaseLayout.get(unavailable.id)).fingerprint)
        self.assertEqual(list((self.directory / "test").iterdir()), [])

    async def test_image_replacement_during_refresh_fences_stale_completion(self):
        await materialize_outbox()
        task = (await claim_tasks(None, 1, "single"))[0]

        async def refresh(*_args, **_kwargs):
            await replace_source(await BaseLayout.get(self.base.id), "https://example.com/new.png", "Barbarian Camp")
            return {"refreshed_urls": [{"original": OLD_URL, "refreshed": FRESH_URL}]}

        self.bot.http.request.side_effect = refresh
        self.service.cpu.extract = AsyncMock(side_effect=self.extract)
        await self.service.process([task], self.service.cpu)
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.image_revision, 1)
        self.assertEqual(saved.fingerprint, self.base.fingerprint)
        self.assertFalse(eligible(saved))
        self.assertEqual(saved.fingerprint_extraction.status, "pending")
        self.assertNotEqual(saved.fingerprint_extraction.request_id, task.id)
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "superseded")
        self.assertEqual(list((self.directory / "test").iterdir()), [])
