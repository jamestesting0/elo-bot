import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from beanie import PydanticObjectId

from elo_bot.config.emojis import LOSS_EMOJI, STAR_EMOJI, WIN_EMOJI
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import EloFinal, EloFinalMatch
from elo_bot.db.final_models import FinalMatchResult, FinalResultSnapshot
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.finals.bracket import automatic_pairs, build_bracket
from elo_bot.features.finals.bracket_ui import bracket_controls, bracket_embed
from elo_bot.features.finals.result_ui import score_embed
from elo_bot.features.tokens.presentation import market_embed, market_controls


class TestFinalPresentation(unittest.IsolatedAsyncioTestCase):
    def edition(self):
        final = EloFinal.model_construct(id=PydanticObjectId(), season_number=7, entrant_ids=[str(i) for i in range(5)],
            player_nicknames={str(i): f"Player {i}" for i in range(5)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        return final

    async def test_public_bracket_groups_rounds_and_never_links_private_threads(self):
        final = self.edition()
        node = final.nodes["QF2"]
        node.thread_id = "123456"
        node.state = "PLAYING"
        final.nodes["SF1"].state = "READY"
        final.nodes["SF1"].scheduled_times = [2000000000, 2000000000]
        embed = bracket_embed(final, {str(i): f"User {i}" for i in range(5)})
        self.assertEqual(len(embed.fields), 3)
        self.assertEqual([field.value.count("`QF") for field in embed.fields], [4, 0, 0])
        text = "\n".join(field.value for field in embed.fields)
        self.assertNotIn("<#", text)
        self.assertNotIn(node.state, text)
        self.assertIn("<t:2000000000:F>", text)
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))

    async def test_result_selector_contains_only_current_decided_non_bye_nodes(self):
        final = self.edition()
        node = final.nodes["QF4"]
        node.winner, node.result_id, node.state = "4", "saved", "DECIDED"
        final.nodes["SF2"] = EloFinalMatch(id="SF2", contestants=["1", "2"], state="READY")
        result = FinalMatchResult.model_construct(id="saved", snapshot=FinalResultSnapshot(
            contestants=["3", "4"], winner="4", scores={"3": ["2.80"] * 7, "4": ["3.100"] * 7}))
        query = MagicMock(to_list=AsyncMock(return_value=[result]))
        with patch.object(FinalMatchResult, "find", return_value=query):
            controls = await bracket_controls(final)
        menu = controls[0].components[0]
        self.assertEqual(menu.custom_id, f"finalresult|{final.id}")
        self.assertEqual([option.value for option in menu.options], ["QF4"])
        self.assertTrue(all(len(option.label) <= 100 and len(option.description) <= 100 for option in menu.options))
        self.assertIn("21⭐ 700%", menu.options[0].description)
        self.assertEqual(await bracket_controls(self.edition()), [])

    async def test_played_and_forfeit_results_show_only_applicable_scores(self):
        final = self.edition()
        for outcome, winner in (("Played", "4"), ("Forfeit", "4")):
            with self.subTest(outcome=outcome, winner=winner):
                snapshot = FinalResultSnapshot(contestants=["3", "4"], winner=winner, outcome=outcome,
                    scores={"3": ["0.0" if outcome == "Forfeit" else "2.80"] * 7,
                            "4": ["0.0" if outcome == "Forfeit" else "3.100"] * 7})
                embed = score_embed(final, "QF2", snapshot, {"3": "Left", "4": "Right"})
                if outcome == "Forfeit":
                    self.assertNotIn(str(STAR_EMOJI), embed.description)
                    self.assertEqual(embed.fields, [])
                    self.assertIn(f"Right {WIN_EMOJI}", embed.description)
                else:
                    district_rows = embed.description.splitlines()[2:]
                    self.assertEqual(len(district_rows), 7)
                    self.assertTrue(all(district in row for district, row in zip(FINAL_DISTRICTS, district_rows, strict=True)))
                    self.assertEqual(embed.fields[0].value, f"14{STAR_EMOJI} 560% — 21{STAR_EMOJI} 700%")
                    self.assertIn(f"Left {LOSS_EMOJI}", embed.description)
                    self.assertIn(f"Right {WIN_EMOJI}", embed.description)
                    self.assertEqual(len(embed.fields), 1)

    async def test_market_pools_render_real_multipliers_and_disable_closed_controls(self):
        final = self.edition()
        market = FinalBettingMarket.model_construct(id="market", node_id="QF2", contestants=["3", "4"],
            pools={"3": 4000, "4": 6000}, desired_winner="4")
        for state, label in (("OPEN", "Open"), ("CLOSED", "Closed"), ("SETTLING", "Paying out"),
                             ("CORRECTING", "Paying out"), ("PAID", "Paid"), ("REFUNDED", "Refunded")):
            with self.subTest(state=state):
                market.state = state
                embed = market_embed(final, market, {"3": "Left", "4": "Right"})
                self.assertEqual(embed.to_dict().get("author"), {"name": label})
                self.assertIn("×2.50", embed.description)
                self.assertIn("×1.67", embed.description)
                self.assertNotIn(label, embed.description)
                self.assertEqual("Winning bets split the whole pool." in embed.description, state == "OPEN")
                self.assertEqual((embed.description.count("Left"), embed.description.count("Right")), (1, 1))
                winner_rows = [line for line in embed.description.splitlines() if str(WIN_EMOJI) in line]
                self.assertEqual(winner_rows, [line for line in embed.description.splitlines()
                                               if line.startswith("Right")] if state == "PAID" else [])
                self.assertTrue(embed.fields[0].value.startswith("10,000 "))
                self.assertEqual(market_controls(market)[0].disabled, state != "OPEN")
        market.pools = {}
        self.assertEqual(market_embed(final, market).description.count("×—"), 2)
