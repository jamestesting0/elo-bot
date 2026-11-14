import asyncio  # noqa: ANYIO_OK - tests exercise the requested asyncio lock and Event semantics
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from elobot.extensions.match import MatchCommands
from tests._patch_sites import patch_sites
import elobot.features.matches.resources as resources_module
import elobot.features.base_store.match_adapters as match_adapters_module
import elobot.extensions.match.base_store as match_base_store_module
import elobot.extensions.match.dashboards as match_dashboards_module
import elobot.extensions.match.hosting as match_hosting_module


class TestResourceMatchSideConcurrency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.dict(resources_module.RESOURCE_CLAN_LOCKS, clear=True))
        self.enterContext(patch.dict(resources_module.RESOURCE_MATCH_SIDE_LOCKS, clear=True))
        for reservations in (
            resources_module._RESOURCE_CLAN_RESERVATIONS, resources_module._RESOURCE_MATCH_SIDE_RESERVATIONS,
        ):
            token = reservations.set({})
            self.addCleanup(reservations.reset, token)

    async def test_same_match_side_different_clans_only_one_claim_commits(self) -> None:
        # Given two stale requests for the same match side targeting different available clans.
        clans = {
            "#A": SimpleNamespace(tag="#A", availability_str="Available"),
            "#B": SimpleNamespace(tag="#B", availability_str="Available"),
        }
        stale_matches = [
            MagicMock(
                token="SAME",
                thread="101",
                host_clans=[None, None],
                persistent_host_clans=[None, None],
                participants=[MagicMock(user_id="1"), MagicMock(user_id="2")],
            )
            for _ in range(2)
        ]
        canonical_match = MagicMock(
            token="SAME",
            thread="101",
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            participants=[MagicMock(user_id="1"), MagicMock(user_id="2")],
        )
        canonical_match.set = AsyncMock()
        ready_count = 0
        both_ready = asyncio.Event()
        start_claims = asyncio.Event()

        async def run_claim(index, reservation):
            nonlocal ready_count
            ready_count += 1
            if ready_count == 2:
                both_ready.set()
            async with asyncio.timeout(5):
                await start_claims.wait()
            return await match_hosting_module.claim_resource_clan(reservation, stale_matches[index], 0)

        async def find_clan(query):
            return clans[query["tag"]]

        with patch.object(match_adapters_module.Match, "find_one", new=AsyncMock(return_value=canonical_match)), \
                patch.object(match_dashboards_module.Clan, "find_one", new=AsyncMock(side_effect=find_clan)), \
                patch('elobot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy:
            # When both ready requests are released to claim simultaneously.
            reservations = [
                await match_base_store_module.try_reserve_resource_clan("#A"),
                await match_base_store_module.try_reserve_resource_clan("#B"),
            ]
            tasks = [
                asyncio.create_task(run_claim(index, reservations[index]))
                for index in range(2)
            ]
            async with asyncio.timeout(5):
                await both_ready.wait()
                start_claims.set()
                results = await asyncio.gather(*tasks)

        # Then exactly one claim may commit and the losing tag remains unoccupied.
        self.assertEqual(results.count(True), 1)
        self.assertEqual(results.count(False), 1)
        self.assertEqual(occupy.await_count, 1)
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#A"].locked())
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#B"].locked())

    async def test_different_sides_of_same_match_remain_independently_claimable(self) -> None:
        # Given two available clans requested for different sides of the same canonical match.
        canonical_match = MagicMock(
            token="SAME",
            thread="101",
            host_clans=[None, None],
            persistent_host_clans=[None, None],
            model_fields_set={"persistent_host_clans"},
            participants=[MagicMock(user_id="1"), MagicMock(user_id="2")],
        )
        canonical_match.set = AsyncMock()
        stale_matches = [
            MagicMock(
                token="SAME",
                thread="101",
                host_clans=[None, None],
                persistent_host_clans=[None, None],
                participants=canonical_match.participants,
            )
            for _ in range(2)
        ]
        clans = {
            "#A": SimpleNamespace(tag="#A", availability_str="Available"),
            "#B": SimpleNamespace(tag="#B", availability_str="Available"),
        }

        async def find_clan(query):
            return clans[query["tag"]]

        with patch.object(match_adapters_module.Match, "find_one", new=AsyncMock(return_value=canonical_match)), \
                patch.object(match_dashboards_module.Clan, "find_one", new=AsyncMock(side_effect=find_clan)), \
                patch('elobot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy:
            # When each side claims its own clan.
            reservations = [
                await match_base_store_module.try_reserve_resource_clan("#A"),
                await match_base_store_module.try_reserve_resource_clan("#B"),
            ]
            async with asyncio.timeout(5):
                results = await asyncio.gather(
                    match_hosting_module.claim_resource_clan(reservations[0], stale_matches[0], 0),
                    match_hosting_module.claim_resource_clan(reservations[1], stale_matches[1], 1),
                )

        # Then both claims commit under distinct side locks.
        self.assertEqual(results, [True, True])
        self.assertEqual(canonical_match.host_clans, ["#A", "#B"])
        self.assertEqual(occupy.await_count, 2)
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("SAME", 0)].locked())
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("SAME", 1)].locked())

    async def test_different_matches_and_clans_remain_independently_claimable(self) -> None:
        # Given available clans requested by different matches.
        canonical_matches = {
            token: MagicMock(
                token=token,
                thread=thread,
                host_clans=[None, None],
                persistent_host_clans=[None, None],
                model_fields_set={"persistent_host_clans"},
                participants=[MagicMock(user_id=token), MagicMock(user_id=f"{token}-opponent")],
            )
            for token, thread in (("ONE", "101"), ("TWO", "202"))
        }
        for canonical_match in canonical_matches.values():
            canonical_match.set = AsyncMock()
        stale_matches = [
            MagicMock(
                token=token,
                thread=canonical_matches[token].thread,
                host_clans=[None, None],
                persistent_host_clans=[None, None],
                participants=canonical_matches[token].participants,
            )
            for token in ("ONE", "TWO")
        ]
        clans = {
            "#A": SimpleNamespace(tag="#A", availability_str="Available"),
            "#B": SimpleNamespace(tag="#B", availability_str="Available"),
        }

        async def find_match(query):
            return canonical_matches[query["token"]]

        async def find_clan(query):
            return clans[query["tag"]]

        with patch.object(match_adapters_module.Match, "find_one", new=AsyncMock(side_effect=find_match)), \
                patch.object(match_dashboards_module.Clan, "find_one", new=AsyncMock(side_effect=find_clan)), \
                patch('elobot.features.matches.resources.occupy_clan', new_callable=AsyncMock) as occupy:
            # When both matches claim their unrelated clans.
            reservations = [
                await match_base_store_module.try_reserve_resource_clan("#A"),
                await match_base_store_module.try_reserve_resource_clan("#B"),
            ]
            async with asyncio.timeout(5):
                results = await asyncio.gather(
                    match_hosting_module.claim_resource_clan(reservations[0], stale_matches[0], 0),
                    match_hosting_module.claim_resource_clan(reservations[1], stale_matches[1], 0),
                )

        # Then both claims commit without sharing a match-side lock.
        self.assertEqual(results, [True, True])
        self.assertEqual(occupy.await_count, 2)
        self.assertEqual(canonical_matches["ONE"].host_clans[0], "#A")
        self.assertEqual(canonical_matches["TWO"].host_clans[0], "#B")

    async def test_same_side_war_start_rejects_before_helper_queue(self) -> None:
        # Given an earlier request holding side 0 before its clan occupancy write.
        side_reservation = await match_base_store_module.try_reserve_resource_match_side("MATCH", 0)
        clan_a = SimpleNamespace(tag="#A", name="Alpha", availability_str="Available")
        clan_b = SimpleNamespace(tag="#B", name="Bravo", availability_str="Available")
        match = MagicMock(
            token="MATCH",
            thread="101",
            is_war=True,
            is_autowin=False,
            times=[None, None],
            hosts=[None, None],
            participants=[MagicMock(user_id="1", result=[]), MagicMock(user_id="2", result=[])],
        )
        bot = MagicMock()
        bot.owner.id = 123
        bot.user.id = 0
        bot.coc_client.get_clan = AsyncMock(return_value=clan_a)
        cog = MatchCommands(bot)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.author.id = 999
        ctx.channel.id = 101
        ctx.channel.fetch_pinned_messages = AsyncMock(
            return_value=[MagicMock(embeds=[MagicMock(fields=[])])],
        )

        try:
            with patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.result_store.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.settings.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model, \
                    patch_sites(('elobot.extensions.match.dashboards.Clan', 'elobot.features.matches.resources.Clan')) as clan_model, \
                    patch('elobot.extensions.match.hosting.is_raid_weekend', return_value=False), \
                    patch.object(match_hosting_module.helper_host, "host_war_automatically", new_callable=AsyncMock) as host:
                match_model.find_one = AsyncMock(return_value=match)
                clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan_a, clan_b])

                # When a second automatic start targets the same side.
                await cog.match_war_autostart.callback.func(cog, ctx, player=0, clan="#A")
        finally:
            side_reservation.release()

        # Then it stops before Helper while both unlocked clans remain listed.
        host.assert_not_awaited()
        response = ctx.send.await_args.args[0]
        self.assertIn("Available clans:", response)
        self.assertIn("- Alpha (#A)", response)
        self.assertIn("- Bravo (#B)", response)
