import unittest

from pydantic import ValidationError

from elo_bot.db.fingerprint_types import BaseFingerprint
from tests._relative_geometry_fixture import layout


class TestRelativeSchema(unittest.TestCase):
    def test_mixed_identity_and_invalid_geometry_rejected(self):
        for version in (1, 2):
            document = layout(version=version).model_dump()
            for updates in ({"pipeline_id": "unknown"}, {"model_sha256": "b" * 64},
                            {"extractor_revision": "elo-99"}, {"coordinate_system": "other"},
                            {"total_detections": 2}, {"image_width": 0}, {"anchor_found": False,
                            "anchor_type": "capital_peak"}):
                with self.subTest(version=version, updates=updates), self.assertRaises(ValidationError):
                    BaseFingerprint.model_validate(document | updates)

    def test_v2_hall_metadata_matches_actual_detections(self):
        for district in range(9):
            original = layout(district=district, hall=True)
            self.assertTrue(original.anchor_found)
            document = original.model_dump()
            document["buildings"][-1]["type"] = "district_hall" if district == 0 else "capital_peak"
            with self.assertRaises(ValidationError):
                BaseFingerprint.model_validate(document)
            document.update(anchor_found=False, anchor_type=None)
            self.assertFalse(BaseFingerprint.model_validate(document).anchor_found)

    def test_v2_negative_and_nonfinite_coordinates_rejected(self):
        for field, value in (("x", -.1), ("x", float("nan")), ("y", float("inf")), ("y", 1.1)):
            document = layout().model_dump()
            document["buildings"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                BaseFingerprint.model_validate(document)
