import time
import unittest

from elo_bot.config.emojis import CLASHING_SWORD_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import Match
from elo_bot.db.match_models import Participant
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.hosting.sequential import ui


class TestSequentialProgressEmbed(unittest.TestCase):
    def test_progress_lists_each_district_with_its_state_and_stored_scores(self):
        snapshot = {
            "stage": "WAITING:1",
            "score_status": "incomplete",
            "match": {"players": [{"key": "player_1", "tag": "#P1"}, {"key": "player_2", "tag": "#P2"}]},
            "completion": {"workflow_finished": False, "districts_completed": 1, "districts_total": 7, "rounds": [{
                "districts": {"player_1": "BL", "player_2": "BL"},
                "players": {key: {"posted": True, "completed": False, "closed": False}
                            for key in ("player_1", "player_2")},
            }]},
            "score_outcomes": [],
        }
        run = HelperSequentialRun.model_construct(
            final_node_id="QF4", side=LIVE_SIDE, state="ACTIVE", job_status="running", job_id="job",
            last_polled_at=time.time(), districts=list(FINAL_DISTRICTS), close_reason=None, snapshot=snapshot)
        match = Match.model_construct(participants=[
            Participant.model_construct(result=["2.83"] + [None] * 6),
            Participant.model_construct(result=["3.100"] + [None] * 6),
        ])
        embed = ui.progress_embed(run, match)
        self.assertEqual(embed.fields, [])
        self.assertIn(f"**Waiting for attacks** · <t:{int(run.last_polled_at)}:R>", embed.description)
        rows = embed.description.split("\n\n", 1)[1].splitlines()
        self.assertEqual(len(rows), len(FINAL_DISTRICTS))
        self.assertTrue(all(district in row for district, row in zip(FINAL_DISTRICTS, rows, strict=True)))
        self.assertTrue(rows[0].startswith(str(SUCCESS_EMOJI)))
        self.assertIn("`2⭐ 83%`", rows[0])
        self.assertIn("`3⭐ 100%`", rows[0])
        self.assertTrue(rows[1].startswith(str(CLASHING_SWORD_EMOJI)))
        self.assertIn("Challenge sent", rows[1])
        self.assertTrue(all(row.startswith("➖") and "`" not in row for row in rows[2:]))
        self.assertNotIn("job", embed.description)
