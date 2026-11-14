import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

from elo_bot.features.base_store import storage
from elo_bot.features.base_store import repository
from elo_bot.features.base_store.messages import StoredBaseMessageRetirement
from elo_bot.features.base_store.slots import clear_all_base_store_slots
from elo_bot.db import BaseStore, Match, Participant


class TestBaseStoreStorage(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True))
        settings.start()
        self.addCleanup(settings.stop)
        self.store = BaseStore(
            clan_tag="#CLAN", slot="Slot 1", stored_at=100, reservation_id="OLD", message_id=43,
        )
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="42",
            participants=[Participant(user_id="100", opponent_bases_stored=True, base_store=self.store)],
        )
        self.match._save_state()
        self.collection = MagicMock(update_one=AsyncMock(return_value=MagicMock(matched_count=1)))
        collection = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection.start()
        self.addCleanup(collection.stop)
        self.bot = MagicMock(spec=ipy.Client)

    def test_collect_occupied_slots_ignores_orphaned_matches(self) -> None:
        participant = Participant(
            user_id="100",
            base_store=BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100),
        )
        live_match = MagicMock(thread="10", participants=[participant])
        orphaned_match = MagicMock(thread=None, participants=[participant])

        occupied = storage.collect_occupied_base_store_slots([live_match, orphaned_match])

        self.assertEqual(occupied, {"#CLAN": {"Slot 1"}})

    async def test_clear_participant_disables_success_before_releasing_and_resynchronizing(self) -> None:
        trace: list[str] = []
        self.collection.update_one.side_effect = lambda *_args: (trace.append("persist"), MagicMock(matched_count=1))[1]
        with (
            patch.object(storage, "retire_stored_base_message", new=AsyncMock(side_effect=lambda *_args: trace.append("retire"))) as retire,
            patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=lambda *_args, **_kwargs: trace.append("sync"))) as sync,
        ):
            cleared = await storage.clear_participant_base_store(
                self.match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
            )

        self.assertTrue(cleared)
        self.assertEqual(trace, ["retire", "persist", "retire", "sync"])
        self.assertIsNone(self.match.participants[0].base_store)
        self.assertFalse(self.match.participants[0].opponent_bases_stored)
        self.assertEqual(retire.await_args_list, [
            call(self.bot, StoredBaseMessageRetirement("42", 43, "releasing")),
            call(self.bot, StoredBaseMessageRetirement("42", 43, "undone")),
        ])
        sync.assert_awaited_once_with("#CLAN", bot=self.bot)

    async def test_stale_clear_preserves_reservation_and_only_neutralizes_the_old_message(self) -> None:
        self.collection.update_one.return_value.matched_count = 0
        with (
            patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock) as retire,
            patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock) as sync,
        ):
            cleared = await storage.clear_participant_base_store(
                self.match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
            )

        self.assertFalse(cleared)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        retire.assert_awaited_once_with(self.bot, StoredBaseMessageRetirement("42", 43, "releasing"))
        sync.assert_not_awaited()

    async def test_clear_propagates_retirement_failure(self) -> None:
        error = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        with (
            patch.object(storage, "retire_stored_base_message", new=AsyncMock(side_effect=error)),
            patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock) as sync,
        ):
            with self.assertRaises(ipy.errors.Forbidden) as raised:
                await storage.clear_participant_base_store(self.match, 0, bot=self.bot, reason="undone")

        self.assertIs(raised.exception, error)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.collection.update_one.assert_not_awaited()
        sync.assert_not_awaited()

    async def test_expiry_retires_the_message_of_the_cleared_reservation(self) -> None:
        with (
            patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=200),
            patch.object(repository, "retire_stored_base_message", new_callable=AsyncMock) as retire,
        ):
            await repository.clear_expired_base_store_reservations([self.match], bot=self.bot)

        self.assertIsNone(self.match.participants[0].base_store)
        self.assertFalse(self.match.participants[0].opponent_bases_stored)
        self.assertEqual(retire.await_args_list, [
            call(self.bot, StoredBaseMessageRetirement("42", 43, "releasing")),
            call(self.bot, StoredBaseMessageRetirement("42", 43, "raid_weekend_ended")),
        ])

    async def test_database_failure_leaves_reservation_held_with_neutral_message(self) -> None:
        error = OperationFailure("write failed")
        self.collection.update_one.side_effect = error
        with (
            patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock) as retire,
            patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock) as sync,
        ):
            with self.assertRaises(OperationFailure) as raised:
                await storage.clear_participant_base_store(self.match, 0, bot=self.bot, reason="undone")

        self.assertIs(raised.exception, error)
        self.assertIs(self.match.participants[0].base_store, self.store)
        retire.assert_awaited_once_with(self.bot, StoredBaseMessageRetirement("42", 43, "releasing"))
        sync.assert_not_awaited()

    async def test_expiry_conflict_reloads_replacement_before_calculating_available_slots(self) -> None:
        replacement = self.match.model_copy(deep=True)
        replacement.participants[0].base_store = BaseStore(
            clan_tag="#CLAN", slot="Slot 2", stored_at=300, reservation_id="NEW", message_id=44,
        )
        self.collection.update_one.return_value.matched_count = 0
        clan = MagicMock(tag="#CLAN", available_slots=[], save_changes=AsyncMock())
        with (
            patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=200),
            patch.object(repository, "Match") as match_model,
            patch.object(repository, "Clan") as clan_model,
            patch.object(repository, "retire_stored_base_message", new_callable=AsyncMock) as retire,
        ):
            match_model.find.return_value.to_list = AsyncMock(side_effect=[[self.match], [replacement]])
            clan_model.find_one = AsyncMock(return_value=clan)
            available = await repository.sync_base_store_slots("#CLAN", bot=self.bot)

        self.assertEqual(available, {"#CLAN": ["Slot 1"]})
        self.assertEqual(clan.available_slots, ["Slot 1"])
        self.assertIs(self.match.participants[0].base_store, self.store)
        retire.assert_awaited_once_with(self.bot, StoredBaseMessageRetirement("42", 43, "releasing"))

    async def test_weekend_reset_preserves_a_replacement_reservation_and_message(self) -> None:
        replacement = self.match.model_copy(deep=True)
        replacement.participants[0].base_store = BaseStore(
            clan_tag="#CLAN", slot="Slot 2", stored_at=300, reservation_id="NEW", message_id=44,
        )
        self.collection.update_one.return_value.matched_count = 0
        clan = MagicMock(tag="#CLAN", available_slots=[], save_changes=AsyncMock())
        with (
            patch("elo_bot.features.base_store.slots.Match") as match_model,
            patch("elo_bot.features.base_store.slots.Clan") as clan_model,
            patch("elo_bot.features.base_store.slots.retire_stored_base_message", new_callable=AsyncMock) as retire,
        ):
            match_model.find.return_value.to_list = AsyncMock(side_effect=[[self.match], [replacement]])
            clan_model.find_all.return_value.to_list = AsyncMock(return_value=[clan])
            await clear_all_base_store_slots(bot=self.bot)

        self.assertEqual(clan.available_slots, ["Slot 1"])
        self.assertIs(self.match.participants[0].base_store, self.store)
        retire.assert_awaited_once_with(self.bot, StoredBaseMessageRetirement("42", 43, "releasing"))

    async def test_match_wide_clear_retires_a_shared_live_message_once_per_phase(self) -> None:
        pair = BaseStore(
            clan_tag="#CLAN", slot="Slot 2", stored_at=100, reservation_id="OLD",
            message_id=43, ready=True, is_live=True,
        )
        self.store.is_live = True
        self.match.participants[0].base_store = self.store
        self.match.participants.append(Participant(
            user_id="200", opponent_bases_stored=True, base_store=pair,
        ))
        self.match._save_state()
        with (
            patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock) as retire,
            patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock),
        ):
            await storage.clear_match_base_stores(
                self.match, reset_stored_flags=True, bot=self.bot, reason="undone",
            )

        self.assertTrue(all(participant.base_store is None for participant in self.match.participants))
        self.assertEqual(retire.await_args_list, [
            call(self.bot, StoredBaseMessageRetirement("42", 43, "releasing")),
            call(self.bot, StoredBaseMessageRetirement("42", 43, "undone", "OLD")),
        ])

    async def test_headless_expiry_keeps_message_bound_reservation_occupied(self) -> None:
        with (
            patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=200),
            patch.object(Match, "save_changes", new_callable=AsyncMock),
        ):
            await repository.clear_expired_base_store_reservations([self.match])

        self.assertIs(self.match.participants[0].base_store, self.store)
        self.collection.update_one.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
