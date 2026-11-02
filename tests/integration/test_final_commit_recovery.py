from tests.integration._final_fixture import FinalScenarioCase
from common.finals.lifecycle import confirm_bracket
from common.finals.results import FinalResultSubmission, submit_result
from common.tokens.bets import BetAction, apply_bet, open_market
from common.tokens.settlement import advance_settlement, recover_market, set_settlement_intent
from common.tokens.wallets import ensure_wallet
from database_models.final_models import FinalMatchResult
from database_models.token_models import TokenLedgerEntry, TokenWallet


class TestFinalCommitRecovery(FinalScenarioCase):
    async def ambiguous_commit_once(self):
        await self.client.admin.command({"configureFailPoint": "failCommand", "mode": {"times": 1}, "data": {
            "failCommands": ["commitTransaction"], "writeConcernError": {"code": 64, "errmsg": "injected ambiguous commit"},
            "errorLabels": ["UnknownTransactionCommitResult"]}})
        self.addAsyncCleanup(self.client.admin.command, {"configureFailPoint": "failCommand", "mode": "off"})

    async def test_ambiguous_grant_commit_keeps_one_wallet_and_receipt(self):
        await self.ambiguous_commit_once()
        wallet = await ensure_wallet("9")
        self.assertEqual(wallet.balance, 5000)
        self.assertEqual(await TokenWallet.count(), 1)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 1)
        self.assertEqual((await ensure_wallet("9")).balance, 5000)

    async def test_ambiguous_official_result_commit_does_not_append_twice(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        node = final.nodes["QF1"]
        request = FinalResultSubmission(final_id=final.id, node_id=node.id, actor_id="manager", operation_key="official",
            expected_version=0, contestant_revision=node.contestant_revision, attempt=0,
            scores={node.contestants[0]: ["3.100"] * 7, node.contestants[1]: ["2.80"] * 7})
        await self.ambiguous_commit_once()
        result = await submit_result(request)
        self.assertEqual((await submit_result(request)).id, result.id)
        self.assertEqual(await FinalMatchResult.count(), 1)

    async def test_ambiguous_payout_commit_resumes_without_extra_credit(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        market = await open_market(final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant=market.contestants[0], amount=1000,
                                expected_version=0, action_key="stake"))
        await set_settlement_intent(market.id, 1, winner=market.contestants[0])
        await self.ambiguous_commit_once()
        await advance_settlement(market.id)
        await recover_market(market.id)
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "payout"}).count(), 1)
