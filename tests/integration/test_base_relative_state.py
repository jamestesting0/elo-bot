from datetime import timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from bson import ObjectId

from common.base_fingerprints.jobs import claim_tasks, commit_result, materialize_outbox
from common.base_fingerprints.service import FingerprintService
from common.base_fingerprints.types import ExtractionResult, ExtractionState, PIPELINE_ID, PREVIOUS_PIPELINE_ID, utcnow
from database_models import BaseExtractionRun, BaseFingerprintTask, BaseLayout
from tests._relative_geometry_fixture import layout
from tests.integration import _fingerprint_fixture as fixture


class TestRelativePersistence(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.FingerprintPersistenceStateMixin.asyncSetUp
    asyncTearDown = fixture.FingerprintPersistenceStateMixin.asyncTearDown
    claim = fixture.FingerprintPersistenceStateMixin.claim

    async def test_previous_relative_job_finishes_then_current_job_preserves_its_fingerprint(self) -> None:
        state = ExtractionState(source_revision=0, pipeline_id=PREVIOUS_PIPELINE_ID)
        await self.base.set({"fingerprint_extraction": state.model_dump()})
        previous = await self.claim()
        self.assertEqual(previous.pipeline_id, PREVIOUS_PIPELINE_ID)
        fingerprint = layout().model_copy(update={"pipeline_id": PREVIOUS_PIPELINE_ID, "extractor_revision": "elo-2"})
        await commit_result(previous, ExtractionResult(fingerprint=fingerprint))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint.pipeline_id, PREVIOUS_PIPELINE_ID)
        self.assertEqual(saved.fingerprint_extraction.status, "ready")
        self.assertEqual((await BaseFingerprintTask.get(previous.id)).state, "succeeded")
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        current, created = await service.request_extraction(saved)
        self.assertTrue(created)
        self.assertEqual(current.pipeline_id, PIPELINE_ID)
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint.pipeline_id, PREVIOUS_PIPELINE_ID)
        self.assertEqual(saved.fingerprint_extraction.status, "pending")
        await commit_result(previous, ExtractionResult(error="stale previous failure"))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint.pipeline_id, PREVIOUS_PIPELINE_ID)
        self.assertEqual(saved.fingerprint_extraction.request_id, current.request_id)
        task = await self.claim()
        self.assertEqual(task.pipeline_id, PIPELINE_ID)
        await commit_result(task, ExtractionResult(fingerprint=layout()))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint.pipeline_id, PIPELINE_ID)
        self.assertEqual(saved.fingerprint_extraction.status, "ready")
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "succeeded")

    async def test_legacy_outbox_and_stale_completion_after_new_request(self):
        await BaseLayout.get_pymongo_collection().update_one({"_id": self.base.id},
            {"$unset": {"fingerprint_extraction.pipeline_id": ""}})
        old = await self.claim()
        self.assertEqual(old.pipeline_id, "capital-v1-4d63ddd9")
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        state, created = await service.request_extraction(await BaseLayout.get(self.base.id))
        self.assertTrue(created)
        self.assertEqual(state.pipeline_id, PIPELINE_ID)
        await commit_result(old, ExtractionResult(fingerprint=layout(version=1)))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint_extraction.request_id, state.request_id)
        self.assertIsNone(saved.fingerprint)
        self.assertEqual((await BaseFingerprintTask.get(old.id)).state, "superseded")
        current = await self.claim()
        await commit_result(current, ExtractionResult(fingerprint=layout()))
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint.schema_version, 2)
        self.assertEqual((await BaseFingerprintTask.get(current.id)).state, "succeeded")

    async def test_legacy_bulk_run_enrollment_and_tick_finish_in_original_pipeline(self):
        await self.base.set({"fingerprint_extraction": None})
        run = BaseExtractionRun(requester_id="123", deployment="isolated", batch_size=1,
                                requested_device="cpu", actual_device="cpu", cutoff=ObjectId())
        await run.insert()
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        service.publish = AsyncMock()

        async def process(tasks, worker):
            for task in tasks:
                self.assertEqual(task.pipeline_id, "capital-v1-4d63ddd9")
                await commit_result(task, ExtractionResult(fingerprint=layout(version=1)))

        service.process = process
        await service.tick()
        saved = await BaseExtractionRun.get(run.id)
        self.assertFalse(saved.active)
        self.assertEqual(saved.status, "completed")
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint.schema_version, 1)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id}).count(), 1)

    async def test_unknown_pipeline_remains_visibly_blocked_without_changing_identity(self):
        await self.base.set({"fingerprint_extraction": None})
        run = BaseExtractionRun(requester_id="123", deployment="isolated", batch_size=1,
                                requested_device="cpu", actual_device="cpu", cutoff=ObjectId(), pipeline_id="future")
        await run.insert()
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.publish = AsyncMock()
        service.process = AsyncMock()
        await service.tick()
        saved = await BaseExtractionRun.get(run.id)
        self.assertTrue(saved.active)
        self.assertEqual(saved.pipeline_id, "future")
        self.assertIn("waiting", saved.status)
        self.assertFalse(saved.enrollment_complete)
        self.assertEqual(await BaseFingerprintTask.count(), 0)
        service.process.assert_not_awaited()

    async def test_unsupported_automatic_task_stays_pending_and_reports_reason(self):
        state = ExtractionState(source_revision=0, pipeline_id="future")
        await self.base.set({"fingerprint_extraction": state.model_dump()})
        await materialize_outbox()
        self.assertEqual(await claim_tasks(None, 1, "local"), [])
        task = await BaseFingerprintTask.get(state.request_id)
        self.assertEqual(task.state, "pending")
        self.assertEqual(task.error_code, "unsupported_pipeline")
        self.assertEqual(task.pipeline_id, "future")
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint_extraction.request_id, state.request_id)

    async def test_v2_expired_claim_cannot_overwrite_reclaimed_success(self):
        await self.base.set({"fingerprint_extraction": ExtractionState(source_revision=0,
                                                                      pipeline_id=PIPELINE_ID).model_dump()})
        old = await self.claim()
        await BaseFingerprintTask.get_pymongo_collection().update_one({"_id": old.id}, {
            "$set": {"lease_expires_at": utcnow() - timedelta(seconds=1)}})
        current = (await claim_tasks(None, 1, "new-worker"))[0]
        await commit_result(current, ExtractionResult(fingerprint=layout()))
        await commit_result(old, ExtractionResult(error="old_failure"))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint_extraction.status, "ready")
        self.assertEqual(saved.fingerprint_extraction.completion_receipt, current.claim_token)
        self.assertEqual((await BaseFingerprintTask.get(old.id)).state, "succeeded")

    async def test_wrong_pipeline_result_cannot_create_success_receipt(self):
        old = await self.claim()
        with self.assertRaises(ValueError):
            await commit_result(old, ExtractionResult(fingerprint=layout()))
        saved = await BaseLayout.get(self.base.id)
        self.assertIsNone(saved.fingerprint)
        self.assertEqual(saved.fingerprint_extraction.status, "running")
        self.assertIsNone(saved.fingerprint_extraction.completion_receipt)
