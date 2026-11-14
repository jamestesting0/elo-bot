import random
import unittest

from pydantic import ValidationError

from elobot.features.bases.fingerprints.matcher import align_layout
from elobot.features.bases.fingerprints.types import (
    BaseFingerprint,
    ComparisonFingerprint,
    ComparisonSummary,
    LayoutMatch,
)
from tests._relative_geometry_fixture import layout, points


class TestComparisonAlignment(unittest.TestCase):
    def compare(self, query: BaseFingerprint, candidate: BaseFingerprint, fuzzy: bool = False):
        from elobot.features.bases.fingerprints.comparison_matcher import align_comparison

        return align_comparison(query, candidate, fuzzy)

    def test_partial_common_layout_keeps_exact_correspondences_below_search_support(self):
        query = layout()
        candidate = layout([*points()[:12], *[("inferno_tower", x, y) for _, x, y in points()[12:]]])
        self.assertNotEqual(align_layout(query, candidate, False).score.status, "matched")

        result = self.compare(query, candidate)

        self.assertEqual(result.score.similarity, 40)
        self.assertEqual(result.pairs, tuple((index, index) for index in range(12)))

    def test_two_shared_classes_can_align_with_fuzzy_grouping(self):
        data = [("cannon" if index % 2 else "tesla", x, y) for index, (_, x, y) in enumerate(points())]
        query = layout(data)
        candidate = layout([("spear" if name == "cannon" else name, x * .8 + .05, y * .8 + .02)
                            for name, x, y in data])

        result = self.compare(query, candidate, True)

        self.assertEqual(result.score.similarity, 100)
        self.assertEqual(result.pairs, tuple((index, index) for index in range(30)))

    def test_matching_region_need_not_cover_seventy_percent_of_layout(self):
        common = [(name, .28 + x * .5, .28 + y * .5) for name, x, y in points(24)]
        extremes = [("inferno_tower", .03, .03), ("inferno_tower", .95, .75)]
        query, candidate = layout([*common, *extremes]), layout(common)
        self.assertNotEqual(align_layout(query, candidate, False).score.status, "matched")

        result = self.compare(query, candidate)

        self.assertEqual(result.score.matched, 24)
        self.assertEqual(result.pairs, tuple((index, index) for index in range(24)))

    def test_noisy_positions_align_without_cross_matching_repeated_types(self):
        names = ["cannon", "tesla", "air_bomb", "crusher", "wizard_tower"]
        original = [(names[row], .15 + column * .085, .15 + row * .085)
                    for row in range(5) for column in range(6)]
        generator = random.Random(18)
        changed = [(name, x + generator.uniform(-.023, .023), y + generator.uniform(-.023, .023))
                   for name, x, y in original]
        query, candidate = layout(original), layout(changed)
        self.assertNotEqual(align_layout(query, candidate, False).score.status, "matched")

        result = self.compare(query, candidate)

        self.assertEqual(result.pairs, tuple((index, index) for index in range(30)))

    def test_one_missing_detection_preserves_indices_after_resize_shuffle_and_reversal(self):
        query = layout(points(53), hall=True)
        candidate = layout([(name, x * .8 + .05, y * .8 + .03)
                            for name, x, y in reversed(points(53)[1:])])
        expected = {(index, 52 - index) for index in range(1, 53)}

        result = self.compare(query, candidate)

        self.assertEqual(result.score.similarity, 98.1)
        self.assertEqual(set(result.pairs), expected)
        reverse = self.compare(candidate, query)
        self.assertEqual(set(reverse.pairs), {(ci, qi) for qi, ci in expected})
        self.assertEqual(reverse.score, result.score)

    def test_equal_competing_alignments_choose_a_deterministic_best_fit(self):
        common = [(name, x * .4, y * .4) for name, x, y in points(24)]
        query = layout(common)
        candidate = layout([*common, *[(name, x + .45, y) for name, x, y in common]])

        result = self.compare(query, candidate)

        self.assertEqual(result.score.status, "matched")
        self.assertEqual(result.score.matched, 24)
        self.assertEqual(len(result.pairs), 24)
        self.assertEqual(len({qi for qi, _ in result.pairs}), 24)
        self.assertEqual(len({ci for _, ci in result.pairs}), 24)
        reverse = self.compare(candidate, query)
        self.assertEqual(set(reverse.pairs), {(ci, qi) for qi, ci in result.pairs})

    def test_single_type_collinear_and_duplicate_layouts_can_be_compared(self):
        cases = [layout(points(1)), layout(points(7)), layout(district=2),
                 layout([(name, .3, .3) for name, _, _ in points()]),
                 layout([(name, x, x) for name, x, _ in points()]),
                 layout([("cannon", x, y) for _, x, y in points()])]
        for query in cases:
            with self.subTest(query=query.buildings[0]):
                result = self.compare(query, query)

                self.assertEqual(result.score.status, "matched")
                self.assertEqual(result.score.matched, len(query.buildings))
                self.assertEqual(result.pairs, tuple((index, index) for index in range(len(query.buildings))))

    def test_no_common_types_returns_zero_matches_for_highlighting_every_building(self):
        query = layout()
        candidate = layout([("inferno_tower", x, y) for _, x, y in points()])

        result = self.compare(query, candidate)

        self.assertEqual((result.score.status, result.score.similarity, result.score.matched), ("matched", 0, 0))
        self.assertEqual(result.pairs, ())
        summary = ComparisonSummary(score=result.score, query_unmatched=30, candidate_unmatched=30)
        self.assertEqual(ComparisonSummary.model_validate_json(summary.model_dump_json()), summary)
        with self.assertRaises(ValidationError):
            LayoutMatch.model_validate(result.score.model_dump())

    def test_empty_detections_can_be_compared_without_changing_persisted_fingerprint_rules(self):
        document = layout().model_dump() | {"total_detections": 0, "buildings": []}
        empty = ComparisonFingerprint.model_validate(document)
        for candidate in (empty, layout()):
            with self.subTest(count=len(candidate.buildings)):
                result = self.compare(empty, candidate)

                self.assertEqual((result.score.status, result.score.matched), ("matched", 0))
                self.assertEqual(result.pairs, ())
                summary = ComparisonSummary(score=result.score, query_unmatched=0,
                                            candidate_unmatched=len(candidate.buildings))
                self.assertEqual(ComparisonSummary.model_validate_json(summary.model_dump_json()), summary)
        with self.assertRaises(ValidationError):
            type(layout()).model_validate(document)

    def test_large_comparison_keeps_assignment_work_bounded_and_all_self_matches(self):
        query = layout(points(300))

        result = self.compare(query, query)

        self.assertEqual(result.score.matched, 300)
        self.assertEqual(result.pairs, tuple((index, index) for index in range(300)))
        self.assertLessEqual(result.score.proposals, 8192)
        self.assertLessEqual(result.score.distinct_proposals, 8192)
        self.assertLessEqual(result.score.verified, 32)
        self.assertLessEqual(result.score.assignment_rounds, 96)


if __name__ == "__main__":
    unittest.main()
