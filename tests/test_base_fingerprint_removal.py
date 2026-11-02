import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import AutoReconnect

from common.base_fingerprints import removal
from common.base_fingerprints.types import ExtractionState, FingerprintError
from database_models import BaseLayout
from tests._relative_geometry_fixture import layout


class TestFingerprintRemoval(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.base = BaseLayout.model_construct(id=ObjectId(), district="Barbarian Camp", link="base",
                                              image_url="image", quality="Good", builder_id="123",
                                              image_revision=0, fingerprint=layout(),
                                              fingerprint_extraction=ExtractionState(source_revision=0))
        self.lock = anyio.Lock()
        self.release_gpu = AsyncMock()
        self.tasks = MagicMock(update_many=AsyncMock())
        self.enterContext(patch.object(removal.BaseFingerprintTask, "get_pymongo_collection", return_value=self.tasks))
        self.collection = MagicMock()
        self.collection.find_one_and_update = AsyncMock(return_value={
            "fingerprint": self.base.fingerprint.model_dump(),
            "fingerprint_extraction": self.base.fingerprint_extraction.model_dump(),
        })
        self.model = self.enterContext(patch.object(removal, "BaseLayout"))
        self.model.get_pymongo_collection.return_value = self.collection
        self.model.find.return_value.count = AsyncMock(return_value=3)
        self.run = self.enterContext(patch.object(removal.BaseExtractionRun, "find_one", new=AsyncMock(
            return_value=SimpleNamespace(id="active-run", set=AsyncMock()))))

    async def test_atomic_unset_fences_request_without_changing_source_or_metadata(self) -> None:
        previous = self.base.fingerprint_extraction.request_id
        result = await removal.remove_fingerprint(self.base, self.lock)
        self.assertEqual(result, removal.FingerprintRemoval(removed=1, cancelled=1))
        query, update = self.collection.find_one_and_update.await_args.args
        self.assertEqual(query["_id"], self.base.id)
        self.assertEqual(query["quality"], "Good")
        self.assertEqual(query["builder_id"], "123")
        self.assertEqual(query["fingerprint_extraction.request_id"], previous)
        self.assertEqual(update["$unset"], {"fingerprint": ""})
        self.assertEqual(set(update["$set"]), {"fingerprint_extraction"})
        state = ExtractionState.model_validate(update["$set"]["fingerprint_extraction"])
        self.assertNotEqual(state.request_id, previous)
        self.assertEqual(state.source_revision, self.base.image_revision)
        self.assertEqual((state.status, state.error_code), ("failed", "unextracted:active-run"))
        self.assertEqual(self.collection.find_one_and_update.await_args.kwargs["return_document"], ReturnDocument.BEFORE)
        self.assertFalse(self.lock.locked())

    async def test_empty_document_counts_zero_and_changed_request_raises(self) -> None:
        self.collection.find_one_and_update.return_value = {}
        self.run.return_value = None
        self.assertEqual(await removal.remove_fingerprint(self.base, self.lock), removal.FingerprintRemoval())
        self.collection.find_one_and_update.return_value = None
        with self.assertRaisesRegex(FingerprintError, "changed"):
            await removal.remove_fingerprint(self.base, self.lock)

    async def test_aggregate_counts_use_atomic_before_images_and_skip_changed_bases(self) -> None:
        self.model.find_all.return_value.sort.return_value.first_or_none = AsyncMock(return_value=self.base)
        self.model.find.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[self.base, self.base, self.base])
        self.collection.find_one_and_update.side_effect = [
            {"fingerprint": layout().model_dump()}, None,
            {"fingerprint_extraction": ExtractionState(source_revision=0).model_dump()},
        ]
        result = await removal.remove_all_fingerprints(self.lock, self.release_gpu)
        self.assertEqual(result, removal.FingerprintRemoval(removed=1, cancelled=1, changed=1))
        self.assertFalse(self.run.return_value.set.await_args.args[0]["active"])
        self.assertEqual(self.tasks.update_many.await_args.args[0]["run_id"], "active-run")
        self.release_gpu.assert_awaited_once()
        self.assertFalse(self.lock.locked())

    async def test_empty_bank_does_not_write_and_lock_guards_active_run_lookup(self) -> None:
        self.model.find_all.return_value.sort.return_value.first_or_none = AsyncMock(return_value=None)

        async def active(*_args, **_kwargs) -> None:
            self.assertTrue(self.lock.locked())

        self.run.side_effect = active
        report = AsyncMock()
        result = await removal.remove_all_fingerprints(self.lock, self.release_gpu, report)
        self.assertEqual(result, removal.FingerprintRemoval())
        report.assert_awaited_once_with(removal.FingerprintRemovalProgress(0, 0, result))
        self.model.find.assert_not_called()
        self.collection.find_one_and_update.assert_not_awaited()

    async def test_bulk_progress_follows_committed_writes_and_counts_processed_empty_and_changed_bases(self) -> None:
        self.model.find_all.return_value.sort.return_value.first_or_none = AsyncMock(return_value=self.base)
        self.model.find.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[self.base, self.base, self.base])
        self.collection.find_one_and_update.side_effect = [{"fingerprint": layout().model_dump()}, {}, None]
        snapshots = []

        async def report(progress: removal.FingerprintRemovalProgress) -> None:
            self.assertTrue(self.lock.locked())
            self.assertEqual(self.collection.find_one_and_update.await_count, progress.processed)
            snapshots.append(progress)

        result = await removal.remove_all_fingerprints(self.lock, self.release_gpu, report)
        self.assertEqual(snapshots, [
            removal.FingerprintRemovalProgress(0, 3, removal.FingerprintRemoval()),
            removal.FingerprintRemovalProgress(1, 3, removal.FingerprintRemoval(removed=1)),
            removal.FingerprintRemovalProgress(2, 3, removal.FingerprintRemoval(removed=1)),
            removal.FingerprintRemovalProgress(3, 3, removal.FingerprintRemoval(removed=1, changed=1)),
        ])
        self.assertEqual(snapshots[-1].result, result)
        self.model.find.assert_any_call({"_id": {"$lte": self.base.id}})

    async def test_bulk_progress_stops_at_last_confirmed_write_on_database_failure(self) -> None:
        self.model.find_all.return_value.sort.return_value.first_or_none = AsyncMock(return_value=self.base)
        self.model.find.return_value.sort.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[self.base, self.base])
        self.collection.find_one_and_update.side_effect = [{"fingerprint": layout().model_dump()},
                                                         AutoReconnect("write uncertain")]
        report = AsyncMock()
        with self.assertRaises(removal.BulkRemovalError) as caught:
            await removal.remove_all_fingerprints(self.lock, self.release_gpu, report)
        self.assertEqual(report.await_count, 2)
        self.assertEqual(report.await_args.args[0], removal.FingerprintRemovalProgress(
            1, 3, removal.FingerprintRemoval(removed=1)))
        self.assertEqual(report.await_args.args[0].result, caught.exception.progress)
        self.assertEqual(caught.exception.progress, removal.FingerprintRemoval(removed=1))
        self.assertFalse(self.lock.locked())
