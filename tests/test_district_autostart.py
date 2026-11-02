# noqa: SIZE_OK - command, payload, and lifecycle cases share the district-host fixture

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.live_hosting import helper_live
from cogs.match_commands import MatchCommands
from cogs.self_commands import SelfCommands
from cogs.war_hosting import helper_host
from cogs.war_hosting.helper_api import (
    HelperHostWarMatchPayload,
    HelperJobCreateRejected,
    HelperJobCreateRequest,
)
from cogs.war_hosting.helper_host_job import HelperHostJobRequest, run_helper_host_job


DISTRICTS = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]


class TestDistrictAutostartCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = MagicMock()
        self.bot.user = MagicMock(id=9999)
        self.bot.owner = MagicMock(id=9998)
        self.bot.coc_client.get_clan = AsyncMock(
            return_value=MagicMock(tag="#CLAN", name="Helper Clan"),
        )
        self.bot.coc_client.get_player = AsyncMock()
        self.match_cog = MatchCommands(self.bot)
        self.self_cog = SelfCommands(self.bot)

    @staticmethod
    def context(author_id: int = 100) -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=author_id)
        ctx.channel = MagicMock(id=12345)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def match(*, is_war: bool, started: bool = True) -> MagicMock:
        return MagicMock(
            token="TOKEN",
            thread="12345",
            is_war=is_war,
            is_autowin=False,
            times=[1000 if started else None, None],
            hosts=["888" if started else None, None],
            host_clans=["#CLAN" if started else None, None],
            final_districts=list(DISTRICTS),
            participants=[
                MagicMock(user_id="100", result=[None] * 3),
                MagicMock(user_id="200", result=[None] * 3),
            ],
        )

    async def test_match_war_district_requires_started_matching_side(self) -> None:
        ctx = self.context()
        match = self.match(is_war=True, started=False)
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.helper_host.host_war_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.match_cog.match_war_autostart.callback.func(
                self.match_cog,
                ctx,
                player=0,
                clan="#CLAN",
                district=DISTRICTS[0],
            )

        host_district.assert_not_awaited()
        self.bot.coc_client.get_clan.assert_not_awaited()
        self.assertIn("only select a district after", ctx.send.await_args.args[0].lower())

    async def test_match_war_district_validates_match_membership(self) -> None:
        ctx = self.context()
        match = self.match(is_war=True)
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.helper_host.host_war_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.match_cog.match_war_autostart.callback.func(
                self.match_cog,
                ctx,
                player=0,
                clan="#CLAN",
                district="Capital Peak",
            )

        host_district.assert_not_awaited()
        self.assertIn("not one of this match's districts", ctx.send.await_args.args[0].lower())

    async def test_match_war_district_reuses_started_clan_without_start_reservation(self) -> None:
        ctx = self.context(author_id=777)
        match = self.match(is_war=True)
        clan = self.bot.coc_client.get_clan.return_value
        side = MagicMock()
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.try_reserve_resource_match_side",
                      new=AsyncMock(return_value=side)),
                patch("cogs.match_commands.try_reserve_resource_clan",
                      new_callable=AsyncMock) as reserve_clan,
                patch("cogs.match_commands.confirm_pending_opponent_bases",
                      new_callable=AsyncMock) as confirm_bases,
                patch("cogs.match_commands.helper_host.host_war_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.match_cog.match_war_autostart.callback.func(
                self.match_cog,
                ctx,
                player=0,
                clan="#CLAN",
                district=DISTRICTS[1],
            )

        reserve_clan.assert_not_awaited()
        confirm_bases.assert_not_awaited()
        host_district.assert_awaited_once()
        request = host_district.await_args.args[2]
        self.assertIs(request.match, match)
        self.assertIs(request.clan, clan)
        self.assertEqual(request.district, DISTRICTS[1])
        self.assertEqual(request.requested_by, "777")
        side.release.assert_called_once_with()

    async def test_match_live_district_requires_started_match(self) -> None:
        ctx = self.context()
        match = self.match(is_war=False, started=False)
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.helper_live.host_live_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.match_cog.match_autostart.callback.func(
                self.match_cog,
                ctx,
                clan="#CLAN",
                district=DISTRICTS[0],
            )

        host_district.assert_not_awaited()
        self.bot.coc_client.get_player.assert_not_awaited()
        self.assertIn("only select a district after", ctx.send.await_args.args[0].lower())

    async def test_match_live_district_validates_match_membership(self) -> None:
        ctx = self.context()
        match = self.match(is_war=False)
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.helper_live.host_live_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await self.match_cog.match_autostart.callback.func(
                self.match_cog,
                ctx,
                clan="#CLAN",
                district="Capital Peak",
            )

        host_district.assert_not_awaited()
        self.assertIn("not one of this match's districts", ctx.send.await_args.args[0].lower())

    async def test_match_district_requires_recorded_host_clan(self) -> None:
        for command_name, is_war, kwargs in (
                (
                    "war",
                    True,
                    {"player": 0, "clan": "#OTHER", "district": DISTRICTS[0]},
                ),
                (
                    "live",
                    False,
                    {"clan": "#OTHER", "district": DISTRICTS[0]},
                ),
        ):
            with self.subTest(command=command_name):
                ctx = self.context()
                match = self.match(is_war=is_war)
                with (
                        patch("cogs.match_commands.Match") as match_model,
                        patch("cogs.match_commands.is_match_finished", return_value=False),
                ):
                    match_model.find_one = AsyncMock(return_value=match)
                    if is_war:
                        await self.match_cog.match_war_autostart.callback.func(
                            self.match_cog,
                            ctx,
                            **kwargs,
                        )
                    else:
                        await self.match_cog.match_autostart.callback.func(
                            self.match_cog,
                            ctx,
                            **kwargs,
                        )

                self.assertIn("select that clan", ctx.send.await_args.args[0].lower())
                self.bot.coc_client.get_clan.assert_not_awaited()

    async def test_match_live_district_routes_registered_players_without_restarting(self) -> None:
        ctx = self.context(author_id=777)
        match = self.match(is_war=False)
        clan = self.bot.coc_client.get_clan.return_value
        player1 = MagicMock(tag="#P1")
        player2 = MagicMock(tag="#P2")
        side0 = MagicMock()
        side1 = MagicMock()
        with (
                patch("cogs.match_commands.Match") as match_model,
                patch("cogs.match_commands.Player") as player_model,
                patch("cogs.match_commands.is_match_finished", return_value=False),
                patch("cogs.match_commands.try_reserve_resource_match_side",
                      new=AsyncMock(side_effect=[side0, side1])),
                patch("cogs.match_commands.try_reserve_resource_clan",
                      new_callable=AsyncMock) as reserve_clan,
                patch("cogs.match_commands.live_start.start_live_match",
                      new_callable=AsyncMock) as start_live,
                patch("cogs.match_commands.helper_live.host_live_district_automatically",
                      new_callable=AsyncMock) as host_district,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(side_effect=[player1, player2])
            await self.match_cog.match_autostart.callback.func(
                self.match_cog,
                ctx,
                clan="#CLAN",
                district=DISTRICTS[2],
            )

        reserve_clan.assert_not_awaited()
        start_live.assert_not_awaited()
        self.bot.coc_client.get_player.assert_not_awaited()
        host_district.assert_awaited_once()
        request = host_district.await_args.args[2]
        self.assertIs(request.match, match)
        self.assertIs(request.clan, clan)
        self.assertEqual(request.player1_tag, "#P1")
        self.assertEqual(request.player2_tag, "#P2")
        self.assertEqual(request.district, DISTRICTS[2])
        self.assertEqual(request.requested_by, "777")
        self.assertFalse(request.allow_opponent_cancel)
        side0.release.assert_called_once_with()
        side1.release.assert_called_once_with()

    async def test_self_host_commands_reject_district_argument(self) -> None:
        for command in (self.self_cog.self_host_live, self.self_cog.self_host_war):
            with self.subTest(command=command.resolved_name):
                ctx = self.context()
                self.assertFalse(command.options)
                with self.assertRaisesRegex(TypeError, "unexpected keyword argument 'district'"):
                    await command.callback.func(self.self_cog, ctx, district=DISTRICTS[0])
                ctx.defer.assert_not_awaited()
                self.bot.coc_client.get_clan.assert_not_awaited()


class TestDistrictHelperPayloads(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=777)
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def match(*, is_war: bool) -> MagicMock:
        return MagicMock(
            token="TOKEN",
            is_war=is_war,
            is_autowin=False,
            times=[1000, None],
            hosts=["888", None],
            host_clans=["#CLAN", None],
            final_districts=list(DISTRICTS),
            participants=[
                MagicMock(
                    user_id="100",
                    result=[None] * 3,
                    bases=[
                        MagicMock(link="https://p1-a"),
                        MagicMock(link="https://p1-b"),
                        MagicMock(link="https://p1-c"),
                    ],
                ),
                MagicMock(
                    user_id="200",
                    result=[None] * 3,
                    bases=[
                        MagicMock(link="https://p2-a"),
                        MagicMock(link="https://p2-b"),
                        MagicMock(link="https://p2-c"),
                    ],
                ),
            ],
        )

    async def test_war_rerun_sends_only_selected_opponent_base(self) -> None:
        match = self.match(is_war=True)
        runtime = MagicMock()
        ctx = self.context()
        request = helper_host.AutomaticWarDistrictHost(
            match=match,
            player=0,
            clan=MagicMock(tag="#CLAN"),
            district=DISTRICTS[1],
            requested_by="777",
        )
        with (
                patch("cogs.war_hosting.helper_host.Match") as match_model,
                patch("cogs.war_hosting.helper_host.Player") as player_model,
                patch("cogs.war_hosting.helper_host.get_available_helper_slot",
                      new=AsyncMock(return_value=2), create=True) as available_slot,
                patch("cogs.war_hosting.helper_host.confirm_helper_availability",
                      new=AsyncMock(return_value=True)),
                patch("cogs.war_hosting.helper_host.run_helper_host_job",
                      new_callable=AsyncMock) as run_job,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(return_value=MagicMock(tag="#P1"))
            await helper_host.host_war_district_automatically(runtime, ctx, request)

        run_job.assert_awaited_once()
        job_request = run_job.await_args.args[2]
        self.assertEqual(job_request.create_request.model_dump(by_alias=True, exclude_none=True), {
            "type": "host_war_match",
            "requested_by": "777",
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_tag": "#P1",
                "base_links": ["https://p2-b"],
                "slot": 2,
            },
        })
        available_slot.assert_awaited_once_with("#CLAN", bot=runtime.bot)
        self.assertIsNone(job_request.started_message)
        self.assertIsNone(job_request.abort_started)

    async def test_live_rerun_sends_only_selected_paired_bases(self) -> None:
        match = self.match(is_war=False)
        runtime = MagicMock()
        ctx = self.context()
        request = helper_live.AutomaticLiveDistrictHost(
            match=match,
            clan=MagicMock(tag="#CLAN"),
            player1_tag="#P1",
            player2_tag="#P2",
            district=DISTRICTS[2],
            requested_by="100",
            allow_opponent_cancel=True,
        )
        with (
                patch("cogs.live_hosting.helper_live.Match") as match_model,
                patch("cogs.live_hosting.helper_live.confirm_helper_availability",
                      new=AsyncMock(return_value=True)),
                patch("cogs.live_hosting.helper_live.run_helper_host_job",
                      new_callable=AsyncMock) as run_job,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            await helper_live.host_live_district_automatically(runtime, ctx, request)

        run_job.assert_awaited_once()
        job_request = run_job.await_args.args[2]
        self.assertEqual(job_request.create_request.model_dump(by_alias=True, exclude_none=True), {
            "type": "host_live_match",
            "requested_by": "100",
            "payload": {
                "host_clan_tag": "#CLAN",
                "player_1_tag": "#P1",
                "player_2_tag": "#P2",
                "base_links_for_player_1": ["https://p2-c"],
                "base_links_for_player_2": ["https://p1-c"],
            },
        })
        self.assertIsNone(job_request.started_message)
        self.assertIsNone(job_request.abort_started)
        self.assertEqual(job_request.additional_cancel_user_ids, frozenset({"200"}))

    def test_live_opponent_authorization_requires_participant_requester(self) -> None:
        match = self.match(is_war=False)

        self.assertEqual(helper_live.opponent_cancel_user_ids(match, "100"), frozenset({"200"}))
        self.assertFalse(helper_live.opponent_cancel_user_ids(match, "777"))

    async def test_rejected_rerun_job_keeps_existing_match_active(self) -> None:
        create_request = HelperJobCreateRequest(
            job_type="host_war_match",
            requested_by="777",
            payload=HelperHostWarMatchPayload(
                host_clan_tag="#CLAN",
                player_tag="#P1",
                base_links=["https://base"],
                slot=1,
            ),
        )
        runtime = MagicMock(helper_api_base_url="https://helper.invalid", helper_api_key="key")
        ctx = self.context()
        with patch(
                "cogs.war_hosting.helper_host_job.HelperJobCreator.create",
                new=AsyncMock(return_value=HelperJobCreateRejected(error="rejected")),
        ):
            await run_helper_host_job(
                runtime,
                ctx,
                HelperHostJobRequest(create_request=create_request, requester_id="777"),
            )

        self.assertIn("existing match remains active", ctx.send.await_args.args[0].lower())


if __name__ == "__main__":
    unittest.main()
