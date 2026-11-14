import subprocess
import sys
import textwrap
import unittest

from elo_bot.app.extensions import EXTENSIONS


class TestCogImports(unittest.TestCase):
    def test_extensions_and_hosting_helpers_import_independently(self) -> None:
        modules = sorted(set(EXTENSIONS) | {
            "elo_bot.features.hosting.war.result_store",
            "elo_bot.features.hosting.war.result_store_task",
            "elo_bot.features.hosting.war.result_store_ui",
            "elo_bot.features.hosting.war.helper_store",
            "elo_bot.features.hosting.war.stored_bases",
            "elo_bot.features.hosting.war.helper_base_store",
            "elo_bot.features.hosting.war.helper_base_store_availability",
            "elo_bot.features.hosting.war.helper_reinvite",
            "elo_bot.features.hosting.war.war_start",
            "elo_bot.features.hosting.war.helper_host",
            "elo_bot.features.base_submission.models",
            "elo_bot.features.base_submission.parsing",
            "elo_bot.features.base_submission.presentation",
            "elo_bot.features.base_store",
            "elo_bot.features.base_store.models",
            "elo_bot.features.base_store.storage",
            "elo_bot.features.base_store.presentation",
        })
        script = textwrap.dedent("""
            import importlib
            import os
            import sys

            # Share third-party import cost, never project module state.
            for name in ("interactions", "coc", "beanie", "numpy", "PIL.Image"):
                importlib.import_module(name)
            assert not any(
                name.split(".")[0] in {"cogs", "common", "elo_bot"}
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
