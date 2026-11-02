import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId

from common.base_fingerprints import jobs
from common.base_fingerprints.types import ExtractionState, PIPELINE_ID
from database_models import BaseFingerprintTask, BaseLayout


class TestBaseBulkExtraction(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.public = BaseLayout.model_construct(
            id=ObjectId("000000000000000000000001"), district="Barbarian Camp", link="public",
            image_url="https://cdn.example/public.png", quality="Good",
        )
        self.private = self.public.model_copy(update={
            "id": ObjectId("000000000000000000000002"), "link": "private", "quality": "Private",
        })
        self.run = SimpleNamespace(
            id="bulk-run", cutoff=ObjectId("ffffffffffffffffffffffff"), enrollment_cursor=None,
            pipeline_id=PIPELINE_ID, set=AsyncMock(),
        )

    async def test_bulk_enrollment_excludes_private_bases_from_durable_tasks(self):
        inserted = []

        async def insert(task):
            inserted.append(task.base_id)

        def find(query):
            candidates = [self.public]
            if query.get("quality") != {"$ne": "Private"}:
                candidates.append(self.private)
            cursor = MagicMock()
            cursor.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=candidates)
            return cursor

        with patch.object(jobs.BaseLayout, "find", side_effect=find), \
                patch.object(jobs.BaseFingerprintTask, "get_pymongo_collection", return_value=MagicMock()), \
                patch.object(jobs.BaseFingerprintTask, "insert", new=insert):
            await jobs.enroll_batch(self.run)

        self.assertEqual(inserted, [self.public.id])
        self.run.set.assert_any_await({"enrollment_cursor": self.public.id})
        self.run.set.assert_any_await({"enrollment_complete": True, "status": "running"})

    async def test_resumed_bulk_enrollment_keeps_cursor_and_existing_fingerprint_filters(self):
        self.run.enrollment_cursor = self.public.id
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[])
        with patch.object(jobs.BaseLayout, "find", return_value=cursor) as find:
            await jobs.enroll_batch(self.run)
        query = find.call_args.args[0]
        self.assertEqual(query["quality"], {"$ne": "Private"})
        self.assertIsNone(query["fingerprint"])
        self.assertEqual(query["_id"], {"$lt": self.run.cutoff, "$gt": self.public.id})

    async def claim(self, base, run_id):
        state = ExtractionState(source_revision=0, pipeline_id=PIPELINE_ID)
        base.fingerprint_extraction = state
        task = BaseFingerprintTask.model_construct(
            id=state.request_id, base_id=base.id, source_revision=0, source_image_url=base.image_url,
            district=base.district, pipeline_id=PIPELINE_ID, run_id=run_id,
            claim_token="claim", attempts=1,
        )
        collection = MagicMock(find_one_and_update=AsyncMock(side_effect=[task.model_dump(), None]))
        writes = MagicMock(update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1)))
        with patch.object(jobs.BaseFingerprintTask, "get_pymongo_collection", return_value=collection), \
                patch.object(jobs.BaseLayout, "get", new=AsyncMock(return_value=base)), \
                patch.object(jobs.BaseLayout, "get_pymongo_collection", return_value=writes), \
                patch.object(jobs, "finish_task", new=AsyncMock()) as finish:
            claimed = await jobs.claim_tasks(run_id, 1, "worker")
        return claimed, writes, finish

    async def test_previously_enrolled_private_base_is_skipped_when_bulk_run_resumes(self):
        claimed, writes, finish = await self.claim(self.private, self.run.id)
        self.assertEqual(claimed, [])
        writes.update_one.assert_not_awaited()
        self.assertEqual(finish.await_args.args[1], "superseded")
        self.assertEqual(self.private.fingerprint_extraction.status, "pending")

    async def test_bulk_claim_rechecks_non_private_quality_in_atomic_write(self):
        for base, run_id in ((self.public, self.run.id), (self.private, None)):
            with self.subTest(quality=base.quality, run_id=run_id):
                claimed, writes, finish = await self.claim(base, run_id)
                self.assertEqual([task.base_id for task in claimed], [base.id])
                query = writes.update_one.await_args.args[0]
                if run_id:
                    self.assertEqual(query["quality"], {"$ne": "Private"})
                else:
                    self.assertNotIn("quality", query)
                finish.assert_not_awaited()
