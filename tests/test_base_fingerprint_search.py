import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.features.base_submission.presentation import build_review_components
from elobot.features.bases.fingerprints.search import SearchSession
from elobot.features.bases.fingerprints.types import FingerprintError
from elobot.config.ids import BASE_SUBMISSION_CHANNEL
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.db import PendingBaseSubmission
from tests._base_fingerprint_fixture import fingerprint
from tests._base_finder_lock_fixture import ContendedLock


from tests._base_search_fixture import FingerprintSearchStateMixin
from tests._patch_sites import patch_sites

class TestFingerprintSearch(FingerprintSearchStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_saved_review_source_read_only_and_exact_button(self):
        row = build_review_components()
        self.assertEqual([button.label for button in row.components], ["Accept", "Reject", "Find Similar"])
        button = row.components[2]
        self.assertEqual(button.emoji.name, "🔍")
        self.assertEqual(button.custom_id, "base_submission_find_similar")
        self.assertEqual(button.style, ipy.ButtonStyle.SECONDARY)
        self.assertTrue(all(button.disabled for button in build_review_components(True).components))
        ctx = self.context()
        await self.search(ctx)
        self.bot.base_fingerprints.query.assert_awaited_once_with("https://review/image", "Barbarian Camp", None)
        self.assertIsNone(self.pending.resolution)
        ctx.message.edit.assert_not_awaited()
        payload = ctx.send.await_args.kwargs
        self.assertEqual(self.find.await_args.args[2]["quality"], {"$ne": "Private"})
        self.assertTrue(payload["ephemeral"])
        self.assertEqual(payload["embeds"][0].title, "**Similar Base 1/3**")
        self.assertIn("100.0%", payload["embeds"][0].fields[0].value)
        self.assertEqual(payload["embeds"][0].description, self.bases[0].link + "\n")
        self.assertEqual(payload["embeds"][0].fields[2].value,
                         "**[Builder](<https://discordapp.com/users/123>)**")
        self.assertEqual([b.disabled for b in payload["components"][0].components], [True, False, False])

    async def test_slash_search_also_excludes_private_candidates(self):
        ctx = self.context()
        await self.cog.base_find.callback.func(self.cog, ctx, "Barbarian Camp", SimpleNamespace(url="https://input"))
        self.assertEqual(self.find.await_args.args[2]["quality"], {"$ne": "Private"})
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_pages_first_middle_last_and_prune_private_changed_deleted(self):
        session = SearchSession("123", "Barbarian Camp", self.scores, [], message_id="777")
        ctx = self.context(message=777)
        self.cog._similarity.add(session)
        ctx.custom_id = f"base_similar|{session.token}|next"
        await self.cog.similar_page.callback.func(self.cog, ctx)
        payload = ctx.edit_origin.await_args.kwargs
        self.assertEqual([b.label for b in payload["components"][0].components], ["◀️️", "2/3", "▶️"])
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertTrue(ctx.edit_origin.await_args.kwargs["components"][0].components[-1].disabled)
        self.bases[0].quality = "Private"
        self.bases[1].image_revision = 1
        self.bases.pop()
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertEqual(ctx.edit_origin.await_args.kwargs["components"], [])
        self.assertIn("No results remain", ctx.edit_origin.await_args.kwargs["content"])
        self.bot.base_fingerprints.query.assert_not_awaited()

    async def test_independent_reviewers_pending_duplicate_and_resolution_during_search(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def query(*args):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            return fingerprint()

        self.bot.base_fingerprints.query.side_effect = query
        first = self.context()
        running = asyncio.create_task(self.search(first))
        await asyncio.wait_for(entered.wait(), 5)
        duplicate = self.context()
        await self.search(duplicate)
        self.assertIn("already pending", duplicate.send.await_args.args[0])
        self.assertTrue(duplicate.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.pending.resolution = SimpleNamespace(phase="rejected")
        release.set()
        await asyncio.wait_for(running, 5)
        self.assertIn("resolved", first.send.await_args.kwargs["embeds"][0].fields[-1].value)
        refused = self.context(456)
        await self.search(refused)
        self.assertIn("New searches are closed", refused.send.await_args.args[0])
        self.pending.resolution = None
        second = self.context(456)
        await self.search(second)
        self.assertEqual(len(self.cog._similarity.sessions), 2)

    async def test_ownership_expiry_restart_and_access_revocation(self):
        session = SearchSession("123", "Barbarian Camp", self.scores, [], review_id="9001", message_id="777")
        self.cog._similarity.add(session)
        ctx = self.context(456, 777)
        ctx.custom_id = f"base_similar|{session.token}|next"
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertIn("belong", ctx.send.await_args.args[0])
        ctx.author.id = 123
        ctx.author.has_any_role = lambda roles: False
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertIn("no longer have access", ctx.send.await_args.args[0])
        ctx.author.has_any_role = lambda roles: True
        session.expires_at = time.monotonic() - 1
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertIn("expired", ctx.send.await_args.args[0])
        ctx.message.edit.assert_awaited_with(components=[])
        await self.cog.similar_page.callback.func(self.cog, ctx)
        self.assertIn("expired", ctx.send.await_args.args[0])

    async def test_failures_empty_and_wrong_channel_have_no_paging_single_has_disabled_arrows(self):
        ctx = self.context()
        self.bot.base_fingerprints.query.side_effect = FingerprintError("invalid_image")
        await self.search(ctx)
        self.assertIn("invalid_image", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertIsNone(self.pending.resolution)
        self.bot.base_fingerprints.query.side_effect = None
        self.find.return_value = ([], 0, 5)
        await self.search(ctx)
        self.assertIn("No current indexed", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.find.return_value = ([], 3, 5)
        await self.search(ctx)
        self.assertIn("No matches", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.find.return_value = (self.scores[:1], 3, 5)
        await self.search(ctx)
        rows = ctx.send.await_args.kwargs["components"]
        self.assertEqual(len(rows), 2)
        self.assertEqual([button.disabled for button in rows[0].components], [True, False, True])
        self.assertEqual([button.label for button in rows[1].components], ["Compare", "Show Labels"])
        ctx.channel_id = 0
        self.bot.base_fingerprints.query.reset_mock()
        await self.search(ctx)
        self.bot.base_fingerprints.query.assert_not_awaited()

    async def test_rapid_page_edits_are_serialized(self):
        session = SearchSession("123", "Barbarian Camp", self.scores, [], message_id="777")
        session.lock = ContendedLock()
        self.cog._similarity.add(session)
        contexts = [self.context(message=777), self.context(message=777)]
        edited = []
        entered, release = asyncio.Event(), asyncio.Event()

        async def edit(**kwargs):
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
            edited.append(kwargs["embeds"][0].title)

        for ctx in contexts:
            ctx.custom_id = f"base_similar|{session.token}|next"
            ctx.edit_origin.side_effect = edit
        first = asyncio.create_task(self.cog.similar_page.callback.func(self.cog, contexts[0]))
        await asyncio.wait_for(entered.wait(), 5)
        second = asyncio.create_task(self.cog.similar_page.callback.func(self.cog, contexts[1]))
        await asyncio.wait_for(session.lock.contended.wait(), 5)
        self.assertEqual(edited, [])
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        self.assertIn("2/3", edited[0])
        self.assertIn("3/3", edited[1])

    async def test_review_refresh_preserves_disabled_and_terminal_messages(self):
        self.pending.id = "9001"
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        message = SimpleNamespace(author=self.bot.user,
                                  components=[ipy.ActionRow(*build_review_components().components[:2])],
                                  edit=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(fetch_message=AsyncMock(return_value=message)))
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        with patch.object(PendingBaseSubmission, "find", return_value=query):
            await callback(self.cog)
            self.assertEqual(len(message.edit.await_args.kwargs["components"].components), 3)
            message.edit.reset_mock()
            message.components[0].components[0].disabled = True
            await callback(self.cog)
            message.edit.assert_not_awaited()
            self.pending.resolution = SimpleNamespace(phase="rejected")
            await callback(self.cog)
            message.edit.assert_not_awaited()

    async def test_review_refresh_skips_messages_owned_by_another_bot(self):
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        message = SimpleNamespace(author=SimpleNamespace(id=5678),
                                  components=[ipy.ActionRow(*build_review_components().components[:2])],
                                  edit=AsyncMock())
        self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(fetch_message=AsyncMock(return_value=message)))
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        with patch.object(PendingBaseSubmission, "find", return_value=query), self.assertNoLogs(level="WARNING"):
            await callback(self.cog)
            await callback(self.cog)
        message.edit.assert_not_awaited()
        self.assertIsNone(self.pending.resolution)
        self.assertFalse(self.cog._review_locks)

    async def test_review_refresh_recovers_saved_pending_publication_after_failed_enable(self):
        self.pending.link = "https://base/pending"
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        embed = ipy.Embed(title="Base Submission Review", description=self.pending.link,
                          fields=[ipy.EmbedField(name="Review Status", value="Pending")])
        client = MagicMock()
        client.cache.get_user.return_value = self.bot.user
        message = ipy.Message(client=client, id=int(self.pending.id), channel_id=BASE_SUBMISSION_CHANNEL,
                              author_id=self.bot.user.id, embeds=[embed],
                              components=[build_review_components(disabled=True)])

        def receive(payload):
            message.components = [ipy.ActionRow.from_dict(row) for row in payload["components"]]
            return message

        client.http.edit_message = AsyncMock(side_effect=lambda payload, *_args, **_kwargs: payload)
        client.cache.place_message_data.side_effect = receive
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        with patch.object(PendingBaseSubmission, "find", return_value=query):
            await callback(self.cog)
            await callback(self.cog)
        self.assertEqual(channel.fetch_message.await_args.kwargs, {"force": True})
        client.http.edit_message.assert_awaited_once()
        payload = client.http.edit_message.await_args.args[0]
        self.assertEqual([button["disabled"] for button in payload["components"][0]["components"]],
                         [False, False, False])
        self.assertNotIn("embeds", payload)
        self.assertTrue(all(not button.disabled for button in message.components[0].components))
        self.assertIsNone(self.pending.resolution)
        self.bot.base_fingerprints.query.assert_not_awaited()

    async def test_review_refresh_does_not_reopen_terminal_missing_or_mismatched_disabled_review(self):
        self.pending.link = "https://base/pending"
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        for state, status, link in ((None, "Pending", self.pending.link),
                                    ("rejected", "Pending", self.pending.link),
                                    ("pending", "Accepted by <@123>", self.pending.link),
                                    ("pending", "Pending", "https://base/other")):
            with self.subTest(state=state, status=status, link=link):
                self.pending.resolution = SimpleNamespace(phase=state) if state == "rejected" else None
                PendingBaseSubmission.get.return_value = self.pending if state else None
                embed = ipy.Embed(title="Base Submission Review", description=link,
                                  fields=[ipy.EmbedField(name="Review Status", value=status)])
                message = SimpleNamespace(author=self.bot.user, embeds=[embed],
                                          components=[build_review_components(disabled=True)], edit=AsyncMock())
                self.bot.fetch_channel = AsyncMock(return_value=SimpleNamespace(fetch_message=AsyncMock(
                    return_value=message)))
                with patch.object(PendingBaseSubmission, "find", return_value=query):
                    await callback(self.cog)
                message.edit.assert_not_awaited()

    async def test_review_refresh_forbidden_retries_after_an_hour_and_recovers(self):
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        for index, stage in enumerate(("channel", "message", "edit")):
            with self.subTest(stage=stage):
                message = SimpleNamespace(author=self.bot.user,
                                          components=[ipy.ActionRow(*build_review_components().components[:2])],
                                          edit=AsyncMock())
                channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))
                self.bot.fetch_channel = AsyncMock(return_value=channel)
                denied = {"channel": self.bot.fetch_channel, "message": channel.fetch_message,
                          "edit": message.edit}[stage]
                denied.side_effect = ipy.errors.Forbidden(
                    response=SimpleNamespace(status=403, reason="Forbidden"),
                    response_data={"code": 50013, "message": "Missing Permissions"})
                with (patch.object(PendingBaseSubmission, "find", return_value=query),
                      patch_sites(('elobot.extensions.base.extraction.time', 'elobot.extensions.base.search.time')) as clock):
                    started = 10000 + index * 7200
                    clock.monotonic.return_value = started
                    with self.assertLogs(level="WARNING") as logs:
                        await callback(self.cog)
                        clock.monotonic.return_value = started + 60
                        await callback(self.cog)
                        clock.monotonic.return_value = started + 3599
                        await callback(self.cog)
                    self.assertEqual(len(logs.output), 1)
                    self.assertIn("50013", logs.output[0])
                    self.assertIn("retrying in 1 hour", logs.output[0])
                    self.bot.fetch_channel.assert_awaited_once()
                    denied.side_effect = None
                    message.edit.reset_mock()
                    other = self.pending.model_copy(update={"id": "9002"})
                    query.sort.return_value.limit.return_value.to_list.return_value = [self.pending, other]
                    PendingBaseSubmission.get.return_value = other
                    with self.assertNoLogs(level="WARNING"):
                        await callback(self.cog)
                    message.edit.assert_awaited_once()
                    self.assertIsNone(other.resolution)
                    query.sort.return_value.limit.return_value.to_list.return_value = [self.pending]
                    PendingBaseSubmission.get.return_value = self.pending
                    message.edit.reset_mock()
                    clock.monotonic.return_value = started + 3600
                    with self.assertNoLogs(level="WARNING"):
                        await callback(self.cog)
                    message.edit.assert_awaited_once()
                    self.assertEqual(len(message.edit.await_args.kwargs["components"].components), 3)
                    self.assertFalse(self.cog._review_refresh_retry_after)
                    self.assertFalse(self.cog._review_locks)
                    self.assertIsNone(self.pending.resolution)
                    self.bot.base_fingerprints.query.assert_not_awaited()

    async def test_review_refresh_rechecks_resolution_after_waiting_for_review_lock(self):
        query = MagicMock()
        query.sort.return_value.limit.return_value.to_list = AsyncMock(return_value=[self.pending])
        self.bot.fetch_channel = AsyncMock()
        lock = ContendedLock()
        self.cog._review_locks[int(self.pending.id)] = lock
        callback = self.cog.refresh_similarity_buttons.callback
        callback = getattr(callback, "func", callback)
        with patch.object(PendingBaseSubmission, "find", return_value=query):
            async with lock:
                running = asyncio.create_task(callback(self.cog))
                await asyncio.wait_for(lock.contended.wait(), 5)
                self.assertFalse(running.done())
                PendingBaseSubmission.get.assert_not_awaited()
                self.pending.resolution = SimpleNamespace(phase="rejected")
            await asyncio.wait_for(running, 5)
        self.bot.fetch_channel.assert_not_awaited()
        self.assertEqual(self.pending.resolution.phase, "rejected")
        self.assertFalse(self.cog._review_locks)
