# noqa: SIZE_OK - ETA scenarios share one local HTTP server fixture
import unittest
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from aiohttp import web
import anyio
import interactions as ipy

from elobot.features.hosting.war.confirmation import confirm_helper_queue
from elobot.integrations.helper_api import HelperEtaReader, HelperJobCanceller, HelperTrackingFailure
from elobot.features.hosting.war.helper_host import AutomaticWarHost, host_war_automatically
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["queued", "cancelled"]


class TestHelperEtaFlow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.eta_status = 200
        self.eta_body: dict[str, JSONValue] = {
            "estimate_start_at": 1000.0,
            "estimate_end_at": 1180.0,
        }
        self.events: list[str] = []
        self.eta_query_type: str | None = None
        self.eta_api_key: str | None = None
        self.created_jobs = 0
        self.cancel_status = 200
        self.cancel_paths: list[str] = []
        self.cancel_api_keys: list[str | None] = []
        app = web.Application()
        app.router.add_get("/v1/eta", self.handle_eta)
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.handle_cancel)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]

        self.bot = MagicMock()
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(id=200))
        self.bot.wait_for_component = AsyncMock(side_effect=self.wait_for_component_forever)
        self.runtime = MagicMock(
            bot=self.bot,
            helper_api_base_url=f"http://127.0.0.1:{port}",
            helper_api_key="test-key",
        )
        self.start_war = AsyncMock(return_value=MagicMock(id=777))
        self.abort_war_start = AsyncMock(return_value=True)
        self.poll = AsyncMock()

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle_eta(self, request: web.Request) -> web.Response:
        self.events.append("eta")
        self.eta_query_type = request.query.get("type")
        self.eta_api_key = request.headers.get("X-Api-Key")
        return web.json_response(self.eta_body, status=self.eta_status)

    async def handle_create(self, _request: web.Request) -> web.Response:
        self.events.append("post")
        self.created_jobs += 1
        return web.json_response({
            **self.job_document("queued"),
            "estimate_start_at": 1000.0,
            "estimate_end_at": 1180.0,
        }, status=201)

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self.events.append("cancel")
        self.cancel_paths.append(request.path)
        self.cancel_api_keys.append(request.headers.get("X-Api-Key"))
        return web.json_response(self.job_document("cancelled"), status=self.cancel_status)

    @staticmethod
    async def wait_for_component_forever(*_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await anyio.Event().wait()

    @staticmethod
    def job_document(status: JobStatus) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": "host_war_match",
            "status": status,
            "created_at": 1000.0,
            "started_at": None,
            "finished_at": 1002.0 if status == "cancelled" else None,
            "events": [],
            "result": None,
            "error": None,
        }

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID, mention=f"<@{REQUESTER_ID}>")
        progress_message = MagicMock(spec=ipy.Message)
        progress_message.id = 902
        ctx.send = AsyncMock(return_value=progress_message)
        ctx.edit = AsyncMock()
        ctx.delete = AsyncMock()
        return ctx

    @staticmethod
    def make_match() -> MagicMock:
        caller = MagicMock(user_id=REQUESTER_ID, bases=[])
        opponent = MagicMock(
            user_id="200",
            bases=[MagicMock(link="https://base.one"), MagicMock(link="https://base.two")],
        )
        return MagicMock(
            participants=[caller, opponent],
            token="match-token",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
        )

    def select_component(self, custom_id: str) -> ipy.ComponentContext:
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.custom_id = custom_id
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()

        async def wait_for_selected_component(*_args, components, **_kwargs):
            nested_components = getattr(components, "components", [components])
            if any(component.custom_id == custom_id for component in nested_components):
                return MagicMock(ctx=component_ctx)
            with anyio.fail_after(5):
                await anyio.Event().wait()

        self.bot.wait_for_component.side_effect = wait_for_selected_component
        return component_ctx

    async def invoke(self, ctx: ipy.SlashContext, match: MagicMock) -> None:
        outcome = HelperTrackingFailure(job_id=JOB_ID, reason="test monitoring stop")
        request = AutomaticWarHost(
            match=match,
            player=0,
            clan=MagicMock(tag="#2RY0QP8GY"),
            host=MagicMock(id=300),
        )
        with patch("elobot.features.hosting.war.helper_host.Player") as player_model, \
                patch("elobot.features.hosting.war.helper_host.get_available_helper_slot", new=AsyncMock(return_value=1)), \
                patch("elobot.extensions.match.choose_bases", new=AsyncMock(return_value=match)), \
                patch(
                    "elobot.features.hosting.war.helper_host_job.time",
                    new=MagicMock(time=MagicMock(return_value=1000.0)),
                ), \
                patch("elobot.features.hosting.war.war_start.start_war", new=self.start_war), \
                patch("elobot.features.hosting.war.war_start.abort_war_start", new=self.abort_war_start), \
                patch("elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait", new=self.poll):
            player_model.find_one = AsyncMock(return_value=MagicMock(tag="#CALLER"))
            self.poll.return_value = outcome
            if not self.start_war.side_effect:
                self.start_war.side_effect = lambda *_args: self.events.append("start") or MagicMock(id=777)
            await host_war_automatically(self.runtime, ctx, request)

    async def test_job_canceller_posts_validated_id(self) -> None:
        async with aiohttp.ClientSession() as session:
            result = await HelperJobCanceller(self.runtime.helper_api_base_url, "test-key").cancel(session, JOB_ID)

        self.assertIsNone(result)
        self.assertEqual(self.events, ["cancel"])
        self.assertEqual(self.cancel_paths, [f"/v1/jobs/{JOB_ID}/cancel"])
        self.assertEqual(self.cancel_api_keys, ["test-key"])

    async def test_start_failure_prevents_helper_job_and_reraises(self) -> None:
        ctx = self.make_context()
        sentinel = RuntimeError("local start failed")

        async def fail_start(*_args) -> None:
            self.events.append("start")
            raise sentinel

        self.start_war.side_effect = fail_start
        with self.assertRaises(RuntimeError) as raised:
            await self.invoke(ctx, self.make_match())

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(self.events, ["eta", "start"])
        self.assertEqual(self.created_jobs, 0)
        self.assertEqual(self.cancel_paths, [])
        self.poll.assert_not_awaited()
        self.abort_war_start.assert_not_awaited()
        ctx.send.assert_not_awaited()

    async def test_unavailable_start_stops_before_helper_job(self) -> None:
        # Given a local resource-clan claim that returns unavailable.
        ctx = self.make_context()
        self.start_war.side_effect = lambda *_args: self.events.append("start") or None

        with patch(
                "elobot.features.hosting.war.helper_host.match_commands.available_resource_clans_text",
                new=AsyncMock(return_value="Available clans:\n- None"),
        ) as available_clans:
            # When automatic hosting tries to commit the local start.
            await self.invoke(ctx, self.make_match())

        # Then Helper is neither submitted nor polled.
        self.assertEqual(self.events, ["eta", "start"])
        self.assertEqual(self.created_jobs, 0)
        self.assertEqual(self.cancel_paths, [])
        self.poll.assert_not_awaited()
        available_clans.assert_awaited_once()
        self.assertIn("Available clans:\n- None", ctx.send.await_args.args[0])

    async def test_committed_start_failure_prevents_helper_job(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        sentinel = RuntimeError("local finalization failed")

        async def fail_after_commit(*_args) -> None:
            self.events.append("start")
            match.times[0], match.hosts[0], match.host_clans[0] = 1000, "300", "#2RY0QP8GY"
            raise sentinel

        self.start_war.side_effect = fail_after_commit
        with self.assertRaises(RuntimeError) as raised:
            await self.invoke(ctx, match)

        self.assertIs(raised.exception, sentinel)
        self.assertEqual((self.events, self.cancel_paths), (["eta", "start"], []))
        self.assertEqual(self.created_jobs, 0)
        self.poll.assert_not_awaited()
        ctx.send.assert_not_awaited()


    async def test_cancelled_start_stops_before_helper_job_and_propagates(self) -> None:
        ctx = self.make_context()
        cancellation_error = anyio.get_cancelled_exc_class()
        sentinel = cancellation_error()

        async def cancel_start(*_args) -> None:
            self.events.append("start")
            raise sentinel

        self.start_war.side_effect = cancel_start
        with self.assertRaises(cancellation_error) as raised:
            await self.invoke(ctx, self.make_match())

        self.assertIs(raised.exception, sentinel)
        self.assertEqual(self.events, ["eta", "start"])
        self.assertEqual(self.created_jobs, 0)
        self.assertEqual(self.cancel_paths, [])
        self.poll.assert_not_awaited()

    async def test_submits_without_prompt_when_eta_is_current(self) -> None:
        # Given
        ctx = self.make_context()

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta", "start", "post"])
        self.assertEqual(self.eta_query_type, "host_war_match")
        self.assertEqual(self.eta_api_key, "test-key")
        self.bot.wait_for_component.assert_awaited_once()
        self.assertEqual(
            self.bot.wait_for_component.await_args.kwargs["components"].custom_id,
            f"helper_job_cancel|{JOB_ID}",
        )
        self.start_war.assert_awaited_once()

    async def test_submits_without_prompt_when_eta_is_thirty_seconds_away(self) -> None:
        # Given
        self.eta_body = {"estimate_start_at": 1030.0, "estimate_end_at": 1210.0}
        ctx = self.make_context()
        self.select_component("helper_eta_continue")

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta", "start", "post"])
        self.bot.wait_for_component.assert_awaited_once()
        self.assertEqual(
            self.bot.wait_for_component.await_args.kwargs["components"].custom_id,
            f"helper_job_cancel|{JOB_ID}",
        )
        self.start_war.assert_awaited_once()

    async def test_continue_submits_after_prompt_when_eta_is_future(self) -> None:
        # Given
        self.eta_body = {"estimate_start_at": 1300.0, "estimate_end_at": 1480.0}
        ctx = self.make_context()
        prompt = MagicMock(spec=ipy.Message)
        prompt.id = 901
        progress_message = ctx.send.return_value
        ctx.send.side_effect = [prompt, progress_message]
        component_ctx = self.select_component("helper_eta_continue")

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta", "start", "post"])
        wait_call = self.bot.wait_for_component.await_args_list[0]
        self.assertEqual(wait_call.kwargs["messages"], 901)
        action_row = wait_call.kwargs["components"]
        self.assertEqual(
            [component.custom_id for component in action_row.components],
            ["helper_eta_continue", "helper_eta_cancel"],
        )
        self.assertEqual(
            [component.emoji for component in action_row.components],
            [ipy.PartialEmoji(name="⏩"), CROSS_EMOJI],
        )
        self.assertIn("<t:1300:R>", ctx.send.await_args_list[0].args[0])
        component_ctx.edit_origin.assert_awaited_once()
        self.start_war.assert_awaited_once()

    async def test_cancel_stops_before_start_or_submit_when_eta_is_future(self) -> None:
        # Given
        self.eta_body = {"estimate_start_at": 1300.0, "estimate_end_at": 1480.0}
        ctx = self.make_context()
        ctx.send.return_value.id = 901
        component_ctx = self.select_component("helper_eta_cancel")

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta"])
        self.assertEqual(self.created_jobs, 0)
        self.start_war.assert_not_awaited()
        component_ctx.edit_origin.assert_awaited_once()

    async def test_timeout_stops_before_start_or_submit_when_eta_is_future(self) -> None:
        # Given
        self.eta_body = {"estimate_start_at": 1300.0, "estimate_end_at": 1480.0}
        ctx = self.make_context()
        prompt = ctx.send.return_value
        prompt.id = 901
        self.bot.wait_for_component.side_effect = TimeoutError

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta"])
        self.start_war.assert_not_awaited()
        ctx.delete.assert_not_awaited()
        ctx.edit.assert_awaited_once_with(
            prompt, content=f"{ERROR_EMOJI} Helper work was not started because the queue confirmation timed out.",
            components=[],
        )
        self.assertEqual(self.created_jobs, 0)

    async def test_queue_timeout_reports_that_hosting_was_not_started(self) -> None:
        bot = MagicMock()
        bot.wait_for_component = AsyncMock(side_effect=TimeoutError)
        ctx = MagicMock()
        prompt = MagicMock(id=901)
        ctx.send = AsyncMock(return_value=prompt)
        ctx.edit = AsyncMock()

        started = await confirm_helper_queue(bot, ctx, 1300.0, ephemeral=False)

        self.assertFalse(started)
        ctx.delete.assert_not_called()
        ctx.edit.assert_awaited_once_with(
            prompt,
            content=f"{ERROR_EMOJI} Helper work was not started because the queue confirmation timed out.",
            components=[],
        )

    async def test_missing_eta_stops_before_start_or_submit(self) -> None:
        # Given
        self.eta_body = {"estimate_start_at": None, "estimate_end_at": None}
        ctx = self.make_context()

        # When
        await self.invoke(ctx, self.make_match())

        # Then
        self.assertEqual(self.events, ["eta"])
        self.bot.wait_for_component.assert_not_awaited()
        self.start_war.assert_not_awaited()
        self.assertEqual(self.created_jobs, 0)

    async def test_fetch_score_eta_uses_the_requested_job_type(self) -> None:
        # Given
        reader = HelperEtaReader(self.runtime.helper_api_base_url, self.runtime.helper_api_key, "fetch_score")

        # When
        outcome = await reader.read()

        # Then
        self.assertEqual(outcome.estimate_start_at, 1000.0)
        self.assertEqual(self.eta_query_type, "fetch_score")
        self.assertEqual(self.eta_api_key, "test-key")


if __name__ == "__main__":
    unittest.main()
