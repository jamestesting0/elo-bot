import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from bson import BSON
from PIL import Image
from pydantic import ValidationError

from elobot.features.bases.fingerprints.artifacts import MANIFEST
from elobot.features.bases.fingerprints.extractor import decode_image, extract_batch
from elobot.features.bases.fingerprints.runtime import selected_device
from elobot.features.bases.fingerprints.search import eligible
from elobot.features.bases.fingerprints.types import BaseFingerprint, Building, DISTRICTS, ExtractionState, FingerprintError, ImageInput
from elobot.db import BaseLayout


from tests._base_fingerprint_fixture import fingerprint

class TestFingerprintModels(unittest.TestCase):
    def test_legacy_defaults_and_source_eligibility(self):
        base = BaseLayout.model_construct(district="Barbarian Camp", image_url="old")
        self.assertEqual(base.image_revision, 0)
        self.assertIsNone(base.fingerprint)
        base.fingerprint = fingerprint()
        self.assertTrue(eligible(base))
        base.fingerprint_extraction = ExtractionState(source_revision=0, status="failed")
        self.assertTrue(eligible(base))
        base.image_revision = 1
        self.assertFalse(eligible(base))
        base.image_revision = 0
        base.district = "Wizard Valley"
        self.assertFalse(eligible(base))

    def test_malformed_layouts_fail_and_bson_stays_structured(self):
        document = fingerprint().model_dump()
        self.assertIsInstance(BSON.encode(document), bytes)
        for updates in ({"total_detections": 3}, {"buildings": []}, {"anchor_found": False},
                        {"model_sha256": "invalid"}, {"district_id": 0}):
            with self.subTest(updates=updates), self.assertRaises(ValidationError):
                BaseFingerprint.model_validate(document | updates)
        for field, value in (("x", float("nan")), ("y", float("inf")), ("conf", 1.01)):
            with self.assertRaises(ValidationError):
                Building.model_validate({"type": "cannon", "x": 0, "y": 0, "conf": 1} | {field: value})
        duplicated = copy.deepcopy(document)
        duplicated["buildings"][1] = duplicated["buildings"][0]
        with self.assertRaises(ValidationError):
            BaseFingerprint.model_validate(duplicated)

    def test_device_selection_and_mapping(self):
        self.assertEqual(selected_device(1), "cpu")
        for size in (2, 8, 32):
            self.assertEqual(selected_device(size), "cuda:0")
        for size in (True, 0, 33, 1.5):
            with self.assertRaises(FingerprintError):
                selected_device(size)
        self.assertEqual(DISTRICTS["Wizard Valley"], 2)
        self.assertEqual(sorted(DISTRICTS.values()), list(range(9)))


class TestFingerprintExtraction(unittest.TestCase):
    def test_calibration_preserves_batch_association_tail_device_and_actual_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = [Path(directory) / f"{index}.png" for index in range(2)]
            for path in paths:
                Image.new("RGB", (100, 100)).save(path)
            inputs = [ImageInput(path=str(path), source_revision=index, source_image_url=f"https://x/{index}",
                                 district_id=1) for index, path in enumerate(paths)]
            model = MagicMock()
            calls = []

            def predict(**kwargs):
                calls.append(kwargs)
                count = 54 if len(calls) == 1 else 53
                results = []
                for index in range(len(kwargs["source"])):
                    size = count if index == 0 else 52
                    rows = [[40, 40, 60, 60, 0.99, 7]] + [[60, 60, 70, 70, 0.9, 4]] * (size - 1)
                    data = SimpleNamespace(cpu=lambda rows=rows: SimpleNamespace(tolist=lambda: rows))
                    results.append(SimpleNamespace(names=dict(enumerate(MANIFEST["classes"])),
                                                   boxes=SimpleNamespace(data=data)))
                return results

            model.predict.side_effect = predict
            results = extract_batch(model, inputs, "cuda:0")
            self.assertEqual([len(call["source"]) for call in calls], [2, 1, 1])
            self.assertTrue(all(call["device"] == "cuda:0" for call in calls))
            self.assertEqual([item.fingerprint.source_revision for item in results], [0, 1])
            self.assertEqual([item.fingerprint.inference_runs for item in results], [1, 3])
            self.assertTrue(results[0].fingerprint.expected_count_match)
            self.assertFalse(results[1].fingerprint.expected_count_match)
            self.assertEqual(results[1].fingerprint.warnings, [])

    def test_bad_image_and_hall_fail_without_fabricating_layout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.png"
            path.write_bytes(b"not an image")
            with self.assertRaises(FingerprintError):
                decode_image(path)
            Image.new("RGB", (100, 100)).save(path)
            model = MagicMock()
            data = MagicMock()
            data.cpu.return_value.tolist.return_value = []
            model.predict.return_value = [SimpleNamespace(names=dict(enumerate(MANIFEST["classes"])),
                                                          boxes=SimpleNamespace(data=data))]
            source = ImageInput(path=str(path), source_revision=0, source_image_url="x", district_id=1)
            result = extract_batch(model, [source], "cpu")[0]
            self.assertIsNone(result.fingerprint)
            self.assertEqual(result.error, "missing_or_ambiguous_hall")
