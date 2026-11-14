from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import torch
from PIL import Image

from elobot.features.bases.fingerprints.artifacts import MANIFEST, PREVIOUS_MANIFEST
from elobot.features.bases.fingerprints.extractor import extract_batch
from elobot.features.bases.fingerprints.slicing import Detection, deduplicate, sliced_detections
from elobot.features.bases.fingerprints.types import LEGACY_PIPELINE_ID, PIPELINE_ID, PREVIOUS_PIPELINE_ID, PREVIOUS_SAHI_PIPELINE_ID, BaseFingerprint, FingerprintError
from tests._relative_geometry_fixture import points
from tests._relative_geometry_fixture import prediction


from tests._base_extraction_fixture import SahiExtractionStateMixin


from tests._base_extraction_fixture import detections, tile_model

class TestSahiAdapter(unittest.TestCase):
    def setUp(self) -> None:
        from sahi.postprocess.backends import get_postprocess_backend, set_postprocess_backend

        previous = get_postprocess_backend()
        self.addCleanup(set_postprocess_backend, previous)
        set_postprocess_backend("numpy")

    def test_two_tiles_shift_to_full_image_and_preserve_rgb_and_predictor_options(self) -> None:
        # Given a colored image and one detection in each tile.
        model = tile_model([[10, 20, 30, 40, .9, MANIFEST["classes"].index("cannon")]])
        with Image.new("RGB", (1000, 800), (10, 20, 30)) as image:
            # When using the real SAHI slicer and coordinate conversion.
            found = sliced_detections(model, image, "cuda:0")
        # Then two serial tiles use the selected device and full-image coordinates.
        self.assertEqual([box.xyxy for box in found], [(10, 20, 30, 40), (454, 20, 474, 40)])
        self.assertEqual(model.predict.call_count, 2)
        for call in model.predict.call_args_list:
            options = call.kwargs
            self.assertEqual(options["source"][0].shape, (800, 556, 3))
            np.testing.assert_array_equal(options["source"][0][0, 0], [30, 20, 10])
            self.assertEqual((options["device"], options["imgsz"], options["conf"], options["batch"]),
                             ("cuda:0", 448, .25, 1))
            self.assertFalse(any(options[key] for key in ("rect", "half", "augment", "save", "save_txt", "save_crop")))

    def test_real_slice_merge_and_cross_class_cleanup_keep_one_building(self) -> None:
        # Given overlapping tile observations of the same building with conflicting classes.
        names = dict(enumerate(MANIFEST["classes"]))
        calls = [
            [[470, 20, 500, 60, .9, MANIFEST["classes"].index("cannon")]],
            [[26, 20, 56, 60, .8, MANIFEST["classes"].index("cannon")],
             [26, 20, 56, 60, .7, MANIFEST["classes"].index("spear")]],
        ]
        model = tile_model([])
        model.predict.side_effect = [
            [SimpleNamespace(names=names, boxes=SimpleNamespace(data=torch.tensor(rows)))] for rows in calls
        ]
        with Image.new("RGB", (1000, 800)) as image:
            # When merging and cleaning predictions.
            found = sliced_detections(model, image, "cpu")
        # Then coordinates are not shifted twice and confidence is not inflated.
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].label, "cannon")
        self.assertEqual(found[0].xyxy, (470, 20, 500, 60))
        self.assertAlmostEqual(found[0].score, .9)

    def test_small_image_remains_two_tiles_and_empty_predictions_are_empty(self) -> None:
        # Given the smallest accepted source and empty predictions.
        model = tile_model([])
        with Image.new("RGB", (32, 32)) as image:
            # When slicing.
            found = sliced_detections(model, image, "cpu")
        # Then there is no fallback pass or fabricated box.
        self.assertEqual(found, ())
        self.assertEqual(model.predict.call_count, 2)

    def test_wrong_class_map_or_batch_length_fails_before_conversion(self) -> None:
        # Given malformed predictor outputs.
        for response in ([], [SimpleNamespace(names={0: "wrong"})]):
            model = tile_model([])
            model.predict.return_value = response
            with self.subTest(response=response), Image.new("RGB", (1000, 800)) as image:
                # When predicting through the real SAHI boundary.
                with self.assertRaisesRegex(FingerprintError, "inference_batch_mismatch|model_class_map_mismatch"):
                    sliced_detections(model, image, "cpu")
                # Then the second tile is not processed.
                self.assertEqual(model.predict.call_count, 1)

    def test_cleanup_is_deterministic_preserves_neighbors_and_caps_global_count(self) -> None:
        # Given a conflict, a neighboring structure and excess detections.
        a = Detection("cannon", (0, 0, 20, 20), .9)
        conflict = Detection("spear", (0, 0, 20, 20), .9)
        neighbor = Detection("spear", (25, 0, 45, 20), .8)
        # When cleaning in either input order.
        forward = deduplicate((a, conflict, neighbor), .8, 300)
        backward = deduplicate((neighbor, conflict, a), .8, 300)
        # Then ties are reproducible, neighboring buildings survive, and overflow is capped.
        self.assertEqual(forward, (a, neighbor))
        self.assertEqual(backward, forward)
        self.assertEqual(deduplicate((a, neighbor), .8, 1), (a,))


