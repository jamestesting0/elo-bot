import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import AutoReconnect

from common.base_fingerprints.types import FingerprintError
from common.constants import ERROR_EMOJI
from database_models import BaseLayout
from tests import _base_search_fixture as fixture
from tests._relative_geometry_fixture import summary


class TestComparisonControls(unittest.IsolatedAsyncioTestCase):
    context = fixture.FingerprintSearchStateMixin.context
    search = fixture.FingerprintSearchStateMixin.search

    async def asyncSetUp(self):
        fixture.FingerprintSearchStateMixin.setUp(self)
        self.comparison = AsyncMock(return_value=(b"png bytes", summary()))
        self.bot.base_fingerprints.compare = self.comparison
        self.initial = self.context()
        await self.search(self.initial)
        self.session = next(iter(self.cog._similarity.sessions.values()))
        self.saved_bases = [base.model_dump() for base in self.bases]
        self.saved_review = self.pending.model_dump()

    def click(self, owner=123, message=777, base_id=None):
        ctx = self.context(owner, message)
        ctx.custom_id = f"base_compare|{self.session.token}|{base_id or self.scores[0].base_id}"
        return ctx

    async def invoke(self, ctx):
        await self.cog.similar_compare.callback.func(self.cog, ctx)

    async def test_both_search_flows_add_separate_compare_row_for_exact_displayed_base(self):
        for flow in ("review", "slash"):
            with self.subTest(flow=flow):
                ctx = self.context()
                if flow == "review":
                    await self.search(ctx)
                else:
                    await self.cog.base_find.callback.func(
                        self.cog, ctx, "Barbarian Camp", SimpleNamespace(url="https://query"))
                rows = ctx.send.await_args.kwargs["components"]
                self.assertEqual(len(rows), 2)
                button, labels_button = rows[1].components
                self.assertEqual(button.label, "Compare")
                self.assertEqual(button.emoji.name, "↔️")
                self.assertTrue(button.custom_id.endswith(self.scores[0].base_id))
                self.assertLessEqual(len(button.custom_id), 100)
                self.assertEqual(labels_button.label, "Show Labels")
                self.assertEqual(labels_button.emoji.name, "🏷️")
                self.assertTrue(labels_button.custom_id.endswith(self.scores[0].base_id))
                self.assertIsNone(ctx.send.await_args.kwargs["embeds"][0].footer)
                self.assertIsNone(self.pending.resolution)
                ctx.message.edit.assert_not_awaited()

    async def test_success_attaches_image_ephemerally_and_preserves_all_durable_state(self):
        ctx = self.click()
        captured = []

        async def send(**payload):
            self.assertTrue(payload["ephemeral"])
            self.assertEqual(payload["file"].file_name, "base-comparison.png")
            captured.append(payload["file"].file.read())

        ctx.send.side_effect = send
        await self.invoke(ctx)
        self.assertEqual(captured, [b"png bytes"])
        self.assertEqual(set(ctx.send.await_args.kwargs), {"file", "ephemeral"})
        self.comparison.assert_awaited_once_with(self.session.query, self.bases[0], fuzzy=True, attachment=None)
        self.assertEqual([base.model_dump() for base in self.bases], self.saved_bases)
        self.assertEqual(self.pending.model_dump(), self.saved_review)
        ctx.edit_origin.assert_not_awaited()
        ctx.message.edit.assert_not_awaited()
        self.assertFalse(self.cog._similarity.comparing)

    async def test_ownership_message_identity_expiry_and_role_guard_before_work(self):
        for ctx, expected in ((self.click(owner=456), "belong"), (self.click(message=9001), "belong")):
            await self.invoke(ctx)
            self.assertIn(expected, ctx.send.await_args.args[0])
        ctx = self.click()
        ctx.author.has_any_role = lambda _roles: False
        await self.invoke(ctx)
        self.assertIn("no longer have access", ctx.send.await_args.args[0])
        self.session.expires_at = time.monotonic() - 1
        ctx = self.click()
        await self.invoke(ctx)
        self.assertIn("expired", ctx.send.await_args.args[0])
        await self.invoke(ctx)
        self.assertIn("expired", ctx.send.await_args.args[0])
        self.comparison.assert_not_awaited()

    async def test_private_deleted_revised_or_stale_page_never_start_comparison(self):
        for change in ("private", "deleted", "revised", "page", "page_in_flight"):
            with self.subTest(change=change):
                base = self.bases[0]
                if change == "private":
                    base.quality = "Private"
                elif change == "deleted":
                    self.bases.remove(base)
                elif change == "revised":
                    base.image_revision = 1
                elif change == "page":
                    self.session.page = 1
                else:
                    self.session.page = len(self.scores)
                ctx = self.click()
                await self.invoke(ctx)
                self.assertIn("changed", ctx.send.await_args.args[0])
                self.comparison.assert_not_awaited()
                base.quality, base.image_revision, self.session.page = "Good", 0, 0
                if change == "deleted":
                    self.bases.insert(0, base)

    async def test_duplicate_and_page_race_do_not_publish_wrong_result(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def compare(*_args, **_kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return b"png", summary()

        self.comparison.side_effect = compare
        first = self.click()
        task = asyncio.create_task(self.invoke(first))
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = self.click()
        await self.invoke(duplicate)
        self.assertIn("already pending", duplicate.send.await_args.args[0])
        page = self.context(message=777)
        page.custom_id = f"base_similar|{self.session.token}|next"
        await self.cog.similar_page.callback.func(self.cog, page)
        self.assertTrue(page.edit_origin.await_args.kwargs["components"][-1].components[0].custom_id.endswith(
            self.scores[1].base_id))
        release.set()
        await asyncio.wait_for(task, 5)
        self.assertIn("selected result changed", first.send.await_args.args[0])
        self.assertNotIn("file", first.send.await_args.kwargs)
        self.assertFalse(self.cog._similarity.comparing)

    async def test_access_privacy_revision_expiry_and_fingerprint_changes_during_work_block_delivery(self):
        for change in ("role", "private", "revision", "expiry", "fingerprint", "session"):
            with self.subTest(change=change):
                ctx = self.click()
                original = self.bases[0].fingerprint.model_copy(deep=True)

                async def compare(*_args, **_kwargs):
                    if change == "role":
                        ctx.author.has_any_role = lambda _roles: False
                    elif change == "private":
                        self.bases[0].quality = "Private"
                    elif change == "revision":
                        self.bases[0].image_revision = 1
                    elif change == "expiry":
                        self.session.expires_at = 0
                    elif change == "fingerprint":
                        self.bases[0].fingerprint = original.model_copy(update={"source_image_sha256": "b" * 64})
                    else:
                        self.cog._similarity.sessions.clear()
                    return b"png", summary()

                self.comparison.side_effect = compare
                await self.invoke(ctx)
                self.assertIn("Comparison unavailable", ctx.send.await_args.args[0])
                self.assertNotIn("file", ctx.send.await_args.kwargs)
                self.assertFalse(self.cog._similarity.comparing)
                self.bases[0].quality, self.bases[0].image_revision = "Good", 0
                self.bases[0].fingerprint = original
                self.session.expires_at = time.monotonic() + 900
                self.cog._similarity.add(self.session)

    async def test_failures_are_visible_cancellation_cleans_guard_and_review_stays_unchanged(self):
        for failure, text in ((FingerprintError("source_unavailable"), "source_unavailable"),
                              (AutoReconnect("offline"), "temporarily unavailable")):
            self.comparison.side_effect = failure
            ctx = self.click()
            await self.invoke(ctx)
            self.assertIn(text, ctx.send.await_args.args[0])
            self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
            self.assertFalse(self.cog._similarity.comparing)
        self.comparison.side_effect = asyncio.CancelledError()
        cancelled = self.click()
        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(cancelled)
        self.assertIn("cancelled", cancelled.send.await_args.args[0])
        self.assertFalse(self.cog._similarity.comparing)
        self.comparison.side_effect = None
        ctx = self.click()
        ctx.send.side_effect = [ipy.errors.HTTPException(MagicMock(status=500)), None]
        await self.invoke(ctx)
        self.assertIn("could not be delivered", ctx.send.await_args.args[0])
        self.assertEqual(self.pending.model_dump(), self.saved_review)
        self.assertEqual([base.model_dump() for base in self.bases], self.saved_bases)
        self.assertFalse(self.cog._similarity.comparing)

    async def test_database_reads_cannot_bypass_access_recheck_for_either_image_action(self):
        self.bot.base_fingerprints.labels = AsyncMock(return_value=b"png")
        for labels, revoke_on in ((False, 1), (False, 2), (True, 1), (True, 2)):
            with self.subTest(labels=labels, revoke_on=revoke_on):
                ctx = self.click()
                reads = 0

                async def revoke(*_args, **_kwargs):
                    nonlocal reads
                    reads += 1
                    if reads == revoke_on:
                        ctx.author.has_any_role = lambda _roles: False
                    return self.bases[0]

                with patch.object(BaseLayout, "find_one", new=AsyncMock(side_effect=revoke)):
                    if labels:
                        ctx.custom_id = ctx.custom_id.replace("base_compare|", "base_labels|")
                        await self.cog.similar_labels.callback.func(self.cog, ctx)
                    else:
                        await self.invoke(ctx)
                self.assertIn("no longer have access", ctx.send.await_args.args[0])
                self.assertNotIn("file", ctx.send.await_args.kwargs)
                self.assertEqual(reads, revoke_on)

    async def test_resolved_review_is_read_only_and_builder_visibility_is_rechecked(self):
        async def compare(*_args, **_kwargs):
            self.pending.resolution = SimpleNamespace(phase="rejected")
            return b"png", summary()

        self.comparison.side_effect = compare
        ctx = self.click()
        await self.invoke(ctx)
        self.assertEqual(set(ctx.send.await_args.kwargs), {"file", "ephemeral"})
        self.assertEqual(self.pending.resolution.phase, "rejected")
        self.session.review_id = None
        from common.constants import LVL3_ROLES
        ctx = self.click()
        ctx.author.has_any_role = lambda roles: roles == LVL3_ROLES
        await self.invoke(ctx)
        self.assertEqual(BaseLayout.find_one.await_args.args[0]["builder_id"], "123")
