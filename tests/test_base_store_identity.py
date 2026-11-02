import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from database_models import BaseStore, Match, Participant


class TestBaseStoreIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="123",
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        )
        self.store = BaseStore(
            clan_tag="#CLAN", slot="Slot 1", stored_at=3_000_000_000,
            reservation_id="RESERVATION", message_id=456,
        )
        self.match.participants[0].base_store = self.store
        self.match.participants[0].opponent_bases_stored = True
        self.match._save_state()
        self.collection = MagicMock()
        self.collection.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        collection_patch = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection_patch.start()
        self.addCleanup(collection_patch.stop)


    async def test_clear_checks_exact_reservation_before_releasing_it(self) -> None:
        # Given: this snapshot owns one stored slot.
        # When: it releases the reservation and stored flag.
        cleared = await self.match.clear_base_store(0, reset_stored_flag=True)
        # Then: a newer reservation cannot match the conditional database update.
        self.assertTrue(cleared)
        query, update = self.collection.update_one.await_args.args
        self.assertEqual(query["participants.0.base_store.reservation_id"], "RESERVATION")
        self.assertEqual(query["participants.0.base_store.message_id"], 456)
        self.assertEqual(query["participants.0.user_id"], "111")
        self.assertEqual(update, {"$set": {
            "participants.0.base_store": None,
            "participants.0.opponent_bases_stored": False,
        }})
        self.assertIsNone(self.match.participants[0].base_store)
        self.assertFalse(self.match.participants[0].opponent_bases_stored)

    async def test_stale_clear_does_not_mutate_or_claim_success(self) -> None:
        # Given: the persisted reservation has changed since this snapshot was loaded.
        self.collection.update_one.return_value.matched_count = 0
        # When: stale cleanup attempts to release it.
        cleared = await self.match.clear_base_store(0, reset_stored_flag=True)
        # Then: no local success state is fabricated.
        self.assertFalse(cleared)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)

    async def test_committed_clear_preserves_other_pending_changes(self) -> None:
        self.match.participants[1].submit_bases = True

        await self.match.clear_base_store(0, reset_stored_flag=True)

        self.assertEqual(self.match.get_changes(), {"participants.1.submit_bases": True})

    async def test_binding_message_requires_current_reservation(self) -> None:
        # Given: the message has not yet been bound to the reservation.
        self.store.message_id = None
        self.match._save_state()
        # When: the pending message is attached before it can show success.
        attached = await self.match.attach_base_store_message(0, 789)
        # Then: the write is conditional on the same reservation and empty message binding.
        self.assertTrue(attached)
        query, update = self.collection.update_one.await_args.args
        self.assertEqual(query["participants.0.base_store.reservation_id"], "RESERVATION")
        self.assertIsNone(query["participants.0.base_store.message_id"])
        self.assertEqual(update, {"$set": {
            "participants.0.base_store.message_id": 789,
            "participants.0.base_store.ready": True,
        }})
        self.assertEqual(self.store.message_id, 789)

    async def test_failed_message_binding_preserves_local_identity(self) -> None:
        # Given: the reservation was retired or replaced before publication completed.
        self.collection.update_one.return_value.matched_count = 0
        # When: publication tries to attach its message.
        attached = await self.match.attach_base_store_message(0, 789)
        # Then: the caller can report failure without overwriting the replacement.
        self.assertFalse(attached)
        self.assertEqual(self.store.message_id, 456)
