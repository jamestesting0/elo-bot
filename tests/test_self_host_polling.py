# noqa: SIZE_OK - polling scenarios share one local HTTP server fixture
import unittest
from typing import Literal, assert_never
from unittest.mock import AsyncMock, MagicMock, call, patch

from aiohttp import web
import anyio
import interactions as ipy

import elobot.features.hosting.war.helper_host as helper_host
from elobot.features.hosting.war import result_store_task
from elobot.extensions.self_service import SelfCommands
from elobot.integrations.helper_api import HelperJob
from elobot.config.emojis import CROSS_EMOJI, ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from tests._patch_sites import patch_sites


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
BOT_USER_ID = "987654321098765432"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class TestSelfHostPolling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.events: list[str] = []
        self.status_responses: list[tuple[int, dict[str, JSONValue] | str]] = []
        self.status_paths: list[str] = []
        self.status_api_keys: list[str | None] = []
        self.create_payloads: list[dict[str, JSONValue]] = []
        self.create_api_keys: list[str | None] = []
        self.cancel_paths: list[str] = []
        self.cancel_api_keys: list[str | None] = []
        self.cancel_status = 200
        self.cancel_job_status: JobStatus = "cancelled"
        app = web.Application()
        eta_response = web.json_response({"estimate_start_at": 0.0, "estimate_end_at": 180.0})
        app.router.add_get("/v1/eta", AsyncMock(return_value=eta_response))
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_get("/v1/jobs/{job_id}", self.handle_status)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.handle_cancel)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]
        self.bot = MagicMock()
        self.bot.user.id = int(BOT_USER_ID)
        self.bot.wait_for_component = AsyncMock(side_effect=self.wait_for_component_forever)
        self.clan = MagicMock(tag="#2RY0QP8GY", name="Helper Clan", share_link="https://clan.example")
        self.bot.coc_client.get_clan = AsyncMock(side_effect=lambda *_args: self.events.append("clan") or self.clan)
        self.bot.coc_client.get_player = AsyncMock(return_value=MagicMock(clan=None))
        self.start_war = AsyncMock(return_value=MagicMock(id=777))
        self.abort_war_start = AsyncMock(return_value=True)
        self.cog = SelfCommands(self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"
        self.sleep_patch = patch.object(anyio, "sleep", new_callable=AsyncMock)
        self.sleep = self.sleep_patch.start()

    async def asyncTearDown(self) -> None:
        self.sleep_patch.stop()
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.events.append("post")
        self.create_payloads.append(await request.json())
        self.create_api_keys.append(request.headers.get("X-Api-Key"))
        return web.json_response({
            **self.job_document("queued"),
            "estimate_start_at": 101.0,
            "estimate_end_at": 281.0,
        }, status=201)

    async def handle_status(self, request: web.Request) -> web.Response:
        self.events.append("get")
        self.status_paths.append(request.path)
        self.status_api_keys.append(request.headers.get("X-Api-Key"))
        status, payload = self.status_responses.pop(0)
        if isinstance(payload, str):
            return web.Response(text=payload, status=status)
        return web.json_response(payload, status=status)

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self.events.append("cancel")
        self.cancel_paths.append(request.path)
        self.cancel_api_keys.append(request.headers.get("X-Api-Key"))
        if self.cancel_status == 200:
            return web.json_response(self.job_document(self.cancel_job_status))
        return web.Response(text="unavailable", status=self.cancel_status)

    @staticmethod
    async def wait_for_component_forever(*_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await anyio.Event().wait()

    @staticmethod
    def job_document(status: JobStatus) -> dict[str, JSONValue]:
        match status:
            case "failed":
                error = "Unable to invite the player"
            case "queued" | "running" | "succeeded" | "cancelled":
                error = None
            case unexpected:
                assert_never(unexpected)
        return {
            "id": JOB_ID,
            "type": "host_war_match",
            "status": status,
            "created_at": 100.0,
            "started_at": 101.0 if status != "queued" else None,
            "finished_at": 102.0 if status in {"succeeded", "failed", "cancelled"} else None,
            "events": [{"t": 101.5, "message": f"Progress: {status}"}],
            "result": None,
            "error": error,
        }

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID, mention=f"<@{REQUESTER_ID}>", send=AsyncMock())
        ctx.channel = MagicMock(id=900, send=AsyncMock())
        ctx.defer = AsyncMock()
        progress_message = MagicMock(spec=ipy.Message)
        progress_message.id = 888
        progress_message.edit = AsyncMock()
        ctx.send = AsyncMock(return_value=progress_message)
        ctx.edit = AsyncMock()
        return ctx

    @staticmethod
    def make_match() -> MagicMock:
        caller = MagicMock(
            user_id=REQUESTER_ID, bases=[], result=[None] * 3, base_store=None, opponent_bases_stored=False,
        )
        opponent = MagicMock(
            user_id="200",
            bases=[MagicMock(link="https://base.one"), MagicMock(link="https://base.two")],
            result=[None] * 3,
            base_store=None,
            opponent_bases_stored=False,
        )
        match = MagicMock(
            participants=[caller, opponent],
            token="match-token",
            is_war=True,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
        )
        match.set = AsyncMock()
        return match

    async def invoke_self_host(self, ctx: ipy.SlashContext, match: MagicMock) -> None:
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(id=200))
        with patch("elobot.extensions.self_service.Match") as mock_match, \
                patch("elobot.extensions.self_service.Clan") as mock_clan, \
                patch("elobot.features.hosting.self_hosted.hosting.Player") as selection_player, \
                patch("elobot.extensions.self_service.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)), \
                patch("elobot.features.hosting.war.helper_host.Player") as mock_player, \
                patch("elobot.features.hosting.war.helper_host.get_available_helper_slot", new=AsyncMock(return_value=1)), \
                patch_sites(('elobot.extensions.match.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(return_value=match)), \
                patch(
                    "elobot.features.hosting.war.war_start.start_war",
                    new=self.start_war,
                ), \
                patch(
                    "elobot.features.hosting.war.war_start.abort_war_start",
                    new=self.abort_war_start,
                    create=True,
                ):
            mock_match.find_one = AsyncMock(return_value=match)
            selected_clan = MagicMock(tag="#2RY0QP8GY", availability_str="Available")
            mock_clan.find_all.return_value.to_list = AsyncMock(return_value=[selected_clan])
            mock_clan.find_one = AsyncMock(return_value=selected_clan)
            selection_player.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
            mock_player.find_one = AsyncMock(return_value=MagicMock(tag="#CALLER"))
            await self.cog.self_host_war.callback.func(self.cog, ctx)

    async def test_progress_cancel_posts_to_helper_and_aborts_for_requester(self) -> None:
        # Given
        self.status_responses = [(200, self.job_document("cancelled"))]
        ctx = self.make_context()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()
        self.bot.wait_for_component.side_effect = None
        self.bot.wait_for_component.return_value = MagicMock(ctx=component_ctx)

        async def poll_forever(*_args) -> None:
            with anyio.fail_after(5):
                await anyio.Event().wait()

        # When
        with patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                new=AsyncMock(side_effect=poll_forever),
        ):
            await self.invoke_self_host(ctx, self.make_match())

        # Then
        cancel_button = ctx.send.await_args.kwargs["components"]
        self.assertEqual(cancel_button.style, ipy.ButtonStyle.DANGER)
        self.assertEqual(cancel_button.label, "Cancel")
        self.assertEqual(cancel_button.emoji, CROSS_EMOJI)
        self.assertEqual(cancel_button.custom_id, f"helper_job_cancel|{JOB_ID}")

        wait_call = self.bot.wait_for_component.await_args
        self.assertIs(wait_call.kwargs["components"], cancel_button)
        self.assertEqual(wait_call.kwargs["messages"], 888)
        requester_check = wait_call.kwargs["check"]
        self.assertTrue(requester_check(MagicMock(ctx=MagicMock(author=MagicMock(id=REQUESTER_ID)))))
        self.assertFalse(requester_check(MagicMock(ctx=MagicMock(author=MagicMock(id="other")))))

        self.assertEqual(self.cancel_paths, [f"/v1/jobs/{JOB_ID}/cancel"])
        self.assertEqual(self.cancel_api_keys, ["test-key"])
        component_ctx.edit_origin.assert_awaited_once_with(components=cancel_button)
        self.assertTrue(cancel_button.disabled)
        self.abort_war_start.assert_awaited_once()
        notification = ctx.edit.await_args.kwargs["content"]
        self.assertIn("cancelled", notification.lower())
        self.assertIn("Match aborted", notification)
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_cancel_acceptance_does_not_abort_a_succeeded_job(self) -> None:
        # Given Helper accepts a cancel request while its authoritative status is already succeeded.
        self.cancel_job_status = "succeeded"
        self.status_responses = [(200, self.job_document("succeeded"))]
        ctx = self.make_context()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()
        self.bot.wait_for_component.side_effect = None
        self.bot.wait_for_component.return_value = MagicMock(ctx=component_ctx)

        async def poll_forever(*_args) -> None:
            with anyio.fail_after(5):
                await anyio.Event().wait()

        # When the cancel path wins the race against the existing status monitor.
        with patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                new=AsyncMock(side_effect=poll_forever),
        ):
            await self.invoke_self_host(ctx, self.make_match())

        # Then EloBot preserves the successful war side instead of ghost-aborting it.
        self.assertEqual(self.status_paths, [f"/v1/jobs/{JOB_ID}"])
        self.abort_war_start.assert_not_awaited()
        notification = ctx.edit.await_args.kwargs["content"]
        self.assertIn("succeeded", notification.lower())
        self.assertNotIn("aborted", notification.lower())
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_cancel_failure_keeps_polling_and_does_not_abort_succeeded_job(self) -> None:
        # Given
        self.cancel_status = 503
        ctx = self.make_context()
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id=REQUESTER_ID)
        component_ctx.edit_origin = AsyncMock()
        cancel_failure_reported = anyio.Event()

        async def report_cancel_failure(*_args, **_kwargs) -> None:
            cancel_failure_reported.set()

        component_ctx.send = AsyncMock(side_effect=report_cancel_failure)
        click = MagicMock(ctx=component_ctx)
        click_count = 0

        async def click_once_then_wait(*_args, **_kwargs):
            nonlocal click_count
            click_count += 1
            if click_count == 1:
                return click
            with anyio.fail_after(5):
                await anyio.Event().wait()

        self.bot.wait_for_component.side_effect = click_once_then_wait

        async def succeed_after_cancel_failure(_session, _job_id, update_progress):
            with anyio.fail_after(5):
                await cancel_failure_reported.wait()
            succeeded = HelperJob.model_validate(self.job_document("succeeded"))
            await update_progress(succeeded)
            return succeeded

        # When
        with patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                new=AsyncMock(side_effect=succeed_after_cancel_failure),
        ):
            await self.invoke_self_host(ctx, self.make_match())

        # Then
        self.assertEqual(self.cancel_paths, [f"/v1/jobs/{JOB_ID}/cancel"])
        component_ctx.send.assert_awaited_once()
        self.assertIn("try again", component_ctx.send.await_args.args[0].lower())
        self.abort_war_start.assert_not_awaited()
        self.assertIn("succeeded", ctx.edit.await_args.kwargs["content"])
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])

    async def test_live_progress_uses_interaction_edit_for_ephemeral_response(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded"))]
        ctx = self.make_context()
        progress_message = ctx.send.return_value
        progress_message.edit.side_effect = AssertionError(
            "ephemeral responses must be edited through the interaction context")

        await self.invoke_self_host(ctx, self.make_match())

        progress_message.edit.assert_not_awaited()
        ctx.edit.assert_awaited_once()
        self.assertIs(ctx.edit.await_args.args[0], progress_message)

    async def test_polls_until_success_then_replies_ephemerally_without_dm(self) -> None:
        self.status_responses = [
            (200, self.job_document("queued")),
            (200, self.job_document("running")),
            (200, self.job_document("succeeded")),
        ]
        ctx = self.make_context()
        match = self.make_match()
        self.start_war.side_effect = lambda *_args: self.events.append("start") or MagicMock(id=777)

        await self.invoke_self_host(ctx, match)

        self.assertEqual(self.status_paths, [f"/v1/jobs/{JOB_ID}"] * 3)
        self.assertEqual(self.events, ["clan", "start", "post", "get", "get", "get"])
        self.assertEqual(self.create_api_keys, ["test-key"])
        self.assertEqual(self.create_payloads, [{
            "type": "host_war_match",
            "requested_by": REQUESTER_ID,
            "payload": {
                "host_clan_tag": "#2RY0QP8GY",
                "player_tag": "#CALLER",
                "base_links": ["https://base.one", "https://base.two"],
                "slot": 1,
            },
        }])
        self.assertEqual(self.status_api_keys, ["test-key"] * 3)
        self.sleep.assert_has_awaits([call(2), call(2)])
        ctx.send.assert_awaited_once()
        self.assertIn("queued", ctx.send.await_args.args[0].lower())
        progress_message = ctx.send.return_value
        updates = [edit.kwargs["content"] for edit in ctx.edit.await_args_list]
        self.assertEqual(len(updates), 3)
        self.assertTrue(all(edit.args[0] is progress_message for edit in ctx.edit.await_args_list))
        cancel_button = ctx.send.await_args.kwargs["components"]
        self.assertTrue(all(
            edit.kwargs["components"] is cancel_button
            for edit in ctx.edit.await_args_list[:2]
        ))
        self.assertFalse(cancel_button.disabled)
        for update, status in zip(updates[:2], ("queued", "running"), strict=True):
            self.assertIn(f"Progress: {status}", update)
            self.assertNotIn("Last event", update)
        notification = updates[2]
        self.assertIn(JOB_ID, notification)
        self.assertIn("succeeded", notification)
        self.assertIn("Progress: succeeded", notification)
        self.assertNotIn("Last event", notification)
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(notification.startswith(str(SUCCESS_EMOJI)))
        self.assertEqual(ctx.edit.await_args.kwargs["components"], [])
        self.assertEqual(self.cancel_paths, [])
        ctx.author.send.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        self.start_war.assert_awaited_once()
        bot, start_ctx, start = self.start_war.await_args.args
        self.assertIs(bot, self.bot)
        self.assertIs(start_ctx, ctx)
        self.assertIs(start.match, match)
        self.assertEqual(start.player, 0)
        self.assertIs(start.clan, self.clan)

    async def test_success_schedules_result_store_for_the_committed_side(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded"))]
        ctx = self.make_context()
        match = self.make_match()

        async def commit_start(*_args) -> MagicMock:
            match.times[0] = 123
            match.hosts[0] = "300"
            match.host_clans[0] = "#2RY0QP8GY"
            return MagicMock(id=777)

        self.start_war.side_effect = commit_start
        with patch.object(helper_host.result_store_task, "schedule_automatic_result_store") as schedule:
            await self.invoke_self_host(ctx, match)

        schedule.assert_called_once_with(
            self.bot,
            result_store_task.ScheduledResultStoreRequest(
                token="match-token",
                player=0,
                requested_by=REQUESTER_ID,
                started_at=123,
                submitter_id=BOT_USER_ID,
            ),
        )

    async def test_success_without_a_committed_start_does_not_schedule_result_store(self) -> None:
        self.status_responses = [(200, self.job_document("succeeded"))]
        with patch.object(helper_host.result_store_task, "schedule_automatic_result_store") as schedule:
            await self.invoke_self_host(self.make_context(), self.make_match())

        schedule.assert_not_called()

    async def test_terminal_failure_aborts_and_replies_ephemerally_without_dm(self) -> None:
        self.status_responses = [(200, self.job_document("failed"))]
        ctx = self.make_context()
        self.start_war.side_effect = lambda *_args: self.events.append("start") or MagicMock(id=777)
        self.abort_war_start.side_effect = lambda *_args: self.events.append("abort") or True

        match = self.make_match()
        with patch.object(helper_host.result_store_task, "schedule_automatic_result_store") as schedule:
            await self.invoke_self_host(ctx, match)

        ctx.author.send.assert_not_awaited()
        ctx.send.assert_awaited_once()
        progress_message = ctx.send.return_value
        self.assertEqual(ctx.edit.await_count, 2)
        self.assertTrue(all(edit.args[0] is progress_message for edit in ctx.edit.await_args_list))
        progress_message.edit.assert_not_awaited()
        notification = ctx.edit.await_args.kwargs["content"]
        self.assertIn(JOB_ID, notification)
        self.assertIn("failed", notification.lower())
        self.assertIn("Unable to invite the player", notification)
        self.assertIn("Last event: Progress: failed", notification)
        self.assertIn("aborted", notification.lower())
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertTrue(notification.startswith(str(ERROR_EMOJI)))
        self.assertEqual(self.events, ["clan", "start", "post", "get", "abort"])
        self.start_war.assert_awaited_once()
        self.abort_war_start.assert_awaited_once()
        schedule.assert_not_called()

    async def test_cancelled_job_stops_polling_and_aborts_started_match_cleanly(self) -> None:
        # Given
        self.status_responses = [(200, self.job_document("cancelled"))]
        ctx = self.make_context()

        # When
        await self.invoke_self_host(ctx, self.make_match())

        # Then
        self.assertEqual(self.status_paths, [f"/v1/jobs/{JOB_ID}"])
        self.sleep.assert_not_awaited()
        self.abort_war_start.assert_awaited_once()
        notification = ctx.edit.await_args.kwargs["content"]
        self.assertTrue(notification.startswith(str(SUCCESS_EMOJI)))
        self.assertIn("cancelled", notification)
        self.assertIn("Match aborted", notification)
        self.assertNotIn("failed", notification)

    async def test_terminal_failure_cas_miss_keeps_started_side_and_warns_helper(self) -> None:
        self.status_responses = [(200, self.job_document("failed"))]
        self.abort_war_start.return_value = False
        ctx = self.make_context()

        await self.invoke_self_host(ctx, self.make_match())

        ctx.author.send.assert_not_awaited()
        ctx.send.assert_awaited_once()
        notification = ctx.edit.await_args.kwargs["content"].lower()
        self.assertIn("unable to invite the player", notification)
        self.assertIn("contact a helper", notification)
        self.assertNotIn("was aborted", notification)
        self.assertTrue(notification.startswith(str(ERROR_EMOJI)))

    async def test_three_status_service_failures_reply_ephemerally_without_dm(self) -> None:
        self.status_responses = [(200, self.job_document("queued")), *((503, "unavailable"),) * 3]
        ctx = self.make_context()

        await self.invoke_self_host(ctx, self.make_match())

        self.assertEqual(len(self.status_paths), 4)
        self.sleep.assert_has_awaits([call(2), call(2), call(2)])
        ctx.send.assert_awaited_once()
        ctx.send.return_value.edit.assert_not_awaited()
        notification = ctx.edit.await_args.kwargs["content"]
        self.assertIn("could not monitor", notification.lower())
        self.assertIn("war remains active", notification.lower())
        self.assertIn("contact a helper before retrying", notification.lower())
        self.assertNotIn("Last event", notification)
        self.assertNotIn("Progress: queued", notification)
        self.assertTrue(notification.startswith(str(ERROR_EMOJI)))
        ctx.author.send.assert_not_awaited()
        ctx.channel.send.assert_not_awaited()
        self.start_war.assert_awaited_once()
        self.abort_war_start.assert_not_awaited()



if __name__ == "__main__":
    unittest.main()
