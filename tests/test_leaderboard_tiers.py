import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from elobot.extensions.leaderboard import LeaderboardCommands, sort_players
from elobot.config.presentation import LB_ENTRIES_PER_PAGE
from elobot.db.player_models import MatchDistrictStats


def make_player(user_id, attacks, played_at=150):
    matches = []
    for index, (quality, stars, destruction) in enumerate(attacks):
        district = "Capital Peak" if index % 2 == 0 else "Wizard Valley"
        stats = MatchDistrictStats(stars=stars, destruction=destruction, **(
            {} if quality == "legacy" else {"base_quality": quality}
        ))
        matches.append(SimpleNamespace(
            time=played_at,
            players={user_id: SimpleNamespace(
                won=True, stars=stars * 3, destruction=destruction * 3, districts={district: stats}
            )},
        ))
    return SimpleNamespace(user_id=user_id, matches=matches, elo=1500, highest_elo=1500)


def context():
    ctx = MagicMock()
    ctx.author.id = "owner"
    ctx.message.interaction.user.id = "owner"
    ctx.guild.fetch_member = AsyncMock(return_value=None)
    ctx.defer = AsyncMock()
    ctx.send = AsyncMock()
    ctx.edit = AsyncMock()
    ctx.edit_origin = AsyncMock()
    return ctx


