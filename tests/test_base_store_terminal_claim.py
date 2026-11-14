import unittest
from unittest.mock import AsyncMock

from elobot.features.base_store.models import BaseStorePreparationFailure
from elobot.features.results.autowin import claim_autowin, mark_autowin
from elobot.db import Match
from elobot.db.result_models import MatchResultOperation
from tests import _match_cleanup_fixture as fixtures


class TestBaseStoreTerminalClaim(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.case = fixtures.ThreadReservationState(self)
        self.case.setUp()

    async def assert_autowin_blocks_claim(self, *, completed: bool) -> None:
        async def refresh_slots(*_args) -> dict[str, list[str]]:
            if self.case.preparer._base_store_sync_slots.await_count == 2:
                snapshot = await Match.find_one({"thread": "321"})
                self.assertIsNotNone(snapshot)
                claimed = await claim_autowin(snapshot, "111", "999", "No show")
                if completed:
                    await mark_autowin(claimed, completed=True)
            return {"#HELD": ["Slot 1"]}

        self.case.preparer._base_store_sync_slots = AsyncMock(side_effect=refresh_slots)
        outcome = await self.case.reserve()
        current = await Match.find_one({"thread": "321"})
        self.assertIsInstance(outcome, BaseStorePreparationFailure)
        self.assertIsNone(current.participants[0].base_store)
        self.assertFalse(current.participants[0].opponent_bases_stored)
        self.assertEqual(current.autowin_winner_id, "111")
        self.assertEqual(current.autowin_completed, completed)

    async def test_autowin_during_slot_refresh_prevents_claim(self) -> None:
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.case = fixtures.ThreadReservationState(self)
                self.case.setUp()
                await self.assert_autowin_blocks_claim(completed=completed)

    async def test_existing_result_operation_prevents_storage_before_selection(self) -> None:
        self.case.case.runtime.fixture.match["result_operation"] = MatchResultOperation(
            key="pending", response={}, created_at=1,
        ).model_dump()
        outcome = await self.case.reserve()
        self.assertIsInstance(outcome, BaseStorePreparationFailure)
        self.assertIn("being recorded", outcome.message)
        self.case.preparer._base_store_choose_bases.assert_not_awaited()
        current = await Match.find_one({"thread": "321"})
        self.assertIsNone(current.participants[0].base_store)
