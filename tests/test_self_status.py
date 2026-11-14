import unittest
from unittest.mock import MagicMock

import interactions as ipy

from elobot.extensions.self_service import SelfCommands


class TestSelfCommandSurface(unittest.TestCase):
    def test_registered_surface_contains_only_supported_commands(self) -> None:
        # Given
        cog = SelfCommands(MagicMock())

        # When
        resolved_names = [command.resolved_name for command in cog.commands if isinstance(command, ipy.SlashCommand)]

        # Then
        self.assertCountEqual(
            resolved_names,
            ["self", "self host", "self host live", "self host war", "self re-invite", "self store",
             "self store base", "self store base-live", "self store result", "self store result-live"],
        )



if __name__ == "__main__":
    unittest.main()
