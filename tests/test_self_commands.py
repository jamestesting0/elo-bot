# noqa: SIZE_OK - self-host scenarios share one local HTTP server fixture
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import aiohttp
from aiohttp import web
import anyio
import interactions as ipy

from elobot.integrations.helper_api import HelperTrackingFailure
from elobot.extensions.self_service import SelfCommands
from elobot.config.game import BASE_STORE_SLOTS
from elobot.config.ids import BOT_ID
from elobot.config.presentation import COLOR
from elobot.config.emojis import ERROR_EMOJI, LOADING_EMOJI, TICK_EMOJI
from tests._patch_sites import patch_sites


JOB_ID = "0123456789abcdef0123456789abcdef"
REQUESTER_ID = "123456789012345678"
type JSONValue = None | bool | int | float | str | list[JSONValue] | dict[str, JSONValue]


class TestSelfCommands(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.response_status = 201
        self.response_body: dict[str, JSONValue] = {
            "id": JOB_ID,
            "type": "host_war_match",
            "status": "queued",
            "created_at": 100.0,
            "started_at": None,
            "finished_at": None,
            "events": [],
            "result": None,
            "error": None,
            "estimate_start_at": 101.0,
            "estimate_end_at": 281.0,
        }
        self.request_payload: dict[str, JSONValue] | None = None
        self.request_api_key: str | None = None
        self.events: list[str] = []
        self.poll_statuses = ["running", "succeeded"]
        self.status_job_type = "host_live_match"
        self.stop_after_eta = False
        self.abort_war_start = AsyncMock(return_value=True)
        self.enterContext(patch("elobot.integrations.helper_api.anyio.sleep", new_callable=AsyncMock))
        app = web.Application()
        app.router.add_get("/v1/eta", self.handle_eta)
        app.router.add_post("/v1/jobs", self.handle_job)
        app.router.add_get("/v1/jobs/{job_id}", self.handle_job_status)
        app.router.add_post("/v1/jobs/{job_id}/cancel", self.handle_job_cancel)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        assert self.site._server is not None
        port = self.site._server.sockets[0].getsockname()[1]
        self.bot = MagicMock()
        self.bot.wait_for_component = AsyncMock(side_effect=self.wait_for_component_forever)
        self.clan_record = MagicMock(
            tag="#2RY0QP8GY",
            availability_str="Available",
            available_slots=list(BASE_STORE_SLOTS),
        )
        self.clan = MagicMock(tag="#2RY0QP8GY", name="Helper Clan", share_link="https://clan.example")
        self.bot.coc_client.get_clan = AsyncMock(return_value=self.clan)
        self.bot.coc_client.get_player = AsyncMock(return_value=MagicMock(clan=None))
        self.self_commands_player_patcher = patch("elobot.features.hosting.self_hosted.hosting.Player")
        self.self_commands_player_model = self.self_commands_player_patcher.start()
        self.addCleanup(self.self_commands_player_patcher.stop)
        self.self_commands_player_model.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
        self.cog = SelfCommands(self.bot)
        self.cog.helper_api_base_url = f"http://127.0.0.1:{port}"
        self.cog.helper_api_key = "test-key"

    async def asyncTearDown(self) -> None:
        await self.runner.cleanup()

    async def handle_job(self, request: web.Request) -> web.Response:
        self.events.append("post")
        self.post_match_is_war = getattr(getattr(self, "live_match", None), "is_war", None)
        self.request_payload = await request.json()
        self.request_api_key = request.headers.get("X-Api-Key")
        return web.json_response(self.response_body, status=self.response_status)

    async def handle_eta(self, _request: web.Request) -> web.Response:
        if self.stop_after_eta:
            await self.site.stop()
        return web.json_response({"estimate_start_at": 0.0, "estimate_end_at": 180.0})

    async def handle_job_status(self, request: web.Request) -> web.Response:
        status = self.poll_statuses.pop(0)
        self.events.append(f"poll-{status}")
        return web.json_response({
            "id": request.match_info["job_id"],
            "type": self.status_job_type,
            "status": status,
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0 if status in {"succeeded", "failed", "cancelled"} else None,
            "events": [],
            "result": None,
            "error": "hosting failed" if status == "failed" else None,
        })

    async def handle_job_cancel(self, _request: web.Request) -> web.Response:
        self.events.append("cancel")
        return web.Response(status=202)

    @staticmethod
    async def wait_for_component_forever(*_args, **_kwargs) -> None:
        with anyio.fail_after(5):
            await anyio.Event().wait()

    @staticmethod
    def make_context(author_id: str = REQUESTER_ID) -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=author_id, mention=f"<@{author_id}>", send=AsyncMock())
        ctx.channel = MagicMock(
            id=900,
            send=AsyncMock(),
            created_at=datetime.now(timezone.utc),
        )
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )
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
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            old_msgs=[None, None],
        )
        match.set = AsyncMock()
        return match

    @staticmethod
    def make_live_request_message(identity, *, opponent_id: str = "200") -> MagicMock:
        from elobot.features.hosting.live.request import build_live_request_components

        message = MagicMock(spec=ipy.Message)
        message.id = 777
        message.content = f"<@{opponent_id}>"
        message.embeds = [ipy.Embed(title="Live Match Request")]
        message.components = [build_live_request_components(identity)]
        message.edit = AsyncMock()
        return message

    @staticmethod
    def make_component_context(message: MagicMock, custom_id: str, author_id: str = "200") -> ipy.ComponentContext:
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.author = MagicMock(id=author_id, mention=f"<@{author_id}>")
        ctx.channel = MagicMock(id=900, fetch_message=AsyncMock(return_value=message))
        ctx.channel.send = AsyncMock()
        ctx.message = message
        ctx.custom_id = custom_id
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.edit = AsyncMock()
        return ctx

    async def invoke_live_accept(self, *, poll_statuses: list[str] | None = None):
        from elobot.features.hosting.live import live_start
        from elobot.features.hosting.live.request import LiveRequestIdentity
        from elobot.extensions.match import MatchCommands

        self.poll_statuses = poll_statuses or ["running", "succeeded"]
        self.response_body = {
            "id": JOB_ID,
            "type": "host_live_match",
            "status": "queued",
            "created_at": 100.0,
            "started_at": None,
            "finished_at": None,
            "events": [],
            "result": None,
            "error": None,
            "estimate_start_at": 101.0,
            "estimate_end_at": 281.0,
        }
        match = self.make_match()
        match.is_war = True
        self.live_match = match
        match.thread = "900"
        match.final_districts = ["Barbarian Camp", "Wizard Valley"]
        match.participants[0].bases = [MagicMock(link="https://p1.one"), MagicMock(link="https://p1.two")]
        match.participants[1].bases = [MagicMock(link="https://p2.one"), MagicMock(link="https://p2.two")]
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        request_message = self.make_live_request_message(identity)
        ctx = self.make_component_context(request_message, identity.custom_id("accept"))
        progress_message = MagicMock(spec=ipy.Message, id=888)
        progress_message.edit = AsyncMock()
        ctx.channel.send.return_value = progress_message
        docs = {
            REQUESTER_ID: MagicMock(tag="#P1", elo=1200),
            "200": MagicMock(tag="#P2", elo=1300),
        }
        accounts = {
            "#P1": MagicMock(tag="#P1", town_hall=3, clan=None),
            "#P2": MagicMock(tag="#P2", town_hall=3, clan=None),
        }

        async def get_player(tag):
            return accounts[tag]

        async def choose(_token, attacker, _defender, **_kwargs):
            self.events.append("choose-p2" if str(attacker.id) == REQUESTER_ID else "choose-p1")
            return match

        async def start(*_args):
            self.events.append("live-start")
            self.live_start_request = _args[2]
            match.is_war = False
            return live_start.LiveStartSucceeded(message=MagicMock(id=999))

        self.bot.fetch_user = AsyncMock(side_effect=lambda user_id: MagicMock(id=user_id, mention=f"<@{user_id}>"))
        self.bot.user.id = "bot"
        self.bot.owner.id = "owner"
        self.bot.coc_client.get_player = AsyncMock(side_effect=get_player)
        self.bot.coc_client.get_clan = AsyncMock(return_value=self.clan)
        match_commands = MatchCommands(self.bot)
        match_commands.helper_api_base_url = self.cog.helper_api_base_url
        match_commands.helper_api_key = self.cog.helper_api_key
        self.bot.get_ext.return_value = match_commands
        abort = AsyncMock(return_value=True)
        with patch("elobot.features.hosting.live.request.Match") as request_match, \
                patch("elobot.features.hosting.live.request.time.time", return_value=1001), \
                patch("elobot.features.hosting.self_hosted.hosting.Clan") as clan_model, \
                patch("elobot.features.hosting.self_hosted.hosting.Player") as player_model, \
                patch("elobot.features.hosting.self_hosted.hosting.random.shuffle"), \
                patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as preflight_match, \
                patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as preflight_player, \
                patch_sites(('elobot.extensions.match.base_store.is_clan_available', 'elobot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)), \
                patch('elobot.extensions.match.hosting.is_raid_weekend', return_value=False), \
                patch("elobot.features.hosting.live.helper_live.Match") as helper_match, \
                patch(
                    "elobot.features.hosting.live.helper_live.live_eligibility.clan_has_all_base_store_slots",
                    new=AsyncMock(return_value=True),
                ), \
                patch_sites(('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=choose), \
                patch("elobot.features.hosting.live.helper_live.live_start.start_live_match", new=start), \
                patch("elobot.features.hosting.live.helper_live.live_start.abort_live_start", new=abort), \
                patch(
                    "elobot.features.hosting.live.helper_live.confirm_helper_availability",
                    wraps=__import__(
                        "elobot.features.hosting.live.helper_live",
                        fromlist=["confirm_helper_availability"],
                    ).confirm_helper_availability,
                ) as eta:
            request_match.find_one = AsyncMock(return_value=match)
            preflight_match.find_one = AsyncMock(return_value=match)
            helper_match.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[self.clan_record])
            player_model.find_one = AsyncMock(side_effect=[docs[REQUESTER_ID], docs["200"]])
            preflight_player.find_one = AsyncMock(side_effect=[docs[REQUESTER_ID], docs["200"]])
            await self.cog.self_host_live_accept.callback.func(self.cog, ctx)
        return ctx, match, request_message, progress_message, abort, eta

    async def test_self_host_live_posts_public_opponent_request(self) -> None:
        from elobot.features.hosting.live.request import parse_live_request_id

        ctx = self.make_context()
        match = self.make_match()
        match.is_war = False
        match.thread = "900"
        opponent = MagicMock(id=200, mention="<@200>")
        self.bot.fetch_user = AsyncMock(return_value=opponent)
        public = MagicMock(id=777)
        public.created_at = datetime.fromtimestamp(1000.9, timezone.utc)
        public.edit = AsyncMock()
        ctx.channel.send.return_value = public
        with patch("elobot.features.hosting.self_hosted.hosting.Match") as match_model, \
                patch.object(self.cog.live_requests, "schedule_expiry") as schedule:
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.self_host_live.callback.func(self.cog, ctx)

        self.assertEqual(self.cog.self_host_live.resolved_name, "self host live")
        self.assertFalse(self.cog.self_host_live.options)
        self.assertEqual(len(self.cog.self_host_live.checks), 1)
        self.assertEqual(self.cog.self_host_live.max_concurrency.concurrent, 1)
        self.assertIs(self.cog.self_host_live.max_concurrency.bucket, ipy.Buckets.USER)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        sent = ctx.channel.send.await_args.kwargs
        self.assertEqual(sent["content"], opponent.mention)
        self.assertEqual(sent["embed"].title, "Live Match Request")
        self.assertEqual(sent["embed"].color, COLOR)
        self.assertNotIn("components", sent)
        public.edit.assert_awaited_once()
        buttons = public.edit.await_args.kwargs["components"].components
        self.assertEqual([(button.label, button.style) for button in buttons], [
            ("Reject", ipy.ButtonStyle.DANGER),
            ("Accept", ipy.ButtonStyle.SUCCESS),
        ])
        self.assertEqual(buttons[0].emoji.name, "whitecross")
        self.assertEqual(buttons[1].emoji, TICK_EMOJI)
        parsed = [parse_live_request_id(button.custom_id) for button in buttons]
        self.assertEqual([item.action for item in parsed], ["reject", "accept"])
        self.assertTrue(all(item.identity.token == "match-token" for item in parsed))
        self.assertTrue(all(item.identity.requester_id == REQUESTER_ID for item in parsed))
        self.assertTrue(all(item.identity.issued_at_ms == 1_000_900 for item in parsed))
        schedule.assert_called_once_with(ctx.channel, 777, parsed[0].identity)
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_live_request_expiry_uses_visible_millisecond_boundary(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        match = self.make_match()
        match.is_war = False
        match.thread = "900"
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_900)
        accept = AsyncMock()
        coordinator = LiveRequestCoordinator(self.cog, accept)

        pending = self.make_live_request_message(identity)
        pending_ctx = self.make_component_context(pending, identity.custom_id("accept"))
        with patch("elobot.features.hosting.live.request.Match") as match_model, \
                patch("elobot.features.hosting.live.request.time.time", return_value=1300.899):
            match_model.find_one = AsyncMock(return_value=match)
            await coordinator.resolve(pending_ctx)
        accept.assert_awaited_once()

        expired = self.make_live_request_message(identity)
        expired_ctx = self.make_component_context(expired, identity.custom_id("accept"))
        accept.reset_mock()
        with patch("elobot.features.hosting.live.request.Match") as match_model, \
                patch("elobot.features.hosting.live.request.time.time", return_value=1300.9):
            match_model.find_one = AsyncMock(return_value=match)
            await coordinator.resolve(expired_ctx)
        accept.assert_not_awaited()
        expired.edit.assert_awaited_once()

    async def test_self_host_live_only_opponent_can_accept_or_reject(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        match = self.make_match()
        match.is_war = False
        match.thread = "900"
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        for actor, action, expected_accepts in (
                (REQUESTER_ID, "accept", 0),
                ("999", "reject", 0),
                ("200", "accept", 1),
                ("200", "reject", 0),
        ):
            with self.subTest(actor=actor, action=action):
                accept = AsyncMock()
                coordinator = LiveRequestCoordinator(self.cog, accept)
                message = self.make_live_request_message(identity)
                ctx = self.make_component_context(message, identity.custom_id(action), actor)
                with patch("elobot.features.hosting.live.request.Match") as match_model, \
                        patch("elobot.features.hosting.live.request.time.time", return_value=1001):
                    match_model.find_one = AsyncMock(return_value=match)
                    await coordinator.resolve(ctx)
                self.assertEqual(accept.await_count, expected_accepts)
                if actor != "200":
                    message.edit.assert_not_awaited()

    async def test_self_host_live_accept_completes_deferred_interaction_before_start(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        # Given an unresolved live request accepted by the opponent.
        match = self.make_match()
        match.is_war = False
        match.thread = "900"
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        message = self.make_live_request_message(identity)
        ctx = self.make_component_context(message, identity.custom_id("accept"))

        async def accept_after_response(
                component_ctx: ipy.ComponentContext,
                _identity: LiveRequestIdentity,
                _match: MagicMock,
                _requester_index: int,
        ) -> None:
            self.assertEqual(component_ctx.send.await_count, 1)

        coordinator = LiveRequestCoordinator(self.cog, accept_after_response)

        # When the opponent accepts it.
        with patch("elobot.features.hosting.live.request.Match") as match_model, \
                patch("elobot.features.hosting.live.request.time.time", return_value=1001):
            match_model.find_one = AsyncMock(return_value=match)
            await coordinator.resolve(ctx)

        # Then Discord receives an ephemeral interaction message before startup.
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])

    async def test_self_host_live_selects_only_clan_with_both_slots_available(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestIdentity

        match = self.make_match()
        match.thread = "900"
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        ctx = self.make_component_context(MagicMock(id=777), "unused")
        partially_available = MagicMock(
            tag="#PARTIAL",
            availability_str="Available",
            available_slots=["Slot 2"],
        )
        fully_available = MagicMock(
            tag="#FULL",
            availability_str="Available",
            available_slots=list(BASE_STORE_SLOTS),
        )
        match_commands = MagicMock()
        match_commands._start_live = AsyncMock()
        self.bot.get_ext.return_value = match_commands

        async def preserve_order(_bot, _match, _player, clans):
            return clans

        with (
                patch("elobot.features.hosting.self_hosted.hosting.Clan") as clan_model,
                patch("elobot.features.hosting.self_hosted.hosting.order_self_host_clans", new=preserve_order),
        ):
            clan_model.find_all.return_value.to_list = AsyncMock(
                return_value=[partially_available, fully_available],
            )
            await self.cog._accept_live_request(ctx, identity, match, 0)

        match_commands._start_live.assert_awaited_once()
        self.assertEqual(match_commands._start_live.await_args.args[1].clan, "#FULL")

    async def test_self_host_live_accept_uses_original_requester_and_exact_http_payload(self) -> None:
        ctx, _match, _request_message, progress_message, abort, eta = await self.invoke_live_accept()
        self.assertEqual(self.events, [
            "choose-p2", "choose-p1", "live-start", "post", "poll-running", "poll-succeeded",
        ])
        self.assertEqual(self.request_api_key, "test-key")
        self.assertFalse(self.post_match_is_war)
        self.assertIs(self.live_start_request.host, self.bot.user)
        ctx.defer.assert_awaited_once_with(ephemeral=True)
        self.bot.get_ext.assert_called_with("MatchCommands")
        self.assertEqual(self.request_payload, {
            "type": "host_live_match",
            "requested_by": REQUESTER_ID,
            "payload": {
                "host_clan_tag": "#2RY0QP8GY",
                "player_1_tag": "#P1",
                "player_2_tag": "#P2",
                "base_links_for_player_1": ["https://p2.one", "https://p2.two"],
                "base_links_for_player_2": ["https://p1.one", "https://p1.two"],
            },
        })
        eta.assert_awaited_once()
        abort.assert_not_awaited()
        progress = next(call for call in ctx.channel.send.await_args_list if JOB_ID in call.args[0])
        self.assertNotIn("ephemeral", progress.kwargs)
        progress_message.edit.assert_awaited()
        self.bot.wait_for_component.assert_awaited()
        cancellation_check = self.bot.wait_for_component.await_args.kwargs["check"]
        self.assertTrue(cancellation_check(MagicMock(ctx=MagicMock(author=MagicMock(id=REQUESTER_ID)))))
        self.assertTrue(cancellation_check(MagicMock(ctx=MagicMock(author=MagicMock(id="200")))))
        self.assertFalse(cancellation_check(MagicMock(ctx=MagicMock(author=MagicMock(id="300")))))

    async def test_self_host_live_opponent_can_cancel_helper_job(self) -> None:
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.author = MagicMock(id="200")
        component_ctx.edit_origin = AsyncMock()
        component_ctx.send = AsyncMock()

        async def opponent_click(*_args, **kwargs):
            event = MagicMock(ctx=component_ctx)
            self.assertTrue(kwargs["check"](event))
            return event

        self.bot.wait_for_component.side_effect = opponent_click
        with patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                new=AsyncMock(side_effect=self.wait_for_component_forever),
        ):
            _ctx, _match, _request_message, progress_message, abort, _eta = await self.invoke_live_accept(
                poll_statuses=["cancelled"],
            )

        self.assertLess(self.events.index("cancel"), self.events.index("poll-cancelled"))
        abort.assert_awaited_once()
        component_ctx.edit_origin.assert_awaited_once()
        self.assertFalse(progress_message.edit.await_args.kwargs["components"])

    async def test_self_host_live_reject_and_timeout_do_not_mutate(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        match = self.make_match()
        match.is_war = False
        match.thread = "900"
        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        accept = AsyncMock()
        coordinator = LiveRequestCoordinator(self.cog, accept)
        rejected = self.make_live_request_message(identity)
        reject_ctx = self.make_component_context(rejected, identity.custom_id("reject"))
        with patch("elobot.features.hosting.live.request.Match") as match_model, \
                patch("elobot.features.hosting.live.request.time.time", return_value=1299):
            match_model.find_one = AsyncMock(return_value=match)
            await coordinator.resolve(reject_ctx)
        expired = self.make_live_request_message(identity)
        channel = MagicMock(fetch_message=AsyncMock(return_value=expired))
        with patch("elobot.features.hosting.live.request.asyncio.sleep", new=AsyncMock()), \
                patch("elobot.features.hosting.live.request.time.time", return_value=1300):
            await coordinator.expire(channel, 778, identity)
        unavailable = self.make_live_request_message(identity)
        unavailable.edit.side_effect = ipy.errors.NotFound(
            MagicMock(status=404, reason="not found"),
            text="not found",
        )
        unavailable_channel = MagicMock(fetch_message=AsyncMock(return_value=unavailable))
        with patch("elobot.features.hosting.live.request.asyncio.sleep", new=AsyncMock()), \
                patch("elobot.features.hosting.live.request.time.time", return_value=1300):
            await coordinator.expire(unavailable_channel, 779, identity)
        accept.assert_not_awaited()
        match.set.assert_not_awaited()
        self.bot.coc_client.get_player.assert_not_awaited()
        self.assertIsNone(self.request_payload)
        rejected.edit.assert_awaited_once()
        expired.edit.assert_awaited_once()

    async def test_self_host_live_stale_click_does_not_mutate(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        stale_cases = []
        stale_cases.append((None, self.make_live_request_message(identity)))
        changed_thread = self.make_match()
        changed_thread.thread = "901"
        stale_cases.append((changed_thread, self.make_live_request_message(identity)))
        changed_participants = self.make_match()
        changed_participants.participants[0].user_id = "other"
        stale_cases.append((changed_participants, self.make_live_request_message(identity)))
        resolved = self.make_live_request_message(identity)
        resolved.components[0].components[0].disabled = True
        stale_cases.append((self.make_match(), resolved))
        for match, message in stale_cases:
            with self.subTest(match=match):
                accept = AsyncMock()
                ctx = self.make_component_context(message, identity.custom_id("accept"))
                with patch("elobot.features.hosting.live.request.Match") as match_model, \
                        patch("elobot.features.hosting.live.request.time.time", return_value=1001):
                    match_model.find_one = AsyncMock(return_value=match)
                    await LiveRequestCoordinator(self.cog, accept).resolve(ctx)
                accept.assert_not_awaited()
                message.edit.assert_not_awaited()
                if match is not None:
                    match.set.assert_not_awaited()
                self.assertIsNone(self.request_payload)
        missing_message = self.make_live_request_message(identity)
        missing_ctx = self.make_component_context(missing_message, identity.custom_id("accept"))
        missing_ctx.channel.fetch_message.side_effect = ipy.errors.NotFound(
            MagicMock(status=404, reason="not found"),
            text="not found",
        )
        accept = AsyncMock()
        await LiveRequestCoordinator(self.cog, accept).resolve(missing_ctx)
        accept.assert_not_awaited()
        missing_ctx.send.assert_awaited_once()

    async def test_self_host_live_double_resolution_starts_once(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity

        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        for losing_action in ("reject", "accept"):
            with self.subTest(losing_action=losing_action):
                match = self.make_match()
                match.is_war = False
                match.thread = "900"
                message = self.make_live_request_message(identity)
                accepted = anyio.Event()
                release = anyio.Event()
                accept_calls = 0

                async def accept(*_args) -> None:
                    nonlocal accept_calls
                    accept_calls += 1
                    accepted.set()
                    with anyio.fail_after(5):
                        await release.wait()

                coordinator = LiveRequestCoordinator(self.cog, accept)
                first = self.make_component_context(message, identity.custom_id("accept"))
                second = self.make_component_context(message, identity.custom_id(losing_action))
                with patch("elobot.features.hosting.live.request.Match") as match_model, \
                        patch("elobot.features.hosting.live.request.time.time", return_value=1001):
                    match_model.find_one = AsyncMock(return_value=match)
                    async with anyio.create_task_group() as task_group:
                        task_group.start_soon(coordinator.resolve, first)
                        with anyio.fail_after(5):
                            await accepted.wait()
                            await coordinator.resolve(second)
                        release.set()
                self.assertEqual(message.edit.await_count, 1)
                self.assertEqual(accept_calls, 1)
                self.assertEqual(first.channel.fetch_message.await_count, 1)
                self.assertEqual(second.channel.fetch_message.await_count, 1)
                self.assertFalse(coordinator._locks)

    async def test_self_host_live_accept_rechecks_both_sides_unstarted(self) -> None:
        from elobot.features.hosting.live.request import LiveRequestCoordinator, LiveRequestIdentity
        from elobot.extensions.match import MatchCommands

        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        for field in ("times", "hosts"):
            for index in range(2):
                with self.subTest(field=field, index=index):
                    match = self.make_match()
                    match.is_war = False
                    getattr(match, field)[index] = 1 if field == "times" else "host"
                    slash = self.make_context()
                    accept = AsyncMock()
                    with patch("elobot.features.hosting.self_hosted.hosting.Match") as command_model:
                        command_model.find_one = AsyncMock(return_value=match)
                        await self.cog.self_host_live.callback.func(self.cog, slash)
                    slash.channel.send.assert_not_awaited()
                    message = self.make_live_request_message(identity)
                    component = self.make_component_context(message, identity.custom_id("accept"))
                    with patch("elobot.features.hosting.live.request.Match") as request_model, \
                            patch("elobot.features.hosting.live.request.time.time", return_value=1001):
                        request_model.find_one = AsyncMock(return_value=match)
                        await LiveRequestCoordinator(self.cog, accept).resolve(component)
                    accept.assert_not_awaited()
                    message.edit.assert_not_awaited()
                    match.set.assert_not_awaited()
                    self.assertIsNone(self.request_payload)

        identity = LiveRequestIdentity("match-token", REQUESTER_ID, 1_000_000)
        for eligibility in ("player1-high-th", "player2-high-th", "raid-weekend"):
            with self.subTest(eligibility=eligibility):
                match = self.make_match()
                match.thread = "900"
                docs = [MagicMock(tag="#P1", elo=1200), MagicMock(tag="#P2", elo=1300)]
                accounts = [
                    MagicMock(tag="#P1", town_hall=6 if eligibility == "player1-high-th" else 3, clan=None),
                    MagicMock(tag="#P2", town_hall=6 if eligibility == "player2-high-th" else 3, clan=None),
                ]
                if eligibility == "player1-high-th":
                    account_results = [accounts[0], MagicMock(clan=None), accounts[0]]
                else:
                    account_results = [accounts[0], MagicMock(clan=None), *accounts]
                self.bot.coc_client.get_player = AsyncMock(side_effect=account_results)
                self.bot.user.id = BOT_ID if eligibility == "raid-weekend" else 0
                self.bot.owner.id = "owner"
                match_commands = MatchCommands(self.bot)
                self.bot.get_ext.return_value = match_commands
                ctx = self.make_component_context(MagicMock(id=777), "unused")
                with patch("elobot.features.hosting.self_hosted.hosting.Clan") as clan_model, \
                        patch("elobot.features.hosting.self_hosted.hosting.Player") as player_model, \
                        patch("elobot.features.hosting.self_hosted.hosting.random.shuffle"), \
                        patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as preflight_match, \
                        patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as preflight_player, \
                        patch_sites(('elobot.extensions.match.base_store.is_clan_available', 'elobot.extensions.match.hosting.is_clan_available'), new=AsyncMock(return_value=True)), \
                        patch('elobot.extensions.match.hosting.is_raid_weekend', return_value=eligibility == "raid-weekend"), \
                        patch(
                            'elobot.extensions.match.hosting.helper_live.host_live_automatically',
                            new_callable=AsyncMock,
                        ) as host_live, \
                        patch_sites(
                            ('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'),
                            new_callable=AsyncMock,
                        ) as choose_bases, \
                        patch(
                            "elobot.features.hosting.live.helper_live.confirm_helper_availability",
                            new_callable=AsyncMock,
                        ) as eta, \
                        patch(
                            "elobot.features.hosting.live.helper_live.live_start.start_live_match",
                            new_callable=AsyncMock,
                        ) as start, \
                        patch_sites(
                            ('elobot.extensions.match.base_store.try_reserve_resource_match_side', 'elobot.extensions.match.hosting.try_reserve_resource_match_side', 'elobot.features.matches.resources.try_reserve_resource_match_side'),
                            new_callable=AsyncMock,
                        ) as reserve_side, \
                        patch_sites(
                            ('elobot.extensions.match.base_store.try_reserve_resource_clan', 'elobot.extensions.match.hosting.try_reserve_resource_clan', 'elobot.features.matches.resources.try_reserve_resource_clan'),
                            new_callable=AsyncMock,
                        ) as reserve_clan, \
                        patch(
                            "elobot.features.hosting.live.helper_live.live_start.abort_live_start",
                            new_callable=AsyncMock,
                        ) as abort:
                    clan_model.find_all.return_value.to_list = AsyncMock(return_value=[self.clan_record])
                    player_model.find_one = AsyncMock(side_effect=docs)
                    preflight_match.find_one = AsyncMock(return_value=match)
                    preflight_player.find_one = AsyncMock(side_effect=docs)
                    await self.cog._accept_live_request(ctx, identity, match, 0)
                host_live.assert_not_awaited()
                choose_bases.assert_not_awaited()
                eta.assert_not_awaited()
                start.assert_not_awaited()
                reserve_side.assert_not_awaited()
                reserve_clan.assert_not_awaited()
                abort.assert_not_awaited()
                self.assertIsNone(self.request_payload)

    async def test_self_host_live_terminal_failure_uses_guarded_abort(self) -> None:
        ctx, _match, _request_message, progress_message, abort, _eta = await self.invoke_live_accept(
            poll_statuses=["failed"],
        )
        abort.assert_awaited_once()
        self.assertEqual(abort.await_args.args[1].message.id, 999)
        self.assertIn("Match aborted", progress_message.edit.await_args.kwargs["content"])
        self.assertEqual(self.events, ["choose-p2", "choose-p1", "live-start", "post", "poll-failed"])
        ctx.edit.assert_not_awaited()

    async def invoke(
            self,
            ctx: ipy.SlashContext,
            match: MagicMock,
            clan_record: MagicMock | None = None,
    ) -> AsyncMock:
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(id=200))
        outcome = HelperTrackingFailure(job_id=JOB_ID, reason="test monitoring stop")
        selected_clan = self.clan_record if clan_record is None else clan_record
        with patch("elobot.extensions.self_service.Match") as match_model, \
                patch("elobot.extensions.self_service.Clan") as clan_model, \
                patch("elobot.features.hosting.war.helper_host.Player") as player_model, \
                patch_sites(('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=AsyncMock(return_value=match)), \
                patch("elobot.features.hosting.war.helper_host.get_available_helper_slot",
                      new=AsyncMock(return_value=1), create=True), \
                patch("elobot.features.hosting.war.war_start.start_war", new_callable=AsyncMock) as start, \
                patch("elobot.features.hosting.war.war_start.abort_war_start", new=self.abort_war_start), \
                patch("elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait", new=AsyncMock(return_value=outcome)):
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(
                return_value=[] if not selected_clan else [selected_clan],
            )
            clan_model.find_one = AsyncMock(return_value=selected_clan)
            player_documents = [MagicMock(tag="#CALLER")]
            if selected_clan and selected_clan.tag == match.host_clans[1]:
                player_documents.append(MagicMock(tag="#OPPONENT"))
            player_model.find_one = AsyncMock(side_effect=player_documents)

            async def commit_start(*_args) -> MagicMock:
                self.events.append("start")
                match.times[0] = 100
                match.hosts[0] = "bot"
                match.host_clans[0] = self.clan.tag
                return MagicMock(id=777)

            start.side_effect = commit_start
            await self.cog.self_host_war.callback.func(self.cog, ctx)
        return start

    def test_extension_exposes_self_host_war_group_without_options(self) -> None:
        self.assertEqual(self.cog.self_host_war.resolved_name, "self host war")
        self.assertFalse(self.cog.self_host_war.options)
        self.assertEqual(len(self.cog.self_host_war.checks), 1)
        self.assertEqual(self.cog.self_host_war.max_concurrency.concurrent, 1)
        self.assertIs(self.cog.self_host_war.max_concurrency.bucket, ipy.Buckets.USER)

    async def test_self_host_war_uses_30_second_helper_timeouts(self) -> None:
        # Given the self-host flow checks ETA and then creates a Helper job.
        ctx = self.make_context()
        match = self.make_match()

        # When both Helper HTTP sessions are opened.
        with patch(
                "elobot.features.hosting.war.helper_host_job.aiohttp.ClientTimeout",
                wraps=aiohttp.ClientTimeout,
        ) as client_timeout:
            await self.invoke(ctx, match)

        # Then both requests receive the requested 30-second timeout.
        self.assertEqual(client_timeout.call_args_list, [call(total=30), call(total=30)])

    def test_host_live_contract_serializes_paired_payload_and_accepts_null_result(self) -> None:
        # Given a paired live-match hosting request and a successful Helper response.
        from elobot.integrations.helper_api import (
            HelperContractMismatchError,
            HelperHostLiveMatchPayload,
            HelperHostWarMatchPayload,
            HelperJob,
            HelperJobCreateRequest,
        )

        request = HelperJobCreateRequest(
            job_type="host_live_match",
            requested_by=REQUESTER_ID,
            payload=HelperHostLiveMatchPayload(
                host_clan_tag="#2RY0QP8GY",
                player_1_tag="#PLAYER1",
                player_2_tag="#PLAYER2",
                base_links_for_player_1=["https://base.one"],
                base_links_for_player_2=["https://base.two"],
            ),
        )

        # When the request is serialized and the terminal response is parsed.
        serialized = request.model_dump(by_alias=True, exclude_none=True)
        job = HelperJob.model_validate({
            "id": JOB_ID,
            "type": "host_live_match",
            "status": "succeeded",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [],
            "result": None,
            "error": None,
        })
        mismatched_request = HelperJobCreateRequest.model_construct(
            job_type="host_live_match",
            requested_by=REQUESTER_ID,
            payload=HelperHostWarMatchPayload(
                host_clan_tag="#2RY0QP8GY",
                player_tag="#PLAYER1",
                base_links=["https://base.one"],
                slot=1,
            ),
        )
        with self.assertRaises(HelperContractMismatchError):
            mismatched_request.validate_payload_type()

        # Then the exact public payload and null-result contract are preserved.
        self.assertEqual(serialized, {
            "type": "host_live_match",
            "requested_by": REQUESTER_ID,
            "payload": {
                "host_clan_tag": "#2RY0QP8GY",
                "player_1_tag": "#PLAYER1",
                "player_2_tag": "#PLAYER2",
                "base_links_for_player_1": ["https://base.one"],
                "base_links_for_player_2": ["https://base.two"],
            },
        })
        self.assertEqual(job.job_type, "host_live_match")
        self.assertIsNone(job.result)

    async def test_shared_host_job_runner_preserves_ambiguous_and_terminal_compensation(self) -> None:
        # Given accepted-but-unconfirmed, failed, and successful shared job outcomes.
        from elobot.integrations.helper_api import (
            HelperHostWarMatchPayload,
            HelperJob,
            HelperJobCreateAcceptedInvalid,
            HelperJobCreateRejected,
            HelperJobCreateRequest,
            HelperJobCreateUnavailable,
            HelperJobCreated,
            HelperTrackingFailure,
        )
        from elobot.features.hosting.war.helper_host_job import HelperHostJobRequest, run_helper_host_job

        create_request = HelperJobCreateRequest(
            job_type="host_war_match",
            requested_by=REQUESTER_ID,
            payload=HelperHostWarMatchPayload(
                host_clan_tag="#2RY0QP8GY",
                player_tag="#PLAYER",
                base_links=["https://base.one"],
                slot=1,
            ),
        )

        def terminal_job(status: str) -> HelperJob:
            return HelperJob.model_validate({
                "id": JOB_ID,
                "type": "host_war_match",
                "status": status,
                "created_at": 100.0,
                "started_at": 101.0,
                "finished_at": 102.0,
                "events": [],
                "result": None,
                "error": "hosting failed" if status == "failed" else None,
            })

        for create_outcome, poll_outcome, expected_aborts, expected_successes in (
                (HelperJobCreateAcceptedInvalid(), None, 0, 0),
                (HelperJobCreateUnavailable(), None, 0, 0),
                (HelperJobCreateRejected(error="invalid request"), None, 1, 0),
                (HelperJobCreated(id=JOB_ID), terminal_job("failed"), 1, 0),
                (HelperJobCreated(id=JOB_ID), terminal_job("cancelled"), 1, 0),
                (HelperJobCreated(id=JOB_ID), HelperTrackingFailure(
                    job_id=JOB_ID,
                    reason="monitoring unavailable",
                ), 0, 0),
                (HelperJobCreated(id=JOB_ID), terminal_job("succeeded"), 0, 1),
        ):
            with self.subTest(create_outcome=create_outcome, poll_outcome=poll_outcome):
                ctx = self.make_context()
                abort_started = AsyncMock(return_value=True)
                success = MagicMock()
                with patch(
                        "elobot.features.hosting.war.helper_host_job.HelperJobCreator.create",
                        new=AsyncMock(return_value=create_outcome),
                ), patch(
                        "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                        new=AsyncMock(return_value=poll_outcome),
                ):
                    # When the shared runner resolves the Helper create/poll lifecycle.
                    await run_helper_host_job(
                        self.cog,
                        ctx,
                        HelperHostJobRequest(
                            create_request=create_request,
                            requester_id=REQUESTER_ID,
                            started_message=MagicMock(id=777),
                            abort_started=abort_started,
                            on_success=success,
                        ),
                    )

                # Then only definitive terminal failure compensates, and success invokes its hook.
                self.assertEqual(abort_started.await_count, expected_aborts)
                self.assertEqual(success.call_count, expected_successes)

    async def test_failed_job_aborts_when_progress_publication_fails(self) -> None:
        import json

        from elobot.integrations.helper_api import HelperHostWarMatchPayload, HelperJobCreateRequest, HelperJobCreated
        from elobot.features.hosting.war.helper_host_job import HelperHostJobRequest, run_helper_host_job

        failed_job = {
            "id": JOB_ID,
            "type": "host_war_match",
            "status": "failed",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [{"t": 101.5, "message": "Progress: failed"}],
            "result": None,
            "error": "Unable to invite the player",
        }

        class FailedJobResponse:
            status = 200

            async def text(self) -> str:
                return json.dumps(failed_job)

            async def __aenter__(self) -> "FailedJobResponse":
                return self

            async def __aexit__(self, *_args: object) -> bool:
                return False

        class FailedJobSession:
            async def __aenter__(self) -> "FailedJobSession":
                return self

            async def __aexit__(self, *_args: object) -> bool:
                return False

            def get(self, *_args: object, **_kwargs: object) -> FailedJobResponse:
                return FailedJobResponse()

        ctx = self.make_context()
        ctx.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=400), "interaction expired")
        started_message = MagicMock(id=777)
        abort_started = AsyncMock(return_value=True)
        with patch(
                "elobot.features.hosting.war.helper_host_job.aiohttp.ClientSession",
                return_value=FailedJobSession(),
        ), patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobCreator.create",
                new=AsyncMock(return_value=HelperJobCreated(id=JOB_ID)),
        ):
            await run_helper_host_job(
                self.cog,
                ctx,
                HelperHostJobRequest(
                    create_request=HelperJobCreateRequest(
                        job_type="host_war_match",
                        requested_by=REQUESTER_ID,
                        payload=HelperHostWarMatchPayload(
                            host_clan_tag="#2RY0QP8GY",
                            player_tag="#PLAYER",
                            base_links=["https://base.one"],
                            slot=1,
                        ),
                    ),
                    requester_id=REQUESTER_ID,
                    started_message=started_message,
                    abort_started=abort_started,
                ),
            )

        abort_started.assert_awaited_once_with(started_message)

    async def test_stored_message_conflict_after_abort_still_reports_the_abort(self) -> None:
        from elobot.features.base_store.publication import BaseStoreMessageConflict
        from elobot.integrations.helper_api import (
            HelperHostWarMatchPayload,
            HelperJob,
            HelperJobCreateRequest,
            HelperJobCreated,
        )
        from elobot.features.hosting.war.helper_host_job import HelperHostJobRequest, run_helper_host_job

        ctx = self.make_context()
        abort_started = AsyncMock(side_effect=BaseStoreMessageConflict(
            "The match was aborted, but its held base-slot message could not be updated.",
        ))
        failed_job = HelperJob.model_validate({
            "id": JOB_ID,
            "type": "host_war_match",
            "status": "failed",
            "created_at": 100.0,
            "started_at": 101.0,
            "finished_at": 102.0,
            "events": [],
            "result": None,
            "error": "hosting failed",
        })
        with patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobCreator.create",
                new=AsyncMock(return_value=HelperJobCreated(id=JOB_ID)),
        ), patch(
                "elobot.features.hosting.war.helper_host_job.HelperJobPoller.wait",
                new=AsyncMock(return_value=failed_job),
        ):
            await run_helper_host_job(
                self.cog,
                ctx,
                HelperHostJobRequest(
                    create_request=HelperJobCreateRequest(
                        job_type="host_war_match",
                        requested_by=REQUESTER_ID,
                        payload=HelperHostWarMatchPayload(
                            host_clan_tag="#2RY0QP8GY",
                            player_tag="#PLAYER",
                            base_links=["https://base.one"],
                            slot=1,
                        ),
                    ),
                    requester_id=REQUESTER_ID,
                    started_message=MagicMock(id=777),
                    abort_started=abort_started,
                ),
            )

        abort_started.assert_awaited_once()
        self.assertIn("Match aborted", ctx.edit.await_args.kwargs["content"])
        self.assertNotIn("could not be aborted safely", ctx.edit.await_args.kwargs["content"])

    async def test_same_type_conflict_adopts_the_open_host_job(self) -> None:
        from elobot.integrations.helper_api import HelperHostWarMatchPayload, HelperJobCreateRequest
        from elobot.features.hosting.war.helper_host_job import HelperHostJobRequest, run_helper_host_job

        self.response_status = 409
        self.response_body = {
            "error": "Job conflict: an open job already exists.",
            "existing_job_id": JOB_ID,
            "existing_job_status": "running",
        }
        self.status_job_type = "host_war_match"
        self.poll_statuses = ["running", "succeeded"]
        ctx = self.make_context()
        abort_started = AsyncMock(return_value=True)
        success = MagicMock()
        await run_helper_host_job(
            self.cog,
            ctx,
            HelperHostJobRequest(
                create_request=HelperJobCreateRequest(
                    job_type="host_war_match",
                    requested_by=REQUESTER_ID,
                    payload=HelperHostWarMatchPayload(
                        host_clan_tag="#2RY0QP8GY",
                        player_tag="#PLAYER",
                        base_links=["https://base.one"],
                        slot=1,
                    ),
                ),
                requester_id=REQUESTER_ID,
                started_message=MagicMock(id=777),
                abort_started=abort_started,
                on_success=success,
            ),
        )
        abort_started.assert_not_awaited()
        success.assert_called_once()
        self.assertIn("is running", ctx.send.await_args_list[0].args[0])
        self.assertNotIn("could not be confirmed", ctx.send.await_args_list[0].args[0])

    async def test_self_host_war_allows_non_war_match_without_converting_during_preflight(self) -> None:
        # Given an unstarted match that has not been marked as a war.
        ctx = self.make_context()
        match = self.make_match()
        match.is_war = False

        # When the real command runs through the local Helper HTTP boundary.
        start = await self.invoke(ctx, match)

        # Then it reaches start without prematurely persisting the match type.
        start.assert_awaited_once()
        self.assertIs(start.await_args.args[2].match, match)
        match.set.assert_not_awaited()
        self.assertEqual(self.request_payload["type"], "host_war_match")

    async def test_war_start_commits_type_with_start_state_and_rolls_back_failed_occupancy(self) -> None:
        from elobot.features.hosting.war import war_start

        for occupancy_fails in (False, True):
            with self.subTest(occupancy_fails=occupancy_fails):
                # Given an unstarted non-war match and an available clan.
                match = self.make_match()
                match.is_war = False
                match.thread = "900"
                match.model_fields_set = {"persistent_host_clans"}
                persisted = {"is_war": False, "times.0": None, "hosts.0": None}

                async def persist(updates):
                    persisted.update(updates)

                async def persist_control(_query, update):
                    await persist(update["$set"])
                    return MagicMock(matched_count=1)

                async def publish(*_args):
                    self.assertTrue(persisted["is_war"])
                    self.assertEqual(persisted["times.0"], match.times[0])
                    self.assertEqual(persisted["hosts.0"], "999")
                    return message

                match.set = AsyncMock(side_effect=persist)
                match.get_pymongo_collection.return_value.update_one = AsyncMock(side_effect=persist_control)
                message = MagicMock(spec=ipy.Message)
                request = war_start.WarStart(
                    match=match, player=0, host=MagicMock(id=999), clan=self.clan,
                    coc_client=self.bot.coc_client, reply=war_start.SelfHostedWarStartReply(),
                )
                occupy = AsyncMock(side_effect=RuntimeError("occupancy failed") if occupancy_fails else None)
                with patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model, \
                        patch_sites(('elobot.extensions.match.dashboards.Clan', 'elobot.features.matches.resources.Clan')) as clan_model, \
                        patch('elobot.features.matches.resources.occupy_clan', new=occupy), \
                        patch_sites(('elobot.extensions.match.results.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new=AsyncMock()), \
                        patch.object(war_start, "complete_war_start", new=AsyncMock(side_effect=publish)) as complete:
                    match_model.find_one = AsyncMock(return_value=match)
                    clan_model.find_one = AsyncMock(return_value=self.clan_record)

                    # When the actual start and reservation claim run.
                    if occupancy_fails:
                        with self.assertRaisesRegex(RuntimeError, "occupancy failed"):
                            await war_start.start_war(self.bot, self.make_context(), request)
                    else:
                        self.assertIs(await war_start.start_war(self.bot, self.make_context(), request), message)

                # Then type and start state commit together, or revert without publishing success.
                self.assertEqual(match.is_war, not occupancy_fails)
                self.assertEqual(persisted["is_war"], not occupancy_fails)
                if occupancy_fails:
                    self.assertIsNone(persisted["times.0"])
                    self.assertIsNone(persisted["hosts.0"])
                    complete.assert_not_awaited()
                else:
                    complete.assert_awaited_once()

    async def test_self_host_war_selects_available_database_clan_and_queues_job(self) -> None:
        ctx = self.make_context()
        match = self.make_match()

        start = await self.invoke(ctx, match)

        self.assertEqual(self.events, ["start", "post"])
        self.assertEqual(self.request_api_key, "test-key")
        self.assertEqual(self.request_payload, {
            "type": "host_war_match",
            "requested_by": REQUESTER_ID,
            "payload": {
                "host_clan_tag": "#2RY0QP8GY",
                "player_tag": "#CALLER",
                "base_links": ["https://base.one", "https://base.two"],
                "slot": 1,
            },
        })
        match.set.assert_not_awaited()
        start.assert_awaited_once()
        war_start_request = start.await_args.args[2]
        self.assertIs(war_start_request.clan, self.clan)
        self.assertIs(war_start_request.host, self.bot.user)
        self.assertEqual(war_start_request.clan_reservation.clan_tag, "#2RY0QP8GY")
        self.assertEqual(war_start_request.side_reservation.key, ("match-token", 0))
        self.assertIn(JOB_ID, ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(LOADING_EMOJI)))
        self.assertIn("has been queued", ctx.send.await_args.args[0])
        self.assertTrue(
            ctx.edit.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))
        ctx.send.return_value.edit.assert_not_awaited()
        self.abort_war_start.assert_not_awaited()

    async def test_self_host_war_applies_persistent_clan_precedence_and_fallbacks(self) -> None:
        async def run_case(
                persistent_availability: str,
                persistent_locked: bool,
                *,
                opponent_finished: bool = False,
        ) -> tuple[list[str], str]:
            ctx = self.make_context()
            match = self.make_match()
            match.persistent_host_clans[0] = "#PERSISTENT"
            if opponent_finished:
                match.participants[1].result = ["3.100", "2.90", "2.80"]
                match.host_clans[1] = "#PERSISTENT"
            fallback = MagicMock(tag="#FALLBACK", availability_str="Available")
            persistent = MagicMock(tag="#PERSISTENT", availability_str=persistent_availability)
            records = [fallback, persistent]
            resolved_clans = {
                "#FALLBACK": MagicMock(tag="#FALLBACK", name="Fallback Helper"),
                "#PERSISTENT": MagicMock(tag="#PERSISTENT", name="Persistent Helper"),
            }
            self.bot.coc_client.get_clan = AsyncMock(side_effect=resolved_clans.__getitem__)
            player_accounts = [MagicMock(clan=None)]
            if not opponent_finished:
                player_accounts.append(MagicMock(clan=MagicMock(tag="#PERSISTENT")))
            self.bot.coc_client.get_player = AsyncMock(side_effect=player_accounts)
            attempts: list[str] = []
            selected_tag: str | None = None

            async def reserve_clan(clan_tag: str) -> MagicMock | None:
                nonlocal selected_tag
                attempts.append(clan_tag)
                if persistent_locked and clan_tag == "#PERSISTENT":
                    return None
                selected_tag = clan_tag
                return MagicMock(clan_tag=clan_tag)

            async def find_selected_clan(*_args, **_kwargs) -> MagicMock:
                return next(record for record in records if record.tag == selected_tag)

            with patch("elobot.extensions.self_service.Match") as match_model, \
                    patch("elobot.extensions.self_service.Clan") as clan_model, \
                    patch("elobot.extensions.self_service.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)), \
                    patch("elobot.features.hosting.self_hosted.hosting.random.shuffle"), \
                    patch("elobot.extensions.self_service.try_reserve_resource_clan", new=AsyncMock(side_effect=reserve_clan)), \
                    patch("elobot.extensions.self_service.try_reserve_resource_match_side", new=AsyncMock(
                        return_value=MagicMock(key=("match-token", 0)))), \
                    patch("elobot.extensions.self_service.host_war_automatically", new_callable=AsyncMock) as automatic_host:
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=records)
                clan_model.find_one = AsyncMock(side_effect=find_selected_clan)

                await self.cog.self_host_war.callback.func(self.cog, ctx)

            return attempts, automatic_host.await_args.args[2].clan.tag

        attempts, selected = await run_case("Available", persistent_locked=False)
        self.assertEqual(attempts, ["#PERSISTENT"])
        self.assertEqual(selected, "#PERSISTENT")

        attempts, selected = await run_case("Available", persistent_locked=True)
        self.assertEqual(attempts, ["#PERSISTENT", "#FALLBACK"])
        self.assertEqual(selected, "#FALLBACK")

        attempts, selected = await run_case("Occupied", persistent_locked=False)
        self.assertEqual(attempts, ["#FALLBACK"])
        self.assertEqual(selected, "#FALLBACK")

        attempts, selected = await run_case("Available", persistent_locked=False, opponent_finished=True)
        self.assertEqual(attempts, ["#FALLBACK"])
        self.assertEqual(selected, "#FALLBACK")

    async def test_self_host_war_opponent_live_clan_overrides_callers_current_clan(self) -> None:
        # Given both players are currently in the clan shuffled to the front.
        ctx = self.make_context()
        match = self.make_match()
        shared = MagicMock(tag="#SHARED", availability_str="Available")
        fallback = MagicMock(tag="#FALLBACK", availability_str="Available")
        records = [shared, fallback]
        resolved_fallback = MagicMock(tag="#FALLBACK", name="Fallback Helper")
        self.bot.coc_client.get_clan = AsyncMock(return_value=resolved_fallback)
        self.self_commands_player_model.find_one = AsyncMock(side_effect=[
            MagicMock(tag="#CALLER"),
            MagicMock(tag="#OPPONENT"),
        ])
        self.bot.coc_client.get_player = AsyncMock(side_effect=[
            MagicMock(clan=MagicMock(tag="#SHARED")),
            MagicMock(clan=MagicMock(tag="#SHARED")),
        ])

        with patch("elobot.extensions.self_service.Match") as match_model, \
                patch("elobot.extensions.self_service.Clan") as clan_model, \
                patch("elobot.extensions.self_service.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)), \
                patch("elobot.features.hosting.self_hosted.hosting.random.shuffle"), \
                patch("elobot.extensions.self_service.host_war_automatically", new_callable=AsyncMock) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=records)
            clan_model.find_one = AsyncMock(return_value=fallback)

            # When the caller requests a self-host clan.
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        # Then opponent safety moves the shared live clan behind the fallback.
        self.assertEqual(
            [call("#CALLER"), call("#OPPONENT")],
            self.bot.coc_client.get_player.await_args_list,
        )
        self.bot.coc_client.get_clan.assert_awaited_once_with("#FALLBACK")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_fallback)

    async def test_self_host_war_kicks_opponent_when_opponent_clan_is_selected(self) -> None:
        # Given the opponent's completed side has released its host clan for reuse.
        ctx = self.make_context()
        match = self.make_match()
        match.participants[1].result = ["3.100", "2.90", "2.80"]
        match.host_clans[1] = self.clan_record.tag

        # When the opponent clan is the available fallback.
        await self.invoke(ctx, match)

        # Then the existing Helper host job evicts that opponent before hosting the caller.
        assert self.request_payload is not None
        payload = self.request_payload["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["opponent_player_tag"], "#OPPONENT")
        self.assertIs(payload["kick_opponent"], True)

    async def test_self_host_war_denies_missing_match_or_outsider(self) -> None:
        for match, author in ((None, REQUESTER_ID), (self.make_match(), "300")):
            with self.subTest(author=author):
                ctx = self.make_context(author)
                with patch("elobot.extensions.self_service.Match") as match_model:
                    match_model.find_one = AsyncMock(return_value=match)
                    await self.cog.self_host_war.callback.func(self.cog, ctx)
                self.assertIsNone(self.request_payload)
                ctx.send.assert_awaited_once()
                if match is not None:
                    match.set.assert_not_awaited()

    async def test_self_host_war_denies_started_side_before_helper_request(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        match.times[0] = 1
        with patch("elobot.extensions.self_service.Match") as match_model:
            match_model.find_one = AsyncMock(return_value=match)
            await self.cog.self_host_war.callback.func(self.cog, ctx)
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        match.set.assert_not_awaited()
        self.assertIsNone(self.request_payload)

    async def test_self_host_war_denies_when_no_clan_is_available(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        choose = AsyncMock(return_value=match)
        with patch("elobot.extensions.self_service.Match") as match_model, \
                patch("elobot.extensions.self_service.Clan") as clan_model, \
                patch_sites(('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=choose):
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[])
            await self.cog.self_host_war.callback.func(self.cog, ctx)
        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.bot.coc_client.get_player.assert_not_awaited()
        choose.assert_not_awaited()
        match.set.assert_not_awaited()
        self.assertIsNone(self.request_payload)

    async def test_missing_caller_player_fails_before_base_selection(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        choose = AsyncMock(return_value=match)
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(id=200))

        with patch("elobot.extensions.self_service.Match") as match_model, \
                patch("elobot.extensions.self_service.Clan") as clan_model, \
                patch("elobot.features.hosting.war.helper_host.Player") as player_model, \
                patch("elobot.features.hosting.war.helper_host.get_available_helper_slot",
                      new=AsyncMock(return_value=1)), \
                patch_sites(('elobot.extensions.match.base_store.choose_bases', 'elobot.features.matches.attack_bases.choose_bases'), new=choose):
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[self.clan_record])
            clan_model.find_one = AsyncMock(return_value=self.clan_record)
            player_model.find_one = AsyncMock(return_value=None)
            with self.assertRaises(AttributeError):
                await self.cog.self_host_war.callback.func(self.cog, ctx)

        choose.assert_not_awaited()

    async def test_raid_weekend_rejection_aborts_started_match(self) -> None:
        self.response_status = 400
        self.response_body = {
            "error": "Can't host a war-style match during Raid Weekend "
                     "(Friday 07:00 UTC to Monday 07:00 UTC).",
        }
        ctx = self.make_context()
        match = self.make_match()

        start = await self.invoke(ctx, match)

        self.assertIn("during Raid Weekend", ctx.send.await_args.args[0])
        self.assertIn("Match aborted", ctx.send.await_args.args[0])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        match.set.assert_not_awaited()
        start.assert_awaited_once()
        self.abort_war_start.assert_awaited_once()

    async def test_malformed_acceptance_keeps_started_match_active(self) -> None:
        self.response_body = {"id": "invalid"}
        ctx = self.make_context()
        match = self.make_match()

        start = await self.invoke(ctx, match)

        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn("remains active", ctx.send.await_args.args[0])
        self.assertIn("before retrying", ctx.send.await_args.args[0])
        match.set.assert_not_awaited()
        start.assert_awaited_once()
        self.abort_war_start.assert_not_awaited()

    async def test_accepted_unconfirmed_job_cannot_be_retried_into_a_duplicate(self) -> None:
        # Given Helper creates a job but its first accepted response cannot be identified.
        valid_response = self.response_body
        self.response_body = {"id": "invalid"}
        match = self.make_match()
        first_ctx = self.make_context()

        # When the requester retries after the ambiguous first response.
        first_start = await self.invoke(first_ctx, match)
        self.response_body = valid_response
        second_start = await self.invoke(self.make_context(), match)

        # Then the durable first start prevents a second Helper job submission.
        self.assertEqual(self.events.count("post"), 1)
        first_start.assert_awaited_once()
        second_start.assert_not_awaited()
        self.assertIn("remains active", first_ctx.send.await_args.args[0])

    async def test_non_201_response_keeps_started_match_active(self) -> None:
        self.response_status = 503
        ctx = self.make_context()

        start = await self.invoke(ctx, self.make_match())

        self.assertTrue(ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.assertIn("remains active", ctx.send.await_args.args[0])
        self.assertIn("before retrying", ctx.send.await_args.args[0])
        start.assert_awaited_once()
        self.abort_war_start.assert_not_awaited()

    async def test_job_post_network_failure_keeps_started_match_active(self) -> None:
        self.stop_after_eta = True
        ctx = self.make_context()

        start = await self.invoke(ctx, self.make_match())

        self.assertIsNone(self.request_payload)
        self.assertIn("remains active", ctx.send.await_args.args[0])
        self.assertIn("before retrying", ctx.send.await_args.args[0])
        start.assert_awaited_once()
        self.abort_war_start.assert_not_awaited()

    async def test_eta_network_failure_stops_before_start(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        await self.site.stop()

        start = await self.invoke(ctx, match)

        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        match.set.assert_not_awaited()
        start.assert_not_awaited()
        self.abort_war_start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
