import unittest
from unittest.mock import MagicMock

import interactions as ipy

from cogs.match_commands import MatchCommands


class TestMatchAutomationConcurrency(unittest.IsolatedAsyncioTestCase):
    async def test_commands_key_single_concurrency_by_channel_and_user(self) -> None:
        for command in (
                MatchCommands.match_war_autostart,
                MatchCommands.match_result_autostore,
        ):
            with self.subTest(command=command.resolved_name):
                # Given
                contexts = [
                    MagicMock(spec=ipy.SlashContext, channel=MagicMock(id=100), author=MagicMock(id=200)),
                    MagicMock(spec=ipy.SlashContext, channel=MagicMock(id=100), author=MagicMock(id=300)),
                    MagicMock(spec=ipy.SlashContext, channel=MagicMock(id=400), author=MagicMock(id=200)),
                ]

                # When
                keys = [await command.max_concurrency.bucket(context) for context in contexts]

                # Then
                self.assertEqual(1, command.max_concurrency.concurrent)
                self.assertEqual([(100, 200), (100, 300), (400, 200)], keys)
