import subprocess
import sys
import textwrap
import unittest

from elobot.app.extensions import get_extensions


class TestCogImports(unittest.TestCase):
    def test_extensions_and_hosting_helpers_import_independently(self) -> None:
        modules = sorted(set(get_extensions("cogs")) | {
            "cogs.war_hosting.result_store",
            "cogs.war_hosting.result_store_task",
            "cogs.war_hosting.result_store_ui",
            "cogs.war_hosting.helper_store",
            "cogs.war_hosting.stored_bases",
            "cogs.war_hosting.helper_base_store",
            "cogs.war_hosting.helper_base_store_availability",
            "cogs.war_hosting.helper_reinvite",
            "cogs.war_hosting.war_start",
            "cogs.war_hosting.helper_host",
            "cogs.base_submission.models",
            "cogs.base_submission.parsing",
            "cogs.base_submission.presentation",
            "cogs.base_store",
            "cogs.base_store.models",
            "cogs.base_store.storage",
            "cogs.base_store.presentation",
        })
        script = textwrap.dedent("""
            import importlib
            import os
            import sys

            # Share third-party import cost, never project module state.
            for name in ("interactions", "coc", "beanie", "numpy", "PIL.Image"):
                importlib.import_module(name)
            assert not any(
                name.split(".")[0] in {"cogs", "common", "elobot"}
                for name in sys.modules
            )
            for name in sys.argv[1:]:
                child = os.fork()
                if child == 0:
                    importlib.import_module(name)
                    os._exit(0)
                _, status = os.waitpid(child, 0)
                if status:
                    raise SystemExit(f"Fresh import failed: {name} ({status})")
        """)
        result = subprocess.run(
            [sys.executable, "-c", script, *modules],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
