import time
import unittest
from unittest.mock import patch

from elo_bot.config.emojis import LOADING_EMOJI, SUCCESS_EMOJI
from elo_bot.core.finals import FINAL_DISTRICTS
from elo_bot.db import Match
from elo_bot.db.match_models import Participant
from elo_bot.db.sequential_models import LIVE_SIDE, HelperSequentialRun
from elo_bot.features.hosting.sequential import ui


class TestSequentialProgressEmbed(unittest.TestCase):
    def test_progress_lists_each_district_with_its_state_and_stored_scores(self):
        run = HelperSequentialRun.model_construct(
            final_node_id="QF4", side=LIVE_SIDE, state="ACTIVE", job_status="running", job_id="job",
            last_polled_at=time.time(), districts=list(FINAL_DISTRICTS), close_reason=None)
        match = Match.model_construct(participants=[
            Participant.model_construct(result=["2.83"] + [None] * 6),
            Participant.model_construct(result=["3.100"] + [None] * 6),
        ])
        states = {FINAL_DISTRICTS[0]: "played", FINAL_DISTRICTS[1]: "in progress"}
        with patch.object(ui, "run_result", return_value=object()), \
                patch.object(ui, "round_state", side_effect=lambda _result, district: states.get(district, "waiting")):
            embed = ui.progress_embed(run, match)
        self.assertEqual(embed.fields, [])
        rows = embed.description.split("\n\n", 1)[1].splitlines()
        self.assertEqual(len(rows), len(FINAL_DISTRICTS))
        self.assertTrue(all(district in row for district, row in zip(FINAL_DISTRICTS, rows, strict=True)))
        self.assertTrue(rows[0].startswith(str(SUCCESS_EMOJI)))
        self.assertIn("`2⭐ 83%`", rows[0])
        self.assertIn("`3⭐ 100%`", rows[0])
        self.assertTrue(rows[1].startswith(str(LOADING_EMOJI)))
        self.assertTrue(all(row.startswith("➖") and "`" not in row for row in rows[2:]))
        self.assertNotIn("job", embed.description)
