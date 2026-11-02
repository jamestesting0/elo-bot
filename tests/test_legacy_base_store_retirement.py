import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from cogs.base_store import repository, storage
from common.utils import clear_all_base_store_slots
from database_models import BaseStore, Match, Participant


class TestLegacyBaseStoreRetirement(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Match, "_document_settings", DocumentSettings(
            name="matches", use_state_management=True,
        ))
        settings.start()
        self.addCleanup(settings.stop)
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 1", stored_at=100)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="42",
            participants=[Participant(user_id="100", opponent_bases_stored=True, base_store=self.store)],
        )
        self.match._save_state()
        self.trace: list[str] = []
        self.collection = MagicMock(update_one=AsyncMock(side_effect=self._clear))
        collection = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection.start()
        self.addCleanup(collection.stop)
        self.bot = ipy.Client()
        self.bot._user = ipy.ClientUser.from_dict({
            "id": "60", "username": "EloBot", "discriminator": "0", "avatar": None, "bot": True, "verified": True,
        }, self.bot)
        self.bot.cache.place_channel_data({"id": "42", "type": 0, "name": "match", "guild_id": "50"})
        self.bot.http.get_channel = AsyncMock(return_value={
            "id": "42", "type": 0, "name": "match", "guild_id": "50",
        })
        self.messages = {43: self._message(43, ["helper_stored_start|TOKEN|0", "helper_stored_undo|TOKEN|0"])}
        self.bot.http.get_channel_messages = AsyncMock(side_effect=self._history)
        self.bot.http.get_message = AsyncMock(
            side_effect=lambda _channel, message: deepcopy(self.messages[int(message)]),
        )
        self.bot.http.edit_message = AsyncMock(side_effect=self._edit)

    def _message(self, message_id: int, controls: list[str], author: str = "60"):
        return {
            "id": str(message_id), "channel_id": "42", "guild_id": "50",
            "author": {"id": author, "username": "EloBot", "discriminator": "0", "avatar": None, "bot": True},
            "content": "<@100>", "timestamp": "2026-09-06T00:00:00+00:00", "type": 0,
            "embeds": [{"title": "**Bases Stored**", "description": "Ready to start.", "color": 123}],
            "components": [{"type": 1, "components": [
                {"type": 2, "style": 2, "label": "Start", "custom_id": custom_id} for custom_id in controls
            ]}] if controls else [],
        }

    def _history(self, _channel: int, limit: int, **kwargs):
        self.trace.append("history")
        before = kwargs.get("before")
        return [
            deepcopy(self.messages[message_id]) for message_id in sorted(self.messages, reverse=True)
            if not before or message_id < int(before)
        ][:limit]

    def _edit(self, payload, _channel: int, message_id: int, **_kwargs) -> None:
        title = payload["embeds"][0]["title"]
        self.trace.append(f"edit:{message_id}:{title}")
        self.messages[message_id].update(deepcopy(payload))

    def _clear(self, *_args):
        self.trace.append("clear")
        return MagicMock(matched_count=1)

    async def test_legacy_expiry_neutralizes_before_clear_and_then_retires_success(self) -> None:
        # Given: a valid expired legacy reservation has no persisted message reference.
        with patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=200):
            # When: the actual expiry path scans and clears that reservation.
            await repository.clear_expired_base_store_reservations([self.match], bot=self.bot)

        # Then: the historical success is disabled before Mongo releases the slot and is finally expired.
        self.assertEqual(self.messages[43]["components"], [])
        self.assertEqual(self.messages[43]["embeds"][0]["title"], "**Base Storage Expired**")
        self.assertLess(self.trace.index("edit:43:**Releasing Base Storage**"), self.trace.index("clear"))
        self.assertLess(self.trace.index("clear"), self.trace.index("edit:43:**Base Storage Expired**"))
        self.assertIsNone(self.match.participants[0].base_store)

    async def test_weekend_reset_retires_legacy_manual_storage(self) -> None:
        # Given: the legacy manual message identifies the exact player, clan and slot.
        self.messages[43] = self._message(43, ["start_match|0|#CLAN|Slot 1", "undo_base_storage|TOKEN|0"])
        clan = MagicMock(tag="#CLAN", available_slots=[], save_changes=AsyncMock())
        with (patch("common.utils.Match") as matches, patch("common.utils.Clan") as clans):
            matches.find.return_value.to_list = AsyncMock(return_value=[self.match])
            clans.find_all.return_value.to_list = AsyncMock(return_value=[clan])

            # When: the actual weekend reset runs.
            await clear_all_base_store_slots(bot=self.bot)

        # Then: the manual success message is retired and its slot is made available.
        self.assertEqual(self.messages[43]["components"], [])
        self.assertEqual(self.messages[43]["embeds"][0]["title"], "**Base Storage Expired**")
        self.assertIn("Slot 1", clan.available_slots)
        self.assertIsNone(self.match.participants[0].base_store)

    async def test_scan_exhausts_history_and_excludes_other_authors_sides_slots_and_nonces(self) -> None:
        # Given: the legacy message is older than 100 messages and new controls refer to other state.
        for message_id in range(1000, 1101):
            self.messages[message_id] = self._message(message_id, [])
        excluded = {
            44: self._message(44, ["helper_stored_start|TOKEN|0|CURRENT"]),
            45: self._message(45, ["helper_stored_undo|TOKEN|1"]),
            46: self._message(46, ["undo_base_storage|OTHER|0"]),
            47: self._message(47, ["start_match|0|#OTHER|Slot 1"]),
            48: self._message(48, ["helper_stored_start|TOKEN|0"], author="61"),
            49: self._message(49, ["helper_stored_start|TOKEN|0|CURRENT", "helper_stored_undo|TOKEN|0"]),
        }
        self.messages.update(deepcopy(excluded))
        with patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock):
            # When: one participant's legacy storage is cleared.
            cleared = await storage.clear_participant_base_store(self.match, 0, bot=self.bot, reason="undone")

        # Then: only its exact legacy message is retired after all history pages were read.
        self.assertTrue(cleared)
        self.assertGreaterEqual(self.bot.http.get_channel_messages.await_count, 3)
        self.assertEqual(self.messages[43]["components"], [])
        for message_id, original in excluded.items():
            self.assertEqual(self.messages[message_id], original)

    async def test_failed_history_keeps_reservation_and_does_not_edit_partial_matches(self) -> None:
        # Given: reading the next history page fails after a candidate message was returned.
        error = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        self.bot.http.get_channel_messages.side_effect = [[deepcopy(self.messages[43])], error]
        with patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock) as sync:
            # When: retirement cannot exhaust the history.
            with self.assertRaises(ipy.errors.Forbidden):
                await storage.clear_participant_base_store(self.match, 0, bot=self.bot, reason="undone")

        # Then: neither the reservation nor its visible message is changed.
        self.collection.update_one.assert_not_awaited()
        self.bot.http.edit_message.assert_not_awaited()
        self.assertIs(self.match.participants[0].base_store, self.store)
        sync.assert_not_awaited()

    async def test_failed_legacy_edit_prevents_expiry_clear(self) -> None:
        # Given: history resolves the legacy message but Discord rejects its neutralization.
        self.bot.http.edit_message.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=200):
            # When: expiry tries to retire the legacy success.
            with self.assertRaises(ipy.errors.HTTPException):
                await repository.clear_expired_base_store_reservations([self.match], bot=self.bot)

        # Then: Mongo still holds the reservation.
        self.collection.update_one.assert_not_awaited()
        self.assertIs(self.match.participants[0].base_store, self.store)

    async def test_replacement_cas_miss_never_retires_the_nonce_message(self) -> None:
        # Given: a replacement reservation and its nonce controls exist while the old snapshot is scanned.
        self.messages[44] = self._message(44, ["helper_stored_start|TOKEN|0|NEW", "helper_stored_undo|TOKEN|0|NEW"])
        replacement_message = deepcopy(self.messages[44])
        self.collection.update_one.side_effect = lambda *_args: MagicMock(matched_count=0)
        with patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock):
            # When: the legacy snapshot loses its compare-and-set race.
            cleared = await storage.clear_participant_base_store(self.match, 0, bot=self.bot, reason="undone")

        # Then: only the old legacy controls are neutralized and the replacement stays untouched.
        self.assertFalse(cleared)
        self.assertEqual(self.messages[43]["components"], [])
        self.assertEqual(self.messages[43]["embeds"][0]["title"], "**Releasing Base Storage**")
        self.assertEqual(self.messages[44], replacement_message)
        self.assertIsNone(self.collection.update_one.await_args.args[0]["participants.0.base_store.reservation_id"])

    async def test_unknown_runtime_reservation_is_not_reconstructed_from_history(self) -> None:
        # Given: only the legacy stored flag remains, without a known clan or slot reservation.
        self.match.participants[0].base_store = None
        self.match._save_state()
        with patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock):
            # When: runtime cleanup has no exact reservation identity to resolve.
            cleared = await storage.clear_participant_base_store(
                self.match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
            )

        # Then: it preserves the flag and neither guesses a message nor clears data.
        self.assertFalse(cleared)
        self.collection.update_one.assert_not_awaited()
        self.bot.http.get_channel_messages.assert_not_awaited()


    async def test_aggregate_clear_conflict_prevents_destructive_continuation(self) -> None:
        # Given: one expected reservation cannot be cleared because its identity changed.
        self.store.message_id = 43
        self.match._save_state()
        self.collection.update_one.side_effect = lambda *_args: MagicMock(matched_count=0)
        with patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock):
            # When: a caller asks to release every reservation before deleting the Match.
            with self.assertRaises(storage.BaseStoreReleaseConflict) as raised:
                await storage.clear_match_base_stores(
                    self.match, reset_stored_flags=True, bot=self.bot, reason="undone",
                )

        # Then: failure propagates with the exact match and participant instead of returning success.
        self.assertEqual(raised.exception.token, "TOKEN")
        self.assertEqual(raised.exception.player, 0)
        self.assertIs(self.match.participants[0].base_store, self.store)

    async def test_aggregate_clear_accepts_empty_participants_as_no_ops(self) -> None:
        # Given: a Match has no remaining reservation or stored flag.
        self.match.participants[0].base_store = None
        self.match.participants[0].opponent_bases_stored = False

        # When: aggregate cleanup is repeated.
        await storage.clear_match_base_stores(
            self.match, reset_stored_flags=True, bot=self.bot, reason="undone",
        )

        # Then: it remains a successful no-op with no database or history request.
        self.collection.update_one.assert_not_awaited()
        self.bot.http.get_channel_messages.assert_not_awaited()




