import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
import coc
import interactions as ipy
from pydantic import JsonValue

from cogs.self_commands import SelfCommands
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI
from elobot.config.ids import MATCH_CHANNEL
from elobot.db import Match, Participant


JOB_ID = "0123456789abcdef0123456789abcdef"


class TestLiveSpectate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.requests: list[dict[str, JsonValue]] = []
        self.trace: list[str] = []
        self.create_status = 201
        self.create_body: dict[str, JsonValue] = {"id": JOB_ID}
        self.status_code = 200
        self.statuses = ["succeeded"]
        self.app = web.Application()
        self.app.router.add_post("/v1/jobs", self.create)
        self.app.router.add_get(f"/v1/jobs/{JOB_ID}", self.status)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.addAsyncCleanup(self.runner.cleanup)
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.bot = MagicMock()
        self.bot.coc_client.get_player = AsyncMock(return_value=MagicMock(town_hall=5))
        self.cog = SelfCommands(self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"
        self.ctx = MagicMock(spec=ipy.ComponentContext)
        self.ctx.bot = self.bot
        self.ctx.author = MagicMock(id=300)
        self.ctx.channel_id = MATCH_CHANNEL
        self.ctx.message = MagicMock(id=80)
        self.ctx.custom_id = "live_spectate|TOKEN|100"
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock(side_effect=self.send)
        self.ctx.edit = AsyncMock(side_effect=self.edit)
        self.match = Match.model_construct(
            token="TOKEN", matched_time=1, thread="42", msg="80", is_war=False,
            times=[100, None], host_clans=["#CLAN", None], old_msgs=[43, None],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        self.player = MagicMock(tag="#PLAYER")
        self.find_player = AsyncMock(side_effect=lambda *_args: self.player)
        self.find_match = AsyncMock(side_effect=lambda *_args: self.match)
        self.candidate_matches = None
        self.find_candidates = MagicMock()
        self.find_candidates.return_value.to_list = AsyncMock(
            side_effect=lambda: [self.match] if self.candidate_matches is None else self.candidate_matches,
        )
        for target, replacement in (
            ("elobot.features.hosting.war.helper_spectate.Player.find_one", self.find_player),
            ("elobot.features.hosting.war.helper_spectate.Match.find_one", self.find_match),
            ("elobot.features.hosting.war.helper_spectate.Match.find", self.find_candidates),
            ("elobot.integrations.helper_api.HELPER_POLL_INTERVAL_SECONDS", 0),
        ):
            patcher = patch(target, new=replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def send(self, content: str, *, ephemeral: bool) -> MagicMock:
        self.assertTrue(ephemeral)
        self.trace.append(content)
        return MagicMock(id=99)

    async def edit(self, _message: ipy.Message, *, content: str) -> None:
        self.trace.append(content)

    async def create(self, request: web.Request) -> web.Response:
        self.assertEqual(request.headers.get("X-Api-Key"), "test-key")
        self.requests.append(await request.json())
        self.trace.append("POST")
        return web.json_response(self.create_body, status=self.create_status)

    async def status(self, _request: web.Request) -> web.Response:
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        self.trace.append(f"GET:{status}")
        return web.json_response({
            "id": JOB_ID, "type": "invite_spectator", "status": status,
            "created_at": 100.0, "started_at": 101.0, "finished_at": 102.0,
            "events": [{"t": 101.5, "message": "Inviting spectator"}],
            "result": None, "error": "Clan is full" if status == "failed" else None,
        }, status=self.status_code)

    async def invoke(self) -> None:
        await self.cog.live_match_spectate.callback.func(self.cog, self.ctx)

    async def test_registered_th5_uses_exact_invitation_and_progress(self) -> None:
        self.statuses = ["running", "succeeded"]
        await self.invoke()
        self.assertEqual(self.requests, [{
            "type": "invite_spectator", "requested_by": "300",
            "payload": {"host_clan_tag": "#CLAN", "player_tag": "#PLAYER"},
        }])
        self.find_player.assert_awaited_once_with({"user_id": "300"})
        self.bot.coc_client.get_player.assert_awaited_once_with("#PLAYER")
        self.ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.assertEqual(self.trace[::2], ["POST", "GET:running", "GET:succeeded"])
        for message, emoji in zip(self.trace[1::2], (LOADING_EMOJI, LOADING_EMOJI, SUCCESS_EMOJI), strict=True):
            self.assertTrue(message.startswith(str(emoji)))
            self.assertIn(JOB_ID, message)

    async def test_below_th5_and_one_finished_participant_are_allowed(self) -> None:
        self.bot.coc_client.get_player.return_value.town_hall = 4
        self.match.participants[0].result = ["3.100"] * 3
        await self.invoke()
        self.assertEqual(len(self.requests), 1)


    async def test_replaced_public_announcement_does_not_invite(self) -> None:
        self.ctx.channel_id = MATCH_CHANNEL
        self.ctx.message.id = 79
        self.match.msg = "80"
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.assertIn("no longer active", self.trace[-1])

    async def test_old_live_thread_button_no_longer_invites(self) -> None:
        self.ctx.channel_id = 42
        self.ctx.message.id = 43
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.assertIn("no longer active", self.trace[-1])

    async def test_unregistered_player_is_rejected_without_account_lookup(self) -> None:
        self.player = None
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.bot.coc_client.get_player.assert_not_awaited()
        self.assertEqual(self.trace, [f"{ERROR_EMOJI} You must be registered to spectate a match."])

    async def test_th6_is_rejected_without_helper_work(self) -> None:
        self.bot.coc_client.get_player.return_value.town_hall = 6
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.trace, [f"{ERROR_EMOJI} Your registered Elo account must be TH5 or below to spectate."])

    async def test_unverifiable_account_is_rejected(self) -> None:
        self.bot.coc_client.get_player.side_effect = coc.errors.NotFound(MagicMock(status=404), "not found")
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.assertIn("Could not verify", self.trace[-1])

    async def test_inactive_or_stale_match_never_invites(self) -> None:
        for updates in (
            {"is_war": True}, {"is_autowin": True}, {"times": [None, None]},
            {"times": [200, None]}, {"host_clans": [None, None]}, {"msg": "81"},
            {"participants": [Participant(user_id=user, result=["3.100"] * 3) for user in ("100", "200")]},
        ):
            with self.subTest(updates=updates):
                original = self.match
                self.match = original.model_copy(update=updates)
                await self.invoke()
                self.assertEqual(self.requests, [])
                self.assertEqual(self.trace[-1], f"{ERROR_EMOJI} This match is no longer active.")
                self.match = original
        self.match = None
        await self.invoke()
        self.assertEqual(self.requests, [])

    async def test_completion_during_account_lookup_is_rechecked(self) -> None:
        async def finish_match(_tag: str) -> MagicMock:
            self.match.is_autowin = True
            return MagicMock(town_hall=5)
        self.bot.coc_client.get_player.side_effect = finish_match
        await self.invoke()
        self.assertEqual(self.requests, [])
        self.assertIn("no longer active", self.trace[-1])

    async def test_terminal_failure_and_cancellation_are_visible(self) -> None:
        for status, detail in (("failed", "failed: Clan is full."), ("cancelled", "was cancelled.")):
            with self.subTest(status=status):
                self.statuses = [status]
                await self.invoke()
                self.assertEqual(self.trace[-1].split("\n")[0], f"{ERROR_EMOJI} Helper job `{JOB_ID}` {detail}")

    async def test_duplicate_request_tracks_existing_job(self) -> None:
        self.create_status = 409
        self.create_body = {
            "error": "Existing job", "existing_job_id": JOB_ID, "existing_job_status": "running",
        }
        await self.invoke()
        self.assertEqual(len(self.requests), 1)
        self.assertIn("is running.", self.trace[1])
        self.assertIn("succeeded", self.trace[-1])

    async def test_rejection_and_invalid_acceptance_do_not_poll(self) -> None:
        for status, body, text in (
            (400, {"error": "No host account"}, "Helper rejected the request: No host account"),
            (201, {}, "Helper accepted the request, but the outcome could not be confirmed."),
            (503, {}, "The Helper service could not queue the spectator invite."),
        ):
            with self.subTest(status=status):
                self.trace.clear()
                self.create_status, self.create_body = status, body
                await self.invoke()
                self.assertEqual(self.trace, ["POST", f"{ERROR_EMOJI} {text}"])

    async def test_tracking_failure_reports_unknown_invitation_outcome(self) -> None:
        self.status_code = 404
        await self.invoke()
        self.assertIn("invitation outcome is unknown", self.trace[-1])
        self.assertNotIn("war remains active", self.trace[-1])

    async def test_failed_progress_publication_does_not_modify_match(self) -> None:
        self.ctx.send.side_effect = ipy.errors.HTTPException(MagicMock(status=500))
        before = self.match.model_dump()
        with self.assertRaises(ipy.errors.HTTPException):
            await self.invoke()
        self.assertEqual(self.match.model_dump(), before)
        self.assertEqual(len(self.requests), 1)
        self.ctx.edit.assert_not_awaited()

    def select_war_side(self, side: int) -> None:
        self.ctx.channel_id = 42
        self.ctx.message.id = 43 + side
        self.ctx.custom_id = f"war_spectate|TOKEN|{side}|{100 + side}"
        self.match.is_war = True
        self.match.times = [100, 101]
        self.match.host_clans = ["#CLAN", "#SECOND"]
        self.match.old_msgs = [43, 44]

    async def invoke_war(self) -> None:
        await self.cog.war_match_spectate.callback.func(self.cog, self.ctx)

    async def test_war_outsider_invites_to_the_selected_sides_clan(self) -> None:
        for side, clan in enumerate(("#CLAN", "#SECOND")):
            with self.subTest(side=side):
                self.select_war_side(side)
                before = self.match.model_dump()
                await self.invoke_war()
                self.assertEqual(self.requests[-1], {
                    "type": "invite_spectator", "requested_by": "300",
                    "payload": {"host_clan_tag": clan, "player_tag": "#PLAYER"},
                })
                self.assertIn("succeeded", self.trace[-1])
                self.assertEqual(self.match.model_dump(), before)
        self.assertEqual(len(self.requests), 2)

    async def test_war_opponent_requires_all_results_stored_on_either_side(self) -> None:
        for side in (0, 1):
            for results in ([None] * 3, ["3.100", None, "2.90"], ["3.100"] * 3):
                with self.subTest(side=side, results=results):
                    self.requests.clear()
                    self.select_war_side(side)
                    for participant in self.match.participants:
                        participant.result = [None] * 3
                    opponent = self.match.participants[1 - side]
                    self.ctx.author.id = int(opponent.user_id)
                    opponent.result = results
                    await self.invoke_war()
                    if all(results):
                        self.assertEqual(len(self.requests), 1)
                        self.assertEqual(self.requests[0]["requested_by"], opponent.user_id)
                        self.assertIn("succeeded", self.trace[-1])
                    else:
                        self.assertEqual(self.requests, [])
                        self.assertTrue(self.trace[-1].startswith(str(ERROR_EMOJI)))
                        self.assertIn(f"<@{self.match.participants[side].user_id}>", self.trace[-1])
                        self.assertIn("<#42>", self.trace[-1])

    async def test_war_own_side_does_not_require_stored_results(self) -> None:
        self.select_war_side(1)
        self.ctx.author.id = 200
        await self.invoke_war()
        self.assertEqual(len(self.requests), 1)

    async def test_stale_war_side_never_invites(self) -> None:
        for side in (0, 1):
            self.select_war_side(side)
            original = self.match.model_copy(deep=True)
            for updates in (
                {"is_war": False}, {"times": [None, None]},
                {"times": [200, 201]}, {"host_clans": [None, None]},
                {"old_msgs": [90, 91]}, {"thread": "90"},
            ):
                with self.subTest(side=side, updates=updates):
                    self.match = original.model_copy(update=updates)
                    await self.invoke_war()
                    self.assertEqual(self.requests, [])
                    self.assertEqual(self.trace[-1], f"{ERROR_EMOJI} This match is no longer active.")
            self.match = original

    async def test_war_registration_and_town_hall_guards(self) -> None:
        self.select_war_side(0)
        self.player = None
        await self.invoke_war()
        self.assertEqual(self.requests, [])
        self.assertIn("must be registered", self.trace[-1])
        self.player = MagicMock(tag="#PLAYER")
        self.bot.coc_client.get_player.return_value.town_hall = 6
        await self.invoke_war()
        self.assertEqual(self.requests, [])
        self.assertIn("TH5 or below", self.trace[-1])

    async def test_war_opponents_reset_results_are_rechecked_after_account_lookup(self) -> None:
        self.select_war_side(0)
        self.ctx.author.id = 200
        self.match.participants[1].result = ["3.100"] * 3

        async def reset_results(_tag: str) -> MagicMock:
            self.match.participants[1].result = [None] * 3
            return MagicMock(town_hall=5)

        self.bot.coc_client.get_player.side_effect = reset_results
        await self.invoke_war()
        self.assertEqual(self.requests, [])
        self.assertIn("store your results", self.trace[-1])

    async def test_completed_war_still_allows_spectating(self) -> None:
        for side in (0, 1):
            for autowin in (False, True):
                with self.subTest(side=side, autowin=autowin):
                    self.select_war_side(side)
                    self.match.is_autowin = autowin
                    for participant in self.match.participants:
                        participant.result = ["3.100"] * 3
                    await self.invoke_war()
                    self.assertIn("succeeded", self.trace[-1])
        self.assertEqual(len(self.requests), 4)

    async def test_finished_opponent_no_longer_blocks_unfinished_caller(self) -> None:
        self.select_war_side(0)
        self.ctx.author.id = 200
        self.match.participants[0].result = ["3.100"] * 3
        await self.invoke_war()
        self.assertEqual(len(self.requests), 1)

    def other_active_match(self, caller_side: int) -> Match:
        active = self.match.model_copy(deep=True)
        active.token = "OTHER"
        active.thread = "123456"
        active.participants[caller_side].user_id = "300"
        active.participants[1 - caller_side].user_id = "999"
        active.times = [200, 201]
        active.host_clans[1 - caller_side] = "#CLAN"
        active.host_clans[caller_side] = "#ELSEWHERE"
        return active

    async def test_old_completed_embed_cannot_bypass_another_active_match(self) -> None:
        for caller_side in (0, 1):
            with self.subTest(caller_side=caller_side):
                self.select_war_side(0)
                active = self.other_active_match(caller_side)
                for participant in self.match.participants:
                    participant.result = ["3.100"] * 3
                self.candidate_matches = [self.match, active]
                await self.invoke_war()
                self.assertEqual(self.requests, [])
                self.assertTrue(self.trace[-1].startswith(str(ERROR_EMOJI)))
                self.assertIn("<@999>", self.trace[-1])
                self.assertIn("<#123456>", self.trace[-1])
                self.find_candidates.assert_called_with({
                    "participants.user_id": "300", "host_clans": "#CLAN",
                })
                for participant in self.match.participants:
                    participant.result = [None] * 3

    async def test_only_a_started_unfinished_opponent_in_destination_clan_blocks(self) -> None:
        self.select_war_side(0)
        original = self.other_active_match(0)
        for change in ("caller_done", "opponent_done", "not_started", "other_clan", "autowin", "not_participant"):
            with self.subTest(change=change):
                active = original.model_copy(deep=True)
                if change == "caller_done":
                    active.participants[0].result = ["3.100"] * 3
                elif change == "opponent_done":
                    active.participants[1].result = ["3.100"] * 3
                elif change == "not_started":
                    active.times[1] = None
                elif change == "other_clan":
                    active.host_clans = ["#CLAN", "#OTHER"]
                elif change == "autowin":
                    active.is_autowin = True
                elif change == "not_participant":
                    active.participants[0].user_id = "888"
                self.candidate_matches = [active]
                await self.invoke_war()
                self.assertIn("succeeded", self.trace[-1])
        self.assertEqual(len(self.requests), 6)

    async def test_live_opponent_in_destination_clan_also_blocks_war_invite(self) -> None:
        self.select_war_side(0)
        active = self.other_active_match(0)
        active.is_war = False
        active.host_clans = ["#CLAN", None]
        self.candidate_matches = [active]
        await self.invoke_war()
        self.assertEqual(self.requests, [])
        self.assertIn("<@999>", self.trace[-1])
        self.assertIn("<#123456>", self.trace[-1])
