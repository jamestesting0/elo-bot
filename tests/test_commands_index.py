import subprocess
import sys
import unittest
from pathlib import Path

from tests._contract_snapshots import snapshot_env

REPO = Path(__file__).resolve().parents[1]


class TestCommandsIndex(unittest.TestCase):
    def test_commands_index_is_current(self) -> None:
        result = subprocess.run([sys.executable, "scripts/commands_index.py", "--check"], cwd=REPO, env=snapshot_env(),
                                capture_output=True, text=True, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr[-2000:])
