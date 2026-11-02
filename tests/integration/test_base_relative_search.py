import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from bson import ObjectId

import cogs.base_commands as commands
from common.base_fingerprints.matcher import score_layout
from common.constants import BASE_SUBMISSION_CHANNEL
from database_models import BaseLayout, PendingBaseSubmission
from tests._relative_geometry_fixture import layout, points
from tests.integration import _fingerprint_fixture as fixture
from tests._relative_geometry_fixture import summary


class TestRelativeSearch(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.FingerprintPersistenceStateMixin.asyncSetUp
    asyncTearDown = fixture.FingerprintPersistenceStateMixin.asyncTearDown

    async def setup_search(self):
        await self.base.set({"fingerprint": layout(version=1), "builder_id": "123"})
        private = self.base.model_copy(update={"id": ObjectId(), "link": "private", "quality": "Private"})
        await private.insert()
        self.submission = PendingBaseSubmission.model_construct(
            id="9001", review_channel_id=str(BASE_SUBMISSION_CHANNEL), district="Barbarian Camp",
            image_url="synthetic", resolution=None)
        self.enterContext(patch.object(PendingBaseSubmission, "get", new=AsyncMock(return_value=self.submission)))
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.cog = commands.BaseCommands(self.bot)
        self.ctx = SimpleNamespace(author=SimpleNamespace(id=123, has_any_role=lambda _: True),
                                   guild=SimpleNamespace(fetch_member=AsyncMock(
                                       return_value=SimpleNamespace(id=123, display_name="Builder"))),
                                   channel_id=BASE_SUBMISSION_CHANNEL,
                                   message=SimpleNamespace(id=9001, edit=AsyncMock()), defer=AsyncMock(),
                                   send=AsyncMock(return_value=SimpleNamespace(id=777)))

        async def worker(payload):
            self.assertEqual(len(payload["candidates"]), 1)
            from common.base_fingerprints.types import BaseFingerprint
            query = BaseFingerprint.model_validate(payload["query"])
            candidates = [BaseFingerprint.model_validate(value) for value in payload["candidates"]]
            return {"scores": [(await asyncio.to_thread(score_layout, query, value,
                                                       payload["fuzzy"])).model_dump() for value in candidates]}

        self.worker = AsyncMock(side_effect=worker)
        self.bot.base_fingerprints = SimpleNamespace(query=AsyncMock(return_value=layout()),
                                                     cpu=SimpleNamespace(request=self.worker))

    async def test_hallless_review_matches_legacy_and_excludes_private_before_counts(self):
        await self.setup_search()
        await self.cog.find_similar_submission.callback.func(self.cog, self.ctx)
        payload = self.ctx.send.await_args.kwargs
        embed = payload["embeds"][0]
        self.assertIn("100.0%", embed.fields[0].value)
        self.assertIn("30/30", embed.fields[0].value)
        self.assertEqual(embed.description, "base\n")
        self.assertEqual(embed.fields[2].value, "**[Builder](<https://discordapp.com/users/123>)**")
        self.assertIn("hall_absent", embed.fields[-1].value)
        self.assertTrue(self.worker.await_args.args[0]["fuzzy"])
        self.assertIsNone(self.submission.resolution)
        self.ctx.message.edit.assert_not_awaited()

    async def test_zero_minimum_cannot_accept_insufficient_geometry(self):
        await self.setup_search()
        self.bot.base_fingerprints.query.return_value = layout(points(12))
        await self.cog.base_find.callback.func(self.cog, self.ctx, "Barbarian Camp",
                                              SimpleNamespace(url="synthetic"), min_similarity=0)
        text = self.ctx.send.await_args.args[0]
        self.assertIn("Extraction succeeded", text)
        self.assertIn("No matches", text)
        self.assertIn("1/1", text)
        self.assertIn("hall_absent", text)
        self.assertIsNone(self.submission.resolution)
        self.ctx.message.edit.assert_not_awaited()

    async def test_malformed_worker_outcome_leaves_review_controls_and_decision_unchanged(self):
        await self.setup_search()
        self.worker.side_effect = None
        for response in ({"scores": []}, {"scores": [[100, 30, 30]]},
                         {"scores": [{"status": "insufficient", "similarity": 0, "total": 30}]}):
            self.worker.return_value = response
            await self.cog.find_similar_submission.callback.func(self.cog, self.ctx)
            self.assertIn("worker_invalid_response", self.ctx.send.await_args.args[0])
            self.assertIsNone(self.submission.resolution)
            self.ctx.message.edit.assert_not_awaited()
            self.assertEqual(self.cog._similarity.pending, set())

    async def test_access_revoked_during_extraction_prevents_counting_or_scoring(self):
        await self.setup_search()

        async def revoke(*args):
            self.ctx.author.has_any_role = lambda _: False
            return layout()

        self.bot.base_fingerprints.query.side_effect = revoke
        await self.cog.find_similar_submission.callback.func(self.cog, self.ctx)
        self.worker.assert_not_awaited()
        self.assertIn("no longer have access", self.ctx.send.await_args.args[0])

    async def test_compare_delivery_is_read_only_in_database(self):
        await self.setup_search()
        await self.cog.find_similar_submission.callback.func(self.cog, self.ctx)
        session = next(iter(self.cog._similarity.sessions.values()))
        before = await BaseLayout.get_pymongo_collection().find_one({"_id": self.base.id})
        self.bot.base_fingerprints.compare = AsyncMock(return_value=(b"png", summary()))
        self.ctx.message.id = 777
        self.ctx.custom_id = f"base_compare|{session.token}|{self.base.id}"
        await self.cog.similar_compare.callback.func(self.cog, self.ctx)
        self.assertIn("file", self.ctx.send.await_args.kwargs)
        after = await BaseLayout.get_pymongo_collection().find_one({"_id": self.base.id})
        self.assertEqual(after, before)
        self.assertIsNone(self.submission.resolution)
        self.ctx.message.edit.assert_not_awaited()

    async def test_compare_rechecks_committed_privacy_change_before_delivery(self):
        await self.setup_search()
        await self.cog.find_similar_submission.callback.func(self.cog, self.ctx)
        session = next(iter(self.cog._similarity.sessions.values()))

        async def compare(*_args, **_kwargs):
            await self.base.set({"quality": "Private"})
            return b"png", summary()

        self.bot.base_fingerprints.compare = AsyncMock(side_effect=compare)
        self.ctx.message.id = 777
        self.ctx.custom_id = f"base_compare|{session.token}|{self.base.id}"
        await self.cog.similar_compare.callback.func(self.cog, self.ctx)
        self.assertNotIn("file", self.ctx.send.await_args.kwargs)
        self.assertIn("no longer available", self.ctx.send.await_args.args[0])
        saved = await BaseLayout.get(self.base.id)
        self.assertEqual(saved.quality, "Private")
        self.assertEqual(saved.fingerprint, layout(version=1))
        self.assertEqual(saved.records, [{"keep": True}])
        self.assertIsNone(self.submission.resolution)
