import unittest
from typing import Literal
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp import web
import anyio
import interactions as ipy
from elo_bot.integrations.helper_api import HelperScoreAttackLimit
from elo_bot.features.hosting.war.helper_store import (
    HelperScoreStoreContext,
    HelperScoreStoreOutcome,
    HelperScoreStoreRequest,
    store_helper_score,
)


JOB_ID = "0123456789abcdef0123456789abcdef"

REQUESTER_ID = "123456789012345678"

type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]

type JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]

class HelperScoreStateMixin:
    async def asyncSetUp(self) -> None:
        self.eta_start = 0.0
        self.create_status = 201
        self.create_body: dict[str, JSONValue] | str = {"id": JOB_ID}
        self.status_responses: list[tuple[int, dict[str, JSONValue] | str]] = []
        self.create_payloads: list[dict[str, JSONValue]] = []
        self.create_api_keys: list[str | None] = []
        self.eta_types: list[str | None] = []
        self.status_api_keys: list[str | None] = []
        self.cancel_paths: list[str] = []
        self.cancel_body: dict[str, JSONValue] = {}
        app = web.Application()
        app.router.add_get("/v1/eta", self.handle_eta)
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
        self.bot.wait_for_component = AsyncMock(side_effect=self.wait_for_component_forever)
        self.runtime = MagicMock(
            bot=self.bot,
            helper_api_base_url=f"http://127.0.0.1:{port}",
            helper_api_key="test-key",
        )
        self.sleep_patch = patch.object(anyio, "sleep", new_callable=AsyncMock)
        self.enterContext(self.sleep_patch)

    async def asyncTearDown(self) -> None:
        self.sleep_patch.stop()
        await self.runner.cleanup()

    async def handle_eta(self, request: web.Request) -> web.Response:
        self.eta_types.append(request.query.get("type"))
        return web.json_response({"estimate_start_at": self.eta_start, "estimate_end_at": 120.0})

    async def handle_create(self, request: web.Request) -> web.Response:
        self.create_payloads.append(await request.json())
        self.create_api_keys.append(request.headers.get("X-Api-Key"))
        if isinstance(self.create_body, str):
            return web.Response(text=self.create_body, status=self.create_status)
        return web.json_response(self.create_body, status=self.create_status)

    async def handle_status(self, request: web.Request) -> web.Response:
        self.status_api_keys.append(request.headers.get("X-Api-Key"))
        status, body = self.status_responses.pop(0)
        if isinstance(body, str):
            return web.Response(text=body, status=status)
        return web.json_response(body, status=status)

    async def handle_cancel(self, request: web.Request) -> web.Response:
        self.cancel_paths.append(request.path)
        return web.json_response(self.cancel_body, status=200)

    @staticmethod
    async def wait_for_component_forever(*_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await anyio.Event().wait()

    @staticmethod
    def job_document(
            status: JobStatus,
            scores: dict[str, JSONValue] | None = None,
    ) -> dict[str, JSONValue]:
        return {
            "id": JOB_ID,
            "type": "fetch_score",
            "status": status,
            "created_at": 100.0,
            "started_at": 101.0 if status != "queued" else None,
            "finished_at": 102.0 if status in {"succeeded", "failed", "cancelled"} else None,
            "events": [],
            "result": None if scores is None else {"scores": scores},
            "error": "partial failure" if status == "failed" else None,
        }

    @staticmethod
    def attack(
            number: int,
            stars: int | None,
            destruction: int | float | None,
            *,
            miner_count: int | None = 0,
            invalid: bool = False,
    ) -> dict[str, JSONValue]:
        return {
            "attack_number": number,
            "stars": stars,
            "destruction": destruction,
            "miner_count": miner_count,
            "invalid_replay": invalid,
        }

    @classmethod
    def score(
            cls,
            stars: int | None = 3,
            destruction: int | float = 100,
            verified: bool = True,
            same_attacker: bool = True,
    ) -> dict[str, JSONValue]:
        return {
            "attacker_name_verified": verified,
            "no_of_valid_attacks": 1,
            "total_no_of_attacks": 1,
            "same_attacker_for_all_attacks": same_attacker,
            "stars_gained": stars,
            "destruction_gained": destruction,
            "total_destruction_on_challenge": destruction,
            "attacks": [cls.attack(1, stars, destruction)],
        }

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID)
        progress_message = MagicMock(spec=ipy.Message)
        progress_message.id = 444
        ctx.send = AsyncMock(return_value=progress_message)
        ctx.edit = AsyncMock()
        ctx.channel = MagicMock(spec=ipy.ThreadChannel)
        ctx.channel.send = AsyncMock()
        return ctx

    @staticmethod
    def make_match(
            start_time: int | None = 100,
            results: list[str | None] | None = None,
    ) -> MagicMock:
        return MagicMock(
            participants=[MagicMock(user_id=REQUESTER_ID, result=results or ["1.50", "2.80", "3.100"]), MagicMock()],
            host_clans=["#CLAN", None],
            times=[start_time, None],
            final_districts=["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"],
        )

    async def invoke(
            self,
            ctx: HelperScoreStoreContext,
            match: MagicMock,
            now: float = 200.0,
            *,
            interactive: bool = True,
            allow_cancellation: bool = True,
            max_allowed_attacks: HelperScoreAttackLimit = 2,
    ) -> HelperScoreStoreOutcome:
        with patch("elo_bot.features.hosting.war.helper_store.Player") as player_model, \
                patch("elo_bot.features.hosting.war.helper_store.time", new=MagicMock(time=MagicMock(return_value=now))):
            player_model.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
            return await store_helper_score(
                self.runtime,
                ctx,
                HelperScoreStoreRequest(
                    match=match,
                    player=0,
                    interactive=interactive,
                    allow_cancellation=allow_cancellation,
                    max_allowed_attacks=max_allowed_attacks,
                ),
            )


class HelperScoreState(HelperScoreStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
