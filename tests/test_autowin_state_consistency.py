import unittest
from unittest.mock import AsyncMock, patch

from pymongo.errors import OperationFailure

from cogs import match_commands
from elobot.db import BaseStore, Helper, Match, Player


from tests._autowin_fixture import AutowinStateMixin

class TestAutowinStateConsistency(AutowinStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_second_player_failure_retries_without_awarding_the_winner_twice(self) -> None:
        # Given: the winner commits before the loser fails.
        self.fail_player = "222"
        failed = self.context()
        with self.assertRaises(OperationFailure):
            await self.invoke(failed)
        self.invite.assert_not_awaited()
        self.assertEqual(self.elos(), [1005, 1000])
        failed.send.assert_not_awaited()
        # When
        await self.invoke(self.context())
        # Then
        self.assertEqual(self.elos(), [1005, 965])
        self.assertEqual([row["wins"] for row in self.rows[Player]], [7, 7])
        self.assertTrue(self.rows[Player][0]["priority"])

    async def test_autowin_does_not_trigger_invitation_check(self) -> None:
        await self.invoke(self.context())
        self.assertTrue(self.rows[Match][0]["autowin_completed"])
        self.assertTrue(all(str(self.match.id) in row["autowin_receipts"] for row in self.rows[Player]))
        self.invite.assert_not_awaited()

    async def test_concurrent_elo_change_is_preserved_with_the_autowin_award(self) -> None:
        # Given: another result adds 20 Elo after the autowin snapshot is read.
        self.concurrent_elo = True
        # When
        await self.invoke(self.context())
        # Then: both deltas and the corresponding highest Elo are persisted.
        self.assertEqual(self.elos(), [1025, 965])
        self.assertEqual(self.rows[Player][0]["highest_elo"], 1025)

    async def test_retry_cannot_replace_the_persisted_winner_decision(self) -> None:
        # Given: one winner was selected before an interrupted loser write.
        self.fail_player = "222"
        with self.assertRaises(OperationFailure):
            await self.invoke(self.context())
        # When: a retry names the opposite winner.
        ctx = self.context()
        await self.invoke(ctx, winner="222")
        # Then: neither participant receives the conflicting result.
        self.assertEqual(self.elos(), [1005, 1000])
        self.assertFalse(self.rows[Match][0]["is_autowin"])

    async def test_result_claim_between_read_and_autowin_write_prevents_both_awards(self) -> None:
        from common.results.operations import begin_result_operation

        # Given: a result operation claims Mongo after the autowin snapshot was read.
        original_claim = match_commands.claim_autowin

        async def competing_result(match, *args):
            await begin_result_operation(match.model_copy(deep=True), {
                "player1": "3.100\n3.100\n3.100", "player2": "2.90\n2.90\n2.90",
            })
            return await original_claim(match, *args)

        # When
        with patch.object(match_commands, "claim_autowin", new=competing_result):
            await self.invoke(self.context())
        # Then: the queued result decision exclusively owns the terminal operation.
        self.assertEqual(self.elos(), [1000, 1000])
        self.assertIsNone(self.rows[Match][0]["autowin_winner_id"])

    async def test_published_result_still_allows_an_operator_autowin_correction(self) -> None:
        from elobot.db.result_models import MatchResultOperation

        # Given: an ordinary result has completed publishing.
        self.rows[Match][0]["result_operation"] = MatchResultOperation(
            key="recorded", response={}, created_at=1, completed=True, published=True,
        ).model_dump()
        # When
        await self.invoke(self.context())
        # Then: the existing operator correction remains supported.
        self.assertEqual(self.elos(), [1005, 965])

    async def test_cleanup_failure_after_ratings_can_be_completed_on_retry(self) -> None:
        # Given: persisted ratings and flag are followed by a cleanup error.
        self.free_clans.side_effect = [OperationFailure("injected clan cleanup failure"), None]
        with self.assertRaises(OperationFailure):
            await self.invoke(self.context())
        # When
        await self.invoke(self.context())
        # Then: retry completes cleanup without changing ratings again.
        self.assertEqual(self.elos(), [1005, 965])
        self.assertEqual(self.free_clans.await_count, 2)
        self.cleanup.assert_has_awaits([unittest.mock.call("321", "111"), unittest.mock.call("321", "222")])

    async def test_helper_credit_response_loss_does_not_duplicate_points_or_report_early_success(self) -> None:
        # Given: one host credit commits but its acknowledgement is lost.
        self.rows[Match][0].update(is_war=False, hosts=["900", None], times=[1000, None])
        self.fail_helper_after_write = True
        failed = self.context()
        with self.assertRaises(OperationFailure):
            await self.invoke(failed)
        failed.send.assert_not_awaited()
        # When
        await self.invoke(self.context())
        # Then: one host credit and one history entry survive retry.
        self.assertEqual(self.rows[Helper][0]["points"], 3)
        self.assertEqual(len(self.rows[Helper][0]["history"]), 1)

    async def test_completed_and_legacy_autowins_do_not_repeat_terminal_work(self) -> None:
        # Given: a completed transition, then a legacy match with no new receipt fields.
        for completed in (False, True):
            with self.subTest(completed=completed):
                self.rows[Match][0].update(is_autowin=True, autowin_completed=completed)
                if completed:
                    self.rows[Match][0]["autowin_winner_id"] = "111"
                else:
                    for field in list(self.rows[Match][0]):
                        if field.startswith("autowin_"):
                            self.rows[Match][0].pop(field)
                # When
                await self.invoke(self.context())
                # Then
                self.assertEqual(self.elos(), [1000, 1000])
                self.free_clans.assert_not_awaited()
                self.autoqueue.assert_not_awaited()

    async def test_autowin_retains_reserved_bases_and_explains_the_held_slot(self) -> None:
        # Given: an ordinary auto-win has a reservation but no complete saved match results.
        store = BaseStore(clan_tag="#CLAN", slot="Slot 1", reservation_id="HELD", message_id=9001)
        self.rows[Match][0]["participants"][0].update(base_store=store.model_dump(), opponent_bases_stored=True)
        ctx = self.context()
        # When
        with patch.object(match_commands, "clear_match_base_stores", new=AsyncMock()) as clear_stores, \
                patch.object(match_commands.base_store_publication, "mark_store_reserved", new=AsyncMock(return_value=True)):
            await self.invoke(ctx)
        # Then: ratings complete while the slot stays reserved and the public response explains why.
        clear_stores.assert_not_awaited()
        self.assertEqual(self.rows[Match][0]["participants"][0]["base_store"], store.model_dump())
        self.assertTrue(self.rows[Match][0]["participants"][0]["opponent_bases_stored"])
        self.assertEqual(self.elos(), [1005, 965])
        self.assertTrue(self.rows[Match][0]["autowin_completed"])
        self.assertIn("Undo", ctx.send.await_args.args[0])
        public_embed = ctx.channel.send.await_args.kwargs["embed"]
        self.assertIn("remain reserved", public_embed.description)
        self.assertIn("raid weekend", public_embed.description)

    async def test_autowin_retains_a_legacy_stored_flag(self) -> None:
        # Given: an unresolved legacy stored marker still reserves capacity.
        self.rows[Match][0]["participants"][0]["opponent_bases_stored"] = True
        ctx = self.context()
        # When
        with patch.object(match_commands, "clear_match_base_stores", new=AsyncMock()) as clear_stores:
            await self.invoke(ctx)
        # Then
        clear_stores.assert_not_awaited()
        self.assertTrue(self.rows[Match][0]["participants"][0]["opponent_bases_stored"])
        self.assertFalse(self.rows[Match][0]["autowin_completed"])
        self.assertIn("rating changes are saved", ctx.send.await_args.args[0])
        self.assertIn("Undo", ctx.send.await_args.args[0])
        self.rows[Match][0]["participants"][0]["opponent_bases_stored"] = False
        await self.invoke(self.context())
        self.assertEqual(self.elos(), [1005, 965])
        self.assertTrue(self.rows[Match][0]["autowin_completed"])

