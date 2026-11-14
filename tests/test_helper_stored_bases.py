# noqa: SIZE_OK — stored-base transport and interaction scenarios share one local HTTP/Discord fixture

import unittest
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientSession
import anyio
import interactions as ipy

import cogs.war_hosting.helper_base_store as helper_base_store
import cogs.war_hosting.helper_base_store_availability as helper_base_store_availability
from elobot.integrations.helper_api import HelperEta, HelperHostWarMatchOnStoredPayload, HelperJob, HelperJobCreateAcceptedInvalid, HelperJobCreateRejected, HelperJobCreateUnavailable, HelperJobPoller, HelperTrackingFailure
from cogs.war_hosting.stored_bases import (
    HelperStoredBaseTerminalJob,
    host_war_match_on_stored,
    remove_bases,
    store_bases,
)
from elobot.config.emojis import SUCCESS_EMOJI


from tests._helper_stored_bases_fixture import StoredBaseJobStateMixin

from tests._helper_stored_bases_fixture import JOB_ID, HUMAN_ID

class TestStoredBaseJobRunner(StoredBaseJobStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_201_is_polled_until_terminal_success(self) -> None:
        # Given
        self.status_responses = [
            (200, self.job_document("store_bases", "queued")),
            (200, self.job_document("store_bases", "running")),
            (200, self.job_document("store_bases", "succeeded")),
        ]

        # When
        outcome = await store_bases(self.runtime, HUMAN_ID, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(self.trace, ["create", "poll:queued", "poll:running", "poll:succeeded"])

    async def test_eta_routing_covers_store_start_and_remove_jobs(self) -> None:
        # Given
        eta = HelperEta(estimate_start_at=1200.0, estimate_end_at=1300.0)
        cases = (
            (helper_base_store_availability.confirm_store_bases_availability, "store_bases"),
            (
                helper_base_store_availability.confirm_host_war_match_on_stored_availability,
                "host_war_match_on_stored",
            ),
            (helper_base_store_availability.confirm_remove_bases_availability, "remove_bases"),
        )

        for confirm_availability, job_type in cases:
            with self.subTest(job_type=job_type):
                ctx, _ = self.slash_context()
                reader = MagicMock()
                reader.return_value.read = AsyncMock(return_value=eta)
                confirm_queue = AsyncMock(return_value=False)
                clock = MagicMock()
                clock.time.return_value = 1000.0
                with (
                        patch.object(helper_base_store_availability, "HelperEtaReader", new=reader),
                        patch.object(
                            helper_base_store_availability,
                            "confirm_helper_queue",
                            new=confirm_queue,
                        ),
                        patch.object(helper_base_store_availability, "time", new=clock),
                ):
                    # When
                    available = await confirm_availability(self.runtime, ctx)

                # Then
                self.assertFalse(available)
                reader.assert_called_once_with(
                    self.runtime.helper_api_base_url,
                    self.runtime.helper_api_key,
                    job_type,
                )
                self.assertEqual(confirm_queue.await_args.args[:3], (self.runtime.bot, ctx, 1200.0))
                self.assertEqual(self.trace, [])

    async def test_interactive_store_bases_shows_cancel_and_live_progress(self) -> None:
        # Given
        self.status_responses = [
            (200, self.job_document("store_bases", "queued")),
            (200, self.job_document("store_bases", "running")),
            (200, self.job_document("store_bases", "succeeded")),
        ]
        ctx, progress_message = self.slash_context()

        async def wait_forever(**_kwargs):
            with anyio.fail_after(5):
                await anyio.sleep_forever()

        self.runtime.bot.wait_for_component = AsyncMock(side_effect=wait_forever)

        # When
        outcome = await helper_base_store.store_bases_interactively(self.runtime, ctx, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "succeeded")
        cancel_button = ctx.send.await_args.kwargs["components"]
        self.assertEqual(cancel_button.style, ipy.ButtonStyle.DANGER)
        self.assertEqual(cancel_button.custom_id, f"helper_base_store_cancel|{JOB_ID}")
        self.assertEqual(
            [call.kwargs["components"] for call in ctx.edit.await_args_list],
            [cancel_button, cancel_button, []],
        )
        self.assertTrue(all(call.args[0] is progress_message for call in ctx.edit.await_args_list))
        self.assertEqual(self.trace, ["create", "poll:queued", "poll:running", "poll:succeeded"])

    async def test_remote_success_leaves_final_confirmation_to_the_caller(self) -> None:
        # Given
        cases = (
            ("store_bases", helper_base_store.store_bases_interactively, self.store_payload()),
            (
                "host_war_match_on_stored",
                helper_base_store.host_war_match_on_stored_interactively,
                self.host_payload(),
            ),
            ("remove_bases", helper_base_store.remove_bases_interactively, self.remove_payload()),
        )

        async def wait_forever(
                *,
                components: ipy.Button,
                messages: int,
                check: Callable[[ipy.events.Component], bool],
        ) -> None:
            with anyio.fail_after(5):
                await anyio.sleep_forever()

        self.runtime.bot.wait_for_component = AsyncMock(side_effect=wait_forever)
        for job_type, run_job, payload in cases:
            with self.subTest(job_type=job_type):
                self.status_responses = [(200, self.job_document(job_type, "succeeded"))]
                ctx, _ = self.slash_context()

                # When
                outcome = await run_job(self.runtime, ctx, payload)

                # Then
                self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
                self.assertEqual(outcome.status, "succeeded")
                self.assertNotIn(str(SUCCESS_EMOJI), ctx.edit.await_args.kwargs["content"])
                self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_requester_cancel_cancels_interactive_store_job(self) -> None:
        # Given
        ctx, progress_message = self.slash_context()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=HUMAN_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()

        async def wait_for_component(**kwargs):
            check = kwargs["check"]
            outsider = SimpleNamespace(ctx=SimpleNamespace(author=SimpleNamespace(id="other")))
            requester = SimpleNamespace(ctx=component_ctx)
            self.assertFalse(check(outsider))
            self.assertTrue(check(requester))
            return requester

        cancellation_reported = anyio.Event()

        async def record_progress(
                _message: ipy.Message,
                *,
                content: str,
                components: ipy.Button | list[ipy.Button],
        ) -> None:
            cancellation_reported.set()

        ctx.edit.side_effect = record_progress

        async def poll_until_cancelled(
                _poller: HelperJobPoller,
                _session: ClientSession,
                _job_id: str,
                update_progress: Callable[[HelperJob], Awaitable[None]],
        ) -> HelperJob:
            with anyio.fail_after(5):
                await cancellation_reported.wait()
            running = HelperJob.model_validate(self.job_document("store_bases", "running"))
            await update_progress(running)
            cancelled = HelperJob.model_validate(self.job_document("store_bases", "cancelled"))
            await update_progress(cancelled)
            return cancelled

        self.runtime.bot.wait_for_component = AsyncMock(side_effect=wait_for_component)
        with patch.object(helper_base_store.HelperJobPoller, "wait", new=poll_until_cancelled):
            # When
            outcome = await helper_base_store.store_bases_interactively(self.runtime, ctx, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(outcome.id, JOB_ID)
        self.assertEqual(self.cancelled_job_ids, [JOB_ID])
        cancel_button = ctx.send.await_args.kwargs["components"]
        self.assertTrue(cancel_button.disabled)
        component_ctx.edit_origin.assert_awaited_once_with(components=cancel_button)
        self.assertEqual([call.kwargs["components"] for call in ctx.edit.await_args_list], [[], [], []])
        self.assertIs(ctx.edit.await_args.args[0], progress_message)
        self.assertIn(JOB_ID, ctx.edit.await_args.kwargs["content"])
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_start_cancel_disappears_at_first_district_setup(self) -> None:
        # Given
        ctx, _ = self.component_context()
        progress_updated = anyio.Event()
        stale_click_handled = anyio.Event()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=HUMAN_ID)

        async def edit_origin(**_kwargs):
            stale_click_handled.set()

        component_ctx.edit_origin = AsyncMock(side_effect=edit_origin)

        async def wait_for_component(**_kwargs):
            with anyio.fail_after(5):
                await progress_updated.wait()
            return SimpleNamespace(ctx=component_ctx)

        async def poll_until_success(_poller, _session, _job_id, update_progress):
            running = self.job_document("host_war_match_on_stored", "running")
            running["events"] = [{"t": 101.0, "message": "Setting up Barbarian Camp (1 of 3)"}]
            await update_progress(helper_base_store.HelperJob.model_validate(running))
            running["events"] = []
            await update_progress(helper_base_store.HelperJob.model_validate(running))
            progress_updated.set()
            with anyio.fail_after(5):
                await stale_click_handled.wait()
            succeeded = self.job_document("host_war_match_on_stored", "succeeded")
            succeeded_job = helper_base_store.HelperJob.model_validate(succeeded)
            await update_progress(succeeded_job)
            return succeeded_job

        self.runtime.bot.wait_for_component = AsyncMock(side_effect=wait_for_component)
        cancel = AsyncMock()
        with (
                patch.object(helper_base_store.HelperJobPoller, "wait", new=poll_until_success),
                patch.object(helper_base_store.HelperJobCanceller, "cancel", new=cancel),
        ):
            # When
            outcome = await helper_base_store.host_war_match_on_stored_interactively(
                self.runtime,
                ctx,
                self.host_payload(),
            )

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "succeeded")
        cancel.assert_not_awaited()
        component_ctx.edit_origin.assert_awaited_once_with(components=[])
        self.assertEqual([call.kwargs["components"] for call in ctx.edit.await_args_list], [[], [], []])

    async def test_requester_cancel_cancels_interactive_remove_job(self) -> None:
        # Given
        ctx, _ = self.component_context()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=HUMAN_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()
        self.runtime.bot.wait_for_component = AsyncMock(return_value=SimpleNamespace(ctx=component_ctx))

        cancellation_reported = anyio.Event()

        async def record_progress(
                _message: ipy.Message,
                *,
                content: str,
                components: ipy.Button | list[ipy.Button],
        ) -> None:
            cancellation_reported.set()

        ctx.edit.side_effect = record_progress

        async def poll_until_cancelled(
                _poller: HelperJobPoller,
                _session: ClientSession,
                _job_id: str,
                update_progress: Callable[[HelperJob], Awaitable[None]],
        ) -> HelperJob:
            with anyio.fail_after(5):
                await cancellation_reported.wait()
            cancelled = HelperJob.model_validate(self.job_document("remove_bases", "cancelled"))
            await update_progress(cancelled)
            return cancelled

        with patch.object(helper_base_store.HelperJobPoller, "wait", new=poll_until_cancelled):
            # When
            outcome = await helper_base_store.remove_bases_interactively(
                self.runtime,
                ctx,
                self.remove_payload(),
            )

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(self.cancelled_job_ids, [JOB_ID])
        self.assertEqual(self.create_payloads[0]["type"], "remove_bases")
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_human_author_is_requested_by(self) -> None:
        # Given
        self.status_responses = [
            (200, self.job_document("host_war_match_on_stored", "succeeded")),
        ]
        payload = HelperHostWarMatchOnStoredPayload(
            host_clan_tag="#CLAN",
            player_tag="#PLAYER",
            districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            slot=2,
            remove_bases_after=False,
        )

        # When
        await host_war_match_on_stored(self.runtime, HUMAN_ID, payload)

        # Then
        self.assertEqual(self.create_payloads, [{
            "type": "host_war_match_on_stored",
            "requested_by": HUMAN_ID,
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_tag": "#PLAYER",
                "districts": ["Barbarian Camp", "Wizard Valley", "Capital Peak"],
                "slot": 2,
                "remove_bases_after": False,
            },
        }])

    async def test_tracking_failure_is_distinct_from_terminal_failure(self) -> None:
        # Given
        self.status_responses = [(404, {"error": "missing"})]
        terminal_failure = HelperStoredBaseTerminalJob.model_validate(
            self.job_document("remove_bases", "failed"),
        )

        # When
        outcome = await remove_bases(self.runtime, HUMAN_ID, self.remove_payload())

        # Then
        self.assertIsInstance(outcome, HelperTrackingFailure)
        self.assertNotIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(terminal_failure.status, "failed")

    async def test_terminal_failure_remains_a_terminal_job(self) -> None:
        # Given
        self.status_responses = [(200, self.job_document("remove_bases", "failed"))]

        # When
        outcome = await remove_bases(self.runtime, HUMAN_ID, self.remove_payload())

        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "failed")

    async def test_400_is_a_rejected_outcome_without_polling(self) -> None:
        # Given
        self.create_status = 400
        self.create_body = {"error": "slot occupied"}

        # When
        outcome = await store_bases(self.runtime, HUMAN_ID, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperJobCreateRejected)
        self.assertEqual(outcome.error, "slot occupied")
        self.assertEqual(self.trace, ["create"])

    async def test_accepted_invalid_is_an_ambiguous_outcome(self) -> None:
        # Given
        self.create_body = {"unexpected": True}

        # When
        outcome = await store_bases(self.runtime, HUMAN_ID, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperJobCreateAcceptedInvalid)
        self.assertEqual(self.trace, ["create"])

    async def test_unavailable_is_an_ambiguous_outcome(self) -> None:
        # Given
        self.create_status = 503
        self.create_body = "unavailable"

        # When
        outcome = await store_bases(self.runtime, HUMAN_ID, self.store_payload())

        # Then
        self.assertIsInstance(outcome, HelperJobCreateUnavailable)
        self.assertEqual(self.trace, ["create"])


if __name__ == "__main__":
    unittest.main()
