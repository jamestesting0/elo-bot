import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import interactions as ipy
from bson import ObjectId
from pymongo.errors import AutoReconnect

from elobot.features.bases.fingerprints.ingestion import replace_source
from elobot.features.bases.fingerprints.jobs import claim_tasks, commit_result, enroll_batch, materialize_outbox, run_counts
from elobot.features.bases.fingerprints.search import eligible, find_matches, visibility_filter
from elobot.features.bases.fingerprints.service import FingerprintService
from elobot.features.bases.fingerprints.types import ExtractionResult, ExtractionState, utcnow
from elobot.features.bases.fingerprints.types import FingerprintError
from elobot.db import BaseExtractionRun, BaseFingerprintTask, BaseLayout
from tests._base_fingerprint_fixture import fingerprint
from tests._relative_geometry_fixture import layout
from elobot.features.bases.fingerprints.types import PIPELINE_ID
from tests._patch_sites import BASE_MODULE_FAMILY, patch_family
import elobot.extensions.preset as preset_commands
from unittest.mock import MagicMock


from tests.integration._fingerprint_fixture import FingerprintPersistenceStateMixin
import elobot.extensions.base as base_module

class TestFingerprintPersistence(FingerprintPersistenceStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_outbox_restart_duplicate_and_atomic_commit_preserve_metadata(self):
        task = await self.claim()
        await materialize_outbox()
        self.assertEqual(await BaseFingerprintTask.count(), 1)
        await BaseLayout.get_pymongo_collection().update_one({"_id": self.base.id},
                                                           {"$push": {"records": {"concurrent": True}}})
        await commit_result(task, ExtractionResult(fingerprint=fingerprint()))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.records, [{"keep": True}, {"concurrent": True}])
        self.assertEqual(saved.fingerprint_extraction.status, "ready")
        self.assertTrue(eligible(saved))
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "succeeded")

    async def test_crash_after_base_commit_recovers_receipt(self):
        task = await self.claim()
        with patch("elobot.features.bases.fingerprints.jobs.finish_task", new=AsyncMock(side_effect=AutoReconnect("crash"))):
            with self.assertRaises(AutoReconnect):
                await commit_result(task, ExtractionResult(fingerprint=fingerprint()))
        await BaseFingerprintTask.get_pymongo_collection().update_one({"_id": task.id},
            {"$set": {"lease_expires_at": utcnow() - timedelta(seconds=1)}})
        self.assertEqual(await claim_tasks(None, 1, "restarted"), [])
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "succeeded")

    async def test_replacement_deleted_and_expired_claim_cannot_commit(self):
        old = await self.claim()
        await replace_source(await BaseLayout.get(self.base.id), "https://cdn.example/new.png", "Wizard Valley")
        await commit_result(old, ExtractionResult(fingerprint=fingerprint()))
        self.assertEqual((await BaseFingerprintTask.get(old.id)).state, "superseded")
        current = await BaseLayout.get(self.base.id)
        self.assertEqual(current.image_revision, 1)
        self.assertIsNone(current.fingerprint)
        newer = await self.claim()
        await current.delete()
        await commit_result(newer, ExtractionResult(error="failed"))
        self.assertEqual((await BaseFingerprintTask.get(newer.id)).state, "deleted")
        self.assertIsNone(await BaseLayout.get(self.base.id))

    async def test_expired_worker_fenced_by_new_claim(self):
        old = await self.claim()
        past = utcnow() - timedelta(seconds=1)
        await BaseFingerprintTask.get_pymongo_collection().update_one({"_id": old.id}, {"$set": {"lease_expires_at": past}})
        current = (await claim_tasks(None, 1, "new-owner"))[0]
        await commit_result(old, ExtractionResult(fingerprint=fingerprint()))
        self.assertIsNone((await BaseLayout.get(self.base.id)).fingerprint)
        await commit_result(current, ExtractionResult(fingerprint=fingerprint()))
        self.assertEqual((await BaseFingerprintTask.get(current.id)).state, "succeeded")

    async def test_expired_uncontended_completion_remains_recoverable(self):
        task = await self.claim()
        past = utcnow() - timedelta(seconds=1)
        await BaseFingerprintTask.get_pymongo_collection().update_one({"_id": task.id},
                                                                    {"$set": {"lease_expires_at": past}})
        await BaseLayout.get_pymongo_collection().update_one({"_id": self.base.id},
            {"$set": {"fingerprint_extraction.lease_expires_at": past}})
        await commit_result(task, ExtractionResult(fingerprint=fingerprint()))
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "running")
        retry = (await claim_tasks(None, 1, "retry"))[0]
        await commit_result(retry, ExtractionResult(fingerprint=fingerprint()))
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "succeeded")

    async def test_failed_regeneration_preserves_searchable_payload(self):
        task = await self.claim()
        await commit_result(task, ExtractionResult(fingerprint=fingerprint()))
        base = await BaseLayout.get(self.base.id)
        await base.set({"fingerprint_extraction": ExtractionState(source_revision=0).model_dump()})
        task = await self.claim()
        await commit_result(task, ExtractionResult(error="source_unavailable"))
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint, fingerprint().model_copy(update={"generated_at": saved.fingerprint.generated_at}))
        self.assertTrue(eligible(saved))
        self.assertEqual(saved.fingerprint_extraction.status, "failed")

    async def test_bulk_membership_restarts_regenerates_and_duplicate_command(self):
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        run, created = await service.begin_run("123", 1)
        self.assertTrue(created)
        again, created = await service.begin_run("456", 8)
        self.assertFalse(created)
        self.assertEqual(run.id, again.id)
        await enroll_batch(run)
        run.enrollment_cursor = None
        await enroll_batch(run)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id}).count(), 1)
        newer = self.base.model_copy(update={"id": ObjectId(), "link": "new"})
        await newer.insert()
        run.enrollment_cursor = None
        await enroll_batch(run)
        self.assertEqual(await BaseFingerprintTask.find({"run_id": run.id}).count(), 1)
        tasks = await claim_tasks(run.id, 8, "bulk")
        self.assertEqual(len(tasks), 1)
        await commit_result(tasks[0], ExtractionResult(fingerprint=layout()))
        self.assertEqual(await run_counts(run.id), {"succeeded": 1})
        await run.set({"active": False, "status": "completed"})
        fresh, created = await service.begin_run("123", 1)
        self.assertTrue(created)
        self.assertNotEqual(fresh.id, run.id)

    async def test_permissions_filter_before_count_and_scoring(self):
        await self.base.set({"fingerprint": layout(version=1).model_dump(), "builder_id": "123"})
        private = self.base.model_copy(update={"id": ObjectId(), "link": "private", "quality": "Private"})
        await private.insert()
        other = self.base.model_copy(update={"id": ObjectId(), "link": "other", "builder_id": "456"})
        await other.insert()
        ctx = SimpleNamespace(author=SimpleNamespace(id=123, has_any_role=lambda roles: False))
        worker = SimpleNamespace(request=AsyncMock(return_value={"scores": [{
            "status": "matched", "similarity": 100, "matched": 30, "total": 30, "residual": 0}]}))
        results, indexed, total = await find_matches(SimpleNamespace(cpu=worker), layout(),
            visibility_filter(ctx, "Barbarian Camp"), top=5, minimum=80, fuzzy=False)
        self.assertEqual((indexed, total), (1, 1))
        self.assertEqual([item.base_id for item in results], [str(self.base.id)])
        self.assertEqual(len(worker.request.await_args.args[0]["candidates"]), 1)

    async def test_creation_hooks_and_image_replacement_persist_outbox_before_response(self):
        bot = MagicMock()
        bot.ext = {}
        bot.owner.send = AsyncMock(return_value=SimpleNamespace(
            id=101, channel=SimpleNamespace(id=102),
            attachments=[SimpleNamespace(id=103, url="https://cdn.example/upload.png")],
        ))
        cog = base_module.BaseCommands(bot)
        preset = preset_commands.PresetCommands(bot)
        player = SimpleNamespace(elo=1000, base_presets={}, save_changes=AsyncMock())
        ctx = SimpleNamespace(defer=AsyncMock(), send=AsyncMock(),
                              author=SimpleNamespace(id=123), guild=SimpleNamespace(id=456))
        self.enterContext(patch_family(BASE_MODULE_FAMILY, "download_base_screenshot", new=AsyncMock(return_value=True)))
        self.enterContext(patch_family(BASE_MODULE_FAMILY, "delete_file", new=AsyncMock()))
        self.enterContext(patch_family(BASE_MODULE_FAMILY, "Player", SimpleNamespace(
            user_id="user_id", find_one=AsyncMock(return_value=player))))
        self.enterContext(patch.object(preset_commands, "download_base_screenshot", new=AsyncMock(return_value=True)))
        self.enterContext(patch.object(preset_commands, "delete_file", new=AsyncMock()))
        self.enterContext(patch.object(preset_commands, "Player", SimpleNamespace(
            user_id="user_id", find_one=AsyncMock(return_value=player))))
        attachment = SimpleNamespace(url="https://cdn.example/input.png")
        for name, command in (("staff", cog.base_add), ("private", cog.base_private_add),
                              ("preset", preset.preset_base_add)):
            link = f"https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3A{name}"
            ctx.send.reset_mock()
            args = (link, attachment, "Good") if name == "staff" else (link, attachment)
            owner = preset if name == "preset" else cog
            await command.callback.func(owner, ctx, *args)
            saved = await BaseLayout.find_one({"link": link})
            self.assertEqual(saved.fingerprint_extraction.status, "pending")
            self.assertEqual(saved.image_attachment.message_id, "101")
            self.assertIn("pending", ctx.send.await_args.args[0])
            first_request = saved.fingerprint_extraction.request_id
            await command.callback.func(owner, ctx, *args)
            changed = await BaseLayout.find_one({"link": link})
            self.assertEqual(changed.image_revision, 1)
            self.assertNotEqual(changed.fingerprint_extraction.request_id, first_request)
            if name == "preset":
                self.assertEqual(player.base_presets["Barbarian Camp"].id, saved.id)
            await cog.base_set_image.callback.func(cog, ctx, link, "https://cdn.example/replaced.png")
            self.assertEqual((await BaseLayout.get(saved.id)).image_revision, 2)
            self.assertIn("Screenshot saved", ctx.send.await_args.args[0])

    async def test_database_failure_before_commit_never_marks_success(self):
        task = await self.claim()
        collection = BaseLayout.get_pymongo_collection()
        with patch.object(collection, "update_one", new=AsyncMock(side_effect=AutoReconnect("lost write"))):
            with self.assertRaises(AutoReconnect):
                await commit_result(task, ExtractionResult(fingerprint=fingerprint()))
        self.assertIsNone((await BaseLayout.get(self.base.id)).fingerprint)
        self.assertEqual((await BaseFingerprintTask.get(task.id)).state, "running")

    async def test_bulk_mixed_counts_and_discord_failure_keep_committed_success(self):
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        service.bot.fetch_channel = AsyncMock(side_effect=ipy.errors.Forbidden(
            response=SimpleNamespace(status=403, reason="Forbidden"), response_data={}))
        run, _ = await service.begin_run("123", 1)
        await enroll_batch(run)
        task = (await claim_tasks(run.id, 1, "bulk"))[0]
        await commit_result(task, ExtractionResult(fingerprint=layout()))
        await run.set({"active": False, "status": "completed"})
        await service.publish(run)
        self.assertEqual(await run_counts(run.id), {"succeeded": 1})
        self.assertEqual((await BaseExtractionRun.get(run.id)).notification_error, "Forbidden")
        self.assertTrue(eligible(await BaseLayout.get(self.base.id)))

    async def test_single_request_reuses_active_then_regenerates_without_losing_valid_data(self):
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        await self.base.set({"fingerprint_extraction": ExtractionState(source_revision=0,
                                                                      pipeline_id=PIPELINE_ID)})
        state, created = await service.request_extraction(self.base)
        self.assertFalse(created)
        self.assertEqual(state.request_id, self.base.fingerprint_extraction.request_id)
        task = await self.claim()
        await commit_result(task, ExtractionResult(fingerprint=layout()))
        ready = await BaseLayout.get(self.base.id)
        state, created = await service.request_extraction(ready)
        self.assertTrue(created)
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.fingerprint_extraction.request_id, state.request_id)
        self.assertEqual(saved.fingerprint_extraction.status, "pending")
        self.assertEqual(saved.image_revision, ready.image_revision)
        self.assertEqual(saved.fingerprint, ready.fingerprint)
        self.assertEqual(saved.records, ready.records)
        task = await self.claim()
        self.assertIsNone(task.run_id)
        existing, created = await service.request_extraction(await BaseLayout.get(self.base.id))
        self.assertFalse(created)
        self.assertEqual(existing.status, "running")
        await commit_result(task, ExtractionResult(error="source_unavailable"))
        self.assertTrue(eligible(await BaseLayout.get(self.base.id)))

    async def test_single_preflight_failure_and_source_race_preserve_current_state(self):
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock(side_effect=FingerprintError("model_missing"))
        original = await BaseLayout.get(self.base.id)
        with self.assertRaises(FingerprintError):
            await service.request_extraction(original)
        self.assertEqual((await BaseLayout.get(self.base.id)).fingerprint_extraction, original.fingerprint_extraction)
        service.cpu.start = AsyncMock()
        await replace_source(original, "https://cdn.example/replacement.png", "Wizard Valley")
        with self.assertRaises(FingerprintError):
            await service.request_extraction(self.base)
        self.assertEqual((await BaseLayout.get(self.base.id)).image_revision, 1)

    async def test_single_request_cas_cannot_overwrite_concurrent_request(self):
        service = FingerprintService(SimpleNamespace(), "isolated")
        service.cpu.start = AsyncMock()
        await self.base.set({"fingerprint_extraction": None})
        base = await BaseLayout.get(self.base.id)
        concurrent = ExtractionState(source_revision=0)
        collection = BaseLayout.get_pymongo_collection()
        original_update = collection.update_one

        async def insert_concurrent_request(query, update):
            await original_update({"_id": base.id}, {"$set": {"fingerprint_extraction": concurrent.model_dump()}})
            return await original_update(query, update)

        with patch.object(collection, "update_one", new=AsyncMock(side_effect=insert_concurrent_request)):
            with self.assertRaises(FingerprintError):
                await service.request_extraction(base)
        self.assertEqual((await BaseLayout.get(base.id)).fingerprint_extraction.request_id, concurrent.request_id)


if __name__ == "__main__":
    unittest.main()
