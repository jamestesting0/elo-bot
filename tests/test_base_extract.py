import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import interactions as ipy
from beanie import PydanticObjectId
from pymongo.errors import AutoReconnect

import cogs.base_commands as commands
from common.base_fingerprints.types import ExtractionState, FingerprintError
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import WHITELISTED_STAFFS


from tests._base_extract_fixture import BaseExtractStateMixin

class TestBaseExtract(BaseExtractStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_command_registers_staff_check_and_string_autocomplete(self):
        command = self.cog.base_extract
        option = command.options[0]
        self.assertEqual(str(option.name), "base_link")
        self.assertEqual(option.type, ipy.OptionType.STRING)
        self.assertTrue(option.required)
        self.assertTrue(option.autocomplete)
        self.assertIn("base_link", command.autocomplete_callbacks)
        ctx = self.context(staff=False)
        self.assertFalse(await command.checks[0](ctx))
        ctx.author.id = WHITELISTED_STAFFS[0]
        self.assertTrue(await command.checks[0](ctx))

    async def test_selects_exact_id_and_acknowledges_after_durable_request(self):
        ctx = self.context()
        events = []

        async def save(base):
            self.assertIs(base, self.base)
            ctx.defer.assert_awaited_once_with(ephemeral=True)
            ctx.send.assert_not_awaited()
            events.append("saved")
            return self.state, True

        self.bot.base_fingerprints.request_extraction.side_effect = save
        ctx.send.side_effect = lambda *args, **kwargs: events.append("sent")
        await self.invoke(ctx)
        self.query.find.assert_called_once_with({"_id": self.base.id})
        self.assertEqual(events, ["saved", "sent"])
        self.assertIn("queued on CPU", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertIn(self.state.request_id, ctx.send.await_args.args[0])
        self.assertIn("/base extract_status job_id:", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.model.find.assert_called_once_with({"$or": [
            {"quality": {"$ne": "Private"}}, {"builder_id": "123"},
        ]})

    async def test_pasted_link_and_existing_request_report_pending(self):
        self.bot.base_fingerprints.request_extraction.return_value = (self.state, False)
        ctx = self.context()
        await self.invoke(ctx, self.base.link)
        self.query.find.assert_called_once_with({"link": self.base.link})
        self.assertIn("already pending", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertNotIn("queued on CPU", ctx.send.await_args.args[0])

    async def test_missing_private_or_deleted_selection_never_requests_extraction(self):
        self.query.find.return_value.first_or_none.return_value = None
        ctx = self.context()
        await self.invoke(ctx)
        self.bot.base_fingerprints.request_extraction.assert_not_awaited()
        self.assertIn("unavailable", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_model_and_database_failures_do_not_announce_queued_work(self):
        for error in (FingerprintError("model_missing"), AutoReconnect("write failed")):
            with self.subTest(error=type(error).__name__):
                self.bot.base_fingerprints.request_extraction.side_effect = error
                ctx = self.context()
                await self.invoke(ctx)
                self.assertNotIn("queued", ctx.send.await_args.args[0])
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
                self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_failed_discord_acknowledgement_does_not_resubmit_or_cancel(self):
        ctx = self.context()
        ctx.send.side_effect = ipy.errors.Forbidden(response=SimpleNamespace(status=403, reason="Forbidden"),
                                                   response_data={})
        with self.assertRaises(ipy.errors.Forbidden):
            await self.invoke(ctx)
        self.bot.base_fingerprints.request_extraction.assert_awaited_once_with(self.base)

    async def test_autocomplete_has_stable_ids_and_cached_builder_names(self):
        ctx = self.context(input_text="wizard")
        await self.cog.base_extract_autocomplete(ctx)
        choices = ctx.send.await_args.args[0]
        self.assertEqual(choices, [{"name": f"Wizard Valley by @builder ({str(self.base.id)[-6:]})",
                                    "value": str(self.base.id)}])
        self.query.sort.return_value.project.assert_called_once_with(commands.BaseExtractionAutocompleteRow)
        self.bot.fetch_user.assert_not_called()

    async def test_autocomplete_requires_staff_and_limits_choices(self):
        ctx = self.context(staff=False)
        await self.cog.base_extract_autocomplete(ctx)
        ctx.send.assert_awaited_once_with([])
        self.model.find.assert_not_called()
        self.query.sort.return_value.project.return_value.to_list.return_value = [
            SimpleNamespace(id=PydanticObjectId(), district="Barbarian Camp", builder_id="123", link=f"https://x/{i}")
            for i in range(30)
        ]
        ctx = self.context()
        await self.cog.base_extract_autocomplete(ctx)
        self.assertEqual(len(ctx.send.await_args.args[0]), 25)
        ctx.author.id = self.bot.owner.id
        await self.cog.base_extract_autocomplete(ctx)
        self.model.find_all.assert_called_once_with()

    async def test_status_reads_pending_outbox_without_starting_work(self):
        ctx = self.context()
        await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
        self.query.find.assert_called_once_with({"fingerprint_extraction.request_id": self.state.request_id})
        self.assertIn("**pending**", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.bot.base_fingerprints.request_extraction.assert_not_awaited()

    async def test_status_reports_task_outcomes_and_error(self):
        self.base.fingerprint_extraction = None
        for status in ("running", "succeeded", "failed", "superseded"):
            with self.subTest(status=status):
                error = "source_unavailable" if status == "failed" else None
                self.tasks.get.return_value = SimpleNamespace(base_id=self.base.id, state=status, error_code=error)
                ctx = self.context()
                await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
                self.query.find.assert_called_with({"_id": self.base.id})
                self.assertIn(f"**{status}**", ctx.send.await_args.args[0])
                emoji = (LOADING_EMOJI if status == "running" else
                         SUCCESS_EMOJI if status == "succeeded" else ERROR_EMOJI)
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(emoji)))
                if error:
                    self.assertIn("Error: `source_unavailable`", ctx.send.await_args.args[0])

    async def test_status_uses_committed_receipt_while_task_finalization_is_pending(self):
        self.tasks.get.return_value = SimpleNamespace(base_id=self.base.id, state="running", error_code=None)
        for status, expected, error in (("ready", "succeeded", None), ("failed", "failed", "invalid_image")):
            with self.subTest(status=status):
                self.base.fingerprint_extraction = self.state.model_copy(update={
                    "status": status, "completion_receipt": "committed", "error_code": error,
                })
                ctx = self.context()
                await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
                self.assertIn(f"**{expected}**", ctx.send.await_args.args[0])
                emoji = SUCCESS_EMOJI if expected == "succeeded" else ERROR_EMOJI
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(emoji)))

    async def test_status_does_not_report_a_newer_requests_result(self):
        self.tasks.get.return_value = SimpleNamespace(base_id=self.base.id, state="superseded", error_code=None)
        self.base.fingerprint_extraction = ExtractionState(source_revision=5, status="ready", completion_receipt="new")
        ctx = self.context()
        await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
        self.assertIn("**superseded**", ctx.send.await_args.args[0])
        self.assertNotIn("succeeded", ctx.send.await_args.args[0])

    async def test_status_hides_missing_deleted_and_inaccessible_bases(self):
        self.query.find.return_value.first_or_none.return_value = None
        for task in (None, SimpleNamespace(base_id=self.base.id, state="deleted", error_code=None)):
            with self.subTest(task=task):
                self.tasks.get.return_value = task
                ctx = self.context()
                await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
                self.assertEqual(ctx.send.await_args.args[0],
                                 f"{ERROR_EMOJI} Job or run not found, or its base is unavailable to you.")
                self.assertNotIn(self.base.district, ctx.send.await_args.args[0])
        self.model.find.assert_called_with({"$or": [
            {"quality": {"$ne": "Private"}}, {"builder_id": "123"},
        ]})

    async def test_status_invalid_id_and_database_failure_are_truthful(self):
        ctx = self.context()
        await self.cog.base_extract_status.callback.func(self.cog, ctx, "invalid")
        self.tasks.get.assert_not_awaited()
        self.assertIn("Invalid job or run ID", ctx.send.await_args.args[0])
        self.tasks.get.side_effect = AutoReconnect("unavailable")
        await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
        self.assertEqual(ctx.send.await_args.args[0],
                         f"{ERROR_EMOJI} Job status is temporarily unavailable. Please retry.")

    async def test_run_status_and_bulk_extraction_mark_current_outcomes(self):
        self.bot.base_fingerprints.status = AsyncMock(side_effect=lambda run: f"Fingerprint run: **{run.status}**.")
        self.bot.base_fingerprints.begin_run = AsyncMock()
        self.bot.base_fingerprints.publish = AsyncMock()
        self.runs.find.return_value.sort.return_value.first_or_none = AsyncMock(return_value=None)
        for status, emoji in (("running", LOADING_EMOJI), ("completed", SUCCESS_EMOJI),
                              ("completed-with-errors", ERROR_EMOJI)):
            with self.subTest(status=status):
                run = SimpleNamespace(status=status, notification_error=None)
                self.runs.get.return_value = run
                ctx = self.context()
                await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
                self.assertEqual(ctx.send.await_args.args[0], f"{emoji} Fingerprint run: **{status}**.")
                self.bot.base_fingerprints.begin_run.return_value = (run, False)
                ctx = self.context()
                await self.cog.base_extract_all.callback.func(self.cog, ctx)
                self.assertEqual(ctx.send.await_args.args[0], f"{emoji} Fingerprint run: **{status}**.")

    async def test_run_status_marks_notification_error_separately(self):
        run = SimpleNamespace(status="completed", notification_error="Forbidden")
        self.runs.get.return_value = run
        self.bot.base_fingerprints.status = AsyncMock(return_value="Fingerprint run complete.")
        ctx = self.context()
        await self.cog.base_extract_status.callback.func(self.cog, ctx, self.state.request_id)
        self.assertEqual(ctx.send.await_args.args[0],
                         f"{SUCCESS_EMOJI} Fingerprint run complete.\n"
                         f"{ERROR_EMOJI} Channel updates unavailable: `Forbidden`.")

    async def test_status_requires_staff_and_normalizes_the_job_id(self):
        command = self.cog.base_extract_status
        ctx = self.context(staff=False)
        self.assertFalse(await command.checks[0](ctx))
        ctx.author.id = WHITELISTED_STAFFS[0]
        self.assertTrue(await command.checks[0](ctx))
        await command.callback.func(self.cog, ctx, f" {self.state.request_id.upper()} ")
        self.tasks.get.assert_awaited_once_with(self.state.request_id)
