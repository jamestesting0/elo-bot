# noqa: SIZE_OK - clan selection and concurrency scenarios share one reservation fixture
import asyncio  # noqa: ANYIO_OK - tests exercise the requested asyncio lock and Event semantics
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import cogs.match_commands as match_commands
import cogs.self_hosting.hosting as self_hosting
from cogs.self_commands import SelfCommands
from elobot.config.ids import BOT_ID


REQUESTER_ID = "123456789012345678"


class TestSelfHostAvailability(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands._RESOURCE_CLAN_RESERVATIONS.set({})
        match_commands._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})
        self.bot = MagicMock()
        self.bot.user = MagicMock(id=BOT_ID)
        self.bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(tag="#CLAN", name="Helper Clan"))
        self.bot.coc_client.get_player = AsyncMock(return_value=MagicMock(clan=None))
        self.player_patcher = patch("cogs.self_hosting.hosting.Player")
        self.player_model = self.player_patcher.start()
        self.addCleanup(self.player_patcher.stop)
        self.player_model.find_one = AsyncMock(return_value=MagicMock(tag="#PLAYER"))
        weekend_patcher = patch("cogs.self_commands.is_raid_weekend", return_value=False)
        weekend_patcher.start()
        self.addCleanup(weekend_patcher.stop)
        self.cog = SelfCommands(self.bot)
        self.cog.helper_api_base_url = "http://127.0.0.1:1"
        self.cog.helper_api_key = "test-key"

    @staticmethod
    def make_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=REQUESTER_ID, mention=f"<@{REQUESTER_ID}>", send=AsyncMock())
        ctx.channel = MagicMock(
            id=900,
            send=AsyncMock(),
            created_at=datetime.now(timezone.utc),
        )
        ctx.channel.parent = None
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock(return_value=MagicMock(id=700))
        ctx.delete = AsyncMock()
        return ctx

    @staticmethod
    def make_match() -> MagicMock:
        match = MagicMock(
            participants=[
                MagicMock(user_id=REQUESTER_ID, result=[None] * 3, base_store=None, opponent_bases_stored=False),
                MagicMock(user_id="200", result=[None] * 3, base_store=None, opponent_bases_stored=False),
            ],
            is_war=True,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            persistent_host_clans=[None, None],
        )
        match.set = AsyncMock()
        return match

    @staticmethod
    def make_match_for(user_id: str) -> MagicMock:
        match = TestSelfHostAvailability.make_match()
        match.participants[0].user_id = user_id
        return match



    async def test_active_side_stops_before_clan_lookup(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        match.times[0] = 1000

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_one = AsyncMock()
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        clan_model.find_one.assert_not_awaited()
        match.set.assert_not_awaited()

    async def test_self_host_randomizes_available_clans_before_reservation(self) -> None:
        # Given two available clans and one unavailable clan in database order.
        ctx = self.make_context()
        match = self.make_match()
        persisted_first = MagicMock(tag="#FIRST", availability_str="Available")
        shuffled_first = MagicMock(tag="#SECOND", availability_str="Available")
        unavailable = MagicMock(tag="#BUSY", availability_str="Occupied")
        resolved_clans = {
            "#FIRST": MagicMock(tag="#FIRST", name="First Helper"),
            "#SECOND": MagicMock(tag="#SECOND", name="Second Helper"),
        }
        self.bot.coc_client.get_clan = AsyncMock(side_effect=resolved_clans.__getitem__)
        shuffled_tags: list[str] = []

        def reverse_candidates(candidates: list[MagicMock]) -> None:
            shuffled_tags.extend(candidate.tag for candidate in candidates)
            candidates.reverse()

        random_module = MagicMock()
        random_module.shuffle.side_effect = reverse_candidates

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host, \
                patch.dict(vars(self_hosting), {"random": random_module}):
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(
                return_value=[persisted_first, shuffled_first, unavailable],
            )
            clan_model.find_one = AsyncMock(return_value=shuffled_first)

            # When the participant requests automatic hosting.
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        # Then only available clans are shuffled and the shuffled first clan is selected.
        self.assertEqual(shuffled_tags, ["#FIRST", "#SECOND"])
        self.bot.coc_client.get_clan.assert_awaited_once_with("#SECOND")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_clans["#SECOND"])

    async def test_self_host_prioritizes_callers_current_clan_after_shuffle(self) -> None:
        # Given the caller's current clan is behind another clan after shuffling.
        ctx = self.make_context()
        match = self.make_match()
        fallback = MagicMock(tag="#FALLBACK", availability_str="Available")
        current = MagicMock(tag="#CURRENT", availability_str="Available")
        resolved_current = MagicMock(tag="#CURRENT", name="Current Helper")
        self.bot.coc_client.get_clan = AsyncMock(return_value=resolved_current)
        self.bot.coc_client.get_player = AsyncMock(side_effect=[
            MagicMock(clan=MagicMock(tag="#CURRENT")),
            MagicMock(clan=None),
        ])

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_hosting.hosting.random.shuffle"), \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[fallback, current])
            clan_model.find_one = AsyncMock(return_value=current)

            # When the caller requests automatic hosting.
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        # Then their current clan is selected before the shuffled fallback.
        self.bot.coc_client.get_clan.assert_awaited_once_with("#CURRENT")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_current)

    async def test_self_host_finished_opponent_uses_recorded_attack_clan(self) -> None:
        # Given the caller is now in the clan where the opponent completed this match.
        ctx = self.make_context()
        match = self.make_match()
        match.participants[1].result = ["3.100", "2.90", "2.80"]
        match.host_clans[1] = "#ATTACKS"
        match.persistent_host_clans[0] = "#ATTACKS"
        attack_clan = MagicMock(tag="#ATTACKS", availability_str="Available")
        fallback = MagicMock(tag="#FALLBACK", availability_str="Available")
        resolved_fallback = MagicMock(tag="#FALLBACK", name="Fallback Helper")
        self.bot.coc_client.get_clan = AsyncMock(return_value=resolved_fallback)
        self.bot.coc_client.get_player = AsyncMock(
            return_value=MagicMock(clan=MagicMock(tag="#ATTACKS")),
        )

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_hosting.hosting.random.shuffle"), \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[attack_clan, fallback])
            clan_model.find_one = AsyncMock(return_value=fallback)

            # When the caller requests automatic hosting.
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        # Then the recorded attack clan is demoted without looking up the opponent's live clan.
        self.assertEqual(self.bot.coc_client.get_player.await_count, 1)
        self.bot.coc_client.get_clan.assert_awaited_once_with("#FALLBACK")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_fallback)

    async def test_self_host_prefers_non_opponent_clan_over_shuffled_opponent_clan(self) -> None:
        # Given an available opponent clan shuffled ahead of another available clan.
        ctx = self.make_context()
        match = self.make_match()
        opponent_clan = MagicMock(tag="#OPP", availability_str="Available")
        preferred_clan = MagicMock(tag="#PREFERRED", availability_str="Available")
        resolved_clans = {
            "#OPP": MagicMock(tag="#OPP", name="Opponent Helper"),
            "#PREFERRED": MagicMock(tag="#PREFERRED", name="Preferred Helper"),
        }
        self.bot.coc_client.get_clan = AsyncMock(side_effect=resolved_clans.__getitem__)
        self.bot.coc_client.get_player = AsyncMock(side_effect=[
            MagicMock(clan=None),
            MagicMock(clan=MagicMock(tag="#OPP")),
        ])
        random_module = MagicMock()
        random_module.shuffle.side_effect = lambda candidates: None

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host, \
                patch.dict(vars(self_hosting), {"random": random_module}):
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(
                return_value=[opponent_clan, preferred_clan],
            )
            clan_model.find_one = AsyncMock(return_value=preferred_clan)

            # When the participant requests automatic hosting.
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        # Then the non-opponent clan is selected before the opponent fallback.
        self.bot.coc_client.get_clan.assert_awaited_once_with("#PREFERRED")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_clans["#PREFERRED"])

    async def test_self_host_falls_back_to_opponent_clan_when_preferred_clan_is_locked(self) -> None:
        # Given an available preferred clan whose reservation is already held.
        ctx = self.make_context()
        match = self.make_match()
        preferred_clan = MagicMock(tag="#PREFERRED", availability_str="Available")
        opponent_clan = MagicMock(tag="#OPP", availability_str="Available")
        resolved_opponent = MagicMock(tag="#OPP", name="Opponent Helper")
        self.bot.coc_client.get_clan = AsyncMock(return_value=resolved_opponent)
        self.bot.coc_client.get_player = AsyncMock(side_effect=[
            MagicMock(clan=None),
            MagicMock(clan=MagicMock(tag="#OPP")),
        ])
        attempts: list[str] = []
        preferred_reservation = await match_commands.try_reserve_resource_clan("#PREFERRED")
        if not preferred_reservation:
            self.fail("The preferred-clan setup reservation was not acquired.")

        async def record_reservation(clan_tag: str) -> match_commands.ResourceClanReservation | None:
            attempts.append(clan_tag)
            return await match_commands.try_reserve_resource_clan(clan_tag)

        random_module = MagicMock()
        random_module.shuffle.side_effect = lambda candidates: None
        try:
            with patch("cogs.self_commands.Match") as match_model, \
                    patch("cogs.self_commands.Clan") as clan_model, \
                    patch("cogs.self_commands.try_reserve_resource_clan",
                          new=AsyncMock(side_effect=record_reservation)), \
                    patch("cogs.self_commands.host_war_automatically",
                          new_callable=AsyncMock) as automatic_host, \
                    patch.dict(vars(self_hosting), {"random": random_module}):
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(
                    return_value=[preferred_clan, opponent_clan],
                )
                clan_model.find_one = AsyncMock(return_value=opponent_clan)

                # When the participant requests automatic hosting.
                await self.cog.self_host_war.callback.func(self.cog, ctx)
        finally:
            preferred_reservation.release()

        # Then the locked preferred clan is tried before the opponent fallback.
        self.assertEqual(attempts, ["#PREFERRED", "#OPP"])
        self.bot.coc_client.get_clan.assert_awaited_once_with("#OPP")
        self.assertIs(automatic_host.await_args.args[2].clan, resolved_opponent)

    async def test_pending_opponent_bases_require_confirmation_before_helper_hosting(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        fields = [MagicMock() for _ in range(5)]
        fields[4].value = "- **[Opponent](<https://discordapp.com/users/200>)** wish to submit bases."
        ctx.channel.fetch_pinned_messages.return_value = [MagicMock(embeds=[MagicMock(fields=fields)])]
        self.bot.wait_for_component = AsyncMock(side_effect=TimeoutError)

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            helper_clan = MagicMock(tag="#CLAN", availability_str="Available")
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[helper_clan])
            clan_model.find_one = AsyncMock(return_value=helper_clan)

            await self.cog.self_host_war.callback.func(self.cog, ctx)

        self.bot.wait_for_component.assert_awaited_once()
        ctx.delete.assert_awaited_once_with(ctx.send.return_value)
        automatic_host.assert_not_awaited()

    async def test_confirmed_pending_opponent_bases_continue_to_helper_hosting(self) -> None:
        ctx = self.make_context()
        match = self.make_match()
        fields = [MagicMock() for _ in range(5)]
        fields[4].value = "- <@200> wish to submit bases."
        ctx.channel.fetch_pinned_messages.return_value = [MagicMock(embeds=[MagicMock(fields=fields)])]
        component_ctx = MagicMock(spec=ipy.ComponentContext)
        component_ctx.edit_origin = AsyncMock()
        self.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=component_ctx))

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host:
            match_model.find_one = AsyncMock(return_value=match)
            helper_clan = MagicMock(tag="#CLAN", availability_str="Available")
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[helper_clan])
            clan_model.find_one = AsyncMock(return_value=helper_clan)

            await self.cog.self_host_war.callback.func(self.cog, ctx)

        component_ctx.edit_origin.assert_awaited_once()
        automatic_host.assert_awaited_once()

    async def test_no_available_database_clan_denies_before_hosting(self) -> None:
        ctx = self.make_context()
        match = self.make_match()

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan", create=True) as clan_model:
            match_model.find_one = AsyncMock(return_value=match)
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[])
            await self.cog.self_host_war.callback.func(self.cog, ctx)

        ctx.send.assert_awaited_once()
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        clan_model.find_all.return_value.to_list.assert_awaited_once()
        match.set.assert_not_awaited()

    async def test_concurrent_self_host_skips_locked_first_clan_for_second_clan(self) -> None:
        # Given two persisted available clans and a first callback parked while using clan A.
        contexts = [self.make_context(), self.make_context()]
        contexts[1].author.id = "222"
        matches = [self.make_match_for(REQUESTER_ID), self.make_match_for("222")]
        clans = [MagicMock(tag="#A", name="Alpha", availability_str="Available"),
                 MagicMock(tag="#B", name="Bravo", availability_str="Available")]
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        selected_tags = []
        random_module = MagicMock()
        random_module.shuffle.side_effect = lambda candidates: None

        async def park_first(_runtime, _ctx, request):
            selected_tags.append(request.clan.tag)
            if len(selected_tags) == 1:
                first_started.set()
                async with asyncio.timeout(5):
                    await release_first.wait()

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)), \
                patch("cogs.self_commands.host_war_automatically", new=AsyncMock(side_effect=park_first)), \
                patch.dict(vars(self_hosting), {"random": random_module}):
            match_model.find_one = AsyncMock(side_effect=matches)
            clan_model.find_one = AsyncMock(return_value=clans[0])
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=clans)
            self.bot.coc_client.get_clan = AsyncMock(side_effect=lambda tag: next(c for c in clans if c.tag == tag))

            # When the second real callback runs while the first callback holds its selection.
            first_task = asyncio.create_task(self.cog.self_host_war.callback.func(self.cog, contexts[0]))
            try:
                async with asyncio.timeout(5):
                    await first_started.wait()
                    await self.cog.self_host_war.callback.func(self.cog, contexts[1])
            finally:
                release_first.set()
                async with asyncio.timeout(5):
                    await first_task

        # Then the callbacks use different clans instead of both selecting clan A.
        self.assertEqual(selected_tags, ["#A", "#B"])

    async def test_self_host_rejects_when_all_persisted_available_clans_are_locked(self) -> None:
        # Given two callbacks parked on the two persisted available clans.
        contexts = [self.make_context(), self.make_context(), self.make_context()]
        for index, ctx in enumerate(contexts, start=1):
            ctx.author.id = str(index)
        matches = [self.make_match_for(str(index)) for index in range(1, 4)]
        clans = [MagicMock(tag="#A", name="Alpha", availability_str="Available"),
                 MagicMock(tag="#B", name="Bravo", availability_str="Available")]
        started = [asyncio.Event(), asyncio.Event()]
        release = asyncio.Event()

        async def park_two(_runtime, _ctx, _request):
            call_index = automatic_host.await_count - 1
            if call_index < 2:
                started[call_index].set()
                async with asyncio.timeout(5):
                    await release.wait()

        with patch("cogs.self_commands.Match") as match_model, \
                patch("cogs.self_commands.Clan") as clan_model, \
                patch("cogs.self_commands.confirm_pending_opponent_bases", new=AsyncMock(return_value=True)), \
                patch("cogs.self_commands.host_war_automatically", new=AsyncMock(side_effect=park_two)) as automatic_host:
            match_model.find_one = AsyncMock(side_effect=matches)
            clan_model.find_one = AsyncMock(return_value=clans[0])
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=clans)
            self.bot.coc_client.get_clan = AsyncMock(side_effect=lambda tag: next(c for c in clans if c.tag == tag))

            # When a third real callback runs while both clan selections are held.
            tasks = []
            try:
                async with asyncio.timeout(5):
                    for index in range(2):
                        tasks.append(asyncio.create_task(self.cog.self_host_war.callback.func(self.cog, contexts[index])))
                        await started[index].wait()
                    await self.cog.self_host_war.callback.func(self.cog, contexts[2])
            finally:
                release.set()
                async with asyncio.timeout(5):
                    await asyncio.gather(*tasks)

        # Then it rejects without reaching CoC or Helper and reports an explicit empty list.
        self.assertEqual(automatic_host.await_count, 2)
        self.assertEqual(self.bot.coc_client.get_clan.await_count, 2)
        response = contexts[2].send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("None", response)

    async def test_self_host_rejects_same_side_request_before_helper_queue(self) -> None:
        # Given the caller's match side reserved by an earlier request that has not occupied its clan yet.
        ctx = self.make_context()
        match = self.make_match()
        match.token = "MATCH"
        clan = MagicMock(tag="#A", availability_str="Available")
        clan.name = "Alpha"
        side_reservation = await match_commands.try_reserve_resource_match_side("MATCH", 0)

        try:
            with patch("cogs.self_commands.Match") as match_model, \
                    patch("cogs.self_commands.Clan") as clan_model, \
                    patch("cogs.self_commands.host_war_automatically", new_callable=AsyncMock) as automatic_host:
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan])
                clan_model.find_one = AsyncMock(return_value=clan)

                # When a second real self-host callback targets the same side.
                await self.cog.self_host_war.callback.func(self.cog, ctx)
        finally:
            side_reservation.release()

        # Then it rejects before CoC or Helper and keeps the unlocked clan in the alternative list.
        automatic_host.assert_not_awaited()
        self.bot.coc_client.get_clan.assert_not_awaited()
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Alpha (#A)", response)


if __name__ == "__main__":
    unittest.main()
