import unittest

from beanie import PydanticObjectId

from elo_bot.core.finals import FinalError
from elo_bot.db import EloFinal
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.finals.bracket import advance_nodes, automatic_pairs, build_bracket
from elo_bot.features.finals.bracket_editor import editor_view, swapped_pairs
from elo_bot.features.finals.dashboard import (
    DashboardState, available_actions, dashboard_controls, dashboard_embed, next_round,
)
from elo_bot.features.tokens.presentation import market_signature


class TestFinalDashboard(unittest.TestCase):
    def edition(self, count=8):
        return EloFinal.model_construct(id=PydanticObjectId(), season_number=7, active=True,
            entrant_ids=[str(user) for user in range(count)], seed_elo={str(user): 2000 - user for user in range(count)},
            player_nicknames={str(user): f"Player {user}" for user in range(count)})

    def test_setup_seed_order_and_nicknames_do_not_expose_manager_fields_to_players(self):
        final = self.edition()
        final.seed_elo["7"] = 2200
        names = {str(user): f"**[Player {user}](<https://discordapp.com/users/{user}?tab=wishlist>)**" for user in range(8)}
        names["6"] = "**[Discord Six](<https://discordapp.com/users/6?tab=wishlist>)**"
        state = DashboardState(final, submitted=42)
        manager = dashboard_embed(state, names, manager=True)
        entrants = manager.fields[0].value
        self.assertTrue(entrants.startswith("1. **[Player 7]"))
        self.assertNotIn("(Player 6)", entrants)
        self.assertNotIn("(Player 7)", entrants)
        player = dashboard_embed(state, names, manager=False)
        self.assertEqual([field.name for field in player.fields], ["**Predictions**"])
        self.assertEqual(player.fields[0].value, "Not opened · 42 submitted")
        self.assertEqual(available_actions(state), [("bracket", "Create Bracket")])

    def test_round_actions_stop_at_unresolved_predecessors_and_offer_recovery_first(self):
        final = self.edition()
        final.nodes = build_bracket(final, automatic_pairs(final))
        final.state = "BRACKET_READY"
        self.assertEqual(next_round(DashboardState(final)), "Quarterfinals")
        self.assertEqual([action for action, _ in available_actions(DashboardState(final))], ["open", "round"])
        final.prediction_state = "OPEN"
        self.assertEqual(available_actions(DashboardState(final))[0], ("close", "Close Predictions"))
        for key in ("QF1", "QF2", "QF3", "QF4"):
            final.nodes[key].thread_id = str(100 + int(key[-1]))
        self.assertEqual(next_round(DashboardState(final)), "Quarterfinals")
        markets = [FinalBettingMarket.model_construct(id=f"market-{key}", node_id=key,
            contestant_revision=final.nodes[key].contestant_revision, announcement_id=str(index))
            for index, key in enumerate(("QF1", "QF2", "QF3", "QF4"))]
        for market in markets:
            market.announcement_signature = market_signature(market)
        state = DashboardState(final, markets)
        self.assertIsNone(next_round(state))
        final.nodes["QF1"].state, final.nodes["QF1"].result_id = "TIED", "tie"
        self.assertIsNone(next_round(state))
        for key in ("QF1", "QF2", "QF3", "QF4"):
            final.nodes[key].winner = final.nodes[key].contestants[0]
        advance_nodes(final.nodes)
        self.assertEqual(next_round(state), "Semifinals")
        final.repair_pending = True
        self.assertIsNone(next_round(state))
        self.assertEqual(available_actions(DashboardState(final))[0], ("recover", "Retry Now"))

    def test_manager_dashboard_links_threads_and_explains_ties_without_leaking_them(self):
        final = self.edition()
        final.state = "RUNNING"
        final.nodes = build_bracket(final, automatic_pairs(final))
        final.nodes["QF2"].thread_id = "777"
        final.nodes["QF2"].state = "TIED"
        market = FinalBettingMarket.model_construct(id="market", node_id="QF2", state="CLOSED", pools={"3": 1000})
        state = DashboardState(final, [market], 42)
        manager = dashboard_embed(state, {}, manager=True)
        player = dashboard_embed(state, {}, manager=False)
        text = "\n".join(field.value for field in manager.fields)
        self.assertIn("<#777>", text)
        self.assertIn("`/final match result match:QF2`", text)
        self.assertNotIn("<#", "\n".join(field.value for field in player.fields))
        self.assertEqual([field.name for field in player.fields], ["**Matches**", "**Predictions**", "**Betting**"])
        self.assertTrue(all(len(field.value) <= 1024 for field in manager.fields + player.fields))
        final.champion = "1"
        final.predictions_ever_opened = True
        final.prediction_state = "CLOSED"
        market.state = "REFUNDED"
        self.assertEqual(available_actions(state)[-1], ("end", "End Final"))
        self.assertTrue(all(len(button.custom_id) <= 100 for row in dashboard_controls(state, "9") for button in row.components))
        final.active = False
        self.assertEqual(dashboard_controls(state, "9"), [])

    def test_slot_swaps_preserve_roster_and_reject_double_byes(self):
        final = self.edition(5)
        final.manual_draft = automatic_pairs(final)
        final.draft_owner = "9"
        original = {key: list(pair) for key, pair in final.manual_draft.items()}
        self.assertEqual(original, {"QF1": ["0", None], "QF2": ["1", None], "QF3": ["2", None], "QF4": ["3", "4"]})
        pairs = swapped_pairs(final, ["QF1:0", "QF4:0"])
        self.assertEqual(pairs["QF1"], ["3", None])
        self.assertEqual(pairs["QF4"], ["0", "4"])
        with self.assertRaises(FinalError):
            swapped_pairs(final, ["QF1:0", "QF3:1"])
        self.assertEqual(final.manual_draft, original)
        embed, controls = editor_view(final, {})
        menu = controls[0].components[0]
        self.assertEqual(menu.min_values, 2)
        self.assertEqual(menu.max_values, 2)
        self.assertEqual(len(menu.options), 8)
        self.assertEqual(menu.options[0].description, "Seed 1 · 2000 Elo")
        self.assertEqual(menu.options[1].description, "Bye")
        self.assertTrue(all(len(option.label) <= 100 for option in menu.options))
        self.assertEqual(embed.description.count("`QF"), 4)
