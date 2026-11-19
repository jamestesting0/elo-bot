import asyncio

from tests.integration._final_fixture import FinalScenarioCase
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.core.finals import FinalError
from elo_bot.features.tokens.bets import BetAction, apply_bet, open_market, projected_return
from elo_bot.features.tokens.settlement import advance_settlement, recover_market, set_settlement_intent
from elo_bot.features.finals.leaderboards import final_board_rows
from elo_bot.features.tokens.history import ledger_label
from elo_bot.features.tokens.stats import betting_lines, betting_stats
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet, wallet_summary
from elo_bot.db import Player
from elo_bot.db.token_models import FinalBettingMarket, TokenLedgerEntry, TokenWallet


class TestFinalTokens(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        for user in range(10, 14):
            await Player(id=str(user), user_id=str(user), tag=f"#P{user}", elo=1000, highest_elo=1000).insert()
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.market = await open_market(self.final, "QF1")

    async def bet(self, user, amount, side=None, version=0, key=None, market=None):
        market = market or self.market
        return await apply_bet(BetAction(market_id=market.id, user_id=str(user),
            contestant=side or market.contestants[0], amount=amount, expected_version=version,
            action_key=key or f"{market.id}:action:{user}:{version}:{amount}"))

    async def assert_conserved(self):
        wallets = await TokenWallet.find_all().to_list()
        markets = await FinalBettingMarket.find_all().to_list()
        grants = await TokenLedgerEntry.find({"kind": {"$in": ["grant", "adjust"]}}).to_list()
        self.assertEqual(sum(wallet.balance for wallet in wallets) + sum(market.escrow for market in markets),
                         sum(entry.amount for entry in grants))
        entries = await TokenLedgerEntry.find_all().to_list()
        for wallet in wallets:
            self.assertEqual(wallet.balance, sum(entry.amount for entry in entries if entry.wallet_id == wallet.id))

    async def test_manager_adjustments_apply_once_and_never_create_debt(self):
        await adjust_balance("10", 300, "manager", "adjust:1")
        await adjust_balance("10", 300, "manager", "adjust:1")
        self.assertEqual((await TokenWallet.get("10")).balance, 5300)
        with self.assertRaises(FinalError):
            await adjust_balance("10", -5301, "manager", "adjust:2")
        async with asyncio.timeout(10):
            outcomes = await asyncio.gather(*(adjust_balance("10", -5300, "manager", f"adjust:{key}")
                                              for key in (3, 4)), return_exceptions=True)
        self.assertEqual(sorted(type(outcome).__name__ for outcome in outcomes), ["FinalError", "TokenWallet"])
        self.assertEqual((await TokenWallet.get("10")).balance, 0)
        entries = await TokenLedgerEntry.find({"wallet_id": "10", "kind": "adjust"}).sort("created_at").to_list()
        self.assertEqual(sorted(entry.amount for entry in entries), [-5300, 300])
        self.assertEqual(ledger_label(entries[0]), "Manager adjustment")
        with self.assertRaises(FinalError):
            await adjust_balance("99", 100, "manager", "adjust:5")
        await self.assert_conserved()

    async def test_grant_is_committed_once_under_concurrency_and_reregistration(self):
        async with asyncio.timeout(5):
            wallets = await asyncio.gather(*(ensure_wallet("9") for _ in range(4)))
        self.assertEqual({wallet.balance for wallet in wallets}, {5000})
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 1)
        await Player.find_one({"user_id": "9"}).delete()
        await Player(id="new-player", user_id="9", tag="#NEW", elo=1000, highest_elo=1000).insert()
        self.assertEqual((await ensure_wallet("9")).id, "9")
        self.assertEqual(await TokenLedgerEntry.find({"kind": "grant"}).count(), 1)
        await self.assert_conserved()

    async def test_edit_switch_cancel_and_replay_are_atomic(self):
        first = await self.bet(9, 3000, key="first")
        self.assertEqual((await TokenWallet.get("9")).balance, 2000)
        self.assertEqual((await self.bet(9, 3000, key="first")).version, first.version)
        with self.assertRaises(FinalError):
            await self.bet(9, 6000, version=1)
        self.assertEqual((await TokenWallet.get("9")).balance, 2000)
        await self.bet(9, 2000, side=self.market.contestants[1], version=1)
        market = await FinalBettingMarket.get(self.market.id)
        self.assertEqual(list(market.pools.values()), [0, 2000])
        from elo_bot.db.token_models import FinalBet
        self.assertEqual(projected_return(market, await FinalBet.get(f"{market.id}:9"), market.contestants[0], 100),
                         (100, 0))
        await self.bet(9, 0, version=2)
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        await self.bet(9, 1000, version=3)
        with self.assertRaises(FinalError):
            await self.bet(9, 100, version=3)
        await self.assert_conserved()

    async def test_own_participant_and_strict_invalid_amounts_are_rejected(self):
        with self.assertRaises(FinalError):
            await self.bet(int(self.market.contestants[0]), 100)
        for amount in (True, 1.5, "100", -1):
            with self.subTest(amount=amount), self.assertRaises(ValueError):
                BetAction(market_id=self.market.id, user_id="9", contestant=self.market.contestants[0],
                          amount=amount, expected_version=0, action_key="invalid")
        await self.assert_conserved()

    async def test_accepted_start_serializes_with_last_bet(self):
        match = await self.scheduled_pairing(self.final, "QF1")
        async with asyncio.timeout(5):
            results = await asyncio.gather(self.bet(9, 1000), accept_final_start(match, "manager"), return_exceptions=True)
        self.assertFalse(isinstance(results[1], Exception), results)
        market = await FinalBettingMarket.get(self.market.id)
        self.assertEqual(market.state, "CLOSED")
        self.assertIn(market.escrow, (0, 1000))
        with self.assertRaises(FinalError):
            await self.bet(10, 100)
        await self.assert_conserved()

    async def test_edit_cancel_and_accepted_start_race_has_one_valid_balance_and_pool(self):
        from elo_bot.db.token_models import FinalBet
        await self.bet(9, 1000)
        source = await self.scheduled_pairing(self.final, "QF1")
        barrier = asyncio.Event()
        async def race(operation):
            async with asyncio.timeout(5):
                await barrier.wait()
            return await operation()
        tasks = [asyncio.create_task(race(lambda: self.bet(9, 2000, version=1, key="race-edit"))),
                 asyncio.create_task(race(lambda: self.bet(9, 0, version=1, key="race-cancel"))),
                 asyncio.create_task(race(lambda: accept_final_start(source, "manager")))]
        barrier.set()
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=5)
        self.assertNotIsInstance(results[2], Exception)
        self.assertLessEqual(sum(not isinstance(result, Exception) for result in results[:2]), 1)
        market = await FinalBettingMarket.get(self.market.id)
        bet = await FinalBet.get(f"{market.id}:9")
        self.assertEqual(market.state, "CLOSED")
        self.assertEqual(market.escrow, bet.amount if bet.active else 0)
        self.assertEqual((await TokenWallet.get("9")).balance + market.escrow, 5000)
        self.assertIn(market.escrow, (0, 1000, 2000))
        await self.assert_conserved()

    async def test_largest_remainder_and_restart_pay_exact_pool_once(self):
        await self.bet(9, 1)
        await self.bet(10, 2)
        await self.bet(11, 7, self.market.contestants[1])
        await set_settlement_intent(self.market.id, 1, winner=self.market.contestants[0])
        self.assertTrue(await advance_settlement(self.market.id))
        await self.assert_conserved()
        self.assertTrue(await recover_market(self.market.id))
        self.assertTrue(await recover_market(self.market.id))
        self.assertEqual((await TokenWallet.get("9")).balance, 5002)
        self.assertEqual((await TokenWallet.get("10")).balance, 5005)
        self.assertEqual((await TokenWallet.get("11")).balance, 4993)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "payout"}).count(), 2)
        self.assertEqual((await FinalBettingMarket.get(self.market.id)).escrow, 0)
        await self.assert_conserved()

    async def test_settled_bets_give_won_profit_and_the_betting_board(self):
        side, other = self.market.contestants
        await self.bet(10, 100, other)
        await self.bet(10, 100, side, version=1)
        await self.bet(11, 300, other)
        self.assertEqual((await TokenLedgerEntry.find_one({"wallet_id": "10", "kind": "switch"})).amount, 0)
        await set_settlement_intent(self.market.id, 1, winner=side)
        await recover_market(self.market.id)
        stats = await betting_stats(str(self.final.id))
        self.assertEqual((stats["10"].won, stats["10"].settled, stats["10"].staked, stats["10"].profit), (1, 1, 100, 300))
        self.assertEqual((stats["11"].won, stats["11"].profit), (0, -300))
        self.assertIn("+300", betting_lines(stats["10"]))
        self.assertIn("`+300.00%`", betting_lines(stats["10"]))
        rows = await final_board_rows("Betting", None)
        self.assertEqual([(row.user_id, row.value, row.rank) for row in rows], [("10", 300, 1), ("11", -300, 2)])

    async def test_no_winning_stake_and_forfeit_refund(self):
        await self.bet(9, 1000)
        await set_settlement_intent(self.market.id, 1, winner=self.market.contestants[1])
        await recover_market(self.market.id)
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertEqual((await FinalBettingMarket.get(self.market.id)).state, "REFUNDED")
        second = await open_market(self.final, "QF2")
        await self.bet(9, 1000, market=second)
        await set_settlement_intent(second.id, 1, winner=second.contestants[0], refund=True)
        await recover_market(second.id)
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        await self.assert_conserved()

    async def test_partial_old_generation_finishes_before_correction_reversal(self):
        await self.bet(9, 100)
        await self.bet(10, 100)
        await self.bet(11, 100, self.market.contestants[1])
        await set_settlement_intent(self.market.id, 1, winner=self.market.contestants[0])
        await advance_settlement(self.market.id)
        await set_settlement_intent(self.market.id, 2, winner=self.market.contestants[1])
        await advance_settlement(self.market.id)
        market = await FinalBettingMarket.get(self.market.id)
        self.assertEqual(len(market.generations[0].paid), 2)
        self.assertEqual(market.generations[0].reversed, [])
        await recover_market(self.market.id)
        market = await FinalBettingMarket.get(self.market.id)
        self.assertEqual(len(market.generations), 2)
        self.assertEqual(market.settled_result_version, 2)
        self.assertEqual((await TokenWallet.get("11")).balance, 5200)
        await self.assert_conserved()

    async def test_spent_winnings_become_debt_and_only_reductions_remain_available(self):
        await self.bet(9, 5000)
        await self.bet(10, 5000, self.market.contestants[1])
        await set_settlement_intent(self.market.id, 1, winner=self.market.contestants[0])
        await recover_market(self.market.id)
        other = await open_market(self.final, "QF2")
        await self.bet(9, 10000, market=other)
        await set_settlement_intent(self.market.id, 2, winner=self.market.contestants[1])
        await recover_market(self.market.id)
        wallet = await TokenWallet.get("9")
        self.assertEqual(wallet.balance, -10000)
        self.assertEqual(await wallet_summary(wallet), (0, 10000, 10000))
        for amount, side in ((10001, other.contestants[0]), (9000, other.contestants[1])):
            with self.assertRaises(FinalError):
                await self.bet(9, amount, side=side, version=1, market=other)
        await self.bet(9, 9000, version=1, market=other)
        self.assertEqual((await TokenWallet.get("9")).balance, -9000)
        await self.bet(9, 0, version=2, market=other)
        self.assertEqual((await TokenWallet.get("9")).balance, 0)
        await self.assert_conserved()
