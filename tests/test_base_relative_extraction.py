import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from PIL import Image

from elobot.features.bases.fingerprints.artifacts import PREVIOUS_MANIFEST
from elobot.features.bases.fingerprints.extractor import extract_batch
from elobot.features.bases.fingerprints.types import FingerprintError, ImageInput
from tests._relative_geometry_fixture import points


from tests._relative_geometry_fixture import prediction

class TestRelativeExtraction(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / "image.png"
        Image.new("RGB", (1000, 800)).save(self.path)

    def source(self, district=1, version=2):
        return ImageInput(path=str(self.path), source_revision=district, source_image_url="synthetic",
                          district_id=district, pipeline_id=("capital-v1-4d63ddd9" if version == 1 else
                                                           "capital-v2-relative-4d63ddd9"))

    def test_all_districts_retain_missing_multiple_wrong_halls(self):
        for district in range(9):
            correct = "capital_peak" if district == 0 else "district_hall"
            wrong = "district_hall" if district == 0 else "capital_peak"
            cases = [((), "hall_absent"), (((correct, .2, .2),), None),
                     (((correct, .2, .2), (correct, .3, .3)), "hall_multiple"),
                     (((wrong, .2, .2),), "hall_wrong_class")]
            for halls, warning in cases:
                model = MagicMock()
                model.predict.return_value = [prediction(points(), halls)]
                with self.subTest(district=district, warning=warning):
                    result = extract_batch(model, [self.source(district)], "cpu")[0]
                    self.assertIsNone(result.error)
                    self.assertEqual(result.fingerprint.district_id, district)
                    self.assertEqual(result.fingerprint.anchor_found, warning is None)
                    self.assertEqual(len(result.fingerprint.buildings), 30 + len(halls))
                    if warning:
                        self.assertIn(warning, result.fingerprint.warnings)
                    self.assertEqual(result.fingerprint.inference_runs, 3)

    def test_mixed_legacy_and_v2_keep_original_contract_and_association(self):
        model = MagicMock()
        model.predict.return_value = [prediction(points()), prediction(points())]
        results = extract_batch(model, [self.source(version=1), self.source(version=2)], "cuda:0")
        self.assertEqual(results[0].error, "missing_or_ambiguous_hall")
        self.assertEqual(results[1].fingerprint.schema_version, 2)
        self.assertTrue(all(call.kwargs["device"] == "cuda:0" for call in model.predict.call_args_list))

    def test_floor_and_retries_do_not_create_ready_fingerprint(self):
        for data in (points(11), [("cannon", x, y) for _, x, y in points()],
                     [(name, .2, y) for name, _, y in points()]):
            model = MagicMock()
            model.predict.return_value = [prediction(data)]
            result = extract_batch(model, [self.source()], "cpu")[0]
            self.assertIsNone(result.fingerprint)
            self.assertEqual(result.error, "insufficient_layout_evidence")
            self.assertEqual(model.predict.call_count, 3)

    def test_attempt_selection_uses_nonhall_count_then_confidence(self):
        model = MagicMock()
        model.predict.side_effect = [[prediction(points(30), confidence=.8)],
                                     [prediction(points(31), confidence=.7)],
                                     [prediction(points(31), confidence=.95)]]
        result = extract_batch(model, [self.source()], "cpu")[0].fingerprint
        self.assertEqual(result.total_detections, 31)
        self.assertEqual(result.buildings[0].conf, .95)
        self.assertEqual(result.inference_runs, 3)

    def test_unusable_exact_count_retries_until_geometry_is_usable(self):
        model = MagicMock()
        model.predict.side_effect = [[prediction([("cannon", .2, .2)] * 53)],
                                     [prediction(points(53))]]
        result = extract_batch(model, [self.source()], "cpu")[0].fingerprint
        self.assertEqual(result.inference_runs, 2)
        self.assertTrue(result.expected_count_match)

    def test_prediction_and_class_map_failures_close_decoded_images(self):
        for failure in (RuntimeError("inference failed"), "wrong_map"):
            image = Image.new("RGB", (1000, 800))
            model = MagicMock()
            if isinstance(failure, RuntimeError):
                model.predict.side_effect = failure
            else:
                value = prediction(points())
                value.names[0] = "wrong"
                model.predict.return_value = [value]
            with patch("elobot.features.bases.fingerprints.extractor.decode_image", return_value=image):
                with self.assertRaises((RuntimeError, FingerprintError)):
                    extract_batch(model, [self.source()], "cpu")
            with self.assertRaises(ValueError):
                image.getpixel((0, 0))

    def test_invalid_image_in_mixed_batch_preserves_other_image_identity(self):
        missing = self.source().model_copy(update={"path": str(self.path.parent / "missing.png")})
        model = MagicMock()
        model.predict.return_value = [prediction(points())]
        bad, good = extract_batch(model, [missing, self.source()], "cpu")
        self.assertEqual(bad.error, "source_unavailable")
        self.assertEqual(good.fingerprint.source_revision, 1)
        self.assertEqual(good.fingerprint.inference_runs, 3)

    def test_recorded_legacy_job_uses_its_own_verified_calibration_settings(self):
        model = MagicMock()

        def predict(**kwargs):
            value = (prediction(points(53), (("district_hall", .2, .2),)) if kwargs["conf"] == .25 else
                     prediction(points(53)))
            return [value for _ in kwargs["source"]]

        model.predict.side_effect = predict
        with patch.dict(PREVIOUS_MANIFEST, confidence=.4):
            legacy, current = extract_batch(model, [self.source(version=1), self.source()], "cpu")
        self.assertIsNone(legacy.error)
        self.assertEqual(legacy.fingerprint.confidence_threshold, .25)
        self.assertEqual(current.fingerprint.confidence_threshold, .4)
        self.assertEqual(current.fingerprint.inference_runs, 1)
