import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from elo_bot.features.base_store import storage
from elo_bot.features.base_store import preparation
from elo_bot.features.base_store.models import BaseStorePreparationFailure, BaseStorePreparationRequest
from elo_bot.db import BaseStore, Match, MatchPlayerStats, Participant
from elo_bot.db.result_models import MatchResultOperation, ResultRecordSnapshot


class TestStorageReleasePolicy(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match,
            "_document_settings",
            DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(),
            token="TOKEN",
            matched_time=1,
            thread="42",
            participants=[
                Participant(
                    user_id="100",
                    opponent_bases_stored=True,
                    base_store=BaseStore(
                        clan_tag="#CLAN",
                        slot="Slot 1",
                        stored_at=100,
                        reservation_id="RESERVATION",
                        message_id=43,
                    ),
                ),
                Participant(user_id="200"),
            ],
        )
        self.match._save_state()
        self.collection = MagicMock(update_one=AsyncMock(return_value=MagicMock(matched_count=1)))
        collection = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection.start()
        self.addCleanup(collection.stop)

    async def test_finished_release_requires_complete_durable_results_for_both_players(self) -> None:
        self.match.result_operation = MatchResultOperation(
            key="operation", response={}, created_at=1, completed=True,
        )
        with (
            patch.object(storage, "resolve_stored_base_message_ids", new=AsyncMock(return_value=[43])),
            patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock) as retire,
        ):
            with self.assertRaises(storage.BaseStoreReleaseDenied):
                await storage.clear_participant_base_store(self.match, 0, reason="finished")

        retire.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_autowin_is_not_a_finished_release(self) -> None:
        self.match.is_autowin = True
        self.match.result_operation = MatchResultOperation(
            key="operation", response={}, created_at=1, completed=True,
        )

        with self.assertRaises(storage.BaseStoreReleaseDenied):
            await storage.clear_participant_base_store(self.match, 0, reason="finished")

        self.collection.update_one.assert_not_awaited()

    async def test_complete_recorded_results_authorize_finished_release(self) -> None:
        for participant in self.match.participants:
            participant.result = ["3.100", "2.80", "1.50"]
        stats = MatchPlayerStats(
            stars=6, destruction=230, opponent_stars=6, opponent_destruction=230,
            opponent="200", elo_change=0, won=False,
        )
        self.match.result_operation = MatchResultOperation(
            key="operation",
            response={},
            created_at=1,
            completed=True,
            record=ResultRecordSnapshot(
                id="TOKEN",
                time=1,
                players={"100": stats, "200": stats.model_copy(update={"opponent": "100"})},
                operation_key=f"{self.match.id}:operation",
            ),
        )
        with (
            patch.object(storage, "resolve_stored_base_message_ids", new=AsyncMock(return_value=[])),
            patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock),
        ):
            cleared = await storage.clear_participant_base_store(self.match, 0, reason="finished")

        self.assertTrue(cleared)
        self.collection.update_one.assert_awaited_once()

    async def test_weekend_release_rejects_a_reservation_newer_than_the_cutoff(self) -> None:
        self.match.participants[0].base_store.stored_at = 300
        with (
            patch.object(storage, "latest_completed_raid_weekend_end_timestamp", return_value=200),
            patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock) as retire,
        ):
            with self.assertRaises(storage.BaseStoreReleaseDenied):
                await storage.clear_participant_base_store(self.match, 0, reason="raid_weekend_ended")

        retire.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()


class TestPreparationReleasePolicy(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match,
            "_document_settings",
            DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(),
            token="TOKEN",
            matched_time=1,
            thread="42",
            final_districts=["A", "B", "C"],
            participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        self.match._save_state()
        self.collection = MagicMock(update_one=AsyncMock(return_value=MagicMock(matched_count=1)))
        collection = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection.start()
        self.addCleanup(collection.stop)
        self.runtime = MagicMock()
        self.runtime._base_store_sync_slots = AsyncMock(return_value={"#CLAN": ["Slot 1"]})
        self.runtime._base_store_choose_bases = AsyncMock()
        self.request = BaseStorePreparationRequest(thread_id="42", player=0, clan=MagicMock(tag="#CLAN"), slot="Slot 1")

    async def test_choose_failure_does_not_create_or_release_a_reservation(self) -> None:
        self.runtime._base_store_choose_bases.side_effect = RuntimeError("selection failed")
        with patch.object(preparation.Match, "find_one", new=AsyncMock(return_value=self.match)):
            with self.assertRaisesRegex(RuntimeError, "selection failed"):
                await preparation.prepare_base_store(self.runtime, self.request)

        self.collection.update_one.assert_not_awaited()
        self.assertIsNone(self.match.participants[0].base_store)

    async def test_terminal_completion_during_selection_cannot_claim_a_slot(self) -> None:
        completed = self.match.model_copy(deep=True)
        for participant in completed.participants:
            participant.result = ["3.100", "2.80", "1.50"]
        completed._save_state()
        with patch.object(preparation.Match, "find_one", new=AsyncMock(side_effect=[self.match, completed])):
            outcome = await preparation.prepare_base_store(self.runtime, self.request)

        self.assertIsInstance(outcome, BaseStorePreparationFailure)
        self.collection.update_one.assert_not_awaited()
        self.assertIsNone(completed.participants[0].base_store)

    async def test_result_change_at_the_claim_write_prevents_the_slot_claim(self) -> None:
        self.collection.update_one.return_value.matched_count = 0
        with patch.object(preparation.Match, "find_one", new=AsyncMock(return_value=self.match)):
            outcome = await preparation.prepare_base_store(self.runtime, self.request)

        self.assertIsInstance(outcome, BaseStorePreparationFailure)
        self.assertIsNone(self.match.participants[0].base_store)
        query = self.collection.update_one.await_args.args[0]
        self.assertEqual(query["participants.0.result"], [None, None, None])
        self.assertIn("result_operation", query)


if __name__ == "__main__":
    unittest.main()
