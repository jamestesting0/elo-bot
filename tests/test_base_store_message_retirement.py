import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from common.base_store.messages import StoredBaseMessageRetirement, retire_stored_base_message


class TestBaseStoreMessageRetirement(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.bot = ipy.Client()
        self.bot.cache.place_channel_data({"id": "42", "type": 0, "name": "match", "guild_id": "50"})
        self.message_data = {
            "id": "43",
            "channel_id": "42",
            "guild_id": "50",
            "author": {"id": "60", "username": "EloBot", "discriminator": "0", "avatar": None},
            "content": "<@100>",
            "timestamp": "2026-09-06T00:00:00+00:00",
            "type": 0,
            "embeds": [{
                "title": "**Bases Stored**",
                "description": "Ready to start.",
                "color": 123,
                "fields": [
                    {"name": "Player", "value": "<@100>", "inline": False},
                    {"name": "**Slots**", "value": "<@100> — Slot 1\n<@200> — Slot 2", "inline": False},
                ],
                "footer": {"text": "EloBot"},
            }],
            "components": [{
                "type": 1,
                "components": [{"type": 2, "style": 2, "label": "Start Match", "custom_id": "stored_start"}],
            }],
        }
        self.get_channel = AsyncMock()
        self.get_message = AsyncMock(side_effect=lambda *_args: deepcopy(self.message_data))
        self.edit_message = AsyncMock(return_value=None)
        self.bot.http.get_channel = self.get_channel
        self.bot.http.get_message = self.get_message
        self.bot.http.edit_message = self.edit_message

    async def test_retirement_reason_replaces_success_and_removes_controls(self) -> None:
        cases = (
            ("releasing", "**Releasing Base Storage**"),
            ("finished", "**Base Storage Released**"),
            ("raid_weekend_ended", "**Base Storage Expired**"),
            ("undone", "**Base Storage Undone**"),
        )
        for reason, title in cases:
            with self.subTest(reason=reason):
                # Given a successful stored-base message with active controls.
                self.get_message.reset_mock()
                self.edit_message.reset_mock()
                request = StoredBaseMessageRetirement("42", 43, reason)

                # When its reservation is retired.
                await retire_stored_base_message(self.bot, request)

                # Then the exact message retains its details but has no active controls.
                self.get_message.assert_awaited_once_with(42, 43)
                self.edit_message.assert_awaited_once()
                payload, channel_id, message_id = self.edit_message.await_args.args
                self.assertEqual((channel_id, message_id), (42, 43))
                self.assertEqual(payload["components"], [])
                embed = payload["embeds"][0]
                self.assertEqual(embed["title"], title)
                self.assertFalse(embed.get("description"))
                self.assertEqual(embed["fields"], self.message_data["embeds"][0]["fields"])
                self.assertEqual(embed["footer"], {"text": "EloBot"})
                self.assertEqual(embed["color"], 123)

    async def test_missing_embed_gets_a_clear_retired_state(self) -> None:
        # Given a stored message whose embed is missing.
        self.message_data["embeds"] = []

        # When its reservation expires.
        await retire_stored_base_message(
            self.bot, StoredBaseMessageRetirement("42", 43, "raid_weekend_ended"),
        )

        # Then the existing message gets the expired state and no controls.
        payload = self.edit_message.await_args.args[0]
        self.assertEqual(payload["embeds"][0]["title"], "**Base Storage Expired**")
        self.assertFalse(payload["embeds"][0].get("description"))
        self.assertEqual(payload["components"], [])

    async def test_missing_legacy_message_reference_does_no_discord_work(self) -> None:
        # Given a legacy reservation with no complete message reference.
        cases = ((None, "42", 43), (self.bot, None, 43), (self.bot, "42", None))
        for bot, thread_id, message_id in cases:
            with self.subTest(thread_id=thread_id, message_id=message_id, bot_present=bot is not None):
                # When the reservation is retired.
                await retire_stored_base_message(bot, StoredBaseMessageRetirement(thread_id, message_id, "undone"))

                # Then no Discord request is needed.
                self.get_channel.assert_not_awaited()
                self.get_message.assert_not_awaited()
                self.edit_message.assert_not_awaited()

    async def test_deleted_message_is_already_retired(self) -> None:
        # Given Discord reports that the stored message was deleted.
        self.get_message.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))

        # When retirement is requested.
        await retire_stored_base_message(self.bot, StoredBaseMessageRetirement("42", 43, "undone"))

        # Then no edit is attempted and deletion is accepted.
        self.edit_message.assert_not_awaited()

    async def test_deleted_channel_is_already_retired(self) -> None:
        # Given Discord reports that the match channel was deleted.
        self.get_channel.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))

        # When retirement is requested for that channel.
        await retire_stored_base_message(self.bot, StoredBaseMessageRetirement("44", 43, "undone"))

        # Then neither message retrieval nor an edit is attempted.
        self.get_message.assert_not_awaited()
        self.edit_message.assert_not_awaited()

    async def test_message_deleted_during_edit_is_already_retired(self) -> None:
        # Given the message disappears between retrieval and edit.
        self.edit_message.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))

        # When retirement is requested.
        await retire_stored_base_message(self.bot, StoredBaseMessageRetirement("42", 43, "undone"))

        # Then the single edit is attempted and deletion is accepted.
        self.edit_message.assert_awaited_once()

    async def test_failed_edit_propagates_without_changing_cached_success(self) -> None:
        # Given Discord refuses the edit while the success message remains visible.
        error = ipy.errors.HTTPException(MagicMock(status=503, reason="Service Unavailable"))
        self.edit_message.side_effect = error

        # When retirement is requested, the original failure reaches the caller.
        with self.assertRaises(ipy.errors.HTTPException) as raised:
            await retire_stored_base_message(self.bot, StoredBaseMessageRetirement("42", 43, "undone"))

        # Then cached message state also remains unchanged.
        self.assertIs(raised.exception, error)
        cached = self.bot.cache.get_message(42, 43)
        self.assertEqual(cached.embeds[0].title, "**Bases Stored**")
        self.assertEqual(cached.embeds[0].description, "Ready to start.")
        self.assertTrue(cached.components)

    async def test_failed_fetch_propagates_without_claiming_retirement(self) -> None:
        # Given Discord refuses to retrieve the stored message.
        error = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        self.get_message.side_effect = error

        # When retirement is requested, the original failure reaches the caller.
        with self.assertRaises(ipy.errors.Forbidden) as raised:
            await retire_stored_base_message(self.bot, StoredBaseMessageRetirement("42", 43, "undone"))

        # Then no edit is attempted.
        self.assertIs(raised.exception, error)
        self.edit_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
