import asyncio  # noqa: ANYIO_OK - Events deterministically park the requested asyncio claim seam
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from tests._patch_sites import patch_sites
import elobot.features.matches.resources as resources_module
import elobot.extensions.match.adapters as match_adapters_module
import elobot.extensions.match.base_store as match_base_store_module
import elobot.extensions.match.dashboards as match_dashboards_module
import elobot.extensions.match.hosting as match_hosting_module


class OccupancyFailure(RuntimeError):
    pass


class TestResourceClanClaimRollback(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        resources_module.RESOURCE_CLAN_LOCKS.clear()
        resources_module.RESOURCE_MATCH_SIDE_LOCKS.clear()
        resources_module._RESOURCE_CLAN_RESERVATIONS.set({})
        resources_module._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})

    async def test_occupancy_failure_rolls_back_match_claim_before_unlocking(self) -> None:
        # Given a replacement claim whose occupancy write parks and then fails.
        canonical_match = MagicMock(
            token="MATCH",
            thread="101",
            host_clans=["#OLD", None],
            persistent_host_clans=["#PREFERRED", None],
            model_fields_set={"persistent_host_clans"},
            hosts=["old-host", None],
            participants=[MagicMock(user_id="1"), MagicMock(user_id="2")],
        )
        canonical_match.set = AsyncMock()
        stale_match = MagicMock(
            token="MATCH",
            thread="101",
            host_clans=["#OLD", None],
            persistent_host_clans=["#PREFERRED", None],
            hosts=["old-host", None],
            participants=canonical_match.participants,
        )
        clan = SimpleNamespace(tag="#NEW", availability_str="Available")
        occupancy_started = asyncio.Event()
        fail_occupancy = asyncio.Event()

        async def occupy_then_fail(*_args):
            occupancy_started.set()
            await asyncio.wait_for(fail_occupancy.wait(), timeout=5)
            raise OccupancyFailure("occupancy failed")

        async def run_claim():
            reservation = await match_base_store_module.try_reserve_resource_clan("#NEW")
            return await match_hosting_module.claim_resource_clan(
                reservation,
                stale_match,
                0,
                updates={"hosts.0": "new-host"},
            )

        with patch.object(match_adapters_module.Match, "find_one", new=AsyncMock(return_value=canonical_match)), \
                patch.object(match_dashboards_module.Clan, "find_one", new=AsyncMock(return_value=clan)), \
                patch('elobot.features.matches.resources.occupy_clan', new=AsyncMock(side_effect=occupy_then_fail)), \
                patch_sites(('elobot.extensions.match.results.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free_clan:
            # When the durable match claim is visible while occupancy remains unfinished.
            claim_task = asyncio.create_task(run_claim())
            await asyncio.wait_for(occupancy_started.wait(), timeout=5)
            self.assertEqual(canonical_match.host_clans[0], "#NEW")
            self.assertEqual(canonical_match.persistent_host_clans[0], "#NEW")
            self.assertIsNone(await match_base_store_module.try_reserve_resource_clan("#NEW"))
            fail_occupancy.set()
            with self.assertRaisesRegex(OccupancyFailure, "occupancy failed"):
                await asyncio.wait_for(claim_task, timeout=5)

        # Then the failed claim is rolled back before either reservation becomes reusable.
        self.assertEqual(canonical_match.host_clans[0], "#OLD")
        self.assertEqual(canonical_match.persistent_host_clans[0], "#PREFERRED")
        self.assertEqual(canonical_match.hosts[0], "old-host")
        self.assertEqual(stale_match.host_clans[0], "#OLD")
        self.assertEqual(stale_match.persistent_host_clans[0], "#PREFERRED")
        self.assertEqual(stale_match.hosts[0], "old-host")
        self.assertEqual(
            canonical_match.set.await_args_list,
            [
                unittest.mock.call({
                    "hosts.0": "new-host",
                    "host_clans.0": "#NEW",
                    "persistent_host_clans.0": "#NEW",
                }),
                unittest.mock.call({
                    "hosts.0": "old-host",
                    "host_clans.0": "#OLD",
                    "persistent_host_clans.0": "#PREFERRED",
                }),
            ],
        )
        free_clan.assert_not_awaited()
        self.assertEqual(clan.availability_str, "Available")
        self.assertFalse(resources_module.RESOURCE_CLAN_LOCKS["#NEW"].locked())
        self.assertFalse(resources_module.RESOURCE_MATCH_SIDE_LOCKS[("MATCH", 0)].locked())
