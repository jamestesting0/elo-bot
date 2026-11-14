import subprocess
import sys
import textwrap
import unittest

from elobot.app.extensions import EXTENSIONS


class TestCogImports(unittest.TestCase):
    def test_extensions_and_hosting_helpers_import_independently(self) -> None:
        modules = sorted(set(EXTENSIONS) | {
            "elobot.features.hosting.war.result_store",
            "elobot.features.hosting.war.result_store_task",
            "elobot.features.hosting.war.result_store_ui",
            "elobot.features.hosting.war.helper_store",
            "elobot.features.hosting.war.stored_bases",
            "elobot.features.hosting.war.helper_base_store",
            "elobot.features.hosting.war.helper_base_store_availability",
            "elobot.features.hosting.war.helper_reinvite",
            "elobot.features.hosting.war.war_start",
            "elobot.features.hosting.war.helper_host",
            "elobot.features.base_submission.models",
            "elobot.features.base_submission.parsing",
            "elobot.features.base_submission.presentation",
            "elobot.features.base_store",
            "elobot.features.base_store.models",
            "elobot.features.base_store.storage",
            "elobot.features.base_store.presentation",
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
