import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from elobot.extensions.leaderboard import construct_leaderboard_string, sort_players


def make_recorded_match(timestamp: int, player_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        time=timestamp,
        players={player_id: SimpleNamespace(districts={})},
    )


class TestAddictionLeaderboard(unittest.IsolatedAsyncioTestCase):
    async def test_addiction_sorts_all_seasons_and_includes_zero_match_players(self):
        # Given: players with three, one, and zero matches inside the all-season bounds
        players = [
            SimpleNamespace(
                user_id="three",
                matches=[make_recorded_match(timestamp, "three") for timestamp in (10, 20, 30)],
            ),
            SimpleNamespace(user_id="one", matches=[make_recorded_match(40, "one")]),
            SimpleNamespace(
                user_id="zero",
                matches=[make_recorded_match(timestamp, "zero") for timestamp in (-1, 1000)],
            ),
        ]

        # When: the Addiction leaderboard is sorted across all seasons
        players_sorted = await sort_players(players, "Addiction", -1, 1000)

        # Then: match count determines descending order and zero-match players remain
        self.assertEqual([player.user_id for player in players_sorted], ["three", "one", "zero"])

    async def test_addiction_sorts_by_selected_season_match_count(self):
        # Given: lifetime match totals that differ from the selected-season totals
        players = [
            SimpleNamespace(
                user_id="lifetime-heavy",
                matches=[make_recorded_match(timestamp, "lifetime-heavy") for timestamp in (10, 20, 150)],
            ),
            SimpleNamespace(
                user_id="season-heavy",
                matches=[make_recorded_match(timestamp, "season-heavy") for timestamp in (120, 130)],
            ),
            SimpleNamespace(
                user_id="zero",
                matches=[make_recorded_match(timestamp, "zero") for timestamp in (100, 200)],
            ),
        ]

        # When: the Addiction leaderboard is sorted for the selected season
        players_sorted = await sort_players(players, "Addiction", 100, 200)

        # Then: only matches strictly inside that season determine descending order
        self.assertEqual(
            [player.user_id for player in players_sorted],
            ["season-heavy", "lifetime-heavy", "zero"],
        )

    async def test_addiction_renders_selected_season_match_count(self):
        # Given: one player with only one match strictly inside the selected season
        player = SimpleNamespace(
            user_id="one",
            matches=[
                make_recorded_match(timestamp, "one")
                for timestamp in (99, 100, 150, 200, 201)
            ],
        )
        guild = SimpleNamespace(fetch_member=AsyncMock(
            return_value=SimpleNamespace(id="one", display_name="Player One"),
        ))

        # When: the Addiction leaderboard entry is rendered for that season
        leaderboard = await construct_leaderboard_string(guild, [player], "Addiction", 100, 200)

        # Then: the entry shows only the in-season match count
        self.assertEqual(leaderboard, "🥇 **[Player One](<https://discordapp.com/users/one>)** `1`\n")


if __name__ == "__main__":
    unittest.main()