class TestLeaderboardTiers(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = LeaderboardCommands(MagicMock())
        self.cog._seasons_cache = [SimpleNamespace(season_number=1, start=100, end=200)]
        clock = self.enterContext(patch("elobot.features.seasons.selection.datetime"))
        clock.now.return_value.timestamp.return_value = 1000

    async def test_mixed_qualities_threshold_ranking_and_season_match_uncached(self):
        strong = make_player("strong", [("Good", 3, 80)] * 5 + [(None, 3, 100)])
        weak = make_player("weak", [("Good", 2, 100)] * 5 + [("Excellent", 3, 100)])
        short = make_player("short", [("Good", 3, 100)] * 4 + [("legacy", 3, 100)])
        old = make_player("old", [("Good", 3, 100)] * 5, played_at=50)
        self.cog._player_cache = [weak, short, strong, old]
        await self.cog._warm_rankings()

        for season, start, end, expected in (
            ("1", 100, 200, ["strong", "weak"]),
            ("All Seasons", -1, 1000, ["old", "strong", "weak"]),
        ):
            with self.subTest(season=season):
                rows, size, _ = await self.cog._board_rows(
                    "Hitrate", season, start, end, 0, self.cog._seasons_cache, "Good"
                )
                self.assertEqual([p.user_id for p in rows], expected)
                self.assertEqual(size, len(expected))
                uncached = await sort_players(list(self.cog._player_cache), "Hitrate|Good", start, end)
                self.assertEqual([p.user_id for p in uncached], expected)

        overall, _, _ = await self.cog._board_rows(
            "Hitrate", "1", 100, 200, 0, self.cog._seasons_cache
        )
        self.assertEqual({p.user_id for p in overall}, {"strong", "weak", "short"})
        district, _, _ = await self.cog._board_rows(
            "Capital Peak Hitrate", "1", 100, 200, 0, self.cog._seasons_cache, "Good"
        )
        self.assertEqual(district, [])

    async def test_each_district_entry_counts_toward_its_quality(self):
        player = make_player("multi", [("Good", 3, 90)] * 2)
        for match in player.matches:
            match.players["multi"].districts["extra"] = SimpleNamespace(
                base_quality="Good", stars=2, destruction=60
            )
        player.matches[0].players["multi"].districts["third"] = SimpleNamespace(
            base_quality="Good", stars=1, destruction=30
        )
        self.cog._player_cache = [player]
        ctx = context()
        await self.cog.leaderboard.callback.func(self.cog, ctx, lb_type="Hitrate", season="1")
        payload = ctx.send.await_args.kwargs
        ctx.message.embeds = [payload["embed"]]
        ctx.message.components = payload["components"]
        ctx.values = ["Good"]
        await self.cog.lb_tier_select.callback.func(self.cog, ctx)
        self.assertIn("`5`", ctx.message.embeds[0].description)
        self.assertIn("/multi>", ctx.message.embeds[0].description)
        rows, size, _ = await self.cog._board_rows("Hitrate", "1", 100, 200, 0, self.cog._seasons_cache, "Good")
        self.assertEqual([row.user_id for row in rows], ["multi"])
        self.assertEqual(size, 1)

    async def test_every_quality_filters_district_attacks_independently(self):
        qualities = ("Average", "Good", "Great", "Excellent")
        self.cog._player_cache = []
        for quality in qualities:
            player = make_player(quality, [(quality, 2, 70)] * 5)
            for match in player.matches:
                stats = next(iter(match.players[quality].districts.values()))
                match.players[quality].districts = {"Capital Peak": stats}
            self.cog._player_cache.append(player)
        await self.cog._warm_rankings()
        for quality in qualities:
            with self.subTest(quality=quality):
                rows, _, _ = await self.cog._board_rows(
                    "Capital Peak Hitrate", "1", 100, 200, 0, self.cog._seasons_cache, quality
                )
                self.assertEqual([row.user_id for row in rows], [quality])

    async def test_live_hitrate_keeps_selected_tier_after_refresh(self):
        self.cog._player_cache = [make_player("player", [("Good", 3, 90)] * 5)]
        ctx = context()
        ctx.message.edit = AsyncMock()
        await self.cog.leaderboard.callback.func(self.cog, ctx, lb_type="Hitrate", season="1", live=True)
        payload = ctx.send.await_args.kwargs
        ctx.message.embeds = [payload["embed"]]
        ctx.message.components = payload["components"]
        ctx.values = ["Good"]
        await self.cog.lb_tier_select.callback.func(self.cog, ctx)
        await self.cog.lb_refresh.callback.func(self.cog, ctx)
        self.assertIn("`5`", ctx.message.embeds[0].description)
        self.assertEqual([option.value for option in ctx.message.components[-1].components[0].options
                          if option.default], ["Good"])

    async def test_selectors_pagination_and_empty_quality_keep_state(self):
        self.cog._player_cache = [
            make_player(str(index), [("Good", 3, 100 - index)] * 6)
            for index in range(LB_ENTRIES_PER_PAGE + 1)
        ]
        ctx = context()
        await self.cog.leaderboard.callback.func(self.cog, ctx, lb_type="Hitrate", season="1")
        payload = ctx.send.await_args.kwargs
        ctx.message.embeds = [payload["embed"]]
        ctx.message.components = payload["components"]
        menu = ctx.message.components[-2].components[0]
        self.assertEqual([option.value for option in menu.options],
                         ["Overall", "Average", "Good", "Great", "Excellent"])

        ctx.values = ["Good"]
        await self.cog.lb_tier_select.callback.func(self.cog, ctx)
        self.assertIn("`6`", ctx.message.embeds[0].description)
        ctx.custom_id = f"lb_right|{LB_ENTRIES_PER_PAGE}"
        await self.cog.lb_pagination.callback.func(self.cog, ctx)
        self.assertEqual(ctx.message.components[-1].components[1].label, "2/2")
        self.assertIn("/10>", ctx.message.embeds[0].description)
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="1")
        self.assertIn("/0>", ctx.message.embeds[0].description)

        ctx.values = ["All Seasons"]
        await self.cog.lb_season_select.callback.func(self.cog, ctx)
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "All Seasons")
        self.assertEqual([o.value for o in ctx.message.components[-2].components[0].options if o.default], ["Good"])

        ctx.values = ["Winrate"]
        await self.cog.lb_type_select.callback.func(self.cog, ctx)
        ctx.message.components = ctx.edit.await_args.kwargs["components"]
        ctx.values = ["Hitrate"]
        await self.cog.lb_type_select.callback.func(self.cog, ctx)
        ctx.message.components = ctx.edit.await_args.kwargs["components"]
        self.assertEqual([o.value for o in ctx.message.components[-2].components[0].options if o.default], ["Good"])

        ctx.values = ["Excellent"]
        await self.cog.lb_tier_select.callback.func(self.cog, ctx)
        self.assertNotIn("/0>", ctx.message.embeds[0].description)
        self.assertTrue(ctx.message.components[-1].components[-1].disabled)
        self.assertEqual([o.value for o in ctx.message.components[-2].components[0].options if o.default],
                         ["Excellent"])
        ctx.values = ["1"]
        await self.cog.lb_season_select.callback.func(self.cog, ctx)
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "Season 1")
        self.assertEqual([o.value for o in ctx.message.components[-2].components[0].options if o.default],
                         ["Excellent"])
        ctx.values = ["Good"]
        await self.cog.lb_tier_select.callback.func(self.cog, ctx)
        self.assertIn("/0>", ctx.message.embeds[0].description)
