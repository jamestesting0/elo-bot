import json
from pathlib import Path
import tempfile
import unittest

from common.base_fingerprints.evaluation import Fixture, PairEvaluation, legacy_score, load_fixtures, quality_report
from common.base_fingerprints.types import ExtractionResult, FingerprintError, LayoutMatch
from common.base_fingerprints.types import DISTRICTS
from tests._relative_geometry_fixture import layout
from tests._base_fingerprint_fixture import fingerprint


class TestFingerprintEvaluation(unittest.TestCase):
    def test_offline_legacy_comparison_preserves_original_hall_cutoff_and_fuzzy_rules(self):
        original = fingerprint()
        changed = original.model_copy(update={"buildings": [original.buildings[0],
            original.buildings[1].model_copy(update={"x": .151})]})
        self.assertEqual(legacy_score(original, changed, False), 50)
        changed = original.model_copy(update={"buildings": [original.buildings[0],
            original.buildings[1].model_copy(update={"type": "spear"})]})
        self.assertEqual(legacy_score(original, changed, False), 50)
        self.assertEqual(legacy_score(original, changed, True), 100)
        self.assertIsNone(legacy_score(layout(), original, False))

    def test_paths_cannot_escape_fixture_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixtures.json").write_text(json.dumps([{"file": "../outside.png", "district": "Capital Peak"}]))
            with self.assertRaisesRegex(FingerprintError, "outside_directory"):
                load_fixtures(root)

    def test_split_leakage_and_missing_files_are_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rows = [{"file": f"{index}.png", "district": "Capital Peak", "image_id": str(index),
                     "layout_id": "same", "split": split, "expected_usable": True}
                    for index, split in enumerate(("calibration", "held_out"))]
            (root / "fixtures.json").write_text(json.dumps(rows))
            _, problems = load_fixtures(root)
            self.assertTrue(any("split_leakage" in issue for issue in problems))
            self.assertEqual(sum("missing_file" in issue for issue in problems), 2)

    def test_pair_denominators_abstentions_rank_and_false_positive_gate(self):
        fixtures = [Fixture(file=f"{i}.png", district="Barbarian Camp", image_id=str(i),
                            layout_id=str(i // 2), split="held_out", expected_usable=True) for i in range(4)]
        extractions = [ExtractionResult(fingerprint=layout()) for _ in fixtures]
        pairs = [PairEvaluation(query=0, candidate=i, fuzzy=False, result=LayoutMatch(
            status="matched" if i == 2 else "insufficient", total=30,
            matched=30 if i == 2 else 0, similarity=100 if i == 2 else None,
            residual=0 if i == 2 else None)) for i in (1, 2, 3)]
        report = quality_report(fixtures, extractions, pairs, [])
        strict = report["modes"]["held_out/strict"]
        self.assertEqual(strict["positive_pairs"], 1)
        self.assertEqual(strict["negative_pairs"], 2)
        self.assertEqual(strict["accepted_negatives"], 1)
        self.assertEqual(strict["missed_positives"], 1)
        self.assertEqual(strict["abstentions"], 2)
        self.assertEqual(strict["precision"], 0)
        self.assertEqual(strict["recall"], 0)
        self.assertEqual(strict["rankings"][0]["correct_rank"], None)
        self.assertFalse(strict["passed"])
        self.assertEqual(report["quality_gate"], "blocked")

    def test_unlabeled_inputs_never_produce_accuracy_claim(self):
        fixtures = [Fixture(file="one.png", district="Capital Peak")]
        report = quality_report(fixtures, [ExtractionResult(error="insufficient_layout_evidence")], [], [])
        self.assertEqual(report["quality_gate"], "smoke_only")
        self.assertNotIn("modes", report)
        self.assertEqual(report["extraction_failures"], 1)

    def test_complete_labeled_matrix_passes_only_with_correct_rankings_and_no_negatives(self):
        fixtures = [Fixture(file=f"{district}-{split}-{base}-{capture}.png", district=district,
                            image_id=f"{district}-{split}-{base}-{capture}",
                            layout_id=f"{district}-{split}-{base}", split=split, expected_usable=True,
                            hall_condition="natural_missing")
                    for district in DISTRICTS for split in ("calibration", "held_out")
                    for base in range(2) for capture in range(2)]
        extractions = [ExtractionResult(fingerprint=layout(district=DISTRICTS[f.district])) for f in fixtures]
        pairs = []
        for q, query in enumerate(fixtures):
            for c, candidate in enumerate(fixtures):
                if q == c or query.district != candidate.district or query.split != candidate.split:
                    continue
                positive = query.layout_id == candidate.layout_id
                for fuzzy in (False, True):
                    pairs.append(PairEvaluation(query=q, candidate=c, fuzzy=fuzzy, result=LayoutMatch(
                        status="matched" if positive else "insufficient", total=30,
                        matched=30 if positive else 0, similarity=100 if positive else None,
                        residual=0 if positive else None)))
        report = quality_report(fixtures, extractions, pairs, [])
        self.assertEqual(report["quality_gate"], "passed")
        self.assertEqual(report["modes"]["held_out/strict"]["recall"], 1)
        rejected = [p.model_copy(update={"result": LayoutMatch(status="insufficient", total=30)}) for p in pairs]
        report = quality_report(fixtures, extractions, rejected, [])
        self.assertEqual(report["quality_gate"], "blocked")
        self.assertEqual(report["modes"]["held_out/strict"]["recall"], 0)
        self.assertEqual(quality_report(fixtures, extractions, pairs[:-1], [])["quality_gate"], "blocked")
