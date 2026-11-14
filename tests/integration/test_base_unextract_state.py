import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
from bson import ObjectId

from elobot.features.bases.fingerprints.jobs import (
    claim_tasks,
    commit_result,
    enroll_batch,
    materialize_outbox,
)
from elobot.features.bases.fingerprints.removal import remove_all_fingerprints, remove_fingerprint
from elobot.features.bases.fingerprints.service import FingerprintService
from elobot.db.fingerprint_types import PIPELINE_ID, utcnow
from elobot.features.bases.fingerprints.types import ExtractionResult
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout
from tests._relative_geometry_fixture import layout, points
from tests.integration import _fingerprint_fixture as fixture


class TestUnextractPersistence(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.FingerprintPersistenceStateMixin.asyncSetUp
    asyncTearDown = fixture.FingerprintPersistenceStateMixin.asyncTearDown

    async def run_record(self) -> BaseExtractionRun:
        run = BaseExtractionRun(requester_id="123", deployment="isolated", batch_size=1,
                                requested_device="cpu", actual_device="cpu", pipeline_id=PIPELINE_ID,
                                cutoff=ObjectId.from_datetime(utcnow() + timedelta(days=1)))
        await run.insert()
        return run

    async def test_removal_fences_inflight_automatic_result_and_new_single_request_can_extract(self) -> None:
        await materialize_outbox()
        task = (await claim_tasks(None, 1, "automatic"))[0]
        await self.base.set({"fingerprint": layout(version=1).model_dump()})
        before = await BaseLayout.get(self.base.id)
        result = await remove_fingerprint(before, anyio.Lock())
        self.assertEqual((result.removed, result.cancelled), (1, 1))
        raw = await BaseLayout.get_pymongo_collection().find_one({"_id": self.base.id})
        self.assertNotIn("fingerprint", raw)
        await commit_result(task, ExtractionResult(fingerprint=layout(version=1)))
        saved = await BaseLayout.get(self.base.id)
        self.assertIsNone(saved.fingerprint)
        self.assertEqual(saved.fingerprint_extraction.error_code, "unextracted")
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "superseded")
        self.assertEqual(saved.records, before.records)
        self.assertEqual((saved.image_revision, saved.image_url, saved.builder_id),
                         (before.image_revision, before.image_url, before.builder_id))
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        state, created = await service.request_extraction(saved)
        self.assertTrue(created)
        self.assertNotEqual(state.request_id, task.id)
        await materialize_outbox()
        fresh = (await claim_tasks(None, 1, "fresh"))[0]
        await commit_result(fresh, ExtractionResult(fingerprint=layout()))
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint.pipeline_id, PIPELINE_ID)

    async def test_existing_run_cannot_enroll_removed_base_but_new_run_can(self) -> None:
        run = await self.run_record()
        await self.base.set({"fingerprint": layout().model_dump()})
        result = await remove_fingerprint(await BaseLayout.get(self.base.id), anyio.Lock())
        self.assertEqual(result.removed, 1)
        await enroll_batch(run)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id}).count(), 0)
        await run.set({"active": False})
        fresh = await self.run_record()
        await enroll_batch(fresh)
        task = (await claim_tasks(fresh.id, 1, "fresh-run"))[0]
        await commit_result(task, ExtractionResult(fingerprint=layout()))
        self.assertIsNotNone((await BaseLayout.get(self.base.id)).fingerprint)

    async def test_bulk_removal_invalidates_enrolled_and_inflight_jobs_and_preserves_base_data(self) -> None:
        run = await self.run_record()
        other = self.base.model_copy(update={"id": ObjectId(), "link": "private", "quality": "Private"})
        await other.insert()
        await enroll_batch(run)
        enrolled = await BaseFingerprintTask.find({"run_id": run.id}).to_list()
        self.assertEqual({task.base_id for task in enrolled}, {self.base.id})
        claimed = await claim_tasks(run.id, 10, "bulk")
        self.assertEqual([task.base_id for task in claimed], [self.base.id])
        task = claimed[0]
        await self.base.set({"fingerprint": layout().model_dump()})
        release = AsyncMock()
        result = await remove_all_fingerprints(anyio.Lock(), release)
        self.assertEqual((result.removed, result.cancelled, result.changed), (1, 2, 0))
        await commit_result(task, ExtractionResult(fingerprint=layout()))
        self.assertEqual(await claim_tasks(run.id, 10, "remaining"), [])
        self.assertEqual(await BaseLayout.count(), 2)
        for base_id in (self.base.id, other.id):
            saved = await BaseLayout.get(base_id)
            self.assertIsNone(saved.fingerprint)
            self.assertEqual(saved.fingerprint_extraction.error_code, f"unextracted:{run.id}")
            self.assertEqual(saved.records, self.base.records)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id, "state": "superseded"}).count(), 1)
        self.assertFalse((await BaseExtractionRun.get(run.id)).active)
        release.assert_awaited_once()
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        fresh, created = await service.begin_run("123", 1)
        self.assertTrue(created)
        self.assertNotEqual(fresh.id, run.id)

    async def test_enrollment_only_includes_missing_or_null_fingerprints_and_excludes_private_bases(self) -> None:
        await self.base.set({"fingerprint": layout().model_dump()})
        first = self.base.model_copy(update={"id": ObjectId(), "link": "missing", "fingerprint": None,
                                            "fingerprint_extraction": None})
        second = first.model_copy(update={"id": ObjectId(), "link": "private", "quality": "Private"})
        third = first.model_copy(update={"id": ObjectId(), "link": "null"})
        await first.insert()
        await second.insert()
        await third.insert()
        await BaseLayout.get_pymongo_collection().update_one({"_id": first.id}, {"$unset": {"fingerprint": ""}})
        run = await self.run_record()
        await enroll_batch(run)
        tasks = await BaseFingerprintTask.find({"run_id": run.id}).to_list()
        self.assertEqual({task.base_id for task in tasks}, {first.id, third.id})
        claimed = await claim_tasks(run.id, 10, "bulk")
        self.assertEqual({task.base_id for task in claimed}, {first.id, third.id})
        self.assertTrue((await BaseExtractionRun.get(run.id)).enrollment_complete)

    async def test_bulk_task_skips_fingerprint_created_after_enrollment(self) -> None:
        run = await self.run_record()
        await enroll_batch(run)
        preserved = layout()
        await self.base.set({"fingerprint": preserved.model_dump()})
        self.assertEqual(await claim_tasks(run.id, 1, "bulk"), [])
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint, preserved)
        task = await BaseFingerprintTask.find_one({"run_id": run.id})
        self.assertEqual(task.state, "superseded")

    async def test_bulk_commit_cannot_overwrite_a_fingerprint_created_after_claim(self) -> None:
        run = await self.run_record()
        await enroll_batch(run)
        task = (await claim_tasks(run.id, 1, "bulk"))[0]
        preserved = layout()
        await self.base.set({"fingerprint": preserved.model_dump()})
        await commit_result(task, ExtractionResult(fingerprint=layout(points(seed=17))))
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint, preserved)
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "superseded")

    async def test_bulk_membership_over_multiple_enrollment_pages(self) -> None:
        collection = BaseLayout.get_pymongo_collection()
        documents = []
        for index in range(203):
            document = self.base.model_dump(by_alias=True)
            document.update(_id=ObjectId(), link=f"page-{index}", fingerprint=None, fingerprint_extraction=None)
            documents.append(document)
        await collection.insert_many(documents)
        run = await self.run_record()
        while not run.enrollment_complete:
            await enroll_batch(run)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id}).count(), 204)
        result = await remove_all_fingerprints(anyio.Lock(), AsyncMock())
        self.assertEqual((result.removed, result.cancelled, result.changed), (0, 1, 0))
        self.assertEqual(await BaseLayout.find({"fingerprint_extraction.error_code": f"unextracted:{run.id}"}).count(), 204)
