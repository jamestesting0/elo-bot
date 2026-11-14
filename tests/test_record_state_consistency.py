import unittest
from unittest.mock import call

from bson import DBRef

from elobot.db import Player


from tests._result_retry_store import RecordStateMixin


from tests._result_retry_store import apply_update
import elobot.features.results.recording as recording_module

class TestRecordStateConsistency(RecordStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_record_refreshes_autocomplete_after_the_match_is_stored(self) -> None:
        await self.invoke_record()
        guild = self.choice_refresh.await_args_list[0].args[1]
        self.choice_refresh.assert_has_awaits([
            call(self.bot, guild, "111"),
            call(self.bot, guild, "222"),
        ])
        self.assertTrue(all(
            any(reference.id == "11" for reference in player["matches"])
            for player in self.players.values()
        ))
        self.invite.assert_not_awaited()

    async def test_record_preserves_concurrent_history_deletion_and_elo_rollback(self) -> None:
        # Given: deletion commits after record_match fetched both players.
        def delete_old_history() -> None:
            for user_id, player in self.players.items():
                apply_update(player, {"$pull": {"matches": self.records["10"].to_ref()},
                                      "$inc": {"elo": -self.records["10"].players[user_id].elo_change}})
                player["priority"] = True
        self.before_insert = delete_old_history
        # When
        await self.invoke_record()
        # Then: only the new link and its delta are added; deleted history stays deleted.
        self.assertEqual([stats.elo_change for stats in self.records["11"].players.values()], [33, -28])
        for user_id, old_elo in (("111", 980), ("222", 1020)):
            self.assertEqual(self.players[user_id]["matches"], [self.records["11"].to_ref()])
            self.assertEqual(self.players[user_id]["elo"], old_elo + self.records["11"].players[user_id].elo_change)
            self.assertTrue(self.players[user_id]["priority"])

    async def test_record_raises_highest_elo_from_the_persisted_rating(self) -> None:
        # Given: another rating award commits before this match is linked.
        self.before_insert = lambda: self.players["111"].update(elo=1300, highest_elo=1300)
        # When
        await self.invoke_record()
        # Then: the current Elo receives the original calculated delta and sets the new high.
        expected = 1300 + self.records["11"].players["111"].elo_change
        self.assertEqual((self.players["111"]["elo"], self.players["111"]["highest_elo"]), (expected, expected))

    async def test_record_recomputes_statistics_when_history_changes_after_refresh(self) -> None:
        # Given: another match arrives after the post-write linked-player snapshot.
        def append_after_read(player: Player) -> None:
            if player.user_id == "111" and any(record.to_ref().id == "11" for record in player.matches):
                self.after_player_read = lambda _player: None
                self.records["12"] = self.make_record("12")
                apply_update(self.players["111"], {"$addToSet": {"matches": self.records["12"].to_ref()},
                                                    "$inc": {"wins": 1}})
        self.after_player_read = append_after_read
        # When
        await self.invoke_record()
        # Then: statistics include the concurrent history entry.
        self.assertEqual(self.players["111"]["wins"], 3)

    async def test_record_handles_reordered_lookup_and_missing_history_links(self) -> None:
        # Given: lookup returns a different order and omits a missing legacy link.
        missing = DBRef("recorded_matches", "missing")
        self.players["111"]["matches"].insert(0, missing)
        self.lookup_reverse = True
        # When
        await self.invoke_record()
        # Then: raw membership is retained and statistics use existing records.
        self.assertEqual(self.players["111"]["matches"], [missing, self.records["10"].to_ref(), self.records["11"].to_ref()])
        self.assertEqual(self.players["111"]["wins"], 2)

    async def test_repeated_record_application_does_not_repeat_rating_or_win_delta(self) -> None:
        # Given: the same new record was already applied from this stale player snapshot.
        self.records["11"] = self.make_record("11")
        stale = await self.find_player({"user_id": "111"})
        await recording_module.add_recorded_match(stale, self.records["11"])
        # When
        await recording_module.add_recorded_match(stale, self.records["11"])
        # Then: match membership and both increments are applied once.
        self.assertEqual((self.players["111"]["elo"], self.players["111"]["wins"]), (1020, 2))
        self.assertEqual(self.players["111"]["matches"], [self.records["10"].to_ref(), self.records["11"].to_ref()])


    async def test_resubmit_removes_selected_full_base_row_and_preserves_concurrent_append(self) -> None:
        # Given: a same-score attack with a different timestamp arrives after the base read.
        ctx = self.prepare_resubmit()
        def append_record() -> None:
            self.after_base_read = lambda: None
            self.base["records"].append(["222", 2, 90, 30.0])
        self.after_base_read = append_record
        # When
        await self.invoke_resubmit(ctx)
        # Then: the selected timestamp-20 row alone is removed.
        self.assertEqual(self.base["records"], [["222", 2, 90, 10.0], ["222", 2, 90, 30.0]])

    async def test_resubmit_preserves_concurrent_player_record_and_rating(self) -> None:
        # Given: a new match is linked after the old Player snapshots were fetched.
        ctx = self.prepare_resubmit()
        def append_record(player: Player) -> None:
            if player.user_id == "222":
                self.after_player_read = lambda _player: None
                self.records["12"] = self.make_record("12")
                apply_update(self.players["111"], {"$addToSet": {"matches": self.records["12"].to_ref()},
                                                    "$inc": {"elo": 30}})
        self.after_player_read = append_record
        # When
        await self.invoke_resubmit(ctx)
        # Then: the selected old match alone is reversed.
        self.assertEqual(self.players["111"]["elo"], 1010)
        self.assertEqual(self.players["111"]["matches"], [self.records["12"].to_ref(), self.records["11"].to_ref()])

    async def test_resubmit_removes_only_one_identical_complete_base_row(self) -> None:
        # Given: the base contains two completely identical old rows.
        ctx = self.prepare_resubmit()
        self.base["records"].append(["222", 2, 90, 20.0])
        # When
        await self.invoke_resubmit(ctx)
        # Then: one identical instance remains.
        self.assertEqual(self.base["records"], [["222", 2, 90, 10.0], ["222", 2, 90, 20.0]])
