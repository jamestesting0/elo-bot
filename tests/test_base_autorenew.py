import unittest
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import anyio
import interactions as ipy

from elo_bot.extensions.base import BaseCommands
from elo_bot.config.ids import BASE_RENEWAL_LOGS_CHANNEL
from elo_bot.config.emojis import LOADING_EMOJI
from elo_bot.config.presentation import RED_COLOR


JOB_ID = "0123456789abcdef0123456789abcdef"
RETRY_JOB_ID = "abcdef0123456789abcdef0123456789"
REQUESTER_ID = "123456789012345678"
BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH5%3ACC%3A0%3AAAAA"
BASE_RENEW_CALLBACK = BaseCommands.base_renew.callback
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]


class TestBaseRenew(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.create_payload: dict[str, JSONValue] | None = None
        self.create_api_key: str | None = None
        self.create_payloads: list[dict[str, JSONValue]] = []
        self.create_status = 201
        self.create_body: dict[str, JSONValue] = {"id": JOB_ID}
        self.status_api_keys: list[str | None] = []
        self.status_responses: list[dict[str, JSONValue]] = []
        app = web.Application()
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_get("/v1/jobs/{job_id}", self.handle_status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]
        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.renewal_logs_channel = MagicMock()
        self.renewal_logs_channel.send = AsyncMock()
        self.bot.fetch_channel = AsyncMock(return_value=self.renewal_logs_channel)
        self.cog = object.__new__(BaseCommands)
        BaseCommands.__init__(self.cog, self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"
        self.sleep_patch = patch.object(anyio, "sleep", new_callable=AsyncMock)
        self.sleep_patch.start()

    async def asyncTearDown(self) -> None:
        self.sleep_patch.stop()
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.create_payload = await request.json()
        self.create_payloads.append(self.create_payload)
        self.create_api_key = request.headers.get("X-Api-Key")
        if len(self.create_payloads) == 2:
            return web.json_response({"id": RETRY_JOB_ID}, status=201)
        return web.json_response(self.create_body, status=self.create_status)

    async def handle_status(self, request: web.Request) -> web.Response:
        self.status_api_keys.append(request.headers.get("X-Api-Key"))
        document = self.status_responses.pop(0)
        document["id"] = request.match_info["job_id"]
        return web.json_response(document)

    @staticmethod
    def job_document(status: JobStatus) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": "preserve_base_link",
            "status": status,
            "created_at": 100.0,
            "started_at": None if status == "queued" else 101.0,
            "finished_at": 102.0 if status in {"succeeded", "failed", "cancelled"} else None,
            "events": [],
            "result": None,
            "error": "Device failed" if status == "failed" else None,
        }

    @staticmethod
    def context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    async def test_successful_job_updates_expiration_after_terminal_poll(self) -> None:
        # Given
        self.status_responses = [self.job_document("queued"), self.job_document("succeeded")]
        base = MagicMock(
            district="Capital Peak",
            link=BASE_LINK,
            image_url="https://example.com/capital-peak.png",
            expiration_date=100.0,
        )
        base.save_changes = AsyncMock()
        ctx = self.context()
        clock = MagicMock()
        clock.now.return_value.timestamp.return_value = 2_000_000_000.25

        with (
                patch("elo_bot.features.hosting.war.helper_preserve.BaseLayout") as base_layout,
                patch("elo_bot.features.hosting.war.helper_preserve.datetime", new=clock),
        ):
            base_layout.find_one = AsyncMock(return_value=base)

            # When
            await BASE_RENEW_CALLBACK(self.cog, ctx, BASE_LINK)

        # Then
        self.assertEqual(self.create_api_key, "test-key")
        self.assertEqual(self.status_api_keys, ["test-key", "test-key"])
        self.assertEqual(self.create_payload, {
            "type": "preserve_base_link",
            "requested_by": REQUESTER_ID,
            "payload": {"base_link": BASE_LINK},
        })
        self.assertEqual(base.expiration_date, 2_000_000_000.25 + 15 * 86400)
        base.save_changes.assert_awaited_once_with()
        self.bot.fetch_channel.assert_awaited_once_with(BASE_RENEWAL_LOGS_CHANNEL)
        self.renewal_logs_channel.send.assert_awaited_once()
        self.assertEqual(ctx.send.await_count, 2)
        queued_message = ctx.send.await_args_list[0].args[0]
        self.assertIn(JOB_ID, queued_message)
        self.assertIn("queued", queued_message.lower())
        self.assertTrue(queued_message.startswith(str(LOADING_EMOJI)))

    async def test_failed_job_keeps_existing_expiration(self) -> None:
        # Given
        self.status_responses = [self.job_document("failed"), self.job_document("failed")]
        base = MagicMock(
            district="Capital Peak",
            link=BASE_LINK,
            image_url="https://example.com/capital-peak.png",
            expiration_date=100.0,
        )
        base.save_changes = AsyncMock()
        ctx = self.context()

        with patch("elo_bot.features.hosting.war.helper_preserve.BaseLayout") as base_layout:
            base_layout.find_one = AsyncMock(return_value=base)

            # When
            await BASE_RENEW_CALLBACK(self.cog, ctx, BASE_LINK)

        # Then
        self.assertEqual(base.expiration_date, 100.0)
        self.assertEqual(len(self.create_payloads), 2)
        self.assertEqual(self.create_payloads[0], self.create_payloads[1])
        self.assertEqual(self.status_api_keys, ["test-key", "test-key"])
        base.save_changes.assert_not_awaited()
        self.bot.fetch_channel.assert_awaited_once_with(BASE_RENEWAL_LOGS_CHANNEL)
        self.renewal_logs_channel.send.assert_awaited_once()
        failure_embed = self.renewal_logs_channel.send.await_args.kwargs["embed"].to_dict()
        self.assertEqual(failure_embed["title"], "Capital Peak Base Renewal Failed")
        self.assertIn(BASE_LINK, failure_embed["description"])
        self.assertIn(RETRY_JOB_ID, failure_embed["description"])
        self.assertIn("Device failed", failure_embed["description"])
        self.assertEqual(failure_embed["image"]["url"], "https://example.com/capital-peak.png")
        self.assertEqual(failure_embed["color"], RED_COLOR)
        self.assertEqual(ctx.send.await_count, 3)
        self.assertIn("The base expiration was not changed.", ctx.send.await_args.args[0])

    async def test_open_job_conflict_is_adopted_and_polled_to_success(self) -> None:
        # Given
        self.create_status = 409
        self.create_body = {
            "error": "An open job already exists",
            "existing_job_id": JOB_ID,
            "existing_job_status": "running",
        }
        self.status_responses = [self.job_document("succeeded")]
        base = MagicMock(
            district="Capital Peak",
            link=BASE_LINK,
            image_url="https://example.com/capital-peak.png",
            expiration_date=100.0,
        )
        base.save_changes = AsyncMock()
        ctx = self.context()
        clock = MagicMock()
        clock.now.return_value.timestamp.return_value = 2_000_000_000.25

        with (
                patch("elo_bot.features.hosting.war.helper_preserve.BaseLayout") as base_layout,
                patch("elo_bot.features.hosting.war.helper_preserve.datetime", new=clock),
        ):
            base_layout.find_one = AsyncMock(return_value=base)

            # When
            await BASE_RENEW_CALLBACK(self.cog, ctx, BASE_LINK)

        # Then
        self.assertEqual(self.status_api_keys, ["test-key"])
        self.assertEqual(base.expiration_date, 2_000_000_000.25 + 15 * 86400)
        base.save_changes.assert_awaited_once_with()
        self.assertEqual(ctx.send.await_count, 2)
        queued_message = ctx.send.await_args_list[0].args[0]
        self.assertIn(JOB_ID, queued_message)
        self.assertIn("running", queued_message.lower())
        self.assertTrue(queued_message.startswith(str(LOADING_EMOJI)))


if __name__ == "__main__":
    unittest.main()
