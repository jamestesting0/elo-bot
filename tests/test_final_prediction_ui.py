import unittest

from common.finals.bracket import automatic_pairs, build_bracket
from common.finals.prediction_ui import prediction_view
from common.finals.types import NODE_IDS
from database_models import EloFinal
from database_models.final_models import FinalPrediction


class TestFinalPredictionNavigation(unittest.TestCase):
    def fixture(self, count):
        final = EloFinal.model_construct(season_number=13, entrant_ids=[str(i) for i in range(count)],
            player_nicknames={str(i): f"Player {i}" for i in range(count)})
        final.nodes = build_bracket(final, automatic_pairs(final))
        prediction = FinalPrediction.model_construct(user_id="viewer", season_number=13,
            draft={key: node.winner for key, node in final.nodes.items() if node.bye})
        return final, prediction

    def next_button(self, final, prediction):
        payload = prediction_view(final, prediction)[0].to_dict()
        return next(button for row in payload["components"] if row["type"] == 1
                    for button in row["components"] if button.get("label") == "Next")

    def test_next_waits_for_every_winner_on_the_current_page(self):
        final, prediction = self.fixture(8)
        for stage, nodes in enumerate((NODE_IDS[:4], NODE_IDS[4:6], NODE_IDS[6:])):
            with self.subTest(stage=stage):
                prediction.stage = stage
                self.assertTrue(self.next_button(final, prediction)["disabled"])
                for node in nodes:
                    prediction.draft[node] = "0"
                self.assertFalse(self.next_button(final, prediction)["disabled"])

    def test_byes_are_complete_without_an_extra_choice(self):
        final, prediction = self.fixture(5)
        played = next(key for key in NODE_IDS[:4] if not final.nodes[key].bye)
        self.assertTrue(self.next_button(final, prediction)["disabled"])
        prediction.draft[played] = final.nodes[played].contestants[0]
        self.assertFalse(self.next_button(final, prediction)["disabled"])

    def test_review_cannot_advance_even_with_complete_draft(self):
        final, prediction = self.fixture(8)
        prediction.stage = 3
        prediction.draft = dict.fromkeys(NODE_IDS, "0")
        self.assertTrue(self.next_button(final, prediction)["disabled"])
        prediction.draft["QF2"] = "1"
        payload = prediction_view(final, prediction, {"0": "**[Player Zero](<https://discordapp.com/users/0>)**"})[0]
        text = "\n".join(component.content for component in payload.components
                         if hasattr(component, "content"))
        self.assertIn("QF1: **[Player Zero](<https://discordapp.com/users/0>)**", text)
        self.assertIn("QF2: **[❌Invalid Player](<https://discordapp.com/users/1>)**", text)
