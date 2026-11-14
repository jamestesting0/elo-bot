import unittest
from unittest.mock import patch

from beanie.odm.settings.document import DocumentSettings
from pydantic import ValidationError

from elobot.db import Match


class TestMessagePublicationShape(unittest.TestCase):
    def test_numeric_objects_keep_side_identity_and_complete_payload(self) -> None:
        publication = {
            "message_id": 1549, "is_war": True, "participants": ["a", "b"],
            "times": [None, 100], "hosts": [None, "b"], "host_clans": [None, "#B"],
            "embeds": [{"title": "Match Started"}],
            "components": [{"components": [{"disabled": False}]}], "pending": True,
        }
        with patch.object(Match, "_document_settings", DocumentSettings(name="matches")):
            for raw in ({"1": publication}, {"0": publication}, {"1": publication, "0": publication}, {}):
                with self.subTest(keys=list(raw)):
                    match = Match.model_validate({"token": "TOKEN", "matched_time": 1,
                                                  "message_publications": raw})
                    dumped = match.model_dump(mode="json")["message_publications"]
                    self.assertEqual(dumped, [raw.get("0"), raw.get("1")])

    def test_unknown_keys_and_invalid_payloads_are_not_silently_discarded(self) -> None:
        with patch.object(Match, "_document_settings", DocumentSettings(name="matches")):
            for raw in ({"2": None}, {"01": None}, {"1": {"message_id": 42}}, None):
                with self.subTest(raw=raw), self.assertRaises(ValidationError):
                    Match.model_validate({"token": "TOKEN", "matched_time": 1,
                                          "message_publications": raw})
