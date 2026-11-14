import unittest
from types import SimpleNamespace
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
import anyio
import interactions as ipy
from elo_bot.integrations.helper_api import HelperHostWarMatchOnStoredPayload, HelperRemoveBasesPayload, HelperStoreBasesPayload


JOB_ID = "0123456789abcdef0123456789abcdef"

HUMAN_ID = "123456789012345678"

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]

type StoredJobType = Literal["store_bases", "host_war_match_on_stored", "remove_bases"]

type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

class StoredBaseJobStateMixin:
    async def asyncSetUp(self) -> None:
        self.create_status = 201
        self.create_body: dict[str, JSONValue] | str = {"id": JOB_ID}
        self.status_responses: list[tuple[int, dict[str, JSONValue] | str]] = []
        self.create_payloads: list[dict[str, JSONValue]] = []
        self.trace: list[str] = []
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
            bot=MagicMock(user=MagicMock(id="bot-requester")),
        )
        self.cancelled_job_ids: list[str] = []
        self.sleep_patch = patch.object(anyio, "sleep", new_callable=AsyncMock)
        self.enterContext(self.sleep_patch)

    async def asyncTearDown(self) -> None:
        self.sleep_patch.stop()
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.trace.append("create")
        self.create_payloads.append(await request.json())
        if isinstance(self.create_body, str):
            return web.Response(text=self.create_body, status=self.create_status)
        return web.json_response(self.create_body, status=self.create_status)

    async def handle_status(self, request: web.Request) -> web.Response:
        status, body = self.status_responses.pop(0)
        if isinstance(body, str):
            self.trace.append(f"poll:{status}")
            return web.Response(text=body, status=status)
        self.trace.append(f"poll:{body.get('status', status)}")
        return web.json_response(body, status=status)

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self.cancelled_job_ids.append(request.match_info["job_id"])
        return web.json_response({"accepted": True}, status=202)

    @staticmethod
    def slash_context() -> tuple[ipy.SlashContext, ipy.Message]:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=HUMAN_ID)
        ctx.send = AsyncMock()
        ctx.edit = AsyncMock()
        progress_message = MagicMock(spec=ipy.Message, id=123456)
        ctx.send.return_value = progress_message
        return ctx, progress_message

    @staticmethod
    def component_context() -> tuple[ipy.ComponentContext, ipy.Message]:
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author = MagicMock(id=HUMAN_ID)
        ctx.send = AsyncMock()
        ctx.edit = AsyncMock()
        progress_message = MagicMock(spec=ipy.Message, id=123456)
        ctx.send.return_value = progress_message
        return ctx, progress_message

    @staticmethod
    def job_document(
            job_type: StoredJobType,
            status: JobStatus,
    ) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": job_type,
            "status": status,
            "created_at": 100.0,
            "started_at": None if status == "queued" else 101.0,
            "finished_at": 102.0 if status in {"succeeded", "failed", "cancelled"} else None,
            "events": [],
            "result": None,
            "error": "device failed" if status == "failed" else None,
        }

    @staticmethod
    def store_payload() -> HelperStoreBasesPayload:
        return HelperStoreBasesPayload(
            host_clan_tag="#CLAN",
            base_links=["https://link.clashofclans.com/en?action=OpenLayout&id=one"],
            slot=1,
        )

    @staticmethod
    def remove_payload() -> HelperRemoveBasesPayload:
        return HelperRemoveBasesPayload(
            host_clan_tag="#CLAN",
            districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            slot=1,
        )

    @staticmethod
    def host_payload() -> HelperHostWarMatchOnStoredPayload:
        return HelperHostWarMatchOnStoredPayload(
            host_clan_tag="#CLAN",
            player_tag="#PLAYER",
            districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            slot=1,
            remove_bases_after=False,
        )


class StoredBaseJobState(StoredBaseJobStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
