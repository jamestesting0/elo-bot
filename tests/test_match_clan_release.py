import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

import interactions as ipy

from elo_bot.extensions.match import MatchCommands
from elo_bot.db import Match
from tests._patch_sites import patch_sites
import elo_bot.extensions.match.lifecycle as match_lifecycle_module


class TestMatchClanRelease(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.bot = MagicMock()
        self.bot.owner.id = 999
        self.bot.ext = {}
        self.cog = MatchCommands(self.bot)

    async def test_result_bookkeeping_failure_preserves_both_war_reservations(self) -> None:
        from pymongo.results import UpdateResult

        from tests._result_finalization_fixture import make_result_match

        # Given a war match whose two sides occupy different clans.
        match = make_result_match(stored_bases=True)
        match.thread = "123"
        match.host_clans = ["#FIRST", "#SECOND"]
        match.hosts = [None, None]
        match.times = [None, None]
        stores = [participant.base_store.model_copy(deep=True) for participant in match.participants]
        for participant in match.participants:
            participant.result = [None, None, None]
        collection = MagicMock(update_one=AsyncMock(
            return_value=UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)
        ))
        match_thread = MagicMock(name="match-thread")
        match_thread.name = "🟢 Match"
        self.bot.fetch_channel = AsyncMock(return_value=match_thread)
        self.bot.fetch_guild = AsyncMock(return_value=MagicMock())

        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.custom_id = "result_modal|TOKEN"
        ctx.channel.name = "🟢 Match"
        ctx.send = AsyncMock()
        response = {
            "player1": "3.100\n2.90\n2.80",
            "player2": "2.90\n2.80\n1.70",
        }

        with (
                patch.object(Match, "save_changes", new_callable=AsyncMock) as save_changes,
                patch.object(Match, "get_pymongo_collection", return_value=collection),
                patch_sites(('elo_bot.features.hosting.live.live_start.Match', 'elo_bot.features.hosting.war.war_start.Match', 'elo_bot.features.base_store.match_adapters.Match', 'elo_bot.extensions.match.lifecycle.Match', 'elo_bot.extensions.match.bases.Match', 'elo_bot.extensions.match.base_store.Match', 'elo_bot.extensions.match.results.Match', 'elo_bot.extensions.match.result_store.Match', 'elo_bot.extensions.match.hosting.Match', 'elo_bot.extensions.match.settings.Match', 'elo_bot.extensions.match.dashboards.Match', 'elo_bot.features.queue.state.Match', 'elo_bot.features.matches.occupancy.Match', 'elo_bot.features.matches.resources.Match', 'elo_bot.features.matches.lifecycle.Match', 'elo_bot.features.matches.attack_bases.Match', 'elo_bot.features.results.recording.Match')) as match_model,
                patch_sites(('elo_bot.extensions.match.lifecycle.clear_match_base_stores', 'elo_bot.extensions.match.results.clear_match_base_stores'), new_callable=AsyncMock) as clear_stores,
                patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free_clan,
                patch('elo_bot.extensions.match.results.record_base_stats', new=AsyncMock(side_effect=RuntimeError("later result work failed"))),
        ):
            match_model.find_one = AsyncMock(return_value=match)
            # When durable result storage succeeds but later result work fails.
            with self.assertRaisesRegex(RuntimeError, "later result work failed"):
                await self.cog._process_match_results(ctx, response)

        # Then failed bookkeeping preserves both reservations.
        save_changes.assert_not_awaited()
        clear_stores.assert_not_awaited()
        operation = match.result_operation
        assert operation is not None
        self.assertEqual(operation.response, response)
        self.assertFalse(operation.completed)
        self.assertFalse(operation.published)
        collection.update_one.assert_awaited_once_with(
            {"_id": match.id, "result_operation.key": None,
             "$or": [{"autowin_winner_id": None}, {"autowin_completed": True}],
             "participants.0.result": [None, None, None], "participants.1.result": [None, None, None]},
            {"$set": {"result_operation": operation.model_dump(),
                      "participants.0.result": response["player1"].split("\n"),
                      "participants.1.result": response["player2"].split("\n")}},
        )
        ctx.send.assert_not_awaited()
        free_clan.assert_not_awaited()
        self.assertEqual([participant.base_store for participant in match.participants], stores)
        self.assertEqual(match.host_clans, ["#FIRST", "#SECOND"])

    async def test_autowin_releases_both_war_clans_before_notifications(self) -> None:
        from tests._autowin_fixture import AutowinState

        release_clans = match_lifecycle_module.free_match_clans
        fixture = AutowinState(self)
        fixture.enterContext = self.enterContext
        fixture.addCleanup = self.addCleanup
        fixture.setUp()
        fixture.rows[Match][0]["host_clans"] = ["#FIRST", "#SECOND"]
        fixture.free_clans.side_effect = release_clans
        fixture.users["111"].send.side_effect = RuntimeError("winner DM failed")

        with patch_sites(('elo_bot.extensions.match.result_store.free_clan', 'elo_bot.extensions.match.dashboards.free_clan', 'elo_bot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free_clan:
            with self.assertRaisesRegex(RuntimeError, "winner DM failed"):
                await fixture.invoke(fixture.context())

        self.assertTrue(fixture.rows[Match][0]["is_autowin"])
        self.assertFalse(fixture.rows[Match][0]["autowin_completed"])
        self.assertEqual(
            free_clan.await_args_list,
            [
                call("#FIRST", expected_thread="321"),
                call("#SECOND", expected_thread="321"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
