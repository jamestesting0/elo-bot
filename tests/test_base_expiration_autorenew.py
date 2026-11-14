import unittest
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, call, patch

from aiohttp import web
import interactions as ipy

from elobot.extensions.base import BaseCommands
from elobot.config.ids import BASE_RENEWAL_LOGS_CHANNEL, BASE_RENEWAL_THREAD


JOB_ID = "0123456789abcdef0123456789abcdef"
BOT_USER_ID = "1212767017917218876"
BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH5%3ACC%3A0%3AAAAA"
IMAGE_URL = "https://example.com/capital-peak.png"
NOW = 2_000_000_000.25
BASE_EXPIRATION_WARNING_CALLBACK = BaseCommands.base_expiration_warning.callback
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]
type JobStatus = Literal["succeeded", "failed"]


class TestBaseExpirationAutorenew(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.status: JobStatus = "succeeded"
        self.statuses: list[JobStatus] = []
        self.create_payload: dict[str, JSONValue] | None = None
        app = web.Application()
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_get(f"/v1/jobs/{JOB_ID}", self.handle_status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]

        self.bot = MagicMock(spec=ipy.Client)
        self.bot.ext = {}
        self.bot.user = MagicMock(id=BOT_USER_ID)
        self.warning_thread = MagicMock()
        self.renewal_logs_channel = MagicMock()
        self.cog = object.__new__(BaseCommands)
        BaseCommands.__init__(self.cog, self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.create_payload = await request.json()
        self.timeline.append("create")
        return web.json_response({"id": JOB_ID}, status=201)

    async def handle_status(self, _request: web.Request) -> web.Response:
        if self.statuses:
            self.status = self.statuses.pop(0)
        self.timeline.append(f"poll:{self.status}")
        return web.json_response({
            "id": JOB_ID,
            "type": "preserve_base_link",
            "status": self.status,
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [],
            "result": None,
            "error": "Device failed" if self.status == "failed" else None,
        })

    async def run_warning(self) -> MagicMock:
        self.timeline: list[str] = []
        self.sent_log: ipy.Embed | None = None
        base = MagicMock(
            district="Capital Peak",
            link=BASE_LINK,
            image_url=IMAGE_URL,
            expiration_date=NOW + 86400,
        )

        async def save_base() -> None:
            self.timeline.append("save")

        async def send_warning(*args, **kwargs) -> None:
            self.timeline.append("warning" if "embed" in kwargs else "separator")

        async def send_log(*, embed: ipy.Embed) -> None:
            self.sent_log = embed
            self.timeline.append("log")

        base.save_changes = AsyncMock(side_effect=save_base)
        self.warning_thread.send = AsyncMock(side_effect=send_warning)
        self.renewal_logs_channel.send = AsyncMock(side_effect=send_log)
        self.bot.fetch_channel = AsyncMock(side_effect=[self.warning_thread, self.renewal_logs_channel])
        clock = MagicMock()
        clock.now.return_value.timestamp.return_value = NOW

        with (
                patch("elobot.extensions.base.BaseLayout") as base_layout,
                patch("elobot.extensions.base.datetime", new=clock),
                patch("elobot.features.hosting.war.helper_preserve.datetime", new=clock),
        ):
            base_layout.expiration_date.__le__.return_value = "expiring"
            base_layout.find.return_value.to_list = AsyncMock(return_value=[base])
            await BASE_EXPIRATION_WARNING_CALLBACK(self.cog)

        return base

    async def test_automatic_renewal_persists_only_after_success_and_retries_failure_once(self) -> None:
        cases = (
            (("succeeded",), True),
            (("failed", "failed"), False),
            (("failed", "succeeded"), True),
        )
        for statuses, renewed in cases:
            with self.subTest(statuses=statuses):
                self.statuses = list(statuses)
                base = await self.run_warning()

                expected = ["warning"]
                for status in statuses:
                    expected.extend(("create", f"poll:{status}"))
                if renewed:
                    expected.append("save")
                expected.extend(("log", "separator"))
                self.assertEqual(self.timeline, expected)
                self.assertEqual(self.create_payload, {
                    "type": "preserve_base_link",
                    "requested_by": BOT_USER_ID,
                    "payload": {"base_link": BASE_LINK},
                })
                self.assertEqual(base.expiration_date, NOW + (15 if renewed else 1) * 86400)
                if renewed:
                    base.save_changes.assert_awaited_once_with()
                else:
                    base.save_changes.assert_not_awaited()
                self.assertIsNotNone(self.sent_log)
                self.assertEqual(
                    self.sent_log.title,
                    "Capital Peak Base Renewed" if renewed else "Capital Peak Base Renewal Failed",
                )
                if not renewed:
                    self.assertIn(JOB_ID, self.sent_log.description)
                self.bot.fetch_channel.assert_has_awaits([
                    call(BASE_RENEWAL_THREAD),
                    call(BASE_RENEWAL_LOGS_CHANNEL),
                ])


if __name__ == "__main__":
    unittest.main()
