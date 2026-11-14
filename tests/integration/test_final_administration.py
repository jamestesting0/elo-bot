from tests.integration._final_fixture import FinalScenarioCase
from elobot.features.finals.administration import deregister_player, guard_deregistration, guard_full_reset, guard_result_deletion, migrate_final_identity
from elobot.features.finals.lifecycle import confirm_bracket
from elobot.features.finals.predictions import begin_prediction, change_prediction_state
from elobot.features.finals.results import FinalResultSubmission, submit_result
from elobot.features.finals.types import FinalError
from elobot.features.tokens.wallets import ensure_wallet
from elobot.db import Player
from elobot.db.final_models import FinalPrediction
from elobot.db.token_models import TokenLedgerEntry, TokenWallet


class TestFinalAdministration(FinalScenarioCase):
    async def test_archived_pending_correction_blocks_identity_migration_before_any_write(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        wallet = await ensure_wallet("9")
        before = [player.model_dump() for player in await Player.find_all().to_list()]
        for flag in ("repair_pending", "reopening_pending"):
            with self.subTest(flag=flag):
                await final.set({"active": False, "state": "COMPLETED", "repair_pending": False,
                                 "reopening_pending": False, flag: True})
                with self.assertRaisesRegex(FinalError, "recovery"):
                    await migrate_final_identity("9", "10", "manager")
                self.assertEqual((await TokenWallet.get(wallet.id)).user_id, "9")
                self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])
                self.assertEqual(await TokenLedgerEntry.find({"kind": "identity"}).count(), 0)

    async def test_active_entrant_and_reset_are_protected_before_any_write(self):
        await self.roster(5)
        before = [player.model_dump() for player in await Player.find_all().to_list()]
        with self.assertRaises(FinalError):
            await guard_deregistration("1")
        with self.assertRaises(FinalError):
            await guard_full_reset()
        with self.assertRaises(FinalError):
            await migrate_final_identity("1", "10", "manager")
        self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])

    async def test_deregistration_races_a_join_without_an_unregistered_entrant(self):
        import asyncio
        from elobot.features.finals.lifecycle import add_entrant, resolve_final
        final = await self.roster(5)
        async with asyncio.timeout(5):
            await asyncio.gather(add_entrant(final, "9", "Bettor"), deregister_player("9"), return_exceptions=True)
        current = await resolve_final()
        player = await Player.find_one({"user_id": "9"})
        self.assertTrue("9" not in current.entrant_ids or player is not None)
        self.assertEqual(player is not None, "9" in current.entrant_ids)

    async def test_migration_retains_grant_identity_debt_and_prediction_owner(self):
        final = await confirm_bracket(await self.roster(5), "manager")
        await change_prediction_state(final, True)
        prediction = await begin_prediction(final, "9")
        wallet = await ensure_wallet("9")
        await TokenWallet.get_pymongo_collection().update_one({"_id": wallet.id}, {"$set": {"balance": -200}})
        await migrate_final_identity("9", "10", "manager")
        moved = await ensure_wallet("10")
        self.assertEqual((moved.id, moved.balance), (wallet.id, -200))
        self.assertEqual(set(moved.identity_claims), {"9", "10"})
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 1)
        self.assertEqual((await FinalPrediction.get(prediction.id)).user_id, "10")
        resumed = await begin_prediction(final, "10")
        self.assertEqual(resumed.id, prediction.id)
        await Player(id="reregistered", user_id="9", tag="#OTHER", elo=1000, highest_elo=1000).insert()
        with self.assertRaises(FinalError):
            await ensure_wallet("9")
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 1)

    async def test_wallet_collision_leaves_every_owner_unchanged(self):
        await ensure_wallet("8")
        await ensure_wallet("9")
        with self.assertRaises(FinalError):
            await migrate_final_identity("9", "8", "manager")
        self.assertEqual((await TokenWallet.get("9")).user_id, "9")
        self.assertEqual((await TokenWallet.get("8")).user_id, "8")
        self.assertIsNotNone(await Player.find_one({"user_id": "9"}))

    async def test_final_result_delete_points_to_correction_and_retains_history(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        await self.scheduled_pairing(final, "QF1")
        from elobot.features.finals.lifecycle import resolve_final
        final = await resolve_final()
        node = final.nodes["QF1"]
        result = await submit_result(FinalResultSubmission(final_id=final.id, node_id=node.id, actor_id="manager",
            operation_key="official", expected_version=0, contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={node.contestants[0]: ["3.100"] * 7, node.contestants[1]: ["2.80"] * 7}))
        with self.assertRaisesRegex(FinalError, "/final match result"):
            await guard_result_deletion(result.id)
