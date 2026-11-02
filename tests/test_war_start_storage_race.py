import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie.odm.fields import ExpressionField
from pymongo.results import UpdateResult

import cogs.match_commands as match_commands
from cogs.base_store import preparation
from cogs.war_hosting import war_start
from database_models import BaseStore
from tests._war_start_fixture import WarStartFixture


class TestWarStartStorageRace(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands._RESOURCE_CLAN_RESERVATIONS.set({})
        match_commands._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})
        self.case = WarStartFixture()
        self.case.addCleanup = self.addCleanup
        self.case.setUp()
        self.case.ctx.author_id = 100
        self.case.bot.cache.place_user_data({"id": "100", "username": "Player", "discriminator": "0", "avatar": None})

    async def test_manual_command_reports_pending_storage_once(self) -> None:
        match = self.case.match
        match.times, match.hosts = [None, None], [None, None]
        match.participants[0].base_store = BaseStore(
            clan_tag="#STORE", slot="Slot 1", reservation_id="PENDING", ready=False,
        )
        match.participants[0].opponent_bases_stored = True
        self.case.ctx.defer = AsyncMock()
        runtime = match_commands.MatchCommands(self.case.bot)
        with patch.object(match_commands.Match, "thread", ExpressionField("thread"), create=True), \
                patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=match)), \
                patch.object(match_commands, "try_reserve_resource_clan", new=AsyncMock()) as claim:
            await runtime.match_war_start.callback.func(runtime, self.case.ctx, player=0, clan="#STORE")

        self.case.ctx.send.assert_awaited_once()
        self.assertIn("Base storage is incomplete", self.case.ctx.send.await_args.args[0])
        self.assertNotIn("currently occupied", self.case.ctx.send.await_args.args[0])
        self.assertEqual(match.times, [None, None])
        self.assertEqual(match.participants[0].base_store.reservation_id, "PENDING")
        claim.assert_not_awaited()

    async def test_manual_start_waits_for_pending_storage_publication(self) -> None:
        # Given a held reservation that has no published stored-base message yet.
        match = self.case.match
        match.times, match.hosts = [None, None], [None, None]
        match.participants[0].base_store = BaseStore(
            clan_tag="#STORE", slot="Slot 1", reservation_id="PENDING", ready=False,
        )
        match.participants[0].opponent_bases_stored = True
        with patch.object(match_commands, "claim_resource_clan", new=AsyncMock()) as claim, \
                patch.object(war_start, "complete_war_start", new=AsyncMock()) as publish:
            # When a manual Start Match tries to start this side.
            message = await war_start.start_war(self.case.bot, self.case.ctx, self.case.war_request())

        # Then it reports incomplete storage without committing or publishing a start.
        self.assertIsNone(message)
        claim.assert_not_awaited()
        publish.assert_not_awaited()
        self.assertEqual(match.times, [None, None])
        self.assertEqual(match.participants[0].base_store.reservation_id, "PENDING")
        self.assertIn("Base storage is incomplete", self.case.ctx.send.await_args.args[0])

    async def test_delayed_storage_claim_cannot_overwrite_a_new_start(self) -> None:
        # Given a side whose start has already committed in the database.
        match = self.case.match
        match.times[0] = None
        store = BaseStore(clan_tag="#STORE", slot="Slot 1", reservation_id="NEW", ready=False)

        async def update(query, _update):
            matched = not ("times.0" in query and query["times.0"] is None)
            return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

        self.case.collection.update_one.side_effect = update

        # When a delayed storage claim reaches MongoDB.
        claimed = await preparation._claim_base_store(match, 0, store)

        # Then it leaves the committed start untouched and claims no stored slot.
        self.assertFalse(claimed)
        self.assertEqual(match.times, [None, None])
        self.assertIsNone(match.participants[0].base_store)
        self.assertFalse(match.participants[0].opponent_bases_stored)

    async def test_fresh_start_cannot_commit_after_a_storage_reservation_appears(self) -> None:
        # Given a stale start request and a newer durable storage reservation.
        stale = self.case.match
        stale.times, stale.hosts, stale.host_clans = [None, None], [None, None], [None, None]
        stale._save_state()
        fresh = stale.model_copy(deep=True)
        fresh.model_fields_set.add("persistent_host_clans")
        fresh.participants[0].base_store = BaseStore(
            clan_tag="#STORE", slot="Slot 1", reservation_id="HELD", ready=False,
        )
        fresh.participants[0].opponent_bases_stored = True
        fresh_set = AsyncMock()

        async def rejected_storage_filter(query, _update):
            self.assertIsNone(query["participants.0.base_store"])
            return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)

        self.case.collection.update_one.side_effect = rejected_storage_filter
        with patch.object(match_commands.Match, "find_one", new=AsyncMock(return_value=fresh)), \
                patch.object(match_commands.Match, "set", new=fresh_set), \
                patch.object(match_commands.Clan, "find_one", new=AsyncMock(return_value=MagicMock(
                    availability_str="Available",
                ))), \
                patch.object(match_commands, "occupy_clan", new=AsyncMock()) as occupy, \
                patch.object(match_commands, "free_clan", new=AsyncMock()), \
                patch.object(war_start, "complete_war_start", new=AsyncMock(return_value=MagicMock(id=700))) as publish:
            # When the real start tries to commit against that newer reservation.
            message = await war_start.start_war(self.case.bot, self.case.ctx, self.case.war_request())

        # Then neither the start nor its success controls can be committed.
        self.assertIsNone(message)
        fresh_set.assert_not_awaited()
        occupy.assert_not_awaited()
        publish.assert_not_awaited()
        self.assertEqual(fresh.participants[0].base_store.reservation_id, "HELD")
        self.assertEqual(fresh.times, [None, None])
