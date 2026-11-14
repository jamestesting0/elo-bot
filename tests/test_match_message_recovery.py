import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

from common.matches.message_lock import match_message_lock
from common.matches.messages import MatchMessageState, publish_match_message
from common.matches.recovery import _repair_match, _repair_message, repair_match_messages
from elobot.db import Match, Participant


class TestMatchMessageRecovery(unittest.IsolatedAsyncioTestCase):
    def message(self, title="**Match**", description="Updating…", message_id=43):
        return SimpleNamespace(
            id=message_id, author=SimpleNamespace(id=99),
            embeds=[ipy.Embed(title=title, description=description)], components=["control"], edit=AsyncMock(),
        )

    async def test_bound_pending_is_explicitly_incomplete_without_guessing_run_type(self):
        message = self.message()
        await _repair_message(message, bound=True, ended=False, active=True)
        payload = message.edit.await_args.kwargs
        self.assertEqual(payload["embeds"][0].title, "**Match Update Incomplete**")
        self.assertNotIn("Updating", payload["embeds"][0].description)
        self.assertEqual(payload["components"], [])

    async def test_finished_message_clears_every_placeholder_and_preserves_controls(self):
        message = self.message("**Match Ended**", "Done")
        message.embeds.append(ipy.Embed(description="Loading…"))
        await _repair_message(message, bound=True, ended=True, active=False)
        payload = message.edit.await_args.kwargs
        self.assertEqual(payload["embeds"][1]["description"], "")
        self.assertNotIn("components", payload)

    async def test_active_committed_and_aborted_messages_keep_their_titles(self):
        for title, active in (("**Match Scheduled**", True), ("**Match Aborted**", False)):
            message = self.message(title, "Details")
            message.components = []
            await _repair_message(message, bound=True, ended=False, active=active)
            message.edit.assert_not_awaited()

    async def test_cancelled_binding_marks_message_incomplete_before_propagating(self):
        message = self.message()
        ctx = SimpleNamespace(channel_id=42, channel=SimpleNamespace(send=AsyncMock(return_value=message)))
        collection = SimpleNamespace(update_one=AsyncMock(side_effect=anyio.get_cancelled_exc_class()))
        match = SimpleNamespace(thread="42", get_pymongo_collection=lambda: collection,
                                model_fields_set={"message_publications"})
        expected = MagicMock(spec=MatchMessageState)
        expected.binding_filter.return_value = {}
        expected.player = 0
        with self.assertRaises(anyio.get_cancelled_exc_class()):
            await publish_match_message(ctx, match, expected, embed=ipy.Embed(title="**Match Started**"))
        initial = ctx.channel.send.await_args.kwargs["embed"]
        self.assertEqual((initial.title, initial.description), ("**Match**", "Updating…"))
        self.assertEqual(message.edit.await_args.kwargs["embed"].title, "**Match Update Incomplete**")
        self.assertEqual(message.edit.await_args.kwargs["components"], [])

    async def test_scan_covers_orphans_beyond_one_history_page_and_ignores_user_messages(self):
        orphan = self.message(message_id=500)
        user = self.message(message_id=501)
        user.author.id = 88
        active = self.message("**Match Started**", "Details", 43)
        async def history(*, limit):
            self.assertEqual(limit, 0)
            for _ in range(110):
                yield user
            yield orphan
            yield active
        channel = SimpleNamespace(history=history)
        bot = SimpleNamespace(user=SimpleNamespace(id=99), fetch_channel=AsyncMock(return_value=channel))
        match = Match.model_construct(
            thread="42", old_msgs=[43, None], times=[123, None],
            participants=[Participant(user_id="1"), Participant(user_id="2")],
        )
        await _repair_match(bot, match)
        orphan.edit.assert_awaited_once()
        active.edit.assert_not_awaited()
        user.edit.assert_not_awaited()

    async def test_recovery_refetches_after_waiting_for_active_writer(self):
        snapshot = SimpleNamespace(id="id", thread="42")
        fresh = SimpleNamespace(thread="42")
        entered, reader_waiting = anyio.Event(), anyio.Event()
        async def snapshots():
            reader_waiting.set()
            yield snapshot
        async def writer():
            async with match_message_lock("42"):
                async with match_message_lock(42):
                    entered.set()
                    await reader_waiting.wait()
                    fresh.thread = "new"
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(writer)
                await entered.wait()
                with patch.object(Match, "find", return_value=snapshots()), \
                        patch.object(Match, "get", new=AsyncMock(return_value=fresh)) as get, \
                        patch("common.matches.recovery._repair_match", new_callable=AsyncMock) as repair:
                    await repair_match_messages(SimpleNamespace())
                    get.assert_awaited_once_with("id")
                    repair.assert_not_awaited()


    async def test_cancel_scope_cleanup_completes_before_writer_exits(self):
        message = self.message()
        binding = anyio.Event()
        async def wait_binding(*_args):
            binding.set()
            await anyio.sleep_forever()
        async def edit(**_kwargs):
            await anyio.sleep(0)
        message.edit.side_effect = edit
        ctx = SimpleNamespace(channel_id=42, channel=SimpleNamespace(send=AsyncMock(return_value=message)))
        collection = SimpleNamespace(update_one=wait_binding)
        match = SimpleNamespace(thread="42", get_pymongo_collection=lambda: collection,
                                model_fields_set={"message_publications"})
        expected = MagicMock(spec=MatchMessageState)
        expected.binding_filter.return_value = {}
        expected.player = 0
        async def publish():
            await publish_match_message(ctx, match, expected, embed=ipy.Embed(title="**Match Started**"))
        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                group.start_soon(publish)
                await binding.wait()
                group.cancel_scope.cancel()
        self.assertEqual(message.edit.await_args.kwargs["embed"].title, "**Match Update Incomplete**")

    async def test_final_summary_title_and_description_are_preserved(self):
        message = self.message("**Match has Ended**", "Full result summary")
        message.components = []
        await _repair_message(message, bound=True, ended=True, active=True)
        message.edit.assert_not_awaited()

    async def test_bad_legacy_results_do_not_claim_ended(self):
        message = self.message()
        async def history(**_kwargs):
            yield message
        bot = SimpleNamespace(user=SimpleNamespace(id=99), fetch_channel=AsyncMock(
            return_value=SimpleNamespace(history=history),
        ))
        match = Match.model_construct(
            thread="42", old_msgs=[43, None], times=[123, None],
            participants=[Participant(user_id="1", result=["", "", ""]), Participant(user_id="2")],
        )
        await _repair_match(bot, match)
        self.assertEqual(message.edit.await_args.kwargs["embeds"][0].title, "**Match Update Incomplete**")

    async def test_failed_edit_does_not_starve_later_orphan(self):
        first, second = self.message(message_id=43), self.message(message_id=44)
        first.edit.side_effect = ipy.errors.HTTPException(response=SimpleNamespace(status=500, reason="failed"))
        async def history(**_kwargs):
            yield first
            yield second
        bot = SimpleNamespace(user=SimpleNamespace(id=99), fetch_channel=AsyncMock(
            return_value=SimpleNamespace(history=history),
        ))
        match = Match.model_construct(
            thread="42", old_msgs=[None, None],
            participants=[Participant(user_id="1"), Participant(user_id="2")],
        )
        with self.assertLogs(level="ERROR"):
            await _repair_match(bot, match)
        second.edit.assert_awaited_once()


    async def test_repaired_inactive_message_is_not_edited_each_sweep(self):
        message = self.message()
        await _repair_message(message, bound=True, ended=False, active=False)
        message.embeds = message.edit.await_args.kwargs["embeds"]
        message.components = []
        message.edit.reset_mock()
        await _repair_message(message, bound=True, ended=False, active=False)
        message.edit.assert_not_awaited()

