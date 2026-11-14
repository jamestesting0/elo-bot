import asyncio
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.append(os.getcwd())

from cogs.leaderboard_commands import LeaderboardCommands, sort_players
from elobot.config.presentation import LB_ENTRIES_PER_PAGE, LB_TYPE_OPTIONS
from elobot.features.seasons.selection import InvalidSeasonSelectionError, interpret_season_string


class _ScanList(list):
    def __init__(self, items):
        super().__init__(items)
        self.scans = 0

    def __iter__(self):
        self.scans += 1
        return super().__iter__()


def _recorded_match(player_id, played_at, won, stars, destruction, district=None):
    districts = {}
    if district is not None:
        districts[district] = SimpleNamespace(stars=stars, destruction=destruction)
    return SimpleNamespace(
        time=played_at,
        players={
            player_id: SimpleNamespace(
                won=won,
                stars=stars,
                destruction=destruction,
                districts=districts,
            )
        },
    )


class TestLeaderboardCommands(unittest.IsolatedAsyncioTestCase):
    async def test_leaderboard_loads_stored_players_when_cache_is_empty(self):
        bot = MagicMock()
        cog = LeaderboardCommands(bot)
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.guild = MagicMock()
        player = MagicMock(user_id="123", elo=1500, highest_elo=1600)
        player_query = MagicMock()
        player_query.count = AsyncMock(return_value=25)
        player_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[player]
        )

        with patch("cogs.leaderboard_commands.construct_leaderboard_string", new_callable=AsyncMock) as \
                mock_construct_leaderboard, patch("cogs.leaderboard_commands.interpret_season_string") as \
                mock_interpret_season, patch("cogs.leaderboard_commands.Season") as mock_season, \
                patch("cogs.leaderboard_commands.Player") as mock_player:
            mock_player.find_all.return_value = player_query
            mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
            mock_interpret_season.return_value = ("All Seasons", 0, float("inf"))
            mock_construct_leaderboard.return_value = "ranked player"

            await cog.leaderboard.callback.func(
                cog, ctx, lb_type="Elo", live=False, season="All Seasons", hidden=False
            )

            for call in mock_player.find_all.call_args_list:
                self.assertFalse(call.kwargs.get("fetch_links", False))
            self.assertEqual(player_query.sort.call_args.args[0], "-elo")
            player_query.sort.return_value.skip.assert_called_once_with(0)
            player_query.sort.return_value.skip.return_value.limit.assert_called_once_with(LB_ENTRIES_PER_PAGE)
            mock_construct_leaderboard.assert_awaited_once_with(
                ctx.guild, [player], "Elo", 0, float("inf"), paged=True
            )
        self.assertEqual(cog._player_cache, [])
        ctx.send.assert_awaited_once()
        self.assertEqual(ctx.send.call_args.kwargs["embed"].description, "ranked player")
        page_button = ctx.send.call_args.kwargs["components"][2].components[1]
        self.assertEqual(page_button.label, "1/3")
        self.assertEqual(page_button.custom_id, "lb_page")
        self.assertFalse(page_button.disabled)
        self.assertFalse(ctx.send.call_args.kwargs["components"][2].components[2].disabled)

    async def test_numeric_season_uses_first_snapshot_for_elo_variants(self):
        for leaderboard_type in ("Elo", "Elo Reversed"):
            with self.subTest(leaderboard_type=leaderboard_type):
                cog = LeaderboardCommands(MagicMock())
                ctx = MagicMock()
                ctx.defer = AsyncMock()
                ctx.send = AsyncMock()
                ctx.guild = MagicMock()
                seasons = [
                    SimpleNamespace(
                        season_number=1,
                        start=100,
                        end=200,
                        players=[SimpleNamespace(player_id="first", elo=1500)],
                    ),
                    SimpleNamespace(season_number=2, start=200, end=300, players=[]),
                ]

                with patch("cogs.leaderboard_commands.Season") as mock_season, \
                        patch("cogs.leaderboard_commands.Player", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)), \
                        patch("cogs.leaderboard_commands.construct_leaderboard_string", new_callable=AsyncMock) as \
                        mock_construct_leaderboard:
                    mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=seasons)
                    mock_construct_leaderboard.return_value = "first-season player"

                    await cog.leaderboard.callback.func(
                        cog, ctx, lb_type=leaderboard_type, live=False, season="1", hidden=False
                    )

                    mock_construct_leaderboard.assert_awaited_once()
                    self.assertEqual(mock_construct_leaderboard.await_args.args[1][0].user_id, "first")
                    page_button = ctx.send.await_args.kwargs["components"][2].components[1]
                    self.assertEqual(page_button.custom_id, "lb_page")
                    self.assertFalse(page_button.disabled)
                self.assertEqual(ctx.send.await_args.kwargs["embed"].description, "first-season player")

    async def test_elo_component_paths_use_selected_season_number(self):
        seasons = [
            SimpleNamespace(
                season_number=3,
                start=300,
                end=400,
                players=[SimpleNamespace(player_id="first", elo=1500)],
            ),
            SimpleNamespace(season_number=7, start=700, end=800, players=[]),
        ]
        for callback_name in ("lb_pagination", "lb_season_select"):
            for leaderboard_type in ("Elo", "Elo Reversed"):
                with self.subTest(callback_name=callback_name, leaderboard_type=leaderboard_type):
                    cog = LeaderboardCommands(MagicMock())
                    ctx = MagicMock()
                    ctx.author.id = "123"
                    ctx.guild = MagicMock()
                    ctx.defer = AsyncMock()
                    ctx.edit = AsyncMock()
                    ctx.edit_origin = AsyncMock()
                    ctx.send = AsyncMock()
                    ctx.custom_id = "lb_right|0"
                    ctx.values = ["3"]
                    ctx.message.interaction.user.id = "123"
                    ctx.message.embeds = [
                        SimpleNamespace(
                            title=f"**{leaderboard_type} Leaderboard**",
                            fields=[SimpleNamespace(value="Season 3")],
                            description="",
                        )
                    ]
                    ctx.message.components = [MagicMock(), MagicMock(), MagicMock()]

                    with patch("cogs.leaderboard_commands.Season") as mock_season, \
                            patch("cogs.leaderboard_commands.Player", side_effect=lambda **kwargs: SimpleNamespace(**kwargs)), \
                            patch("cogs.leaderboard_commands.construct_leaderboard_string", new_callable=AsyncMock) as \
                            mock_construct_leaderboard:
                        mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=seasons)
                        mock_construct_leaderboard.return_value = "first-season player"

                        await getattr(cog, callback_name).callback.func(cog, ctx)

                    mock_construct_leaderboard.assert_awaited_once()
                    self.assertEqual(mock_construct_leaderboard.await_args.args[1][0].user_id, "first")
                    page_button = ctx.message.components[2].components[1]
                    self.assertEqual(page_button.custom_id, "lb_page")
                    self.assertFalse(page_button.disabled)

    async def test_pagination_disables_left_button_on_first_page(self):
        # Given: page two's left button targets the first page
        cog = LeaderboardCommands(MagicMock())
        cog._player_cache = [
            SimpleNamespace(user_id=str(index), elo=1500 - index, highest_elo=1600 - index)
            for index in range(LB_ENTRIES_PER_PAGE * 2)
        ]
        ctx = MagicMock()
        ctx.author.id = "123"
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        ctx.custom_id = "lb_left|0"
        ctx.message.interaction.user.id = "123"
        ctx.message.embeds = [
            SimpleNamespace(
                title="**Elo Leaderboard**",
                fields=[SimpleNamespace(value="All Seasons")],
                description="",
            )
        ]
        ctx.message.components = [MagicMock(), MagicMock(), MagicMock()]
        ctx.edit_origin = AsyncMock()
        ctx.send = AsyncMock()

        # When: the user navigates back to page one
        page_query = MagicMock()
        page_query.count = AsyncMock(return_value=len(cog._player_cache))
        page_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=cog._player_cache[:LB_ENTRIES_PER_PAGE]
        )
        with patch("cogs.leaderboard_commands.Season") as mock_season, \
                patch("cogs.leaderboard_commands.Player") as mock_player:
            mock_player.find_all.return_value = page_query
            mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=[])
            await cog.lb_pagination.callback.func(cog, ctx)
            self.assertEqual(page_query.sort.call_args.args[0], "-elo")
            page_query.sort.return_value.skip.assert_called_once_with(0)
            page_query.sort.return_value.skip.return_value.limit.assert_called_once_with(LB_ENTRIES_PER_PAGE)
            for call in mock_player.find_all.call_args_list:
                self.assertFalse(call.kwargs.get("fetch_links", False))

        # Then: the first page renders and cannot navigate farther left
        left_button = ctx.message.components[2].components[0]
        self.assertTrue(left_button.disabled)
        page_button = ctx.message.components[2].components[1]
        self.assertEqual(page_button.custom_id, "lb_page")
        self.assertFalse(page_button.disabled)
        self.assertEqual(len(ctx.message.embeds[0].description.splitlines()), LB_ENTRIES_PER_PAGE)
        self.assertTrue(ctx.message.embeds[0].description.startswith("🥇"))
        ctx.edit_origin.assert_awaited_once()

    async def test_leaderboard_season_options_preserve_season_numbers(self):
        cog = LeaderboardCommands(MagicMock())
        ctx = MagicMock()
        ctx.defer = AsyncMock()
        ctx.send = AsyncMock()
        ctx.guild = MagicMock()
        player_query = MagicMock()
        player_query.count = AsyncMock(return_value=1)
        player_query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            return_value=[MagicMock(user_id="123", elo=1500, highest_elo=1600)]
        )
        seasons = [
            SimpleNamespace(season_number=3, start=300, end=400, players=[]),
            SimpleNamespace(season_number=7, start=700, end=800, players=[]),
        ]

        with patch("cogs.leaderboard_commands.Player") as mock_player, \
                patch("cogs.leaderboard_commands.Season") as mock_season, \
                patch("cogs.leaderboard_commands.interpret_season_string", return_value=("All Seasons", 0, 999)), \
                patch("cogs.leaderboard_commands.construct_leaderboard_string", new_callable=AsyncMock, \
                      return_value="ranked player"):
            mock_player.find_all.return_value = player_query
            mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=seasons)

            await cog.leaderboard.callback.func(
                cog, ctx, lb_type="Elo", live=False, season="All Seasons", hidden=False
            )

        options = ctx.send.await_args.kwargs["components"][1].components[0].options
        self.assertEqual(
            [(option.label, option.value) for option in options],
            [("Current Season", "Current Season"), ("Season 3", "3"), ("Season 7", "7")],
        )

    async def test_startup_warms_match_rankings(self):
        # Given: one stored player and one numbered season
        cog = LeaderboardCommands(MagicMock())
        player = SimpleNamespace(user_id="1", elo=1500, highest_elo=1600, matches=[])
        seasons = [SimpleNamespace(season_number=3, start=300, end=400)]
        player_query = MagicMock()
        player_query.to_list = AsyncMock(return_value=[player])
        match_types = [
            option.value for option in LB_TYPE_OPTIONS
            if option.value not in {"Elo", "Elo Reversed", "Highest Elo", "Tokens", "Predictions"}
        ]

        with patch("cogs.leaderboard_commands.Player") as mock_player, \
                patch("cogs.leaderboard_commands.Season") as mock_season:
            mock_player.find_all.return_value = player_query
            mock_season.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=seasons)

            # When: the leaderboard extension starts
            await cog.on_start.callback(cog)

            # Then: every match board is ranked for all seasons, the current season, and season 3
            warmed = {(lb_type, season_key) for lb_type, season_key, _start in cog._rankings}
            self.assertFalse(any(lb_type in {"Tokens", "Predictions"} for lb_type, _season in warmed))
            self.assertEqual(
                warmed,
                {
                    (lb_type, season_key)
                    for lb_type in match_types
                    for season_key in ("All Seasons", "Current Season", "3")
                } | {
                    (f"{lb_type}|{tier}", season_key)
                    for lb_type in match_types if lb_type == "Hitrate" or lb_type.endswith(" Hitrate")
                    for tier in ("Average", "Good", "Great", "Excellent")
                    for season_key in ("All Seasons", "Current Season", "3")
                },
            )
            self.assertEqual(cog._rankings[("Addiction", "All Seasons", -1)], [player])
            self.assertEqual(cog._rankings[("Hitrate", "All Seasons", -1)], [])
            mock_player.find_all.assert_called_once_with(fetch_links=True)

            with patch("cogs.leaderboard_commands.sort_players", new_callable=AsyncMock) as mock_sort:
                await cog._ranked_players(cog._player_cache, "Hitrate", "All Seasons", -1, 999)
                mock_sort.assert_not_awaited()

            # A full reload drops the warmed rankings and refills them in the background.
            await cog.refresh_cache()
            self.assertEqual(cog._rankings, {})
            await asyncio.wait_for(cog._ranking_task, timeout=5)
            self.assertEqual(cog._rankings[("Addiction", "All Seasons", -1)], [player])

    async def test_warm_rankings_stops_after_player_cache_is_replaced(self):
        # Given: a warm pass over a cache that a match update replaces after the first season
        cog = LeaderboardCommands(MagicMock())
        cog._player_cache = [SimpleNamespace(user_id="1", matches=[])]
        cog._seasons_cache = [SimpleNamespace(season_number=3, start=300, end=400)]

        async def replace_player_cache(_seconds):
            cog._player_cache = []
            cog._rankings.clear()

        with patch("cogs.leaderboard_commands.asyncio.sleep", side_effect=replace_player_cache):
            # When: startup ranking reaches the next season
            await cog._warm_rankings()

        # Then: rankings built from the replaced cache are not stored
        self.assertEqual(cog._rankings, {})

    async def test_player_update_refreshes_rankings_after_returning(self):
        # Given: an empty ranking cache and one replacement player
        cog = LeaderboardCommands(MagicMock())
        cog._seasons_cache = []
        player = SimpleNamespace(user_id="1", elo=1500, highest_elo=1600, matches=[])

        # When: a recorded match updates that player
        await cog.update_player_cache(player)

        # Then: the caller returns before the rankings are rebuilt
        self.assertEqual(cog._rankings, {})
        await asyncio.wait_for(cog._ranking_task, timeout=5)
        self.assertEqual(cog._rankings[("Addiction", "All Seasons", -1)], [player])
        self.assertEqual(cog._rankings[("Hitrate", "All Seasons", -1)], [])

    async def test_player_update_recomputes_only_that_players_stats(self):
        # Given: three players whose match totals already rank Addiction, Winrate, Hitrate, and a district
        seasons = [SimpleNamespace(season_number=1, start=100, end=200)]
        cog = LeaderboardCommands(MagicMock())
        cog._seasons_cache = seasons
        strong = SimpleNamespace(
            user_id="strong",
            matches=_ScanList([
                _recorded_match("strong", 150, True, 3, 100, "Capital Peak") for _ in range(6)
            ]),
        )
        tied = SimpleNamespace(
            user_id="tied",
            matches=_ScanList([
                _recorded_match("tied", 150, index < 3, 1, 40, "Capital Peak") for index in range(6)
            ]),
        )
        light = SimpleNamespace(
            user_id="light",
            matches=_ScanList([
                _recorded_match("light", 150, False, 0, 0) for _ in range(2)
            ]),
        )
        cog._player_cache = [strong, tied, light]

        async def ranked_ids():
            ids = {}
            for lb_type in ("Addiction", "Winrate", "Hitrate", "Capital Peak Hitrate"):
                for season_key in ("All Seasons", "Current Season", "1"):
                    _, season_start, season_end = interpret_season_string(seasons, season_key)
                    expected = await sort_players(cog._player_cache, lb_type, season_start, season_end)
                    actual = cog._rankings[(lb_type, season_key, season_start)]
                    self.assertEqual(
                        [player.user_id for player in actual],
                        [player.user_id for player in expected],
                    )
                    ids[(lb_type, season_key)] = [player.user_id for player in actual]
            return ids

        # When: the first warm ranks every board from those totals
        await cog._warm_rankings()
        before = await ranked_ids()

        # Then: equal match counts keep cache order, and short histories stay off the rate boards
        self.assertEqual(before[("Addiction", "All Seasons")], ["strong", "tied", "light"])
        self.assertEqual(before[("Winrate", "All Seasons")], ["strong", "tied"])
        self.assertEqual(before[("Hitrate", "1")], ["strong", "tied"])
        self.assertEqual(before[("Capital Peak Hitrate", "Current Season")], [])

        updated = SimpleNamespace(
            user_id="light",
            matches=_ScanList([
                *light.matches,
                *[
                    _recorded_match("light", 250, True, 3, 100, "Capital Peak")
                    for _ in range(6)
                ],
            ]),
        )
        scans = {
            "strong": strong.matches.scans,
            "tied": tied.matches.scans,
            "light": light.matches.scans,
        }
        await cog.update_player_cache(updated)
        await asyncio.wait_for(cog._ranking_task, timeout=5)

        # The changed player is the only history scanned again, and every board follows that one change
        self.assertEqual(strong.matches.scans, scans["strong"])
        self.assertEqual(tied.matches.scans, scans["tied"])
        self.assertEqual(light.matches.scans, scans["light"])
        self.assertEqual(updated.matches.scans, 1)
        after = await ranked_ids()
        self.assertEqual(after[("Addiction", "All Seasons")], ["light", "strong", "tied"])
        self.assertEqual(after[("Addiction", "1")], ["strong", "tied", "light"])
        self.assertEqual(after[("Winrate", "All Seasons")], ["strong", "light", "tied"])
        self.assertEqual(after[("Hitrate", "Current Season")], ["light"])
        self.assertEqual(after[("Capital Peak Hitrate", "Current Season")], ["light"])
        self.assertEqual(after[("Capital Peak Hitrate", "1")], ["strong", "tied"])

    async def test_match_board_waits_until_player_reload_finishes(self):
        # Given: a reload that has not finished reading the new player documents
        cog = LeaderboardCommands(MagicMock())
        old_player = SimpleNamespace(user_id="1", matches=[])
        fresh_player = SimpleNamespace(user_id="1", matches=[])
        cog._player_cache = [old_player]
        cog._seasons_cache = []
        started = asyncio.Event()
        release = asyncio.Event()

        async def load_players():
            started.set()
            await asyncio.wait_for(release.wait(), timeout=5)
            return [fresh_player]

        query = MagicMock()
        query.to_list = load_players
        with patch("cogs.leaderboard_commands.Player") as mock_player:
            mock_player.find_all.return_value = query
            cog.request_player_reload()
            getter = asyncio.create_task(cog.get_players())
            await asyncio.wait_for(started.wait(), timeout=5)
            self.assertFalse(getter.done())

            # When: the database read finishes
            release.set()
            loaded = await asyncio.wait_for(getter, timeout=5)

        # Then: the match board sees the reloaded players, not the stale list
        self.assertIs(loaded[0], fresh_player)
        await asyncio.wait_for(cog._ranking_task, timeout=5)
        self.assertEqual(cog._rankings[("Addiction", "All Seasons", -1)], [fresh_player])

    def test_interpret_season_string_uses_season_number_not_list_position(self):
        seasons = [
            SimpleNamespace(season_number=3, start=300, end=400),
            SimpleNamespace(season_number=7, start=700, end=800),
        ]

        self.assertEqual(interpret_season_string(seasons, "7"), ("Season 7", 700, 800))
        with self.assertRaisesRegex(InvalidSeasonSelectionError, "Season 1 does not exist"):
            interpret_season_string(seasons, "1")


if __name__ == "__main__":
    unittest.main()
