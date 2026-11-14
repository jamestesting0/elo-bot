import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, call, patch

import interactions as ipy
from pymongo.errors import OperationFailure

from elobot.extensions import match as match_commands
from elobot.features.base_store import storage
from elobot.features.results.operations import ResultOperationConflict, begin_result_operation
from elobot.features.seasons.stats import RecordedStatsRow, SeasonWindow, StatsAccumulator
from elobot.db import Match
from elobot.db.result_models import MatchResultOperation
from tests import _result_retry_store as record_fixture


from tests._result_retry_store import ResultRetryStateMixin

from tests._result_retry_store import RESULTS

class TestResultRetryConsistency(ResultRetryStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_result_tiers_follow_defenders_and_survive_checkpoint_retry(self) -> None:
        self.match["participants"][0]["bases"] = [
            {"link": "base", "image": "image", "base_quality": "Good"},
            {"link": "custom", "image": "image"},
            None,
        ]
        self.match["participants"][1]["bases"] = [
            {"link": "other", "image": "image", "base_quality": "Excellent"},
            {"link": "legacy", "image": "image"},
            {"link": "third", "image": "image", "base_quality": "Average"},
        ]

        def fail_checkpoint(updates):
            if updates.get("$set", {}).get("result_operation.completed"):
                self.before_match_update = lambda _updates: None
                raise OperationFailure("injected completion failure")

        self.before_match_update = fail_checkpoint
        with self.assertRaises(OperationFailure):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        self.match["participants"][1]["bases"][0]["base_quality"] = "Good"
        self.state.player_collection.update_one.reset_mock()
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)

        recorded = self.state.records["11"]
        self.assertEqual([s.base_quality for s in recorded.players["111"].districts.values()],
                         ["Excellent", None, "Average"])
        self.assertEqual([s.base_quality for s in recorded.players["222"].districts.values()],
                         ["Good", None, None])
        self.assertEqual(self.match["result_operation"]["record"]["players"]["111"]["districts"]
                         ["Barbarian Camp"]["base_quality"], "Excellent")

    async def test_correction_retains_original_tiers_without_guessing_legacy(self) -> None:
        old = self.state.records["10"]
        old.players["111"].districts["Barbarian Camp"].base_quality = "Excellent"
        old.players["222"].districts["Barbarian Camp"].base_quality = "Average"
        self.match["participants"][0]["bases"][0]["base_quality"] = "Good"
        self.match["participants"][1]["bases"][0] = {
            "link": "replacement", "image": "image", "base_quality": "Great",
        }
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}

        await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)

        recorded = self.state.records["11"]
        self.assertEqual([s.base_quality for s in recorded.players["111"].districts.values()],
                         ["Excellent", None, None])
        self.assertEqual([s.base_quality for s in recorded.players["222"].districts.values()],
                         ["Average", None, None])

    async def test_manual_record_has_unknown_tiers(self) -> None:
        with patch.object(Match, "find_one", new=AsyncMock(return_value=None)):
            await match_commands.record_match(
                MagicMock(), self.state.bot, "111", "222", RESULTS, record_fixture.DISTRICTS,
            )
        recorded = self.state.records["11"]
        self.assertTrue(all(
            district.base_quality is None
            for stats in recorded.players.values() for district in stats.districts.values()
        ))

    async def test_challenge_confirmation_times_reach_completed_season_statistics(self) -> None:
        self.match["is_challenge"] = True
        self.match["confirmation_tracking"] = True
        self.match["participants"][0].update(confirmed_at=100.0, confirmation_method="ready")
        self.match["participants"][1].update(confirmed_at=460.0, confirmation_method="manual")

        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)

        recorded = self.state.records["11"]
        self.assertTrue(recorded.is_challenge)
        window = SeasonWindow("current", None, 32536799999)
        accumulator = StatsAccumulator()
        accumulator.add(RecordedStatsRow.model_validate(recorded.model_dump(by_alias=True)), window)
        stats = accumulator.finish(window)
        self.assertEqual((stats.confirmation_average, stats.confirmation_samples), (180, 2))
        self.assertEqual((stats.non_instant_average, stats.non_instant_samples), (360, 1))

    async def test_confirmation_times_survive_result_checkpoint_retry(self) -> None:
        self.match["confirmation_tracking"] = True
        self.match["participants"][0].update(confirmed_at=100.0, confirmation_method="ready")
        self.match["participants"][1].update(confirmed_at=460.0, confirmation_method="manual")

        def fail_checkpoint(updates):
            if updates.get("$set", {}).get("result_operation.completed"):
                self.before_match_update = lambda _updates: None
                raise OperationFailure("injected completion checkpoint failure")

        self.before_match_update = fail_checkpoint
        with self.assertRaises(OperationFailure):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        self.state.player_collection.update_one.reset_mock()
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)

        recorded = self.state.records["11"]
        self.assertEqual(recorded.matched_time, 100.0)
        self.assertEqual((recorded.players["111"].confirmed_at, recorded.players["111"].confirmation_method),
                         (100.0, "ready"))
        self.assertEqual((recorded.players["222"].confirmed_at, recorded.players["222"].confirmation_method),
                         (460.0, "manual"))

    async def test_new_completion_invites_only_after_its_checkpoint_is_stored(self) -> None:
        async def invite(_bot, _user_ids, *, pending_only=False):
            self.assertFalse(pending_only)
            self.assertTrue(self.match["result_operation"]["completed"])
            self.assertEqual(self.match["recorded_match_id"], "11")
            self.assertTrue(all(
                self.state.records["11"].to_ref() in player["matches"] for player in self.state.players.values()
            ))
            self.assertEqual(len(self.base["records"]), 1)
            self.assertEqual(self.helper["points"], 0.5)

        self.state.invite.side_effect = invite
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN", RESULTS))
        self.state.invite.assert_awaited_once_with(self.state.bot, ("111", "222"), pending_only=False)

    async def test_failed_completion_checkpoint_does_not_invite_until_retry_commits(self) -> None:
        def fail_checkpoint(updates):
            if updates.get("$set", {}).get("result_operation.completed"):
                self.before_match_update = lambda _updates: None
                raise OperationFailure("injected completion checkpoint failure")

        self.before_match_update = fail_checkpoint
        with self.assertRaises(OperationFailure):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        self.assertFalse(self.match["result_operation"]["completed"])
        self.state.invite.assert_not_awaited()
        self.state.player_collection.update_one.reset_mock()
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN", RESULTS))
        self.assertTrue(self.match["result_operation"]["completed"])
        self.assertEqual(set(self.state.records), {"10", "11"})
        self.state.invite.assert_awaited_once_with(self.state.bot, ("111", "222"), pending_only=False)

    async def test_correction_only_recovers_pending_invitations_after_completion(self) -> None:
        self.state.records["9"] = self.state.make_record("9")
        for player in self.state.players.values():
            player["matches"].append(self.state.records["9"].to_ref())

        async def invite(_bot, _user_ids, *, pending_only=False):
            self.assertTrue(pending_only)
            self.assertTrue(self.match["result_operation"]["completed"])
            self.assertIsNotNone(self.match["result_operation"]["previous_record"])

        self.state.invite.side_effect = invite
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}
        await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        self.state.invite.assert_awaited_once_with(self.state.bot, ("111", "222"), pending_only=True)
        self.assertTrue(all(len(player["matches"]) == 2 for player in self.state.players.values()))

    async def test_automatic_recovery_of_correction_only_recovers_pending_invitations(self) -> None:
        def fail_checkpoint(updates):
            if updates.get("$set", {}).get("result_operation.completed"):
                self.before_match_update = lambda _updates: None
                raise OperationFailure("injected correction checkpoint failure")

        self.before_match_update = fail_checkpoint
        corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        with self.assertRaises(OperationFailure):
            await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        self.state.invite.assert_not_awaited()
        self.state.player_collection.update_one.reset_mock()
        self.assertTrue(await self.cog._finalize_match_results_locked("TOKEN", corrected))
        self.assertTrue(self.match["result_operation"]["completed"])
        self.state.invite.assert_awaited_once_with(self.state.bot, ("111", "222"), pending_only=True)

    async def test_record_retry_after_second_player_failure_reuses_original_record_and_delta(self) -> None:
        # Given: Player 1 commits and Player 2's write fails.
        def fail_second_player(query, updates) -> None:
            if query.get("user_id") == "222" and "$inc" in updates:
                self.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("injected second player write failure")
        self.state.before_player_update = fail_second_player
        with self.assertRaises(OperationFailure):
            await self.invoke_record()
        self.state.invite.assert_not_awaited()
        # When
        await self.invoke_record()
        # Then: one result is recorded and each original delta is applied once.
        self.assertEqual(set(self.state.records), {"10", "11"})
        self.assertEqual([player["elo"] for player in self.state.players.values()], [1033, 972])

    async def test_finalization_retry_after_discord_failure_does_not_repeat_persistent_effects(self) -> None:
        # Given: persistence succeeds but posting the result summary fails.
        self.thread.send.side_effect = [
            ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable")), MagicMock(edit=AsyncMock()),
        ]
        self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN", RESULTS))
        self.state.player_collection.update_one.reset_mock()
        # When
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        # Then: retry leaves one host credit, one base row, and one new recorded match.
        self.assertEqual((self.helper["points"], len(self.helper["history"]), len(self.base["records"])), (0.5, 1, 1))
        self.assertEqual(set(self.state.records), {"10", "11"})
        self.assertEqual(self.state.invite.await_args_list, [
            call(self.state.bot, ("111", "222"), pending_only=False),
            call(self.state.bot, ("111", "222"), pending_only=True),
        ])

    async def test_resubmit_retry_after_second_player_failure_finishes_original_correction(self) -> None:
        # Given: the old record is rolled back, then the corrected record fails for Player 2.
        self.state.records["10"].players["111"].districts["Barbarian Camp"].base_quality = "Great"
        corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        def fail_second_player(query, updates) -> None:
            if query.get("user_id") == "222" and "$addToSet" in updates:
                self.state.before_player_update = lambda _query, _updates: None
                raise OperationFailure("injected correction player write failure")
        self.state.before_player_update = fail_second_player
        with self.assertRaises(OperationFailure):
            await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        self.state.player_collection.update_one.reset_mock()
        first_player_elo = self.state.players["111"]["elo"]
        # When
        await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        # Then: both histories converge on the same correction without repeating Player 1's change.
        self.assertEqual(len(self.state.players["222"]["matches"]), 1)
        self.assertEqual(self.state.players["111"]["matches"], self.state.players["222"]["matches"])
        self.assertEqual(self.state.players["111"]["elo"], first_player_elo)
        self.assertEqual(self.state.records["11"].players["111"].districts["Barbarian Camp"].base_quality, "Great")

    async def test_finalization_clears_flags_for_consumed_started_base_stores(self) -> None:
        # Given: starting the match consumed the store and retained its stored flag.
        self.match["participants"][0]["opponent_bases_stored"] = True
        # When
        with patch.object(match_commands, "clear_match_base_stores", new=storage.clear_match_base_stores), \
                patch.object(storage, "sync_base_store_slots", new=AsyncMock()):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        # Then: durable result completion permits releasing the consumed marker.
        self.assertFalse(self.match["participants"][0]["opponent_bases_stored"])
        self.assertTrue(self.match["result_operation"]["completed"])

    async def test_retry_after_base_write_commits_does_not_append_again(self) -> None:
        # Given: the base append commits but its acknowledgement fails.
        def fail_after_append(updates) -> None:
            if "$push" in updates:
                self.after_base_update = lambda _updates: None
                raise OperationFailure("injected base acknowledgement failure")
        self.after_base_update = fail_after_append
        with self.assertRaises(OperationFailure):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        # When
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        # Then: receipt and base row remain paired through the replay.
        self.assertEqual((len(self.base["records"]), len(self.base["operation_receipts"])), (1, 1))

    async def test_new_reservation_prevents_publication_and_success(self) -> None:
        # Given: a reservation appears after the finalizer's last Match read.
        def replace_store(updates) -> None:
            if updates.get("$set", {}).get("result_operation.completed"):
                self.match["participants"][0]["base_store"] = {"clan_tag": "#NEW", "slot": "Slot 1"}
        self.before_match_update = replace_store
        ctx = MagicMock(send=AsyncMock(), message=None)
        # When
        with self.assertRaises(ResultOperationConflict):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS, ctx)
        # Then: the remaining store is retained and no terminal success is sent.
        self.assertTrue(self.match["result_operation"]["completed"])
        self.assertFalse(self.match["result_operation"]["published"])
        ctx.send.assert_not_awaited()

    async def test_result_claim_rejects_intervening_unfinished_autowin(self) -> None:
        # Given: autowin claims after this result submission read the match.
        stale = Match.model_validate(deepcopy(self.match))
        self.match.update(autowin_winner_id="111", autowin_completed=False)
        # When / Then: the opposite decision cannot claim the stale match.
        with self.assertRaises(ResultOperationConflict):
            await begin_result_operation(stale, RESULTS)

    async def test_result_claim_allows_a_later_operator_action_after_autowin_completes(self) -> None:
        # Given: an earlier auto-win is complete.
        self.match.update(autowin_winner_id="111", autowin_completed=True)
        # When
        operation = await begin_result_operation(Match.model_validate(deepcopy(self.match)), RESULTS)
        # Then: the existing manual result path remains available.
        self.assertEqual(self.match["result_operation"]["key"], operation.key)

    async def test_recorded_attack_never_shortens_a_newer_base_renewal(self) -> None:
        # Given: a base was renewed later than either this pending operation or a new legacy write.
        for operation in (None, MatchResultOperation(key="pending", response=RESULTS, created_at=10.0)):
            with self.subTest(operation=operation):
                self.base["expiration_date"] = 10_000_000_000.0
                match = Match.model_validate(deepcopy(self.match))
                match.result_operation = operation
                # When
                await match_commands.record_base_stats({"111": RESULTS["player1"].splitlines(),
                                                        "222": RESULTS["player2"].splitlines()}, match)
                # Then: extending a link cannot reduce its existing lifetime.
                self.assertEqual(self.base["expiration_date"], 10_000_000_000.0)

    async def test_recorded_attack_extends_expired_base_by_fifteen_days(self) -> None:
        self.base["expiration_date"] = 1.0
        match = Match.model_validate(deepcopy(self.match))
        match.result_operation = MatchResultOperation(key="pending", response=RESULTS, created_at=10.0)

        await match_commands.record_base_stats({"111": RESULTS["player1"].splitlines(),
                                                "222": RESULTS["player2"].splitlines()}, match)

        self.assertEqual(self.base["expiration_date"], 10.0 + 15 * 86400)

    async def test_publication_retry_preserves_an_explicitly_deleted_completed_record(self) -> None:
        # Given: bookkeeping completes, publication fails, then an operator deletes that record.
        self.thread.send.side_effect = [ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable")), MagicMock()]
        self.assertFalse(await self.cog._finalize_match_results_locked("TOKEN", RESULTS))
        deleted = self.state.records.pop("11")
        for player_id, player in self.state.players.items():
            record_fixture.apply_update(player, {"$pull": {"matches": deleted.to_ref()},
                                                "$inc": {"elo": -deleted.players[player_id].elo_change}})
        self.state.player_collection.update_one.reset_mock()
        # When / Then: retry reports a conflict without recreating deleted data.
        with self.assertRaises(ResultOperationConflict):
            await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        self.assertEqual(set(self.state.records), {"10"})
        self.assertEqual([player["elo"] for player in self.state.players.values()], [1000, 1000])

    async def test_resubmit_resumes_after_previous_record_delete_and_checkpoint_failure(self) -> None:
        # Given: the correction finishes its writes and deletes the old record before checkpoint persistence fails.
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        self.state.invite.reset_mock()
        self.state.player_collection.update_one.reset_mock()
        corrected = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]}
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        def fail_checkpoint(updates) -> None:
            if updates.get("$set", {}).get("result_operation.completed"):
                self.before_match_update = lambda _updates: None
                raise OperationFailure("injected completion checkpoint failure")
        self.before_match_update = fail_checkpoint
        with self.assertRaises(OperationFailure):
            await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        self.state.invite.assert_not_awaited()
        self.assertEqual(set(self.state.records), {"10", "12"})
        committed_elos = [player["elo"] for player in self.state.players.values()]
        self.state.player_collection.update_one.reset_mock()
        # When
        await self.cog.resubmit_modal.callback.func(self.cog, ctx, **corrected)
        self.state.invite.assert_awaited_once_with(self.state.bot, ("111", "222"), pending_only=True)
        # Then: the saved rollback snapshot resumes without repeating either correction delta or base write.
        self.assertEqual(set(self.state.records), {"10", "12"})
        self.assertEqual([player["elo"] for player in self.state.players.values()], committed_elos)
        self.assertEqual(len(self.base["records"]), 1)
        self.assertTrue(self.match["result_operation"]["published"])

    async def test_distinct_resubmissions_replace_the_current_record_and_preserve_older_history(self) -> None:
        # Given: one result has already been intentionally corrected.
        await self.cog._finalize_match_results_locked("TOKEN", RESULTS)
        ctx = MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                        guild=self.state.bot.fetch_guild.return_value)
        self.state.player_collection.update_one.reset_mock()
        await self.cog.resubmit_modal.callback.func(self.cog, ctx,
            player1=RESULTS["player2"], player2=RESULTS["player1"])
        self.state.player_collection.update_one.reset_mock()
        # When
        await self.cog.resubmit_modal.callback.func(self.cog, ctx, **RESULTS)
        # Then: a distinct correction replaces the latest result and keeps unrelated old history.
        self.assertEqual(set(self.state.records), {"10", "13"})
        self.assertEqual([ref.id for ref in self.state.players["111"]["matches"]], ["10", "13"])
        self.assertEqual([player["elo"] for player in self.state.players.values()], [1033, 972])
        self.assertEqual((len(self.base["records"]), self.helper["points"]), (1, 0.5))
