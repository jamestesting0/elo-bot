import json
import subprocess
import sys
import unittest
from pathlib import Path

from tests._contract_snapshots import SNAPSHOT_DIR, SNAPSHOTS, snapshot_env

REPO = Path(__file__).resolve().parents[1]


def first_difference(expected, actual, path="$"):
    if type(expected) is not type(actual):
        return f"{path}: {str(expected)[:200]!r} -> {str(actual)[:200]!r}"
    if isinstance(expected, dict):
        for key in dict.fromkeys([*expected, *actual]):
            if key not in expected or key not in actual:
                return f"{path}.{key}: {'added' if key not in expected else 'removed'}"
            found = first_difference(expected[key], actual[key], f"{path}.{key}")
            if found:
                return found
        return None
    if isinstance(expected, list):
        for index, (a, b) in enumerate(zip(expected, actual)):
            found = first_difference(a, b, f"{path}[{index}]")
            if found:
                return found
        return f"{path}: length {len(expected)} -> {len(actual)}" if len(expected) != len(actual) else None
    return None if expected == actual else f"{path}: {expected!r} -> {actual!r}"


class TestContractSnapshots(unittest.TestCase):
    def assert_matches_snapshot(self, kind: str) -> None:
        result = subprocess.run([sys.executable, "-m", "tests._contract_snapshots", kind], cwd=REPO, env=snapshot_env(),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        expected = json.loads((SNAPSHOT_DIR / SNAPSHOTS[kind]).read_text())
        actual = json.loads(result.stdout)
        self.assertTrue(expected == actual, f"{kind} changed at {first_difference(expected, actual)}; if intended, run "
                                            "`python -m tests._contract_snapshots --update` and review the diff")

    def test_interaction_surface_matches_snapshot(self) -> None:
        self.assert_matches_snapshot("surface")

    def test_persistence_contract_matches_snapshot(self) -> None:
        self.assert_matches_snapshot("persistence")
