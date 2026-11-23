import asyncio
from unittest.mock import patch

from beanie import PydanticObjectId
from pymongo.errors import OperationFailure

from elo_bot.core.finals import FinalError
from elo_bot.core.token_items import TokenItem
from elo_bot.db import Match, MatchPlayerStats, Participant, RecordedMatch
from elo_bot.db.final_models import FinalPrediction
from elo_bot.db.token_models import ParticipantStake, TokenLedgerEntry, TokenWallet
from elo_bot.features.finals.persistence import transaction
from elo_bot.features.finals.lifecycle import confirm_bracket
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.matches import bind_thread
from elo_bot.features.finals.replacement import replace_entrant
from elo_bot.features.finals.recovery import recover_final
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.features.matches.cleanup import delete_unreserved_match
from elo_bot.features.results.operations import (
    begin_result_operation, complete_result_operation, delete_archived_result, persist_result_record,
    publish_result_operation,
)
from elo_bot.features.results.records import add_recorded_match
from elo_bot.features.tokens.awards import freeze_prediction_awards, recover_awards
from elo_bot.features.tokens.items import activate, purchase
from elo_bot.features.tokens.challenge_items import accept_challenge, insert_challenge, owned_challenge_code
from elo_bot.features.tokens.queue_items import finish_district_items, insert_queued_match, reserve_queue_items
from elo_bot.features.tokens.stake_publication import stake_lines
from elo_bot.features.tokens.stakes import reconcile_stake, set_stake, stake_window_closed
from elo_bot.features.tokens.receipts import write_receipt
from elo_bot.features.tokens.wallets import adjust_balance, ensure_wallet
from elo_bot.db import Player
from tests.integration._final_fixture import FinalScenarioCase


