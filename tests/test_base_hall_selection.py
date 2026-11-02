from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from pydantic import ValidationError
from ultralytics.utils import plotting

from common.base_fingerprints.extractor import extract_batch
from common.base_fingerprints.labels import create_labels
from common.base_fingerprints.slicing import Detection
from common.base_fingerprints.types import HALL_TYPES, BaseFingerprint
from tests import _base_extraction_fixture as fixture
from tests._relative_geometry_fixture import layout
from tests._base_extraction_fixture import detections


class TestHallSelection(unittest.TestCase):
    setUp = fixture.SahiExtractionStateMixin.setUp
    source = fixture.SahiExtractionStateMixin.source

    def test_new_fingerprint_identity_rejects_multiple_halls_and_old_sahi_remains_readable(self) -> None:
        document = layout().model_dump()
        document["buildings"].extend([
            {"type": "district_hall", "x": .2, "y": .2, "conf": .9},
            {"type": "capital_peak", "x": .5, "y": .5, "conf": .8},
        ])
        document["total_detections"] = len(document["buildings"])
        with self.assertRaisesRegex(ValidationError, "at most one hall"):
            BaseFingerprint.model_validate(document)
        document.update(pipeline_id="capital-v2-sahi448-4d63ddd9", extractor_revision="elo-3")
        self.assertEqual(BaseFingerprint.model_validate(document).total_detections, 32)

    def test_highest_confidence_across_both_classes_preserves_defenses_and_bounds(self) -> None:
        defenses = detections()
        for district in range(9):
            expected = "capital_peak" if district == 0 else "district_hall"
            for winner_type in HALL_TYPES:
                winner = Detection(winner_type, (500, 500, 560, 560), .90004)
                runners = (Detection("capital_peak", (100, 100, 160, 160), .88),
                           Detection("district_hall", (250, 250, 310, 310), .90003))
                with self.subTest(district=district, winner=winner_type), patch(
                    "common.base_fingerprints.extractor.sliced_detections",
                    return_value=(*runners, *defenses, winner),
                ):
                    result = extract_batch(MagicMock(), [self.source(district=district)], "cpu", include_bounds=True)[0]
                fp = result.fingerprint
                self.assertIsNone(result.error)
                self.assertIsNotNone(fp)
                self.assertEqual(fp.total_detections, len(defenses) + 1)
                self.assertEqual([building.type for building in fp.buildings[:-1]], [box.label for box in defenses])
                self.assertEqual([building.type for building in fp.buildings if building.type in HALL_TYPES],
                                 [winner_type])
                self.assertEqual((fp.buildings[-1].x, fp.buildings[-1].y, fp.buildings[-1].conf), (.53, .6625, .9))
                self.assertEqual(fp.anchor_found, winner_type == expected)
                self.assertEqual(fp.anchor_type, expected if winner_type == expected else None)
                self.assertNotIn("hall_multiple", fp.warnings)
                self.assertEqual("hall_wrong_class" in fp.warnings, winner_type != expected)
                for box, bounds in zip((*defenses, winner), result.bounds, strict=True):
                    x1, y1, x2, y2 = box.xyxy
                    self.assertEqual(bounds, (x1 / 1000, y1 / 800, x2 / 1000, y2 / 800))

    def test_ties_are_deterministic_and_earlier_sahi_jobs_keep_original_halls(self) -> None:
        halls = (Detection("district_hall", (200, 200, 260, 260), .9),
                 Detection("capital_peak", (400, 400, 460, 460), .9),
                 Detection("capital_peak", (100, 100, 160, 160), .9))
        for ordered in (halls, tuple(reversed(halls))):
            for source, expected_count in ((self.source(district=0), 1),
                                          (self.source(pipeline="capital-v2-sahi448-4d63ddd9", district=0), 3)):
                with self.subTest(pipeline=source.pipeline_id, order=ordered), patch(
                    "common.base_fingerprints.extractor.sliced_detections", return_value=(*detections(), *ordered)
                ):
                    model = MagicMock()
                    result = extract_batch(model, [source], "cpu")[0]
                self.assertIsNone(result.error)
                model.predict.assert_not_called()
                found = [building for building in result.fingerprint.buildings if building.type in HALL_TYPES]
                self.assertEqual(len(found), expected_count)
                if expected_count == 1:
                    self.assertEqual((found[0].type, found[0].x, found[0].y), ("capital_peak", .13, .1625))
                else:
                    self.assertIn("hall_multiple", result.fingerprint.warnings)
                    self.assertEqual(result.fingerprint.extractor_revision, "elo-3")

    def test_labels_draw_only_the_selected_hall_on_each_panel(self) -> None:
        winner = Detection("district_hall", (500, 500, 560, 560), .95)
        extra = Detection("capital_peak", (100, 100, 160, 160), .8)
        with patch("common.base_fingerprints.extractor.sliced_detections",
                   return_value=(*detections(), extra, winner)):
            fp = extract_batch(MagicMock(), [self.source()], "cpu")[0].fingerprint
            original = plotting.Annotator.box_label
            with patch.object(plotting.Annotator, "box_label", autospec=True, side_effect=original) as draw:
                create_labels(MagicMock(), [self.source(), self.source()], (fp, fp), "cpu")
        labels = [call.args[2] for call in draw.call_args_list]
        self.assertEqual(len(labels), 62)
        self.assertEqual(labels.count("district_hall 0.95"), 2)
        self.assertFalse(any(label.startswith("capital_peak") for label in labels))
        self.assertTrue((self.directory / "labels.png").is_file())
