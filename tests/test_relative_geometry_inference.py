import unittest

from elobot.features.bases.fingerprints.matcher import score_layout
from tests._relative_geometry_fixture import layout, points


class TestRelativeGeometry(unittest.TestCase):
    def test_legacy_translation_uses_relative_geometry(self):
        candidate = layout(version=1)
        query = layout([(name, x + .08, y + .07) for name, x, y in points()], version=1)
        result = score_layout(query, candidate, False)
        self.assertEqual(result.similarity, 100)
        self.assertEqual(result.matched, 30)