class TestTokenEconomy(FinalScenarioCase):
    async def test_purchase_and_replay_share_one_debit_and_unit(self):
        wallet = await ensure_wallet("1")
        await asyncio.gather(*(purchase(wallet, TokenItem.PRIORITY_PRO, "one") for _ in range(2)))
        current = await TokenWallet.get(wallet.id)
        self.assertEqual(current.balance, 4200)
        self.assertEqual(current.inventory, {TokenItem.PRIORITY_PRO: 1})
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 1)

    async def test_concurrent_purchases_cannot_spend_the_same_balance(self):
        wallet = await ensure_wallet("1")
        outcomes = await asyncio.gather(
            purchase(wallet, TokenItem.PROTECTION, "one"),
            purchase(wallet, TokenItem.PROTECTION, "two"), return_exceptions=True,
        )
        self.assertEqual(sum(isinstance(result, FinalError) for result in outcomes), 1)
        current = await TokenWallet.get(wallet.id)
        self.assertEqual(current.balance, 1400)
        self.assertEqual(current.inventory, {TokenItem.PROTECTION: 1})

    async def test_purchase_receipt_rolls_back_when_inventory_write_cannot_complete(self):
        wallet = await ensure_wallet("1")
        async def fail_after_debit(session, entry, **kwargs):
            await write_receipt(session, entry, **kwargs)
            raise OperationFailure("inventory unavailable")
        with patch("elo_bot.features.tokens.items.write_receipt", new=fail_after_debit):
            with self.assertRaises(OperationFailure):
                await purchase(wallet, TokenItem.PRIORITY_PRO, "retry")
        self.assertEqual((await TokenWallet.get(wallet.id)).balance, 5000)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "purchase"}).count(), 0)
        await purchase(wallet, TokenItem.PRIORITY_PRO, "retry")
        self.assertEqual((await TokenWallet.get(wallet.id)).inventory, {TokenItem.PRIORITY_PRO: 1})

    async def test_boost_consumes_on_pairing_and_district_refunds_on_cancellation(self):
        wallet = await ensure_wallet("1")
        for item in (TokenItem.PRIORITY_ULTRA, TokenItem.DISTRICT):
            await purchase(wallet, item, f"buy-{item}")
            await activate(wallet, item, f"use-{item}", district="Barbarian Camp" if item is TokenItem.DISTRICT else None)
        player = await Player.find_one({"user_id": "1"})
        attempt, boost = await reserve_queue_items(player)
        self.assertEqual(boost, 2)
        match = Match(id=PydanticObjectId(), token="queue-items", matched_time=1,
                      participants=[Participant(user_id="1"), Participant(user_id="2")])
        await insert_queued_match(match, [["Barbarian Camp"], ["Golem Quarry"], ["Goblin Mines"]], (attempt, None))
        self.assertEqual((await Match.get(match.id)).token_districts, {"0": "Barbarian Camp"})
        await finish_district_items(match, consume=False)
        await finish_district_items(match, consume=False)
        current = await TokenWallet.get(wallet.id)
        self.assertEqual(current.balance, 1600)
        self.assertEqual(current.inventory, {TokenItem.DISTRICT: 1, TokenItem.PRIORITY_ULTRA: 0})

    async def queued_match(self):
        return await Match(token="stake", thread="100", matched_time=1,
                           participants=[Participant(user_id="1"), Participant(user_id="2")]).insert()

    async def test_queued_all_in_above_cap_is_rejected_without_an_offer(self):
        match = await self.queued_match()
        with self.assertRaisesRegex(FinalError, "1,000-token"):
            await set_stake(match, "1", "all-in")
        self.assertIsNone(await ParticipantStake.get(str(match.id)))
        self.assertEqual((await TokenWallet.get("1")).balance, 5000)

    async def test_short_all_in_compares_with_the_offer_in_a_queued_match(self):
        match = await self.queued_match()
        await adjust_balance("2", -4400, "manager", "trim-caller")
        await set_stake(match, "1", 1000)
        with self.assertRaisesRegex(FinalError, "1,000 tokens to match"):
            await set_stake(match, "2", 600)
        locked = await set_stake(match, "2", "all-in")
        self.assertEqual(locked.contributions, {"1": 1000, "2": 600})
        self.assertEqual(locked.all_in_users, ["2"])

    async def test_final_all_in_threshold_and_asymmetric_payout_corrections(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        first = await self.scheduled_pairing(final, "QF1")
        rich, short = [participant.user_id for participant in first.participants]
        await adjust_balance(rich, 5000, "manager", "top-up-qf1")
        await set_stake(first, rich, "all-in")
        locked = await set_stake(first, short, "all-in")
        self.assertEqual(locked.contributions, {rich: 10_000, short: 5000})
        self.assertEqual(locked.all_in_users, [rich, short])
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (rich, short)], [0, 0])
        for outcome, balances in (
            (("result", short), [0, 15_000]),
            (("corrected", rich), [15_000, 0]),
            (("cancelled", None), [10_000, 5000]),
        ):
            await reconcile_stake(str(first.id), official_outcome=outcome)
            await reconcile_stake(str(first.id), official_outcome=outcome)
            self.assertEqual([(await TokenWallet.get(user)).balance for user in (rich, short)], balances)

        second = await self.scheduled_pairing(final, "QF2")
        rich, short = [participant.user_id for participant in second.participants]
        await adjust_balance(rich, 5000, "manager", "top-up-qf2")
        await adjust_balance(short, -1000, "manager", "trim-caller-qf2")
        await set_stake(second, rich, "all-in")
        locked = await set_stake(second, short, "all-in")
        self.assertEqual(locked.contributions, {rich: 4000, short: 4000})
        self.assertEqual(locked.all_in_users, [short])
        self.assertIn("stake was reduced to 4,000", stake_lines(locked)[0])
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (rich, short)], [6000, 0])
        await reconcile_stake(str(second.id), official_outcome=("result", short))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (rich, short)], [6000, 8000])

    async def test_stakes_lock_both_wallets_and_refund_an_unplayed_match(self):
        match = await self.queued_match()
        first = await set_stake(match, "1", 500)
        self.assertEqual(first.state, "offer")
        self.assertEqual(len(stake_lines(first)), 1)
        with self.assertRaisesRegex(FinalError, "500"):
            await set_stake(match, "2", 490)
        self.assertEqual((await TokenWallet.get("1")).balance, 5000)
        locked = await set_stake(match, "2", 500)
        self.assertEqual(locked.state, "locked")
        self.assertEqual(len(stake_lines(locked)), 2)
        await set_stake(match, "2", 500)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [4500, 4500])
        await reconcile_stake(str(match.id), deleted=match)
        await reconcile_stake(str(match.id), deleted=match)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5000, 5000])

    async def test_second_wallet_failure_rolls_back_first_debit_and_leaves_offer(self):
        match = await self.queued_match()
        await set_stake(match, "1", 500)
        async def fail_second(session, entry, **kwargs):
            if entry.wallet_id == "2":
                raise OperationFailure("second wallet unavailable")
            return await write_receipt(session, entry, **kwargs)
        with patch("elo_bot.features.tokens.stakes.write_receipt", new=fail_second):
            with self.assertRaises(OperationFailure):
                await set_stake(match, "2", 500)
        self.assertEqual((await ParticipantStake.get(str(match.id))).state, "offer")
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5000, 5000])
        self.assertEqual(await TokenLedgerEntry.find({"kind": "match_hold"}).count(), 0)

    async def test_start_closes_pending_offer_without_debit(self):
        match = await self.queued_match()
        await set_stake(match, "1", 500)
        await stake_window_closed(match)
        with self.assertRaisesRegex(FinalError, "when the match starts"):
            await set_stake(match, "2", 500)
        await reconcile_stake(str(match.id))
        self.assertEqual(stake_lines(await ParticipantStake.get(str(match.id))), [])
        self.assertEqual(await TokenLedgerEntry.find({"kind": "match_hold"}).count(), 0)

    async def test_ordinary_schedule_keeps_stakes_open(self):
        match = await self.queued_match()
        await set_stake(match, "1", 400)
        await match.set({"times.0": 12000, "hosts.0": None})
        await reconcile_stake(str(match.id))
        scheduled = await Match.get(match.id)
        self.assertEqual((await ParticipantStake.get(str(match.id))).state, "offer")
        self.assertEqual((await set_stake(scheduled, "2", 400)).state, "locked")

    async def test_start_racing_acceptance_cannot_leave_one_wallet_debited(self):
        match = await self.queued_match()
        await set_stake(match, "1", 500)
        accepted, closed = await asyncio.gather(
            set_stake(match, "2", 500), stake_window_closed(match), return_exceptions=True,
        )
        self.assertIsNone(closed)
        await reconcile_stake(str(match.id))
        state = await ParticipantStake.get(str(match.id))
        if isinstance(accepted, FinalError):
            self.assertEqual(state.state, "closed")
            balances = [5000, 5000]
        else:
            self.assertEqual(state.state, "locked")
            balances = [4500, 4500]
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], balances)

    async def test_prediction_awards_freeze_scored_revision_and_pay_once(self):
        final = await self.roster(2)
        for user, revision, points in (("1", 3, 7), ("2", 2, 5)):
            await FinalPrediction(id=f"{final.id}:{user}", final_id=str(final.id), season_number=final.season_number,
                                  setup_revision=0, user_id=user, submitted_at=1,
                                  scoring_revision=revision, points=points).insert()
        final.result_revision = 3
        await transaction(lambda session: freeze_prediction_awards(final, session))
        await recover_awards(str(final.id))
        await recover_awards(str(final.id))
        self.assertEqual((await TokenWallet.get("1")).balance, 8500)
        self.assertIsNone(await TokenWallet.get("2"))

    async def test_failed_prediction_credit_remains_pending_and_retries_once(self):
        final = await self.roster(2)
        await FinalPrediction(id=f"{final.id}:1", final_id=str(final.id), season_number=final.season_number,
                              setup_revision=0, user_id="1", submitted_at=1,
                              scoring_revision=0, points=2).insert()
        await transaction(lambda session: freeze_prediction_awards(final, session))
        async def fail_credit(session, entry, **kwargs):
            await write_receipt(session, entry, **kwargs)
            raise OperationFailure("credit interrupted")
        with (patch("elo_bot.features.tokens.awards.write_receipt", new=fail_credit),
              self.assertLogs(level="ERROR")):
            self.assertFalse(await recover_awards(str(final.id)))
        self.assertEqual((await TokenWallet.get("1")).balance, 5000)
        self.assertTrue(await recover_awards(str(final.id)))
        self.assertTrue(await recover_awards(str(final.id)))
        self.assertEqual((await TokenWallet.get("1")).balance, 6000)
        self.assertEqual(await TokenLedgerEntry.find({"kind": "prediction_reward"}).count(), 1)

    async def test_challenge_code_is_private_and_consumed_on_acceptance(self):
        wallet = await ensure_wallet("1")
        await purchase(wallet, TokenItem.CHALLENGE, "buy-code")
        use = await activate(wallet, TokenItem.CHALLENGE, "issue-code")
        await ensure_wallet("2")
        self.assertIsNone(await owned_challenge_code("2", use.target))
        owned = await owned_challenge_code("1", use.target)
        self.assertEqual(owned.id, use.id)
        match = Match(token="challenge", matched_time=1, is_challenge=True,
                      participants=[Participant(user_id="1", confirmed=True), Participant(user_id="2")])
        await insert_challenge(match, owned)
        self.assertIsNone(await owned_challenge_code("1", use.target))
        match.participants[1].confirmed = True
        await accept_challenge(match)
        self.assertTrue((await Match.get(match.id)).participants[1].confirmed)
        with self.assertRaises(FinalError):
            await accept_challenge(match)

    async def test_ranked_result_protects_floor_and_rewards_win_and_loss(self):
        wallet = await ensure_wallet("1")
        await Player.get_pymongo_collection().update_one({"user_id": "1"}, {"$set": {"elo": 1210}})
        await purchase(wallet, TokenItem.PROTECTION, "buy-protection")
        await activate(wallet, TokenItem.PROTECTION, "arm-protection")
        match = Match(id=PydanticObjectId(), token="ranked", matched_time=1, token_rewards=True,
                      participants=[Participant(user_id="1"), Participant(user_id="2")])
        await insert_queued_match(match, [["Barbarian Camp"], ["Golem Quarry"], ["Goblin Mines"]], (None, None))
        await begin_result_operation(match, {"player1": "2.80\n2.80\n2.80", "player2": "3.100\n3.100\n3.100"})
        record = RecordedMatch(id="1", time=1, players={
            "1": MatchPlayerStats(stars=6, destruction=240, opponent_stars=9, opponent_destruction=300,
                                  opponent="2", elo_change=-30, won=False),
            "2": MatchPlayerStats(stars=9, destruction=300, opponent_stars=6, opponent_destruction=240,
                                  opponent="1", elo_change=25, won=True),
        })
        stored = await persist_result_record(match, record)
        self.assertEqual(stored.players["1"].elo_change, -10)
        self.assertEqual(stored.players["2"].elo_change, 25)
        async def fail_loser_or_winner_credit(session, entry, **kwargs):
            if entry.wallet_id == "2":
                raise OperationFailure("second reward interrupted")
            return await write_receipt(session, entry, **kwargs)
        with patch("elo_bot.features.tokens.ranked.write_receipt", new=fail_loser_or_winner_credit):
            with self.assertRaises(OperationFailure):
                await complete_result_operation(match)
        self.assertFalse((await Match.get(match.id)).result_operation.completed)
        self.assertEqual((await TokenWallet.get("1")).balance, 1400)
        await complete_result_operation(match)
        await complete_result_operation(match)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [1500, 5200])
        await begin_result_operation(match, {"player1": "3.100\n3.100\n3.100", "player2": "2.80\n2.80\n2.80"}, stored)
        correction = record.model_copy(deep=True, update={"id": "2"})
        correction.players["1"].won, correction.players["1"].elo_change = True, 25
        correction.players["2"].won, correction.players["2"].elo_change = False, -30
        await persist_result_record(match, correction)
        await complete_result_operation(match)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [1600, 5100])
        self.assertEqual((await TokenWallet.get("1")).inventory[TokenItem.PROTECTION], 1)

    async def test_archived_ranked_record_deletion_refunds_stake_and_reverses_rewards(self):
        match = await self.queued_match()
        await match.set({"token_rewards": True})
        await set_stake(match, "1", 500)
        await set_stake(match, "2", 500)
        match = await Match.get(match.id)
        await begin_result_operation(match, {"player1": "3.100\n3.100\n3.100", "player2": "2.80\n2.80\n2.80"})
        record = RecordedMatch(id="11", time=1, players={
            "1": MatchPlayerStats(stars=9, destruction=300, opponent_stars=6, opponent_destruction=240,
                                  opponent="2", elo_change=25, won=True),
            "2": MatchPlayerStats(stars=6, destruction=240, opponent_stars=9, opponent_destruction=300,
                                  opponent="1", elo_change=-30, won=False),
        })
        record = await persist_result_record(match, record)
        first = await Player.find_one({"user_id": "1"})
        await add_recorded_match(first, record)
        self.assertEqual((await Player.find_one({"user_id": "1"})).elo, first.elo + 25)
        await complete_result_operation(match)
        await publish_result_operation(match)
        self.assertTrue(await delete_unreserved_match(match))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5700, 4600])
        self.assertTrue(await delete_archived_result(record))
        self.assertIsNone(await RecordedMatch.get(record.id))
        self.assertEqual((await Player.find_one({"user_id": "1"})).elo, first.elo)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in ("1", "2")], [5000, 5000])

    async def test_final_stake_exceeds_normal_cap_and_pays_once(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        match = await self.scheduled_pairing(final, "QF1")
        users = [participant.user_id for participant in match.participants]
        await set_stake(match, users[0], 1001)
        await set_stake(match, users[1], 1001)
        await accept_final_start(match, "manager")
        await submit_result(FinalResultSubmission(
            final_id=final.id, node_id="QF1", actor_id="manager", operation_key="qf1-stakes", expected_version=0,
            contestant_revision=match.final_contestant_revision, attempt=match.final_attempt,
            scores={users[0]: ["3.100"] * 7, users[1]: ["2.80"] * 7},
        ))
        await reconcile_stake(str(match.id))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in users], [6001, 3999])

    async def test_final_stake_correction_reconciles_after_the_match_row_is_deleted(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        match = await self.scheduled_pairing(final, "QF1")
        users = [participant.user_id for participant in match.participants]
        await set_stake(match, users[0], 600)
        await set_stake(match, users[1], 600)
        await accept_final_start(match, "manager")
        pairing = dict(final_id=final.id, node_id="QF1", actor_id="manager",
                       contestant_revision=match.final_contestant_revision, attempt=match.final_attempt)
        await submit_result(FinalResultSubmission(
            **pairing, operation_key="before-deletion", expected_version=0,
            scores={users[0]: ["3.100"] * 7, users[1]: ["2.80"] * 7},
        ))
        await recover_final(final)
        source = await Match.get(match.id)
        await source.set({"final_result_operation.published": True})
        self.assertTrue(await delete_unreserved_match(source))
        self.assertIsNone(await Match.get(match.id))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in users], [5600, 4400])
        await submit_result(FinalResultSubmission(
            **pairing, operation_key="after-deletion", expected_version=1, reason="Rechecked scores",
            scores={users[0]: ["2.80"] * 7, users[1]: ["3.100"] * 7},
        ))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in users], [4400, 5600])

    async def test_upstream_correction_refunds_a_deleted_downstream_stake(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        async def submit(match, winner_side, revision=0):
            users = [participant.user_id for participant in match.participants]
            return await submit_result(FinalResultSubmission(
                final_id=final.id, node_id=match.final_node_id, actor_id="manager",
                contestant_revision=match.final_contestant_revision, attempt=match.final_attempt,
                operation_key=f"{match.id}:{revision}", expected_version=revision,
                reason="Rechecked scores" if revision else None,
                scores={user: ["3.100" if side == winner_side else "2.80"] * 7
                        for side, user in enumerate(users)},
            ))

        quarters = {}
        for node in ("QF1", "QF2", "QF3", "QF4"):
            match = await self.scheduled_pairing(final, node)
            quarters[node] = match
            await accept_final_start(match, "manager")
            await submit(match, 0)
            await recover_final(final)

        final = await type(final).get(final.id)
        semi = await self.scheduled_pairing(final, "SF1")
        users = [participant.user_id for participant in semi.participants]
        for user in users:
            await set_stake(semi, user, 500)
        await accept_final_start(semi, "manager")
        await submit(semi, 0)
        await recover_final(final)
        source = await Match.get(semi.id)
        await source.set({"final_result_operation.published": True})
        self.assertTrue(await delete_unreserved_match(source))
        self.assertEqual([(await TokenWallet.get(user)).balance for user in users], [5500, 4500])
        await submit(quarters[final.nodes["SF1"].feeders[0]], 1, 1)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in users], [5000, 5000])

    async def test_reused_final_thread_refunds_old_pair_and_accepts_fresh_stakes(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        match = await self.scheduled_pairing(final, "QF1")
        await match.set({"thread": "100"})
        await bind_thread(final, match, "100")
        leaving, staying = [participant.user_id for participant in match.participants]
        await set_stake(match, leaving, 900)
        await set_stake(match, staying, 900)
        await replace_entrant(final, leaving, "9", "Replacement")
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (leaving, staying)], [5000, 5000])
        self.assertEqual(stake_lines(await ParticipantStake.get(str(match.id))), [])
        with self.assertRaisesRegex(FinalError, "pairing changed"):
            await set_stake(match, staying, 900)
        reused = await Match.get(match.id)
        await set_stake(reused, staying, 900)
        await set_stake(reused, "9", 900)
        self.assertEqual([(await TokenWallet.get(user)).balance for user in (leaving, staying, "9")], [5000, 4100, 4100])
