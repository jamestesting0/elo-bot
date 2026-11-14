import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elobot.extensions.match import MatchCommands
from elobot.db import Player
from elobot.features.queue.state import autoqueue_expiry, is_autoqueue_active
from tests._patch_sites import patch_sites


class TestAutoqueueCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author.id = 123
        self.ctx.send = AsyncMock()
        self.command_owner = SimpleNamespace()

    async def invoke_autoqueue(self, enabled: bool, duration: str | None) -> None:
        callback = getattr(MatchCommands.match_autoqueue.callback, "func", MatchCommands.match_autoqueue.callback)
        await callback(self.command_owner, self.ctx, enabled, duration)

    @staticmethod
    def make_player() -> MagicMock:
        player = MagicMock(spec=Player)
        player.auto_queue = "0"
        player.save_changes = AsyncMock()
        return player

    def test_metadata_has_exactly_required_enabled_and_optional_duration(self) -> None:
        options = {str(option.name): option for option in MatchCommands.match_autoqueue.options}

        self.assertEqual(set(options), {"enabled", "duration"})
        self.assertEqual(options["enabled"].type, ipy.OptionType.BOOLEAN)
        self.assertTrue(options["enabled"].required)
        self.assertEqual(options["duration"].type, ipy.OptionType.STRING)
        self.assertFalse(options["duration"].required)
        self.assertEqual(
            [(str(choice.name), choice.value) for choice in options["duration"].choices],
            [("Forever", "Forever"), ("7 Days", "7 Days"), ("3 Days", "3 Days"), ("1 Day", "1 Day")],
        )

    async def test_preferences_persist_exact_expiry(self) -> None:
        for enabled, duration, expected_expiry in (
            (True, "Forever", "9223372036854775807"),
            (True, "7 Days", "2000604800"),
            (True, "3 Days", "2000259200"),
            (True, "1 Day", "2000086400"),
            (False, "7 Days", "0"),
        ):
            with self.subTest(enabled=enabled, duration=duration):
                player = self.make_player()
                with patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model, \
                        patch_sites(('elobot.extensions.match.datetime', 'elobot.extensions.match.queue.datetime', 'elobot.extensions.match.lifecycle.datetime', 'elobot.extensions.match.bases.datetime', 'elobot.extensions.match.hosting.datetime', 'elobot.features.queue.state.datetime', 'elobot.features.queue.matching.datetime', 'elobot.features.matches.resources.datetime', 'elobot.features.matches.lifecycle.datetime', 'elobot.features.matches.attack_bases.datetime', 'elobot.features.results.recording.datetime')) as datetime_model:
                    player_model.find_one = AsyncMock(return_value=player)
                    datetime_model.now.return_value.timestamp.return_value = 2_000_000_000

                    await self.invoke_autoqueue(enabled=enabled, duration=duration)

                self.assertEqual(player.auto_queue, expected_expiry)
                player.save_changes.assert_awaited_once()

    async def test_enable_without_duration_is_rejected_without_persistence(self) -> None:
        player = self.make_player()
        with patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model:
            player_model.find_one = AsyncMock(return_value=player)

            await self.invoke_autoqueue(enabled=True, duration=None)

        self.ctx.send.assert_awaited_once()
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])
        player.save_changes.assert_not_awaited()

    async def test_unregistered_user_is_rejected_without_persistence(self) -> None:
        with patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model:
            player_model.find_one = AsyncMock(return_value=None)

            await self.invoke_autoqueue(enabled=False, duration=None)

        self.ctx.send.assert_awaited_once()
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])

    def test_expiry_helpers_are_integer_deterministic_and_strict_at_boundary(self) -> None:
        now = 2_000_000_000

        self.assertEqual(autoqueue_expiry("Forever", now), "9223372036854775807")
        self.assertEqual(autoqueue_expiry("7 Days", now), "2000604800")
        self.assertEqual(autoqueue_expiry("3 Days", now), "2000259200")
        self.assertEqual(autoqueue_expiry("1 Day", now), "2000086400")
        self.assertFalse(is_autoqueue_active("0", now))
        self.assertFalse(is_autoqueue_active(str(now - 1), now))
        self.assertFalse(is_autoqueue_active(str(now), now))
        self.assertFalse(is_autoqueue_active("not-a-timestamp", now))
        self.assertTrue(is_autoqueue_active(str(now + 1), now))
        self.assertTrue(is_autoqueue_active("9223372036854775807", now))
