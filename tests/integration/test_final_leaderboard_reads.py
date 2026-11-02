from tests.integration._final_fixture import FinalScenarioCase
from common.finals.leaderboards import final_board_rows
from common.finals.types import FinalError
from common.tokens.wallets import ensure_wallet
from database_models import EloFinal, Season
from database_models.final_models import FinalPrediction
from database_models.token_models import TokenLedgerEntry, TokenWallet


class TestFinalLeaderboardReads(FinalScenarioCase):
    async def test_predictions_all_seasons_aggregates_submitted_scores_across_final_editions(self):
        old_archive = await Season(season_number=3, start=0, end=1, players=[]).insert()
        latest_archive = await Season(season_number=9, start=2, end=3, players=[]).insert()
        old = await EloFinal(season_number=3, season_id=old_archive.id, schema_version=2).insert()
        current = await EloFinal(
            season_number=7, season_id=self.archive.id, schema_version=2, active=True
        ).insert()
        latest = await EloFinal(season_number=9, season_id=latest_archive.id, schema_version=2).insert()
        for final, user, points, eligible, submitted_at in (
            (old, "1", 2, 2, 1), (current, "1", 6, 100, 1),
            (old, "2", 5, 5, 1), (current, "2", 3, 3, 1),
            (latest, "3", 4, 4, 1), (current, "4", 0, 0, 1),
            (current, "5", 999, 999, None),
        ):
            await FinalPrediction(
                id=f"{final.id}:{user}", final_id=str(final.id), season_number=final.season_number,
                user_id=user, setup_revision=0, submitted_at=submitted_at, points=points, eligible=eligible,
            ).insert()

        rows = await final_board_rows("Predictions", "All Seasons")

        self.assertEqual([(row.user_id, row.value, row.eligible, row.rank) for row in rows],
                         [("1", 8, 102, 1), ("2", 8, 8, 2), ("3", 4, 4, 3), ("4", 0, 0, 4)])
        self.assertEqual({row.scope for row in rows}, {"All Seasons"})
        current_rows = await final_board_rows("Predictions", "Current Season")
        self.assertEqual([(row.user_id, row.value) for row in current_rows], [("1", 6), ("2", 3), ("4", 0)])
        self.assertEqual({row.scope for row in current_rows}, {"Season 7 Final"})

    async def test_verified_wallets_use_bulk_reads_without_regranting_or_transactions(self):
        for user in range(1, 10):
            await ensure_wallet(str(user))
        await TokenWallet.get_pymongo_collection().update_one({"user_id": "1"}, {"$set": {"balance": -50}})
        before_wallets = await TokenWallet.find_all().sort("user_id").to_list()
        before_receipts = await TokenLedgerEntry.find_all().sort("_id").to_list()
        await self.database.command({"profile": 2})
        try:
            rows = await final_board_rows("Tokens", "All Seasons")
        finally:
            await self.database.command({"profile": 0})
        commands = await self.database["system.profile"].find({"command.find": {"$exists": True}}).to_list()
        wallet_reads = [entry for entry in commands if entry["command"]["find"] == "token_wallets"]
        self.assertEqual(len(wallet_reads), 1, "A board load must not read each wallet in its own transaction")
        self.assertFalse(any(entry["command"].get("autocommit") is False for entry in commands))
        self.assertEqual(len(rows), 9)
        self.assertEqual(rows[-1].user_id, "1")
        self.assertEqual((rows[-1].value, rows[-1].debt, rows[-1].scope), (0, 50, "Current Season"))
        self.assertEqual([w.model_dump() for w in await TokenWallet.find_all().sort("user_id").to_list()],
                         [w.model_dump() for w in before_wallets])
        self.assertEqual([r.model_dump() for r in await TokenLedgerEntry.find_all().sort("_id").to_list()],
                         [r.model_dump() for r in before_receipts])

    async def test_missing_wallet_is_granted_once_and_receiptless_wallet_is_not_trusted(self):
        await ensure_wallet("1")
        rows = await final_board_rows("Tokens", "All Seasons")
        self.assertEqual(len(rows), 9)
        self.assertEqual([row.rank for row in rows], list(range(1, 10)))
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 9)
        await final_board_rows("Tokens", "All Seasons")
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 9)
        await TokenLedgerEntry.get_pymongo_collection().delete_one({"operation_key": "grant:1"})
        with self.assertRaisesRegex(FinalError, "initialization is incomplete"):
            await final_board_rows("Tokens", "All Seasons")
        self.assertEqual((await TokenWallet.get("1")).balance, 5000)
        self.assertIsNone(await TokenLedgerEntry.find_one({"operation_key": "grant:1"}))
