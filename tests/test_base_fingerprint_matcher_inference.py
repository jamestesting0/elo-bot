import random
import unittest

import numpy as np

from elobot.features.bases.fingerprints.geometry import Geometry
from elobot.features.bases.fingerprints.matcher import assign, score_layout
from elobot.features.bases.fingerprints.types import BaseFingerprint, PREVIOUS_SAHI_PIPELINE_ID
from tests._relative_geometry_fixture import layout, points


class TestFingerprintMatcher(unittest.TestCase):
    def test_all_districts_formats_halls_and_isotropic_resize(self):
        for district in range(9):
            for first, second in ((1, 1), (1, 2), (2, 2)):
                query = layout(version=first, district=district)
                candidate = layout(version=second, district=district, width=1600, height=1200)
                result = score_layout(query, candidate, False)
                with self.subTest(district=district, formats=(first, second)):
                    self.assertEqual((result.status, result.similarity, result.matched, result.total),
                                     ("matched", 100, 30, 30))

    def test_translation_scale_missing_extra_and_moved_defenses(self):
        query = layout()
        for data, expected in (([(n, x * .8 + .1, y * .8 + .05) for n, x, y in points()], 100),
                               (points()[:-4], 86.7),
                               ([*points(), ("inferno_tower", .4, .4)], 96.8),
                               ([*points()[:-1], (points()[-1][0], .75, .72)], 96.7)):
            with self.subTest(expected=expected):
                result = score_layout(query, layout(data), False)
                self.assertEqual(result.status, "matched")
                self.assertEqual(result.similarity, expected)
                self.assertEqual(result.similarity, score_layout(layout(data), query, False).similarity)

    def test_fuzzy_merges_only_cannon_and_spear(self):
        original = points()
        changed = [("spear" if n == "cannon" else n, x, y) for n, x, y in original]
        self.assertEqual(score_layout(layout(), layout(changed), False).similarity, 80)
        self.assertEqual(score_layout(layout(), layout(changed), True).similarity, 100)
        changed = [("air_defence" if n == "cannon" else n, x, y) for n, x, y in original]
        self.assertEqual(score_layout(layout(), layout(changed), True).similarity, 80)

    def test_hall_changes_permutations_and_pair_reversal_do_not_change_score(self):
        data = points()
        random.Random(81).shuffle(data)
        original = layout(hall=True)
        duplicate = original.model_dump()
        duplicate["buildings"].append(duplicate["buildings"][-1])
        duplicate.update(anchor_found=False, anchor_type=None, total_detections=32,
                         pipeline_id=PREVIOUS_SAHI_PIPELINE_ID, extractor_revision="elo-3")
        for query in (layout(), layout(data), BaseFingerprint.model_validate(duplicate)):
            result = score_layout(query, original, False)
            self.assertEqual(result.similarity, 100)
            self.assertEqual(result.total, 30)
            self.assertEqual(result.model_dump(), score_layout(original, query, False).model_dump())

    def test_hard_negatives_and_insufficient_evidence_abstain(self):
        original = points()
        cases = {
            "different arrangement": points(seed=113),
            "reflected": [(n, .82 - x, y) for n, x, y in original],
            "rotated": [(n, .82 - y, x) for n, x, y in original],
            "anisotropic": [(n, x, y * .65) for n, x, y in original],
            "scale": [(n, x * .4, y * .4) for n, x, y in original],
            "severe partial": original[:15],
            "collinear": [(n, x, x) for n, x, _ in original],
            "duplicates": [(n, .3, .3) for n, _, _ in original],
            "low diversity": [("cannon", x, y) for _, x, y in original],
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                result = score_layout(layout(), layout(data), False)
                self.assertNotEqual(result.status, "matched")
                self.assertIsNone(result.similarity)

    def test_competing_repeated_grid_alignments_are_ambiguous(self):
        classes = ["cannon", "tesla", "air_bomb", "crusher"]
        grid = [(classes[row % 4], .12 + column * .055, .12 + row * .08)
                for row in range(4) for column in range(12)]
        other = [(n, x, y) for n, x, y in grid if x < .68]
        result = score_layout(layout(grid), layout(other), False)
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.similarity)

    def test_assignment_maximizes_eligible_matches_and_never_reuses_a_point(self):
        query = Geometry(np.array([[0., 0.], [.3, 0.]]), ("cannon",) * 2,
                         {"cannon": np.array([0, 1])}, 1.)
        candidate = Geometry(np.array([[0.1, 0.], [0., .34]]), ("cannon",) * 2,
                             {"cannon": np.array([0, 1])}, 1.)
        result = assign((query, candidate), np.array([1., 0., 0.]))
        self.assertEqual(len(result.query_indices), 2)
        self.assertEqual(len(set(result.candidate_indices)), 2)
        self.assertEqual(list(result.candidate_indices), [1, 0])

    def test_proposal_and_assignment_work_is_bounded(self):
        result = score_layout(layout(points(300)), layout(points(300)), False)
        self.assertEqual(result.similarity, 100)
        self.assertLessEqual(result.proposals, 8192)
        self.assertLessEqual(result.verified, 32)
        self.assertLessEqual(result.assignment_rounds, 96)
        self.assertTrue(result.truncated)
