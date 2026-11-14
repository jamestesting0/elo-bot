import unittest
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from elobot.db import Match, Participant
from elobot.core.finals import FinalError


DISTRICTS = ["Barbarian Camp", "Balloon Lagoon", "Builder's Workshop", "Dragon Cliffs",
             "Golem Quarry", "Skeleton Park", "Goblin Mines"]


class TestFinalMatchShape(unittest.TestCase):
    def test_domain_error_keeps_its_message_through_a_context_manager(self):
        @contextmanager
        def boundary():
            yield
        with self.assertRaisesRegex(FinalError, "already exists"):
            with boundary():
                raise FinalError("An edition already exists.")

    def test_final_participant_preserves_all_seven_slots(self):
        participant = Participant(user_id="42", format="elo_final_seven", districts=DISTRICTS,
                                  bases=[None] * 7, result=["0.00"] * 7, secondary_base_index=6)
        self.assertEqual(participant.result, ["0.00"] * 7)
        self.assertEqual(participant.secondary_base_index, 6)

    def test_ordinary_participant_rejects_extra_slots(self):
        with self.assertRaises(ValidationError):
            Participant(user_id="42", districts=DISTRICTS, bases=[None] * 7, result=[None] * 7)

    def test_final_match_rejects_duplicate_or_excluded_districts(self):
        participants = [Participant.model_construct(user_id=str(i), format="elo_final_seven",
                                                   districts=DISTRICTS, bases=[None] * 7, result=[None] * 7)
                        for i in (42, 43)]
        with patch.object(Match, "get_pymongo_collection", return_value=MagicMock()):
            valid = Match(token="final", matched_time=1, format="elo_final_seven", final_id="final-id",
                          final_node_id="QF1", final_districts=DISTRICTS, participants=participants)
            self.assertEqual(valid.final_districts, DISTRICTS)
            for districts in (DISTRICTS[:-1] + [DISTRICTS[0]], DISTRICTS[:-1] + ["Capital Peak"]):
                with self.subTest(districts=districts), self.assertRaises(ValidationError):
                    Match(token="final", matched_time=1, format="elo_final_seven", final_id="final-id",
                          final_node_id="QF1", final_districts=districts, participants=participants)

if __name__ == "__main__":
    unittest.main()
