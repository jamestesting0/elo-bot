import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import anyio
import interactions as ipy
from pymongo.errors import AutoReconnect

from elo_bot.features.bases.fingerprints.removal import (
    BulkRemovalError,
    FingerprintRemoval,
    FingerprintRemovalProgress,
)
from elo_bot.features.bases.fingerprints.types import FingerprintError
from tests import _base_extract_fixture as fixture
import elo_bot.extensions.base.extraction as base_extraction_module


class TestBaseUnextract(unittest.IsolatedAsyncioTestCase):
    def context(self, **kwargs):
        ctx = fixture.BaseExtractStateMixin.context(self, **kwargs)
        ctx.edit = AsyncMock()
        return ctx

    def setUp(self) -> None:
        fixture.BaseExtractStateMixin.setUp(self)
        self.bot.base_fingerprints.run_lock = anyio.Lock()
        self.bot.base_fingerprints.gpu = SimpleNamespace(close=AsyncMock())
        self.single = self.enterContext(patch('elo_bot.extensions.base.extraction.remove_fingerprint', new=AsyncMock(
            return_value=FingerprintRemoval(removed=1, cancelled=1))))
        self.bulk = self.enterContext(patch('elo_bot.extensions.base.extraction.remove_all_fingerprints', new=AsyncMock(
            return_value=FingerprintRemoval(removed=7, cancelled=2))))

    async def invoke(self, ctx: ipy.SlashContext, selection: str | None = None) -> None:
        await self.cog.base_unextract.callback.func(self.cog, ctx, selection or str(self.base.id))

    async def test_both_commands_require_staff_and_single_has_shared_autocomplete(self) -> None:
        for command in (self.cog.base_unextract, self.cog.base_unextract_all):
            self.assertFalse(await command.checks[0](self.context(staff=False)))
            self.assertTrue(await command.checks[0](self.context()))
        option = self.cog.base_unextract.options[0]
        self.assertEqual(str(option.name), "base_link")
        self.assertTrue(option.required)
        self.assertTrue(option.autocomplete)
        self.assertIn("base_link", self.cog.base_unextract.autocomplete_callbacks)
        ctx = self.context(input_text="wizard")
        await self.cog.base_unextract.autocomplete_callbacks["base_link"](ctx)
        self.assertEqual(ctx.send.await_args.args[0][0]["value"], str(self.base.id))

    async def test_single_uses_access_filter_and_responds_after_committed_removal(self) -> None:
        ctx = self.context()
        events = []

        async def remove(*_args, **_kwargs) -> FingerprintRemoval:
            ctx.defer.assert_awaited_once_with(ephemeral=True)
            ctx.send.assert_not_awaited()
            events.append("committed")
            return FingerprintRemoval(removed=1, cancelled=1)

        self.single.side_effect = remove
        ctx.send.side_effect = lambda *args, **kwargs: events.append("sent")
        await self.invoke(ctx)
        self.query.find.assert_called_once_with({"_id": self.base.id})
        self.model.find.assert_called_once_with({"$or": [
            {"quality": {"$ne": "Private"}}, {"builder_id": "123"},
        ]})
        self.single.assert_awaited_once_with(self.base, self.bot.base_fingerprints.run_lock)
        self.assertEqual(events, ["committed", "sent"])
        self.assertIn("Fingerprint removed", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(base_extraction_module.SUCCESS_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_link_and_already_empty_base_report_actual_outcome(self) -> None:
        self.single.return_value = FingerprintRemoval()
        ctx = self.context()
        await self.invoke(ctx, self.base.link)
        self.query.find.assert_called_once_with({"link": self.base.link})
        self.assertIn("No fingerprint was stored", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(base_extraction_module.SUCCESS_EMOJI)))

    async def test_inaccessible_base_never_mutates_and_failures_never_claim_success(self) -> None:
        self.query.find.return_value.first_or_none.return_value = None
        ctx = self.context()
        await self.invoke(ctx)
        self.single.assert_not_awaited()
        self.assertIn("unavailable", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(base_extraction_module.ERROR_EMOJI)))
        self.query.find.return_value.first_or_none.return_value = self.base
        for error in (AutoReconnect("uncertain write"), FingerprintError("base changed")):
            with self.subTest(error=error):
                self.single.side_effect = error
                ctx = self.context()
                await self.invoke(ctx)
                self.assertNotIn("Fingerprint removed", ctx.send.await_args.args[0])
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(base_extraction_module.ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_bulk_reports_confirmed_counts_changed_records_and_partial_failure(self) -> None:
        self.bulk.return_value = FingerprintRemoval(removed=7, cancelled=2, changed=1)
        ctx = self.context()
        await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.assertEqual(self.bulk.await_args.args[:2],
                         (self.bot.base_fingerprints.run_lock, self.bot.base_fingerprints.gpu.close))
        self.assertIn(str(base_extraction_module.LOADING_EMOJI), ctx.send.await_args.args[0])
        self.assertIn("Removed 7 fingerprints", ctx.edit.await_args.kwargs["content"])
        self.assertIn("1 bases changed", ctx.edit.await_args.kwargs["content"])
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(base_extraction_module.ERROR_EMOJI)))
        self.assertNotIn(str(base_extraction_module.LOADING_EMOJI), ctx.edit.await_args.kwargs["content"])
        ctx.send.assert_awaited_once()
        self.bulk.side_effect = BulkRemovalError(FingerprintRemoval(removed=3))
        ctx = self.context()
        await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.assertIn("incomplete", ctx.edit.await_args.kwargs["content"])
        self.assertIn("Confirmed removal of 3", ctx.edit.await_args.kwargs["content"])
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(base_extraction_module.ERROR_EMOJI)))
        self.assertNotIn(str(base_extraction_module.LOADING_EMOJI), ctx.edit.await_args.kwargs["content"])
        ctx.send.assert_awaited_once()

    async def test_bulk_progress_updates_one_message_with_confirmed_counts_and_throttles_edits(self) -> None:
        ctx = self.context()
        completed = FingerprintRemoval(removed=3, cancelled=1)

        async def remove(_lock, _release, on_progress) -> FingerprintRemoval:
            ctx.send.assert_awaited_once()
            self.assertIn(str(base_extraction_module.LOADING_EMOJI), ctx.send.await_args.args[0])
            await on_progress(FingerprintRemovalProgress(0, 4, FingerprintRemoval()))
            await on_progress(FingerprintRemovalProgress(1, 4, FingerprintRemoval(removed=1)))
            await on_progress(FingerprintRemovalProgress(2, 4, FingerprintRemoval(removed=2)))
            await on_progress(FingerprintRemovalProgress(4, 4, completed))
            self.assertEqual(ctx.edit.await_count, 2)
            self.assertIn("**2 / 4**", ctx.edit.await_args.kwargs["content"])
            self.assertIn("**2** fingerprints removed", ctx.edit.await_args.kwargs["content"])
            return completed

        self.bulk.side_effect = remove
        with patch.object(base_extraction_module.time, "monotonic", side_effect=[10, 10.2, 11.1, 11.2]):
            await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.assertEqual(ctx.edit.await_count, 3)
        for call in ctx.edit.await_args_list:
            self.assertIs(call.args[0], ctx.send.return_value)
        self.assertIn("Removed 3 fingerprints", ctx.edit.await_args.kwargs["content"])
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(base_extraction_module.SUCCESS_EMOJI)))
        self.assertNotIn(str(base_extraction_module.LOADING_EMOJI), ctx.edit.await_args.kwargs["content"])

    async def test_bulk_progress_delivery_failure_keeps_removal_running_and_publishes_final_outcome(self) -> None:
        ctx = self.context()
        ctx.edit.side_effect = [ipy.errors.Forbidden(
            response=SimpleNamespace(status=403, reason="Forbidden"), response_data={}), None]

        async def remove(_lock, _release, on_progress) -> FingerprintRemoval:
            await on_progress(FingerprintRemovalProgress(0, 2, FingerprintRemoval()))
            await on_progress(FingerprintRemovalProgress(2, 2, FingerprintRemoval(removed=2)))
            return FingerprintRemoval(removed=2)

        self.bulk.side_effect = remove
        with self.assertLogs(level="WARNING"):
            await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.bulk.assert_awaited_once()
        self.assertEqual(ctx.edit.await_count, 2)
        self.assertIn("Removed 2 fingerprints", ctx.edit.await_args.kwargs["content"])
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(base_extraction_module.SUCCESS_EMOJI)))

    async def test_bulk_initial_delivery_failure_does_not_start_removal_and_final_failure_does_not_repeat_it(self) -> None:
        ctx = self.context()
        ctx.send.side_effect = RuntimeError("initial delivery failed")
        with self.assertRaisesRegex(RuntimeError, "initial delivery failed"):
            await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.bulk.assert_not_awaited()
        ctx = self.context()
        ctx.edit.side_effect = RuntimeError("final delivery failed")
        with self.assertRaisesRegex(RuntimeError, "final delivery failed"):
            await self.cog.base_unextract_all.callback.func(self.cog, ctx)
        self.bulk.assert_awaited_once()

    async def test_discord_delivery_failure_does_not_repeat_or_reverse_removal(self) -> None:
        ctx = self.context()
        ctx.send.side_effect = RuntimeError("delivery failed")
        with self.assertRaisesRegex(RuntimeError, "delivery failed"):
            await self.invoke(ctx)
        self.single.assert_awaited_once()

