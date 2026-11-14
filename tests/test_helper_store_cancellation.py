import unittest
from collections.abc import Callable
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import anyio
import interactions as ipy

from elo_bot.features.hosting.war import helper_base_store
from elo_bot.integrations import helper_api
from elo_bot.integrations.helper_api import HelperStoreBasesPayload, HelperTrackingFailure
from elo_bot.features.hosting.war.stored_bases import HelperStoredBaseTerminalJob
from elo_bot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["running", "succeeded", "failed", "cancelled"]


class TestHelperStoreCancellation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.final_status: Literal["succeeded", "failed", "cancelled"] = "succeeded"
        self.tracking_failure = False
        self.first_progress = anyio.Event()
        self.cancel_requested = anyio.Event()
        self.cancel_reported = anyio.Event()
        self.release_terminal = anyio.Event()
        self.trace: list[str] = []
        self.progress: list[tuple[str, bool]] = []
        self.create_payloads: list[dict[str, JSONValue]] = []
        self.poll_count = 0
        self.progress_message = MagicMock(spec=ipy.Message, id=123456)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author = MagicMock(id=REQUESTER_ID)
        self.ctx.send = AsyncMock(return_value=self.progress_message)
        self.ctx.edit = AsyncMock(side_effect=self.record_progress)
        self.component_ctx = MagicMock(spec=ipy.ComponentContext)
        self.component_ctx.author = MagicMock(id=REQUESTER_ID)
        self.component_ctx.edit_origin = AsyncMock()
        self.component_ctx.send = AsyncMock()
        app = web.Application()
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.handle_cancel)
        app.router.add_get("/v1/jobs/{job_id}", self.handle_status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]
        self.runtime = SimpleNamespace(
            helper_api_base_url=f"http://127.0.0.1:{port}",
            helper_api_key="test-key",
            bot=SimpleNamespace(wait_for_component=self.click_cancel),
        )

    async def asyncTearDown(self) -> None:
        self.release_terminal.set()
        self.cancel_reported.set()
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.create_payloads.append(await request.json())
        self.trace.append("create")
        return web.json_response({"id": JOB_ID}, status=201)

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self.assertEqual(request.match_info["job_id"], JOB_ID)
        self.assertEqual(request.headers["X-Api-Key"], "test-key")
        self.assertEqual(await request.read(), b"")
        status = "cancelled" if self.tracking_failure else self.final_status
        self.trace.append(f"cancel:200:{status}")
        self.cancel_requested.set()
        return web.json_response(self.job_document(status))

    async def handle_status(self, _request: web.Request) -> web.Response:
        self.poll_count += 1
        if self.poll_count > 1:
            with anyio.fail_after(5):
                await self.release_terminal.wait()
            if self.tracking_failure:
                self.trace.append("poll:404")
                return web.json_response({"error": "missing"}, status=404)
        status = "running" if self.poll_count == 1 else self.final_status
        self.trace.append(f"poll:{status}")
        return web.json_response(self.job_document(status))

    @staticmethod
    def job_document(status: JobStatus) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": "store_bases",
            "status": status,
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": None if status == "running" else 102.0,
            "events": [],
            "result": None,
            "error": "device failed" if status == "failed" else None,
        }

    async def click_cancel(
            self,
            *,
            check: Callable[[ipy.events.Component], bool],
            components: ipy.Button,
            messages: int,
    ) -> ipy.events.Component:
        with anyio.fail_after(5):
            await self.first_progress.wait()
        self.assertEqual(components.custom_id, f"helper_base_store_cancel|{JOB_ID}")
        self.assertEqual(messages, self.progress_message.id)
        outsider = MagicMock(spec=ipy.events.Component)
        outsider.ctx.author.id = "other"
        requester = MagicMock(spec=ipy.events.Component)
        requester.ctx = self.component_ctx
        self.assertFalse(check(outsider))
        self.assertTrue(check(requester))
        return requester

    async def record_progress(
            self,
            message: ipy.Message,
            *,
            content: str,
            components: ipy.Button | list[ipy.Button],
    ) -> None:
        self.assertIs(message, self.progress_message)
        self.progress.append((content, bool(components)))
        self.first_progress.set()
        if self.cancel_requested.is_set() and not components:
            self.cancel_reported.set()

    async def run_cancellation(self) -> helper_base_store.HelperStoredBaseInteractiveOutcome:
        outcomes: list[helper_base_store.HelperStoredBaseInteractiveOutcome] = []

        async def run_job() -> None:
            outcomes.append(await helper_base_store.store_bases_interactively(
                self.runtime,
                self.ctx,
                HelperStoreBasesPayload(host_clan_tag="#CLAN", base_links=["https://example.com/base"], slot=1),
            ))

        with patch.object(helper_api, "HELPER_POLL_INTERVAL_SECONDS", 0), anyio.fail_after(5):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(run_job)
                await self.cancel_reported.wait()
                premature_outcomes = list(outcomes)
                self.release_terminal.set()

        self.assertEqual(premature_outcomes, [], "HTTP success alone must not complete the stored-base job")
        cancel_status = "cancelled" if self.tracking_failure else self.final_status
        self.assertEqual(self.trace[:3], ["create", "poll:running", f"cancel:200:{cancel_status}"])
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(self.progress[0][0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(self.progress[1][0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(all(not visible for _, visible in self.progress[1:]))
        self.assertEqual(self.create_payloads, [{
            "type": "store_bases",
            "requested_by": REQUESTER_ID,
            "payload": {"host_clan_tag": "#CLAN", "base_links": ["https://example.com/base"], "slot": 1},
        }])
        return outcomes[0]

    async def test_returns_success_when_completion_wins_the_cancel_race(self) -> None:
        # Given
        self.final_status = "succeeded"
        # When
        outcome = await self.run_cancellation()
        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "succeeded")
        self.assertEqual(self.trace[-1], "poll:succeeded")
        self.assertNotIn(str(SUCCESS_EMOJI), self.progress[-1][0])

    async def test_returns_cancelled_only_after_confirmed_terminal_cancellation(self) -> None:
        # Given
        self.final_status = "cancelled"
        # When
        outcome = await self.run_cancellation()
        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "cancelled")
        self.assertEqual(self.trace[-1], "poll:cancelled")
        self.assertTrue(self.progress[-1][0].startswith(str(ERROR_EMOJI)))

    async def test_returns_uncertainty_when_tracking_fails_after_cancel_response(self) -> None:
        # Given
        self.tracking_failure = True
        # When
        outcome = await self.run_cancellation()
        # Then
        self.assertIsInstance(outcome, HelperTrackingFailure)
        self.assertEqual(outcome.job_id, JOB_ID)
        self.assertEqual(self.trace[-1], "poll:404")
        self.assertTrue(self.progress[-1][0].startswith(str(ERROR_EMOJI)))

    async def test_returns_failure_when_cancel_returns_an_already_failed_job(self) -> None:
        # Given
        self.final_status = "failed"
        # When
        outcome = await self.run_cancellation()
        # Then
        self.assertIsInstance(outcome, HelperStoredBaseTerminalJob)
        self.assertEqual(outcome.status, "failed")
        self.assertEqual(outcome.error, "device failed")
        self.assertEqual(self.trace[-1], "poll:failed")
        self.assertTrue(self.progress[-1][0].startswith(str(ERROR_EMOJI)))
