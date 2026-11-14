import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from elo_bot.db import BaseStore, Match, Participant
from elo_bot.db.match_models import ExpectedLiveStartState


class TestConsumedBaseStoreAbort(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Match, "_document_settings", DocumentSettings(
            name="matches", use_state_management=True,
        ))
        settings.start()
        self.addCleanup(settings.stop)
        self.collection = MagicMock(update_one=AsyncMock(return_value=MagicMock(matched_count=1)))
        collection = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection.start()
        self.addCleanup(collection.stop)

    def _match(self, *, live: bool = False) -> Match:
        match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, is_war=not live,
            times=[100, 100], hosts=["900", "900"], host_clans=["#A", "#A"],
            persistent_host_clans=["#A", "#A"], old_msgs=[43, 44],
            participants=[
                Participant(user_id="111", opponent_bases_stored=True),
                Participant(user_id="222", opponent_bases_stored=True),
            ],
        )
        match._save_state()
        return match

    async def _abort(self, match: Match) -> bool:
        if match.is_war:
            return await match.abort_started_war_side(0, 100, "900", "#A", 43)
        return await match.abort_started_live_match(ExpectedLiveStartState(
            times=(100, 100), hosts=("900", "900"), host_clans=("#A", "#A"), old_messages=(43, 44),
        ))

    async def test_abort_preserves_both_legacy_stored_flags(self) -> None:
        for live in (False, True):
            with self.subTest(live=live):
                match = self._match(live=live)

                self.assertTrue(await self._abort(match))

                query, update = self.collection.update_one.await_args.args
                for player in (0, 1):
                    self.assertNotIn(f"participants.{player}.base_store", query)
                    self.assertNotIn(f"participants.{player}.opponent_bases_stored", update["$set"])
                    self.assertTrue(match.participants[player].opponent_bases_stored)
                self.assertEqual(match.times, [None, None] if live else [None, 100])

    async def test_abort_preserves_non_null_reservations(self) -> None:
        for live in (False, True):
            with self.subTest(live=live):
                # Given: the target still has an actual reservation.
                match = self._match(live=live)
                store = BaseStore(clan_tag="#A", slot="Slot 1", reservation_id="CURRENT", message_id=45)
                match.participants[0].base_store = store
                match._save_state()

                # When: the start state is aborted.
                self.assertTrue(await self._abort(match))

                # Then: storage and its flag remain held after abort.
                update = self.collection.update_one.await_args.args[1]["$set"]
                self.assertNotIn("participants.0.base_store", update)
                self.assertNotIn("participants.0.opponent_bases_stored", update)
                self.assertIs(match.participants[0].base_store, store)
                self.assertTrue(match.participants[0].opponent_bases_stored)

    async def test_abort_does_not_change_a_replacement_reservation(self) -> None:
        for live in (False, True):
            with self.subTest(live=live):
                # Given: a replacement reservation appears after the consumed-state snapshot was loaded.
                match = self._match(live=live)
                replacement = BaseStore(clan_tag="#B", slot="Slot 2", reservation_id="NEW", message_id=46)

                def update_after_replacement(query, _updates):
                    matches = "participants.0.base_store" not in query or query["participants.0.base_store"] == replacement
                    return MagicMock(matched_count=int(matches))

                self.collection.update_one.side_effect = update_after_replacement

                # When: abort updates the original start state.
                aborted = await self._abort(match)

                # Then: only start fields change, leaving the replacement reservation alone.
                self.assertTrue(aborted)
                self.assertEqual(match.times, [None, None] if live else [None, 100])
                self.assertTrue(match.participants[0].opponent_bases_stored)
                update = self.collection.update_one.await_args.args[1]["$set"]
                self.assertFalse(any(key.startswith("participants.") for key in update))
