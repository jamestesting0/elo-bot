from itertools import combinations, product
import unittest

import numpy as np

from common.base_fingerprints.geometry import geometry
from common.base_fingerprints.matcher import assign, refine, score_layout, supported
from tests._relative_geometry_fixture import layout, points


def exhaustive_matches(query, candidate):
    q, c = geometry(query, False), geometry(candidate, False)
    maximum = 0
    for first, second in combinations(sorted(q.groups.keys() & c.groups.keys()), 2):
        for qi, qj, ci, cj in product(q.groups[first], q.groups[second], c.groups[first], c.groups[second]):
            u, v = q.points[qj] - q.points[qi], c.points[cj] - c.points[ci]
            if np.linalg.norm(v) == 0 or np.dot(u, v) <= 0:
                continue
            scale = np.linalg.norm(u) / np.linalg.norm(v)
            if not .5 <= scale <= 2:
                continue
            translation = (q.points[qi] + q.points[qj] - scale * (c.points[ci] + c.points[cj])) / 2
            fit = assign((q, c), np.array([scale, *translation]))
            for _ in range(2):
                updated = refine((q, c), fit)
                if updated is None:
                    break
                improved = assign((q, c), updated)
                if improved.order >= fit.order:
                    break
                fit = improved
            if supported((q, c), fit):
                maximum = max(maximum, len(fit.query_indices))
    return maximum


class TestRelativeOracle(unittest.TestCase):
    def test_bounded_search_matches_exhaustive_small_layouts(self):
        query = layout(points(20))
        cases = [points(20), [(n, x * .8 + .07, y * .8 + .06) for n, x, y in points(20)],
                 points(20, seed=9), [(n, .82 - x, y) for n, x, y in points(20)]]
        for data in cases:
            candidate = layout(data)
            expected = exhaustive_matches(query, candidate)
            actual = score_layout(query, candidate, False)
            self.assertEqual(actual.matched, expected)
            self.assertEqual(actual.status == "matched", expected >= 20)

    def test_support_boundary_and_two_dimensional_rejection(self):
        for count in (19, 20):
            value = layout(points(count))
            result = score_layout(value, value, False)
            self.assertEqual(result.status == "matched", count == 20)
        narrow = layout([(n, x, .3 + y * .02) for n, x, y in points()])
        self.assertIsNone(score_layout(narrow, narrow, False).similarity)

    def test_fuzzy_class_floor_uses_canonical_classes(self):
        classes = ["cannon", "spear", "tesla", "air_bomb"]
        value = layout([(classes[i % 4], x, y) for i, (_, x, y) in enumerate(points())])
        self.assertEqual(score_layout(value, value, False).similarity, 100)
        self.assertIsNone(score_layout(value, value, True).similarity)

    def test_small_observation_noise_preserves_known_correspondences(self):
        value = layout(version=1)
        rounded = value.model_dump()
        for building in rounded["buildings"]:
            building["x"] = round(building["x"], 4)
            building["y"] = round(building["y"], 4)
        candidate = type(value).model_validate(rounded)
        result = score_layout(layout(), candidate, False)
        self.assertEqual(result.matched, 30)
        self.assertEqual(result.similarity, 100)
