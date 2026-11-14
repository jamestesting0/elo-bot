import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

from pymongo.errors import OperationFailure

from elobot.features.base_store.storage import BaseStoreReleaseConflict
from elobot.db import BaseStore, Match
from tests import _result_retry_store as fixture
from tests._patch_sites import patch_sites


class TestResultReservationRelease(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="held").model_dump()
        self.runtime.match["participants"][0].update(base_store=deepcopy(self.store), opponent_bases_stored=True)
        self.release_fails = False
        self.release_proofs = []
        self.release = self.enterContext(patch_sites(('elobot.extensions.match.lifecycle.clear_match_base_stores', 'elobot.extensions.match.results.clear_match_base_stores'), new=AsyncMock(
            side_effect=self.release_store)))
        self.ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(), message=None,
                             guild=self.runtime.state.bot.fetch_guild.return_value)

    async def release_store(self, match: Match, **kwargs) -> None:
        operation = self.runtime.match["result_operation"]
        self.release_proofs.append({
            "completed": operation["completed"], "reason": kwargs.get("reason"),
            "record_id": operation["record"]["id"] if operation["record"] else None,
            "references": [[ref.id for ref in player["matches"]] for player in self.runtime.state.players.values()],
        })
        if self.release_fails:
            self.release_fails = False
            raise BaseStoreReleaseConflict("TOKEN", 0)
        await Match.get_pymongo_collection().update_one(
            {"_id": match.id, "participants.0.base_store": self.store},
            {"$set": {"participants.0.base_store": None, "participants.0.opponent_bases_stored": False}},
        )
        match.participants[0].base_store = None
        match.participants[0].opponent_bases_stored = False

    def fail_second_player(self) -> None:
        def fail(query, updates) -> None:
            if query.get("user_id") == "222" and "$addToSet" in updates:
                self.runtime.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("injected second player failure")
        self.runtime.state.before_player_update = fail

    def assert_store_held(self) -> None:
        self.assertEqual(self.runtime.match["participants"][0]["base_store"], self.store)
        self.assertTrue(self.runtime.match["participants"][0]["opponent_bases_stored"])

    async def test_partial_result_persistence_retains_the_reservation(self) -> None:
        self.fail_second_player()
        with self.assertRaises(OperationFailure):
            await self.runtime.cog._finalize_match_results("TOKEN", fixture.RESULTS)
        self.assert_store_held()
        self.release.assert_not_awaited()
        self.assertFalse(self.runtime.match["result_operation"]["completed"])

    async def test_failed_completion_checkpoint_retains_the_reservation(self) -> None:
        def fail_checkpoint(updates) -> None:
            if updates.get("$set", {}).get("result_operation.completed"):
                raise OperationFailure("injected durable completion failure")
        self.runtime.before_match_update = fail_checkpoint
        with self.assertRaises(OperationFailure):
            await self.runtime.cog._finalize_match_results("TOKEN", fixture.RESULTS)
        self.assert_store_held()
        self.release.assert_not_awaited()

    async def test_completed_result_retries_release_without_reapplying_ratings(self) -> None:
        self.release_fails = True
        with self.assertRaises(BaseStoreReleaseConflict):
            await self.runtime.cog._finalize_match_results("TOKEN", fixture.RESULTS)
        self.assert_store_held()
        self.assertTrue(self.runtime.match["result_operation"]["completed"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        players = deepcopy(self.runtime.state.players)
        self.runtime.state.player_collection.update_one.reset_mock()
        await self.runtime.cog._finalize_match_results("TOKEN", fixture.RESULTS)
        self.runtime.state.player_collection.update_one.assert_not_awaited()
        self.assertEqual(self.runtime.state.players, players)
        self.assertTrue(self.runtime.match["result_operation"]["published"])
        self.assertIsNone(self.runtime.match["participants"][0]["base_store"])
        self.assertEqual(self.release_proofs, [
            {"completed": True, "reason": "finished", "record_id": "11", "references": [["10", "11"], ["10", "11"]]},
        ] * 2)

    async def test_partial_resubmission_retains_the_reservation(self) -> None:
        self.fail_second_player()
        corrected = {"player1": fixture.RESULTS["player2"], "player2": fixture.RESULTS["player1"]}
        with self.assertRaises(OperationFailure):
            await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, self.ctx, **corrected)
        self.assert_store_held()
        self.release.assert_not_awaited()

    async def test_completed_resubmission_retries_only_release_and_publication(self) -> None:
        self.release_fails = True
        corrected = {"player1": fixture.RESULTS["player2"], "player2": fixture.RESULTS["player1"]}
        with self.assertRaises(BaseStoreReleaseConflict):
            await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, self.ctx, **corrected)
        self.assert_store_held()
        self.assertTrue(self.runtime.match["result_operation"]["completed"])
        players = deepcopy(self.runtime.state.players)
        self.runtime.state.player_collection.update_one.reset_mock()
        await self.runtime.cog.resubmit_modal.callback.func(self.runtime.cog, self.ctx, **corrected)
        self.runtime.state.player_collection.update_one.assert_not_awaited()
        self.assertEqual(self.runtime.state.players, players)
        self.assertTrue(self.runtime.match["result_operation"]["published"])
        self.assertIsNone(self.runtime.match["participants"][0]["base_store"])

    async def test_a_finished_side_alone_does_not_release_the_reservation(self) -> None:
        self.runtime.match["participants"][1]["result"] = [None, None, None]
        release_side = self.enterContext(patch('elobot.extensions.match.base_store.clear_participant_base_store', new=AsyncMock()))
        self.enterContext(patch_sites(('elobot.extensions.match.result_store.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new=AsyncMock()))
        await self.runtime.cog._finish_stored_result_side(await Match.find_one({"token": "TOKEN"}), 0)
        release_side.assert_not_awaited()
        self.assert_store_held()
