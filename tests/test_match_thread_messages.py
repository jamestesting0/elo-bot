import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from elobot.features.matches.thread_messages import find_match_thread_announcement
from elobot.db import Match


class TestMatchThreadMessages(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def message(message_id: int, token: str = "TOKEN") -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id, author=SimpleNamespace(id=999), embeds=[ipy.Embed()],
            components=[ipy.ActionRow(ipy.Button(
                style=ipy.ButtonStyle.GRAY, label="Submit Result", custom_id=f"submit_button|{token}",
            ))],
        )

    def setUp(self) -> None:
        self.match = Match.model_construct(token="TOKEN", matched_time=1, thread="123")
        self.channel = MagicMock()
        self.channel.fetch_message = AsyncMock()
        self.channel.fetch_pinned_messages = AsyncMock(return_value=[])

    async def test_persisted_id_takes_precedence_over_other_pins(self) -> None:
        self.match.thread_message_id = 456
        message = self.message(456)
        self.channel.fetch_message.return_value = message
        self.channel.fetch_pinned_messages.return_value = [self.message(789)]

        result = await find_match_thread_announcement(self.channel, self.match, 999)

        self.assertIs(result, message)
        self.channel.fetch_message.assert_awaited_once_with(456)
        self.channel.fetch_pinned_messages.assert_not_awaited()
        self.channel.history.assert_not_called()

    async def test_legacy_match_uses_first_pin_verified_for_its_token(self) -> None:
        first = self.message(456)
        self.channel.fetch_pinned_messages.return_value = [self.message(100, "OTHER"), first, self.message(789)]

        result = await find_match_thread_announcement(self.channel, self.match, 999)

        self.assertIs(result, first)
        self.channel.fetch_message.assert_not_awaited()
        self.channel.history.assert_not_called()

    async def test_deleted_stored_message_falls_back_to_a_verified_pin(self) -> None:
        self.match.thread_message_id = 456
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(SimpleNamespace(status=404, reason="Not Found"))
        replacement = self.message(789)
        self.channel.fetch_pinned_messages.return_value = [replacement]

        result = await find_match_thread_announcement(self.channel, self.match, 999)

        self.assertIs(result, replacement)
        self.assertEqual(self.match.thread_message_id, 456)

    async def test_temporary_lookup_error_does_not_trigger_fallback_or_resend(self) -> None:
        self.match.thread_message_id = 456
        self.channel.fetch_message.side_effect = ipy.errors.DiscordError(
            SimpleNamespace(status=503, reason="Service Unavailable"),
        )

        with self.assertRaises(ipy.errors.DiscordError):
            await find_match_thread_announcement(self.channel, self.match, 999)

        self.channel.fetch_pinned_messages.assert_not_awaited()
        self.channel.history.assert_not_called()
