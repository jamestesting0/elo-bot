import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import interactions as ipy

from elobot.extensions.self_service import SelfCommands
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


class TestSelfReinvite(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.request_payload: dict[str, JSONValue] | None = None
        self.request_api_key: str | None = None
        self.status_requests = 0
        self.create_status = 201
        self.create_body: dict[str, JSONValue] = {"id": JOB_ID}
        self.status_body: dict[str, JSONValue] = {
            "id": JOB_ID,
            "type": "kick_and_reinvite",
            "status": "succeeded",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [{"t": 101.5, "message": "Player invited"}],
            "result": None,
            "error": None,
        }
        app = web.Application()
        app.router.add_post("/v1/jobs", self.handle_create)
        app.router.add_get(f"/v1/jobs/{JOB_ID}", self.handle_status)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]
        self.bot = MagicMock()
        self.cog = SelfCommands(self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle_create(self, request: web.Request) -> web.Response:
        self.request_payload = await request.json()
        self.request_api_key = request.headers.get("X-Api-Key")
        return web.json_response(self.create_body, status=self.create_status)

    async def handle_status(self, _request: web.Request) -> web.Response:
        self.status_requests += 1
        return web.json_response(self.status_body)

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID)
        ctx.channel = MagicMock(id=900)
        ctx.defer = AsyncMock()
        progress_message = MagicMock(spec=ipy.Message)
        progress_message.id = 888
        ctx.send = AsyncMock(return_value=progress_message)
        ctx.edit = AsyncMock()
        return ctx

    @staticmethod
    def make_match(*, is_war: bool = True, caller_index: int = 0) -> MagicMock:
        caller = MagicMock(user_id=REQUESTER_ID, result=[None, None, None])
        opponent = MagicMock(user_id="200", result=[None, None, None])
        participants = [caller, opponent] if caller_index == 0 else [opponent, caller]
        return MagicMock(
            participants=participants,
            is_war=is_war,
            is_autowin=False,
            times=[100, None],
            host_clans=["#WAR", None],
        )

    async def invoke(self, ctx: ipy.SlashContext, matches: list[MagicMock]) -> None:
        with (
            patch("elobot.extensions.self_service.Match") as match_model,
            patch("elobot.extensions.self_service.Player", create=True) as player_model,
        ):
            match_model.find.return_value.to_list = AsyncMock(return_value=matches)
            player_model.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
            await self.cog.self_reinvite.callback.func(self.cog, ctx)

    async def test_started_war_queues_exact_kick_and_reinvite_job(self) -> None:
        # Given
        ctx = self.make_context()
        match = self.make_match()

        # When
        await self.invoke(ctx, [match])

        # Then
        self.assertEqual(self.request_api_key, "test-key")
        self.assertEqual(self.request_payload, {
            "type": "kick_and_reinvite",
            "requested_by": REQUESTER_ID,
            "payload": {"host_clan_tag": "#WAR", "player_tag": "#PLAYER"},
        })
        self.assertEqual(self.status_requests, 1)
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertTrue(ctx.edit.await_args.kwargs["content"].startswith(str(SUCCESS_EMOJI)))

    async def test_live_match_uses_shared_match_clan_for_second_player(self) -> None:
        # Given
        ctx = self.make_context()
        match = self.make_match(is_war=False, caller_index=1)
        match.host_clans = ["#LIVE", None]

        # When
        await self.invoke(ctx, [match])

        # Then
        assert self.request_payload is not None
        payload = self.request_payload["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["host_clan_tag"], "#LIVE")

    async def test_ineligible_matches_are_rejected_before_helper_request(self) -> None:
        cases: list[tuple[str, list[MagicMock]]] = []

        war_not_started = self.make_match()
        war_not_started.times = [None, None]
        cases.append(("war not started", [war_not_started]))

        ended_war = self.make_match()
        ended_war.participants[0].result = ["3.100", "3.100", "3.100"]
        cases.append(("war ended for caller", [ended_war]))

        live_not_started = self.make_match(is_war=False)
        live_not_started.times = [None, None]
        cases.append(("live match not started", [live_not_started]))

        ended_live = self.make_match(is_war=False)
        ended_live.participants[0].result = ["3.100", "3.100", "3.100"]
        ended_live.participants[1].result = ["2.90", "2.90", "2.90"]
        cases.append(("live match ended", [ended_live]))

        missing_clan = self.make_match()
        missing_clan.host_clans = [None, None]
        cases.append(("match clan missing", [missing_clan]))

        cases.append(("no match", []))

        for label, matches in cases:
            with self.subTest(label=label):
                # Given
                self.request_payload = None
                ctx = self.make_context()

                # When
                await self.invoke(ctx, matches)

                # Then
                self.assertIsNone(self.request_payload)
                self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))

    async def test_helper_rejection_is_reported_without_polling(self) -> None:
        # Given
        self.create_status = 400
        self.create_body = {"error": "Player is not in the clan"}
        ctx = self.make_context()

        # When
        await self.invoke(ctx, [self.make_match()])

        # Then
        self.assertEqual(self.status_requests, 0)
        self.assertIn("Player is not in the clan", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))


if __name__ == "__main__":
    unittest.main()