class TestSahiExtraction(SahiExtractionStateMixin, unittest.TestCase):
    def test_fixed_threshold_one_round_bounds_and_all_district_hall_warnings(self) -> None:
        # Given an unexpected count with enough geometry, including different hall conditions.
        for district in range(9):
            correct = "capital_peak" if district == 0 else "district_hall"
            wrong = "district_hall" if district == 0 else "capital_peak"
            for halls, warning in (((), "hall_absent"), (((correct, .2, .2),), None),
                                   (((wrong, .2, .2),), "hall_wrong_class"),
                                   (((correct, .2, .2), (correct, .3, .3)), None)):
                with self.subTest(district=district, warning=warning), patch(
                    "elobot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections(halls=halls)
                ) as sliced:
                    # When extracting through the public production API.
                    result = extract_batch(MagicMock(), [self.source(district=district)], "cpu", include_bounds=True)[0]
                # Then count mismatch never lowers confidence or retries.
                fp = result.fingerprint
                self.assertIsNone(result.error)
                self.assertEqual((fp.pipeline_id, fp.extractor_revision, fp.confidence_threshold, fp.inference_runs),
                                 (PIPELINE_ID, "elo-4", .25, 1))
                self.assertEqual(sliced.call_count, 1)
                self.assertEqual(len(result.bounds), fp.total_detections)
                self.assertFalse(fp.expected_count_match)
                self.assertEqual(sum(building.type in {"capital_peak", "district_hall"} for building in fp.buildings),
                                 min(1, len(halls)))
                if warning:
                    self.assertIn(warning, fp.warnings)
                for building, (x1, y1, x2, y2) in zip(fp.buildings, result.bounds, strict=True):
                    self.assertAlmostEqual(building.x, (x1 + x2) / 2)
                    self.assertAlmostEqual(building.y, (y1 + y2) / 2)
                self.assertEqual(BaseFingerprint.model_validate_json(fp.model_dump_json()), fp)

    def test_sparse_or_degenerate_layout_fails_without_retries(self) -> None:
        # Given insufficient detections or a degenerate geometry.
        for data in (points(11), [("cannon", x, y) for _, x, y in points()],
                     [(name, .2, y) for name, _, y in points()]):
            with self.subTest(data=data), patch("elobot.features.bases.fingerprints.extractor.sliced_detections",
                                               return_value=detections(data)) as sliced:
                # When extracting.
                result = extract_batch(MagicMock(), [self.source()], "cpu")[0]
            # Then no success fingerprint is returned and confidence is not lowered.
            self.assertIsNone(result.fingerprint)
            self.assertEqual(result.error, "insufficient_layout_evidence")
            self.assertEqual(sliced.call_count, 1)

    def test_mixed_historical_and_current_batch_preserves_identity_and_device(self) -> None:
        # Given pending jobs from all supported extraction generations.
        model = MagicMock()
        model.predict.side_effect = lambda **kwargs: [prediction(points(53), (("district_hall", .2, .2),))
                                                     for _ in kwargs["source"]]
        sources = [self.source(LEGACY_PIPELINE_ID, revision=1), self.source(PREVIOUS_PIPELINE_ID, revision=2),
                   self.source(PREVIOUS_SAHI_PIPELINE_ID, revision=3), self.source(PIPELINE_ID, revision=4)]
        with patch("elobot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections(points(53))) as sliced:
            # When extracting on the selected device.
            results = extract_batch(model, sources, "cuda:0")
        # Then historical jobs retain their manifests and only current work uses SAHI.
        self.assertEqual([result.fingerprint.pipeline_id for result in results], [source.pipeline_id for source in sources])
        self.assertEqual([result.fingerprint.source_revision for result in results], [1, 2, 3, 4])
        self.assertEqual([result.fingerprint.extractor_revision for result in results], ["elo-1", "elo-2", "elo-3", "elo-4"])
        self.assertEqual(model.predict.call_count, 1)
        self.assertEqual(model.predict.call_args.kwargs["imgsz"], PREVIOUS_MANIFEST["imgsz"])
        self.assertEqual(sliced.call_args.args[2], "cuda:0")

    def test_bad_source_preserves_other_batch_entry_and_inference_failure_closes_images(self) -> None:
        # Given a missing source alongside a valid source.
        missing = self.source().model_copy(update={"path": str(self.directory / "missing.png")})
        with patch("elobot.features.bases.fingerprints.extractor.sliced_detections", return_value=detections()):
            # When extracting both entries.
            bad, good = extract_batch(MagicMock(), [missing, self.source(revision=4)], "cpu")
        # Then the output positions and successful source identity remain correct.
        self.assertEqual(bad.error, "source_unavailable")
        self.assertEqual(good.fingerprint.source_revision, 4)
        image = Image.new("RGB", (1000, 800))
        with patch("elobot.features.bases.fingerprints.extractor.decode_image", return_value=image), patch(
            "elobot.features.bases.fingerprints.extractor.sliced_detections", side_effect=RuntimeError("inference failed")
        ), self.assertRaisesRegex(RuntimeError, "inference failed"):
            extract_batch(MagicMock(), [self.source()], "cpu")
        with self.assertRaises(ValueError):
            image.getpixel((0, 0))
