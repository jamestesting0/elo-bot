import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.extensions.match import MatchCommands
from elobot.features.players.data_boundary import PLAYER_DATA_BOUNDARY
from elobot.db import Match
from tests._patch_sites import patch_sites


VALID_RESULT = "0.0\n3.100\n2.20"
INVALID_RESULTS = (
    "3.100\n2.20",
    "4.100\n3.100\n2.20",
    "3.101\n3.100\n2.20",
    "-1.50\n3.100\n2.20",
    "3.-1\n3.100\n2.20",
    "3.100\n3.100\n2.20\n0.0",
    "3.100\n3.100\n2.20 trailing",
)


class TestMatchResultGuards(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = MagicMock()
        self.bot.owner.id = 123456789
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@111>"))
        self.cog = MatchCommands(self.bot)

    @patch.object(MatchCommands, "_process_match_results", new_callable=AsyncMock)
    async def test_result_submit_modal_rejects_invalid_result_for_either_player(
            self, process_results):
        for field in ("player1", "player2"):
            for invalid_result in INVALID_RESULTS:
                with self.subTest(field=field, invalid_result=invalid_result):
                    # Given: one player has a malformed or out-of-range complete result.
                    ctx = MagicMock(spec=ipy.ModalContext)
                    ctx.defer = AsyncMock()
                    ctx.send = AsyncMock()
                    process_results.reset_mock()
                    response = {"player1": VALID_RESULT, "player2": VALID_RESULT}
                    response[field] = invalid_result

                    # When: the helper submits both players' results.
                    await self.cog.result_submit_modal.callback(ctx, **response)

                    # Then: processing never starts.
                    process_results.assert_not_awaited()
                    self.assertIn("invalid", ctx.send.await_args.args[0].lower())

    @patch.object(MatchCommands, "_process_match_results", new_callable=AsyncMock)
    async def test_result_submit_modal_accepts_boundary_values(self, process_results):
        # Given: both players have exactly three bounded results.
        ctx = MagicMock(spec=ipy.ModalContext)
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        response = {"player1": VALID_RESULT, "player2": VALID_RESULT}
        data_generation = PLAYER_DATA_BOUNDARY.generation

        # When: the helper submits the results.
        await self.cog.result_submit_modal.callback(ctx, **response)

        # Then: the validated payload reaches result processing once.
        process_results.assert_awaited_once_with(ctx, response, data_generation=data_generation)

    @patch('elobot.extensions.match.results.record_match', new_callable=AsyncMock)
    @patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))
    async def test_match_submit_rejects_invalid_result_for_either_player(
            self, mock_player, mock_record_match):
        for field in ("player1_result", "player2_result"):
            for invalid_result in INVALID_RESULTS:
                with self.subTest(field=field, invalid_result=invalid_result):
                    # Given: two registered players and one invalid slash-command result.
                    ctx = MagicMock(spec=ipy.SlashContext)
                    ctx.guild = MagicMock()
                    ctx.defer = AsyncMock()
                    ctx.send = AsyncMock()
                    player1 = MagicMock(spec=ipy.User, id=111, mention="<@111>")
                    player2 = MagicMock(spec=ipy.User, id=222, mention="<@222>")
                    mock_player.find_one = AsyncMock(side_effect=[
                        MagicMock(elo=1300, capital_peak_enabled=False),
                        MagicMock(elo=1300, capital_peak_enabled=False),
                    ])
                    mock_record_match.reset_mock()
                    results = {
                        "player1_result": VALID_RESULT.replace("\n", "/"),
                        "player2_result": VALID_RESULT.replace("\n", "/"),
                    }
                    results[field] = invalid_result.replace("\n", "/")

                    # When: staff manually submit the match.
                    await self.cog.match_submit.callback(
                        ctx,
                        player1=player1,
                        player2=player2,
                        district1="Barbarian Camp",
                        district2="Balloon Lagoon",
                        district3="Dragon Cliffs",
                        **results,
                    )

                    # Then: the invalid result never reaches recording.
                    mock_record_match.assert_not_awaited()
                    self.assertIn("invalid", ctx.send.await_args.args[0].lower())

    @patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player'))
    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_resubmit_modal_rejects_invalid_result_before_history_lookup(
            self, mock_match, mock_player):
        for field in ("player1", "player2"):
            for invalid_result in INVALID_RESULTS:
                with self.subTest(field=field, invalid_result=invalid_result):
                    # Given: a finished match and one invalid corrected result.
                    ctx = MagicMock(spec=ipy.ModalContext)
                    ctx.custom_id = "resubmit_modal|token"
                    ctx.defer = AsyncMock()
                    ctx.send = AsyncMock()
                    match = MagicMock(spec=Match)
                    match.result_operation = None
                    match.autowin_winner_id = None
                    match.autowin_completed = False
                    match.participants = [MagicMock(user_id="111"), MagicMock(user_id="222")]
                    match.final_districts = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
                    mock_match.find_one = AsyncMock(return_value=match)
                    mock_player.find_one = AsyncMock(return_value=None)
                    response = {"player1": VALID_RESULT, "player2": VALID_RESULT}
                    response[field] = invalid_result

                    # When: staff submit the correction.
                    await self.cog.resubmit_modal.callback(ctx, **response)

                    # Then: no player history is loaded or mutated.
                    mock_player.find_one.assert_not_awaited()
                    self.assertIn("invalid", ctx.send.await_args.args[0].lower())

    @patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.features.base_store.match_adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match'))
    async def test_result_store_modal_rejects_out_of_range_entry(self, mock_match):
        for invalid_entry in ("4.50", "3.101", "-1.50", "3.-1"):
            with self.subTest(invalid_entry=invalid_entry):
                # Given: an incremental result modal with one out-of-range entry.
                ctx = MagicMock(spec=ipy.ModalContext)
                ctx.custom_id = "store_modal|token|0"
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                match = MagicMock(spec=Match)
                match.result_operation = None
                match.autowin_winner_id = None
                match.autowin_completed = False
                match.id = "MATCH"
                match.is_war = True
                match.old_msgs = [123, None]
                match.times = [100, None]
                match.hosts = [None, None]
                match.host_clans = [None, None]
                ctx.message = MagicMock(id=123)
                match.participants = [
                    MagicMock(user_id="111", result=[None, None, None]),
                    MagicMock(user_id="222", result=[None, None, None]),
                ]
                write = match.get_pymongo_collection.return_value.update_one = AsyncMock()
                mock_match.find_one = AsyncMock(return_value=match)

                # When: the helper stages the entry.
                await self.cog.result_store_modal.callback.func(
                    self.cog,
                    ctx,
                    result1=invalid_entry,
                    result2="",
                    result3="",
                )

                # Then: no participant result is persisted.
                write.assert_not_awaited()
                self.assertIn("incorrect", ctx.send.await_args.args[0].lower())

    def test_match_result_store_options_enforce_numeric_bounds(self):
        # Given: the single-district slash command options.
        options = {
            str(option.name): option
            for option in MatchCommands.match_result_store.options
        }

        # When/Then: Discord constrains both numeric inputs to their domain ranges.
        self.assertEqual((options["stars"].min_value, options["stars"].max_value), (0, 3))
        self.assertEqual(
            (options["destruction"].min_value, options["destruction"].max_value),
            (0, 100),
        )


if __name__ == "__main__":
    unittest.main()
