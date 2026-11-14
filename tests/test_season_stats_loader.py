import unittest
from unittest.mock import MagicMock, patch

from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.features.seasons.stats import SeasonStatsError, SeasonWindow
from elo_bot.features.seasons.stats_data import load_season_stats

PARENT = PydanticObjectId("000000000000000000000001")


class Cursor:
    def __init__(self, documents):
        self.documents = documents
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def __aiter__(self):
        return self.iterate()

    async def iterate(self):
        for document in self.documents:
            yield document


def document(record_id, **changes):
    return {"_id": record_id, "time": 15, "players": {
        user: {"districts": {"Capital Peak": {"stars": 3, "destruction": 100}}}
        for user in ("100", "200")}, **changes}


class TestSeasonStatsLoader(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.records = MagicMock()
        self.matches = MagicMock()
        self.patch_records = patch("elo_bot.features.seasons.stats_data.RecordedMatch.get_pymongo_collection",
                                   return_value=self.records)
        self.patch_matches = patch("elo_bot.features.seasons.stats_data.Match.get_pymongo_collection",
                                   return_value=self.matches)
        self.patch_records.start()
        self.patch_matches.start()
        self.addCleanup(self.patch_records.stop)
        self.addCleanup(self.patch_matches.stop)
        self.window = SeasonWindow("1", 10, 20)
        self.matches.find.return_value = Cursor([])

    async def test_legacy_matches_use_selected_range_and_close_cursor(self):
        # Given: legacy history with two participants.
        cursor = Cursor([document("1"), document("2", is_challenge=True)])
        self.records.find.return_value = cursor
        # When: the selected season is loaded.
        stats = await load_season_stats(self.window)
        # Then: one count per match and no player or parent history hydration.
        self.assertEqual((stats.matches, stats.challenges, dict(stats.districts)["Capital Peak"]), (2, 1, 2))
        self.assertEqual(self.records.find.call_args.args[0], {"time": {"$gt": 10, "$lt": 20}})
        self.matches.find.assert_not_called()
        self.assertTrue(cursor.closed)

    async def test_confirmation_timestamps_are_loaded_with_match_history(self):
        stored = document("1", matched_time=100, players={
            "100": {"confirmed_at": 110, "confirmation_method": "manual"},
            "200": {},
        })

        def projected_find(_query, projection):
            return Cursor([{key: value for key, value in stored.items() if projection.get(key)}])

        self.records.find.side_effect = projected_find
        stats = await load_season_stats(self.window)
        self.assertEqual((stats.confirmation_average, stats.confirmation_samples), (10, 1))

    async def test_manual_completion_marker_excludes_pending_record(self):
        # Given: one pending and one finished manual submission.
        self.records.find.return_value = Cursor([
            document("1", manual_submission_key="pending"),
            document("2", manual_submission_key="finished", manual_submission_completed=True),
        ])
        # When: the selected season is loaded.
        stats = await load_season_stats(self.window)
        # Then: only the committed manual result contributes.
        self.assertEqual((stats.matches, dict(stats.districts)["Capital Peak"]), (1, 1))

    async def test_pending_correction_keeps_only_previous_official_result(self):
        # Given: both correction versions are persisted while the replacement is pending.
        self.records.find.return_value = Cursor([
            document("old", operation_key=f"{PARENT}:old"), document("new", operation_key=f"{PARENT}:new"),
        ])
        self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
            "record": {"id": "new", "operation_key": f"{PARENT}:new"},
            "previous_record": {"id": "old", "operation_key": f"{PARENT}:old"},
        }}])
        # When: stats are read before correction completion.
        stats = await load_season_stats(self.window)
        # Then: the previous official match/district are counted once.
        self.assertEqual((stats.matches, dict(stats.districts)["Capital Peak"]), (1, 1))

    async def test_completed_correction_discards_old_row_fetched_before_deletion(self):
        # Given: the result cursor saw both rows, then the parent completed.
        self.records.find.return_value = Cursor([
            document("old", operation_key=f"{PARENT}:old"), document("new", operation_key=f"{PARENT}:new"),
        ])
        self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
            "record": {"id": "new", "operation_key": f"{PARENT}:new"}, "completed": True,
        }}])
        # When: the refreshed parent identity is applied.
        stats = await load_season_stats(self.window)
        # Then: the fetched superseded version contributes nothing.
        self.assertEqual(stats.matches, 1)

    async def test_pending_first_and_cancelled_results_are_excluded(self):
        # Given / When / Then: an unfinished first result, or a cancelled result, is not official.
        for state in ({}, {"completed": True, "cancelled": True}):
            with self.subTest(state=state):
                self.records.find.return_value = Cursor([document("new", operation_key=f"{PARENT}:new")])
                self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
                    "record": {"id": "new", "operation_key": f"{PARENT}:new"}, **state,
                }}])
                self.assertEqual((await load_season_stats(self.window)).matches, 0)

    async def test_cleaned_up_parent_retains_persisted_history(self):
        # Given: normal channel cleanup removed the completed Match parent.
        self.records.find.return_value = Cursor([document("old", operation_key=f"{PARENT}:old")])
        # When: old result history is summarized.
        stats = await load_season_stats(self.window)
        # Then: the persisted result still contributes.
        self.assertEqual(stats.matches, 1)

    async def test_parent_identity_is_stable_across_cursor_batches(self):
        # Given: correction versions straddle the hundred-row buffer boundary.
        rows = [document("old", operation_key=f"{PARENT}:old")]
        rows.extend(document(str(index)) for index in range(99))
        rows.append(document("new", operation_key=f"{PARENT}:new"))
        self.records.find.return_value = Cursor(rows)
        self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
            "previous_record": {"id": "old", "operation_key": f"{PARENT}:old"},
            "record": {"id": "new", "operation_key": f"{PARENT}:new"},
        }}])
        # When: a streamed read crosses batches.
        stats = await load_season_stats(self.window)
        # Then: one captured parent identity governs both versions.
        self.assertEqual(stats.matches, 100)
        self.matches.find.assert_called_once()

    async def test_corrupt_row_fails_and_closes_cursor(self):
        # Given: a stored row lacks participant results.
        cursor = Cursor([{"_id": "broken", "time": 15, "players": {}}])
        self.records.find.return_value = cursor
        # When / Then: fail explicitly while releasing the cursor.
        with self.assertRaises(SeasonStatsError):
            await load_season_stats(self.window)
        self.assertTrue(cursor.closed)

    async def test_database_failure_is_not_a_zero_match_success(self):
        # Given: the result query fails.
        self.records.find.side_effect = PyMongoError("unavailable")
        # When / Then: the failure propagates for the command boundary to report.
        with self.assertRaises(PyMongoError):
            await load_season_stats(self.window)

    async def test_parent_lookup_failure_closes_the_result_cursor(self):
        cursor = Cursor([document("1", operation_key=f"{PARENT}:operation")])
        self.records.find.return_value = cursor
        self.matches.find.side_effect = PyMongoError("unavailable")
        with self.assertRaises(PyMongoError):
            await load_season_stats(self.window)
        self.assertTrue(cursor.closed)

    async def test_completed_correction_replaces_legacy_record_without_operation_key(self):
        self.records.find.return_value = Cursor([
            document("old"), document("new", operation_key=f"{PARENT}:new", is_challenge=True),
        ])
        self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
            "record": {"id": "new", "operation_key": f"{PARENT}:new"},
            "previous_record": {"id": "old", "operation_key": None}, "completed": True,
        }}])
        stats = await load_season_stats(self.window)
        self.assertEqual((stats.matches, stats.challenges, dict(stats.districts)["Capital Peak"]), (1, 1, 1))

    async def test_legacy_correction_versions_across_batches_count_only_official_result(self):
        rows = [document("old"), *(document(str(index)) for index in range(99)),
                document("new", operation_key=f"{PARENT}:new")]
        self.records.find.return_value = Cursor(rows)
        self.matches.find.return_value = Cursor([{"_id": PARENT, "result_operation": {
            "record": {"id": "new", "operation_key": f"{PARENT}:new"},
            "previous_record": {"id": "old", "operation_key": None}, "completed": True,
        }}])
        self.assertEqual((await load_season_stats(self.window)).matches, 100)
