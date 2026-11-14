from tests.integration._final_fixture import FinalScenarioCase
from common.finals.lifecycle import confirm_bracket, end_final, request_cancellation, resolve_final, reset_bracket, start_final
from common.finals.matches import prepare_match
from common.finals.recovery import recover_final
from common.finals.results import FinalResultSubmission, submit_result
from common.finals.types import FinalError, ROUND_NODES
from common.tokens.bets import BetAction, apply_bet, open_market
from elobot.db import EloFinal, Match, Player, RecordedMatch, Season
from elobot.db.final_models import FinalMatchResult, FinalPlayerProfile
from elobot.db.token_models import FinalBettingMarket, TokenLedgerEntry, TokenWallet


class TestFinalRecovery(FinalScenarioCase):
    async def result(self, final, node_id, *, reverse=False, outcome="Played", reason=None):
        final = await resolve_final(final.season_number)
        if outcome == "Played" and final.nodes[node_id].result_id is None:
            await self.scheduled_pairing(final, node_id)
            final = await resolve_final(final.season_number)
        node = final.nodes[node_id]
        scores = {user: (["0.00"] * 7 if outcome == "Forfeit" else ["3.100" if (side == 0) != reverse else "2.80"] * 7)
                  for side, user in enumerate(node.contestants)}
        result = await submit_result(FinalResultSubmission(final_id=final.id, node_id=node_id, actor_id="manager",
            operation_key=f"result:{node_id}:{node.result_version + 1}:{node.contestant_revision}",
            expected_version=node.result_version, contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores=scores, winner=node.contestants[int(reverse)] if outcome == "Forfeit" else None,
            outcome=outcome, reason=reason))
        await recover_final(final)
        return result

    async def test_eight_and_five_player_journeys_archive_the_captured_season(self):
        for count, expected in ((8, 7), (5, 4)):
            with self.subTest(count=count):
                final = await confirm_bracket(await self.roster(count), "manager")
                before = [player.model_dump() for player in await Player.find_all().to_list()]
                for round_name, nodes in ROUND_NODES.items():
                    for node_id in nodes:
                        current = await resolve_final()
                        if current.nodes[node_id].bye:
                            with self.assertRaises(FinalError):
                                await open_market(current, node_id)
                            continue
                        await self.result(current, node_id)
                current = await resolve_final()
                await Season(season_number=99, start=3, end=4).insert()
                await end_final(current)
                saved = await EloFinal.get(final.id)
                self.assertEqual(saved.state, "COMPLETED")
                self.assertFalse(saved.active)
                self.assertEqual(saved.champion, "1")
                self.assertEqual((await Season.get(self.archive.id)).final_summary["champion"], "1")
                self.assertEqual(await FinalMatchResult.count(), expected)
                self.assertEqual(await RecordedMatch.count(), 0)
                self.assertEqual(before, [player.model_dump() for player in await Player.find_all().to_list()])
                self.assertEqual((await FinalPlayerProfile.get("1")).editions[str(final.id)].district_count,
                                 21 if count == 8 else 14)
                await EloFinal.find_all().delete()
                await FinalMatchResult.find_all().delete()
                await FinalPlayerProfile.find_all().delete()
                await Season.find({"season_number": 99}).delete()

    async def test_same_winner_correction_retains_descendant_and_equivalent_payout(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        market = await open_market(final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant=market.contestants[0], amount=1000,
                                expected_version=0, action_key="stake"))
        await self.result(final, "QF1")
        await self.result(final, "QF2")
        await self.result(final, "QF3")
        await self.result(final, "QF4")
        source = await prepare_match(await resolve_final(), "SF1")
        before = await FinalBettingMarket.get(market.id)
        await self.result(final, "QF1", reason="Corrected destruction")
        current = await resolve_final()
        self.assertEqual(current.nodes["SF1"].match_id, source.id)
        after = await FinalBettingMarket.get(market.id)
        self.assertEqual(after.generations, before.generations)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "reversal"}).count(), 0)

    async def test_changed_winner_invalidates_played_branch_and_refunds_its_pool(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        for node in ROUND_NODES["Quarterfinals"]:
            await self.result(final, node)
        current = await resolve_final()
        sf = await prepare_match(current, "SF1")
        market = await open_market(await resolve_final(), "SF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant=market.contestants[0], amount=1000,
                                expected_version=0, action_key="sf-stake"))
        await self.result(final, "SF1")
        await self.result(final, "SF2")
        await self.result(final, "F1")
        unaffected = (await resolve_final()).nodes["SF2"].result_id
        await self.result(final, "QF1", reverse=True, reason="Winner corrected")
        current = await resolve_final()
        self.assertEqual(current.nodes["SF1"].state, "NEEDS_MANAGER")
        self.assertIsNone(current.nodes["SF1"].result_id)
        self.assertIsNone(current.champion)
        self.assertEqual(current.nodes["SF2"].result_id, unaffected)
        self.assertTrue((await Match.get(sf.id)).final_superseded)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "REFUNDED")
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)
        self.assertEqual(await FinalMatchResult.count(), 8)

    async def test_played_to_forfeit_reverses_and_refunds_without_played_stats(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        market = await open_market(final, "QF1")
        await apply_bet(BetAction(market_id=market.id, user_id="9", contestant=market.contestants[0], amount=1000,
                                expected_version=0, action_key="stake"))
        await self.result(final, "QF1")
        await self.result(final, "QF1", outcome="Forfeit", reason="Unplayed fixture")
        market = await FinalBettingMarket.get(market.id)
        self.assertEqual(market.state, "REFUNDED")
        self.assertEqual(len(market.generations), 2)
        stats = (await FinalPlayerProfile.get("1")).editions[str(final.id)]
        self.assertEqual((stats.played, stats.forfeits), (0, 1))
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)

    async def test_cancellation_refunds_undecided_and_preserves_paid_result(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        paid = await open_market(final, "QF1")
        open_pair = await open_market(final, "QF2")
        for market in (paid, open_pair):
            await apply_bet(BetAction(market_id=market.id, user_id="9", contestant=market.contestants[0], amount=1000,
                                    expected_version=0, action_key=f"stake:{market.id}"))
        official = await self.result(final, "QF1")
        await request_cancellation(await resolve_final(), "Event cancelled")
        await recover_final(final)
        current = await resolve_final()
        self.assertEqual(current.state, "CANCELLED")
        self.assertFalse(current.active)
        self.assertEqual(current.nodes["QF1"].result_id, official.id)
        self.assertEqual((await FinalBettingMarket.get(paid.id)).state, "PAID")
        self.assertEqual((await FinalBettingMarket.get(open_pair.id)).state, "REFUNDED")
        self.assertEqual((await TokenWallet.get("9")).balance, 5000)

    async def test_reset_is_blocked_after_a_market_exists(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        await reset_bracket(final)
        final = await confirm_bracket(await resolve_final(), "manager")
        await open_market(final, "QF1")
        with self.assertRaises(FinalError):
            await reset_bracket(final)
        self.assertEqual((await resolve_final()).state, "BRACKET_READY")

    async def test_archived_correction_waits_for_another_active_final_before_reopening(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        for nodes in ROUND_NODES.values():
            for node_id in nodes:
                await self.result(final, node_id)
        await end_final(await resolve_final())
        await Season(season_number=8, start=2, end=3, players=[]).insert()
        ongoing = await start_final(8, final.guild_id, final.channel_id)
        await self.result(final, "QF1", reverse=True, reason="Archived winner corrected")
        old = await resolve_final(7)
        self.assertTrue(old.reopening_pending)
        self.assertFalse(old.active)
        self.assertEqual((await resolve_final()).id, ongoing.id)
        self.assertIsNone(old.champion)
        await request_cancellation(ongoing, "Test event cancelled")
        await recover_final(ongoing)
        await recover_final(old)
        old = await resolve_final(7)
        self.assertTrue(old.active)
        self.assertFalse(old.reopening_pending)
        self.assertEqual(old.state, "RUNNING")
