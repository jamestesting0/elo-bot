import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs.leaderboard_commands import LeaderboardCommands
from common.constants import LB_ENTRIES_PER_PAGE, LB_TYPE_OPTIONS
from common.finals.leaderboards import final_board_rows
from common.utils import interpret_season_string


def player(user_id, times=()):
    return SimpleNamespace(
        user_id=user_id, elo=1500, highest_elo=3000,
        matches=[
            SimpleNamespace(time=time, players={user_id: SimpleNamespace(
                won=True, stars=9, destruction=300,
                districts={"Capital Peak": SimpleNamespace(stars=3, destruction=100)},
            )})
            for time in times
        ],
    )


def context():
    ctx = MagicMock()
    ctx.author.id = "owner"
    ctx.message.interaction.user.id = "owner"
    ctx.guild.fetch_member = AsyncMock(return_value=None)
    for method in ("defer", "send", "edit", "edit_origin"):
        setattr(ctx, method, AsyncMock())
    ctx.message.edit = AsyncMock()
    return ctx


def selected(components, row=1):
    return [option.value for option in components[row].components[0].options if option.default]


class TestLeaderboardScopes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.seasons = [
            SimpleNamespace(season_number=3, start=0, end=100, players=[]),
            SimpleNamespace(season_number=7, start=100, end=200, players=[]),
            SimpleNamespace(season_number=9, start=200, end=300, players=[]),
        ]
        self.players = [player("old", [150] * 6), player("current", [350] * 5), player("zero")]
        self.cog = LeaderboardCommands(MagicMock())
        self.cog._player_cache = self.players
        self.cog._seasons_cache = self.seasons
        clock = self.enterContext(patch("common.utils.datetime"))
        clock.now.return_value.timestamp.return_value = 1000
        self.player_model = self.enterContext(patch("cogs.leaderboard_commands.Player"))
        self.query = self.player_model.find_all.return_value
        self.query.count = AsyncMock(side_effect=lambda: len(self.players))
        self.query.to_list = AsyncMock(side_effect=lambda: self.players)
        self.query.sort.return_value.skip.return_value.limit.return_value.to_list = AsyncMock(
            side_effect=lambda: self.players[:LB_ENTRIES_PER_PAGE]
        )

    async def invoke(self, lb_type, **kwargs):
        ctx = context()
        await self.cog.leaderboard.callback.func(self.cog, ctx, lb_type=lb_type, **kwargs)
        return ctx

    def attach(self, ctx):
        payload = ctx.send.await_args.kwargs
        ctx.message.embeds = [payload["embed"]]
        ctx.message.components = payload["components"]

    def test_slash_and_dropdown_share_requested_order(self):
        expected = [
            "Elo", "Elo Reversed", "Highest Elo", "Addiction", "Winrate", "Hitrate", "Tokens", "Predictions",
            "Capital Peak Hitrate", "Wizard Valley Hitrate", "Barbarian Camp Hitrate", "Balloon Lagoon Hitrate",
            "Builder's Workshop Hitrate", "Dragon Cliffs Hitrate", "Golem Quarry Hitrate",
            "Skeleton Park Hitrate", "Goblin Mines Hitrate",
        ]
        option = next(option for option in LeaderboardCommands.leaderboard.options if str(option.name) == "lb_type")
        self.assertEqual([choice.value for choice in option.choices], expected)
        self.assertEqual([option.value for option in LB_TYPE_OPTIONS], expected)

    async def test_elo_defaults_and_all_seasons_normalize_to_current(self):
        for lb_type in ("Elo", "Elo Reversed"):
            for kwargs in ({}, {"season": "All Seasons"}):
                with self.subTest(lb_type=lb_type, kwargs=kwargs):
                    ctx = await self.invoke(lb_type, **kwargs)
                    payload = ctx.send.await_args.kwargs
                    self.assertEqual(payload["embed"].fields[0].value, "Current Season")
                    self.assertEqual(selected(payload["components"]), ["Current Season"])
                    self.assertNotIn("All Seasons", [
                        option.value for option in payload["components"][1].components[0].options
                    ])
                    self.assertEqual(self.query.sort.call_args.args[0], "-elo" if lb_type == "Elo" else "+elo")

    async def test_highest_elo_restricts_every_input_without_resetting_lifetime_value(self):
        for season in (None, "All Seasons", "7", "Current Season"):
            with self.subTest(season=season):
                ctx = await self.invoke("Highest Elo", season=season)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, "Current Season")
                self.assertEqual(len(payload["components"]), 2)
                self.assertIn("`3000`", payload["embed"].description)
                self.assertEqual(self.query.sort.call_args.args[0], "-highest_elo")

    async def test_match_defaults_use_current_eligible_entries(self):
        for lb_type in ("Addiction", "Winrate", "Hitrate", "Capital Peak Hitrate"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke(lb_type)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, "Current Season")
                self.assertEqual(selected(payload["components"]), ["Current Season"])
                self.assertIn("/current>", payload["embed"].description.splitlines()[0])
                if lb_type != "Addiction":
                    self.assertNotIn("/old>", payload["embed"].description)

    async def test_match_defaults_fall_back_when_current_has_no_eligible_entries(self):
        self.players[1].matches = self.players[1].matches[:4]
        for lb_type in ("Winrate", "Hitrate", "Capital Peak Hitrate"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke(lb_type)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, "All Seasons")
                self.assertEqual(selected(payload["components"]), ["All Seasons"])
                self.assertIn("/old>", payload["embed"].description)
                self.assertNotIn("/current>", payload["embed"].description)

    async def test_explicit_empty_match_scopes_do_not_fall_back(self):
        self.players[1].matches = []
        for lb_type in ("Winrate", "Hitrate", "Capital Peak Hitrate"):
            for season in ("Current Season", "9"):
                with self.subTest(lb_type=lb_type, season=season):
                    ctx = await self.invoke(lb_type, season=season)
                    if "Hitrate" in lb_type:
                        self.assertEqual(ctx.send.await_args.kwargs["embed"].fields[0].value,
                                         "Current Season" if season == "Current Season" else "Season 9")
                    else:
                        self.assertNotIn("embed", ctx.send.await_args.kwargs)
                    ctx.send.assert_awaited_once()

    async def test_addiction_zero_current_activity_triggers_default_fallback(self):
        self.players[1].matches = []
        ctx = await self.invoke("Addiction")
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "All Seasons")
        self.assertEqual(selected(payload["components"]), ["All Seasons"])
        self.assertIn("`6`", payload["embed"].description)
        self.assertIn("/zero>", payload["embed"].description)

    async def test_addiction_explicit_empty_scopes_keep_zero_players(self):
        self.players[1].matches = []
        for season, scope in (("Current Season", "Current Season"), ("9", "Season 9")):
            with self.subTest(season=season):
                ctx = await self.invoke("Addiction", season=season)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, scope)
                self.assertEqual(selected(payload["components"]), [season])
                self.assertEqual(payload["embed"].description.count("`0`"), len(self.players))

    async def test_all_zero_addiction_default_still_displays_players(self):
        self.cog._player_cache = [player("zero")]
        ctx = await self.invoke("Addiction")
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "All Seasons")
        self.assertIn("`0`", payload["embed"].description)

    async def test_type_switch_uses_default_instead_of_previous_archive(self):
        for lb_type in ("Elo", "Elo Reversed", "Highest Elo", "Addiction", "Winrate", "Hitrate"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke("Addiction", season="7")
                self.attach(ctx)
                ctx.values = [lb_type]
                await self.cog.lb_type_select.callback.func(self.cog, ctx)
                payload = ctx.edit.await_args.kwargs
                self.assertEqual(payload["embeds"][0].fields[0].value, "Current Season")
                self.assertEqual(selected(payload["components"], 0), [lb_type])
                if lb_type != "Highest Elo":
                    self.assertEqual(selected(payload["components"]), ["Current Season"])

    async def test_type_switch_uses_empty_default_fallback(self):
        self.players[1].matches = []
        for lb_type in ("Addiction", "Winrate", "Hitrate", "Capital Peak Hitrate"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke("Elo", season="Current Season")
                self.attach(ctx)
                ctx.values = [lb_type]
                await self.cog.lb_type_select.callback.func(self.cog, ctx)
                payload = ctx.edit.await_args.kwargs
                self.assertEqual(payload["embeds"][0].fields[0].value, "All Seasons")
                self.assertEqual(selected(payload["components"]), ["All Seasons"])

    async def test_explicit_empty_dropdown_keeps_previous_board_without_fallback(self):
        ctx = await self.invoke("Winrate", season="7")
        self.attach(ctx)
        before = ctx.message.embeds[0].description
        ctx.values = ["9"]
        await self.cog.lb_season_select.callback.func(self.cog, ctx)
        ctx.edit.assert_not_awaited()
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "Season 7")
        self.assertEqual(ctx.message.embeds[0].description, before)
        self.assertEqual(selected(ctx.message.components), ["7"])

    async def test_no_archives_support_current_and_match_defaults(self):
        self.cog._seasons_cache = []
        for lb_type in ("Elo", "Highest Elo", "Addiction", "Winrate"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke(lb_type)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, "Current Season")
        self.assertEqual(interpret_season_string([], "Current Season"), ("Current Season", -1, 1000))

    async def test_legacy_elo_scope_is_normalized_by_arrows_modal_and_refresh(self):
        for callback in ("lb_pagination", "lb_page_modal", "lb_refresh"):
            with self.subTest(callback=callback):
                ctx = await self.invoke("Elo")
                self.attach(ctx)
                ctx.message.embeds[0].fields[0].value = "All Seasons"
                ctx.custom_id = "lb_right|0"
                kwargs = {"page": "1"} if callback == "lb_page_modal" else {}
                await getattr(self.cog, callback).callback.func(self.cog, ctx, **kwargs)
                self.assertEqual(ctx.message.embeds[0].fields[0].value, "Current Season")
                self.assertEqual(selected(ctx.message.components), ["Current Season"])

    async def test_live_elo_refresh_has_no_season_menu(self):
        for lb_type in ("Elo", "Elo Reversed", "Highest Elo"):
            with self.subTest(lb_type=lb_type):
                ctx = await self.invoke(lb_type, live=True)
                ctx.message.embeds = [ctx.send.await_args.kwargs["embed"]]
                ctx.message.components = [ipy.ActionRow(ctx.send.await_args.kwargs["components"])]
                await self.cog.lb_refresh.callback.func(self.cog, ctx)
                self.assertEqual(ctx.message.embeds[0].fields[0].value, "Current Season")
                ctx.message.edit.assert_awaited_once()

    async def test_autocomplete_respects_type_restrictions_and_no_archives(self):
        from cogs.season_commands import SeasonCommands

        season_cog = SeasonCommands(MagicMock())
        with patch("cogs.season_commands.Season") as model:
            for seasons in (self.seasons, []):
                model.find_all.return_value.sort.return_value.to_list = AsyncMock(return_value=seasons)
                for lb_type in ("Elo", "Elo Reversed", "Highest Elo", "Tokens", "Addiction", "Predictions", None):
                    with self.subTest(lb_type=lb_type, seasons=bool(seasons)):
                        ctx = SimpleNamespace(
                            command=SimpleNamespace(name="leaderboard"), input_text="", send=AsyncMock(),
                            kwargs={} if lb_type is None else {"lb_type": lb_type},
                        )
                        await season_cog.season_autocomplete.callback(season_cog, ctx)
                        values = [choice["value"] for choice in ctx.send.await_args.args[0]]
                        self.assertIn("Current Season", values)
                        if lb_type in ("Highest Elo", "Tokens"):
                            self.assertEqual(values, ["Current Season"])
                        elif lb_type in ("Elo", "Elo Reversed", None):
                            self.assertNotIn("All Seasons", values)
                        else:
                            self.assertIn("All Seasons", values)


class TestFinalLeaderboardScopes(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cog = LeaderboardCommands(MagicMock())
        self.cog._seasons_cache = [
            SimpleNamespace(season_number=3, start=0, end=100),
            SimpleNamespace(season_number=7, start=100, end=200),
            SimpleNamespace(season_number=9, start=200, end=300),
        ]
        self.finals = [
            SimpleNamespace(id=f"final-{number}", season_number=number, active=number == 7,
                            schema_version=2, season_id=f"archive-{number}")
            for number in (3, 7, 9)
        ]
        self.predictions = []
        final_model = self.enterContext(patch("common.finals.leaderboards.EloFinal"))
        self.enterContext(patch("common.finals.lifecycle.EloFinal", final_model))
        final_model.find_all.return_value.to_list = AsyncMock(side_effect=lambda: self.finals)
        final_model.find_all.return_value.sort.return_value.first_or_none = AsyncMock(
            side_effect=lambda: self.finals[-1] if self.finals else None
        )
        final_model.find_one = AsyncMock(side_effect=lambda query: next(
            (final for final in self.finals if all(getattr(final, key) == value for key, value in query.items())), None
        ))
        predictions_model = self.enterContext(patch("common.finals.leaderboards.FinalPrediction"))

        def find_predictions(query):
            final_ids = query["final_id"]
            ids = final_ids["$in"] if isinstance(final_ids, dict) else [final_ids]
            return SimpleNamespace(to_list=AsyncMock(return_value=[
                prediction for prediction in self.predictions
                if prediction.final_id in ids and prediction.submitted_at is not None
            ]))

        predictions_model.find.side_effect = find_predictions
        self.wallets = [SimpleNamespace(id="wallet", user_id="token-user", initialized=True, balance=8765)]
        players = self.enterContext(patch("common.finals.leaderboards.Player"))
        players.find_all.return_value.to_list = AsyncMock(side_effect=lambda: [
            SimpleNamespace(user_id=wallet.user_id) for wallet in self.wallets
        ])
        wallets = self.enterContext(patch("common.finals.leaderboards.TokenWallet"))
        wallets.find.return_value.to_list = AsyncMock(side_effect=lambda: self.wallets)
        ledger = self.enterContext(patch("common.finals.leaderboards.TokenLedgerEntry"))
        ledger.find.return_value.to_list = AsyncMock(side_effect=lambda: [
            SimpleNamespace(operation_key=f"grant:{wallet.id}") for wallet in self.wallets
        ])
        self.ensure_wallet = self.enterContext(patch(
            "common.finals.leaderboards.ensure_wallet", new_callable=AsyncMock
        ))

    def prediction(self, season, user, points, eligible, submitted_at=1):
        self.predictions.append(SimpleNamespace(
            final_id=f"final-{season}", user_id=user, points=points, eligible=eligible, submitted_at=submitted_at,
        ))

    async def invoke(self, lb_type="Predictions", **kwargs):
        ctx = context()
        await self.cog.leaderboard.callback.func(self.cog, ctx, lb_type=lb_type, **kwargs)
        return ctx

    def attach(self, ctx):
        payload = ctx.send.await_args.kwargs
        ctx.message.embeds = [payload["embed"]]
        ctx.message.components = payload["components"]

    async def test_all_seasons_sums_every_final_by_player_and_preserves_points_ties(self):
        self.prediction(3, "a", 2, 2)
        self.prediction(7, "a", 6, 100)
        self.prediction(3, "b", 5, 5)
        self.prediction(7, "b", 3, 3)
        self.prediction(9, "c", 4, 4)
        self.prediction(7, "zero", 0, 0)
        self.prediction(7, "draft", 999, 999, submitted_at=None)
        self.prediction(99, "orphan", 999, 999)
        rows = await final_board_rows("Predictions", "All Seasons")
        self.assertEqual(
            [(row.user_id, row.value, row.eligible, row.rank, row.scope) for row in rows],
            [("a", 8, 102, 1, "All Seasons"), ("b", 8, 8, 2, "All Seasons"),
             ("c", 4, 4, 3, "All Seasons"), ("zero", 0, 0, 4, "All Seasons")],
        )

    async def test_default_uses_active_final_not_ranked_current_season(self):
        self.prediction(7, "active", 2, 3)
        self.prediction(9, "latest", 9, 9)
        ctx = await self.invoke()
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "Season 7 Final")
        self.assertEqual(selected(payload["components"]), ["7"])
        self.assertIn("`2/3`", payload["embed"].description)
        self.assertNotIn("/latest>", payload["embed"].description)

    async def test_default_empty_final_falls_back_to_aggregate_not_latest_final(self):
        self.prediction(7, "draft", 5, 5, submitted_at=None)
        self.prediction(3, "a", 2, 3)
        self.prediction(9, "a", 3, 4)
        ctx = await self.invoke()
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "All Seasons")
        self.assertEqual(selected(payload["components"]), ["All Seasons"])
        self.assertIn("`5/7`", payload["embed"].description)

    async def test_submitted_zero_score_is_not_an_empty_final(self):
        self.prediction(7, "zero", 0, 0)
        self.prediction(3, "old", 10, 10)
        ctx = await self.invoke()
        self.assertEqual(ctx.send.await_args.kwargs["embed"].fields[0].value, "Season 7 Final")
        self.assertIn("`0/0`", ctx.send.await_args.kwargs["embed"].description)

    async def test_explicit_empty_final_scopes_never_fall_back(self):
        self.prediction(3, "old", 10, 10)
        for season in ("Current Season", "9"):
            with self.subTest(season=season):
                ctx = await self.invoke(season=season)
                self.assertNotIn("embed", ctx.send.await_args.kwargs)

    async def test_explicit_numbered_final_stays_specific(self):
        self.prediction(3, "a", 2, 3)
        self.prediction(7, "a", 8, 8)
        ctx = await self.invoke(season="3")
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "Season 3 Final")
        self.assertEqual(selected(payload["components"]), ["3"])
        self.assertIn("`2/3`", payload["embed"].description)
        self.assertNotIn("`10/11`", payload["embed"].description)

    async def test_predictions_type_switch_uses_active_final_and_empty_fallback(self):
        self.prediction(3, "old", 2, 3)
        for has_current in (False, True):
            with self.subTest(has_current=has_current):
                if has_current:
                    self.prediction(7, "current", 1, 2)
                ctx = await self.invoke("Tokens", season="3")
                self.attach(ctx)
                ctx.values = ["Predictions"]
                await self.cog.lb_type_select.callback.func(self.cog, ctx)
                payload = ctx.edit.await_args.kwargs
                self.assertEqual(payload["embeds"][0].fields[0].value,
                                 "Season 7 Final" if has_current else "All Seasons")
                self.assertEqual(selected(payload["components"]),
                                 ["7" if has_current else "All Seasons"])

    async def test_aggregate_arrows_modal_and_refresh_keep_totals_scope_and_page(self):
        for index in range(LB_ENTRIES_PER_PAGE + 2):
            self.prediction(3, str(index), 20 - index, 20)
            self.prediction(7, str(index), 1, 1)
        ctx = await self.invoke(season="All Seasons")
        self.attach(ctx)
        ctx.custom_id = f"lb_right|{LB_ENTRIES_PER_PAGE}"
        await self.cog.lb_pagination.callback.func(self.cog, ctx)
        second_page = ctx.message.embeds[0].description
        self.assertIn("`11/21`", second_page)
        self.assertEqual(ctx.message.components[-1].components[1].label, "2/2")
        await self.cog.lb_refresh.callback.func(self.cog, ctx)
        self.assertIn(second_page, ctx.message.embeds[0].description)
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "All Seasons")
        self.assertEqual(selected(ctx.message.components), ["All Seasons"])
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="1")
        self.assertIn("`21/21`", ctx.message.embeds[0].description)
        self.assertEqual(ctx.message.components[-1].components[1].label, "1/2")

    async def test_tokens_restrict_input_and_keep_lifetime_balance(self):
        for season in (None, "All Seasons", "3", "Current Season"):
            with self.subTest(season=season):
                ctx = await self.invoke("Tokens", season=season)
                payload = ctx.send.await_args.kwargs
                self.assertEqual(payload["embed"].fields[0].value, "Current Season")
                self.assertEqual(len(payload["components"]), 2)
                self.assertIn("`8,765`", payload["embed"].description)
        self.ensure_wallet.assert_not_awaited()

    async def test_tokens_callbacks_and_refresh_relabel_legacy_scope(self):
        for callback in ("lb_pagination", "lb_page_modal", "lb_refresh", "lb_type_select", "lb_season_select"):
            with self.subTest(callback=callback):
                ctx = await self.invoke("Tokens")
                self.attach(ctx)
                ctx.message.embeds[0].fields[0].value = "Lifetime"
                ctx.custom_id = "lb_right|0"
                ctx.values = ["Tokens"] if callback == "lb_type_select" else ["3"]
                kwargs = {"page": "1"} if callback == "lb_page_modal" else {}
                await getattr(self.cog, callback).callback.func(self.cog, ctx, **kwargs)
                self.assertEqual(ctx.message.embeds[0].fields[0].value, "Current Season")
                self.assertIn("8,765", ctx.message.embeds[0].description)

    async def test_tokens_second_page_and_refresh_keep_current_scope_and_balance(self):
        self.wallets = [
            SimpleNamespace(id=str(index), user_id=str(index), initialized=True, balance=20000 - index)
            for index in range(LB_ENTRIES_PER_PAGE + 2)
        ]
        ctx = await self.invoke("Tokens")
        self.attach(ctx)
        ctx.custom_id = f"lb_right|{LB_ENTRIES_PER_PAGE}"
        await self.cog.lb_pagination.callback.func(self.cog, ctx)
        second_page = ctx.message.embeds[0].description
        self.assertIn("`19,990`", second_page)
        self.assertEqual(ctx.message.components[-1].components[1].label, "2/2")
        await self.cog.lb_refresh.callback.func(self.cog, ctx)
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "Current Season")
        self.assertIn(second_page, ctx.message.embeds[0].description)
        self.assertEqual(ctx.message.components[-1].components[1].label, "2/2")
        await self.cog.lb_page_modal.callback.func(self.cog, ctx, page="1")
        self.assertIn("`20,000`", ctx.message.embeds[0].description)
        self.assertEqual(ctx.message.embeds[0].fields[0].value, "Current Season")

    async def test_no_finals_or_archives_return_empty_without_crashing(self):
        self.cog._seasons_cache = []
        self.finals = []
        ctx = await self.invoke()
        self.assertNotIn("embed", ctx.send.await_args.kwargs)
        self.assertEqual(await final_board_rows("Predictions", "All Seasons"), [])
        ctx = await self.invoke("Tokens")
        self.assertEqual(ctx.send.await_args.kwargs["embed"].fields[0].value, "Current Season")

    async def test_active_final_without_ranked_archives_keeps_selected_scope(self):
        self.cog._seasons_cache = []
        self.prediction(7, "active", 1, 2)
        ctx = await self.invoke()
        payload = ctx.send.await_args.kwargs
        self.assertEqual(payload["embed"].fields[0].value, "Season 7 Final")
        self.assertEqual(selected(payload["components"]), ["7"])


if __name__ == "__main__":
    unittest.main()
