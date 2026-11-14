from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.features.base_store import storage
from elobot.features.base_store.publication import mark_store_reserved
from elobot.features.hosting.live import live_start
from elobot.features.hosting.war import war_start
from elobot.config.ids import MATCH_CHANNEL
from elobot.db import BaseStore
from tests import _match_publication_fixture as publication_tests


class TestStartReservationRetention(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.case = publication_tests.MatchPublicationState(self)
        self.case.setUp()
        self.messages = {}
        self.public_channel = self.case.bot.cache.place_channel_data({
            "id": str(MATCH_CHANNEL), "type": 0, "name": "public", "guild_id": "50",
        })
        self.case.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: (
            self.public_channel if int(channel_id) == MATCH_CHANNEL else self.case.ctx.channel
        ))
        self.case.bot.http.get_message = AsyncMock(side_effect=lambda _channel, message: deepcopy(self.messages[message]))
        self.case.bot.http.edit_message = AsyncMock(side_effect=self.edit_message)

    async def edit_message(self, payload, channel_id, message_id, **kwargs):
        if message_id not in self.messages:
            return await self.case.edit_message(payload, channel_id, message_id, **kwargs)
        self.messages[message_id].update(deepcopy(payload))
        return deepcopy(self.messages[message_id])

    def add_store(self, player: int, *, manual: bool = False) -> BaseStore:
        store = BaseStore(
            clan_tag="#CLAN", slot=f"Slot {player + 1}", reservation_id=f"HELD{player}", message_id=90 + player, ready=True,
        )
        self.case.match.participants[player].base_store = store
        self.case.match.participants[player].opponent_bases_stored = True
        self.case.match._save_state()
        self.case.persisted.update({f"participants.{player}.base_store.{key}": value for key, value in store.model_dump().items()})
        message = deepcopy(self.case.message_data)
        undo_prefix = "undo_base_storage" if manual else "helper_stored_undo"
        message.update({"id": str(store.message_id), "embeds": [{"title": "**Bases Stored**"}], "components": [{
            "type": 1, "components": [
                {"type": 2, "style": 2, "label": "Start Match", "custom_id": f"helper_stored_start|TOKEN|{player}|HELD{player}"},
                {"type": 2, "style": 4, "label": "Undo", "custom_id": f"{undo_prefix}|TOKEN|{player}|HELD{player}"},
            ],
        }]})
        self.messages[store.message_id] = message
        return store

    def assert_store_retained(self, player: int, store: BaseStore, undo_prefix: str, *, ready: bool = False) -> None:
        self.assertIs(self.case.match.participants[player].base_store, store)
        self.assertTrue(self.case.match.participants[player].opponent_bases_stored)
        self.assertIs(store.ready, ready)
        self.assertEqual((store.clan_tag, store.slot, store.reservation_id, store.message_id),
                         ("#CLAN", f"Slot {player + 1}", f"HELD{player}", 90 + player))
        message = self.messages[store.message_id]
        self.assertEqual(message["embeds"][0]["title"], "**Bases Stored**")
        self.assertEqual([component["custom_id"] for row in message["components"] for component in row["components"]],
                         [f"helper_stored_start|TOKEN|{player}|HELD{player}", f"{undo_prefix}|TOKEN|{player}|HELD{player}"])
        for deletion in self.case.bot.http.delete_message.await_args_list:
            self.assertNotEqual(deletion.args[1], store.message_id)
        storage.clear_participant_base_store.assert_not_awaited()

    async def test_started_war_keeps_reserved_store_and_original_undo(self) -> None:
        store = self.add_store(0)
        await war_start.complete_war_start(self.case.bot, self.case.ctx, self.case.war_request())
        self.assert_store_retained(0, store, "helper_stored_undo")
        self.assertEqual(self.case.match.times, [1000, None])
        self.assertEqual(self.case.match.old_msgs, [43, None])
        war_start.match_commands.remove_helper_task.assert_awaited_once()

    async def complete_live(self) -> None:
        request = self.case.live_request()
        self.case.match.msg = "80"
        self.case.persisted["msg"] = "80"
        public = deepcopy(self.case.message_data)
        public.update({"id": "80", "channel_id": str(MATCH_CHANNEL), "embeds": [{"fields": [
            {"name": "Players", "value": "100 vs 200"}, {"name": "Time", "value": "1000"},
        ]}]})
        self.messages[80] = public
        with patch.object(live_start.match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.case.match))), \
                patch.object(live_start, "schedule_channel_name_edit"):
            await live_start.complete_live_start(self.case.bot, self.case.ctx, request)

    async def test_started_live_keeps_both_slots_and_original_undo_controls(self) -> None:
        stores = [self.add_store(0), self.add_store(1, manual=True)]
        await self.complete_live()
        self.assert_store_retained(0, stores[0], "helper_stored_undo")
        self.assert_store_retained(1, stores[1], "undo_base_storage")
        live_start.match_commands.clear_match_base_stores.assert_not_awaited()
        self.assertEqual(self.case.match.times, [1000, None])

    async def test_stored_message_delivery_failure_retains_durable_start_and_slot(self) -> None:
        store = self.add_store(0)

        async def reject_stored_edit(payload, channel_id, message_id, **kwargs):
            if message_id == 90:
                raise ipy.errors.HTTPException(MagicMock(status=503))
            return await self.edit_message(payload, channel_id, message_id, **kwargs)

        self.case.bot.http.edit_message.side_effect = reject_stored_edit
        with self.assertRaises(ipy.errors.HTTPException):
            await war_start.complete_war_start(self.case.bot, self.case.ctx, self.case.war_request())
        self.assertIs(self.case.match.participants[0].base_store, store)
        self.assertEqual(self.case.match.times, [1000, None])
        self.assertEqual(self.case.match.old_msgs, [43, None])
        self.assertFalse(store.ready)
        self.case.ctx.send.assert_not_awaited()
        storage.clear_participant_base_store.assert_not_awaited()

    async def test_war_abort_keeps_reservation_and_repaints_original_undo_message(self) -> None:
        store = self.add_store(0)
        await war_start.complete_war_start(self.case.bot, self.case.ctx, self.case.war_request())
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        with patch.object(war_start.match_commands, "free_clan", new=AsyncMock()) as free, \
                patch.object(war_start, "schedule_channel_name_edit"):
            self.assertTrue(await war_start.abort_war_start(ctx, war_start.WarAbort(self.case.match, 0, message)))
        self.assert_store_retained(0, store, "helper_stored_undo", ready=True)
        self.assertEqual(self.case.match.times, [None, None])
        free.assert_awaited_once_with("#CLAN", expected_thread="42")

    async def test_replacement_during_retained_message_edit_does_not_confirm_old_ownership(self) -> None:
        self.add_store(0)

        async def replace_during_edit(payload, channel_id, message_id, **kwargs):
            result = await self.edit_message(payload, channel_id, message_id, **kwargs)
            self.case.persisted["participants.0.base_store.reservation_id"] = "NEW"
            return result

        self.case.bot.http.edit_message.side_effect = replace_during_edit
        result = await mark_store_reserved(self.case.bot, self.case.match, 0)
        self.assertFalse(result)
        self.assertEqual(self.case.persisted["participants.0.base_store.reservation_id"], "NEW")
        self.assertEqual(self.messages[90]["components"], [])
        self.assertEqual(self.messages[90]["embeds"][0]["title"], "**Base Storage Unavailable**")
        self.assertFalse(self.messages[90]["embeds"][0].get("description"))

    async def test_live_abort_keeps_both_reservations_and_undo_messages(self) -> None:
        stores = [self.add_store(0), self.add_store(1, manual=True)]
        await self.complete_live()
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        self.case.persisted.update({"times": list(self.case.match.times), "hosts": list(self.case.match.hosts),
                                    "host_clans": list(self.case.match.host_clans), "old_msgs": list(self.case.match.old_msgs)})
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        with patch.object(live_start.match_commands, "free_clan", new=AsyncMock()) as free, \
                patch.object(live_start, "schedule_channel_name_edit"):
            self.assertTrue(await live_start.abort_live_start(ctx, live_start.LiveAbort(self.case.match, message)))
        self.assert_store_retained(0, stores[0], "helper_stored_undo", ready=True)
        self.assert_store_retained(1, stores[1], "undo_base_storage", ready=True)
        self.assertEqual(self.case.match.times, [None, None])
        free.assert_awaited_once_with("#CLAN", expected_thread="42")

    async def test_live_abort_retires_public_and_initiation_after_commit(self) -> None:
        await self.complete_live()
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        self.case.persisted.update({"times": list(self.case.match.times), "hosts": list(self.case.match.hosts),
                                    "host_clans": list(self.case.match.host_clans), "old_msgs": list(self.case.match.old_msgs)})
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        original_edit = self.case.bot.http.edit_message.side_effect

        async def assert_committed_edit(payload, channel_id, message_id, **kwargs):
            self.assertEqual(self.case.persisted["times"], [None, None])
            self.assertEqual(self.case.persisted["old_msgs"], [43, None])
            return await original_edit(payload, channel_id, message_id, **kwargs)

        self.case.bot.http.edit_message.side_effect = assert_committed_edit
        with patch.object(live_start.match_commands, "free_clan", new=AsyncMock()), \
                patch.object(live_start, "schedule_channel_name_edit"):
            self.assertTrue(await live_start.abort_live_start(ctx, live_start.LiveAbort(self.case.match, message)))
        for data in (self.case.message_data, self.messages[80]):
            self.assertEqual(data["embeds"][0]["title"], "**Match Aborted**")
            self.assertEqual(data["components"], [])
        self.assertEqual(self.messages[80]["embeds"][0]["color"], 0xBE0000)
        self.assertEqual(self.case.match.old_msgs, [43, None])
        self.case.bot.http.delete_message.assert_not_awaited()

    async def test_live_abort_edit_failure_preserves_retry_refs_and_slots(self) -> None:
        stores = [self.add_store(0), self.add_store(1)]
        await self.complete_live()
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        self.case.persisted.update({"times": list(self.case.match.times), "hosts": list(self.case.match.hosts),
                                    "host_clans": list(self.case.match.host_clans), "old_msgs": list(self.case.match.old_msgs)})
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        original_edit = self.case.bot.http.edit_message.side_effect

        async def reject_initiation(payload, channel_id, message_id, **kwargs):
            if message_id == 43:
                raise ipy.errors.HTTPException(MagicMock(status=503))
            return await original_edit(payload, channel_id, message_id, **kwargs)

        self.case.bot.http.edit_message.side_effect = reject_initiation
        with patch.object(live_start.match_commands, "free_clan", new=AsyncMock()), \
                patch.object(live_start, "schedule_channel_name_edit"):
            with self.assertRaises(ipy.errors.HTTPException):
                await live_start.abort_live_start(ctx, live_start.LiveAbort(self.case.match, message))
        self.assertEqual(self.case.persisted["times"], [None, None])
        self.assertEqual(self.case.persisted["old_msgs"], [43, None])
        self.assertEqual(self.case.match.msg, "80")
        self.assertEqual(self.messages[80]["embeds"][0]["title"], "**Match Aborted**")
        self.assertEqual(self.messages[80]["embeds"][0]["color"], 0xBE0000)
        self.assertEqual(self.messages[80]["components"], [])
        for player, store in enumerate(stores):
            self.assert_store_retained(player, store, "helper_stored_undo", ready=True)

    async def test_war_abort_edit_failure_preserves_ref_and_reserved_slot(self) -> None:
        store = self.add_store(0)
        await war_start.complete_war_start(self.case.bot, self.case.ctx, self.case.war_request())
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        original_edit = self.case.bot.http.edit_message.side_effect

        async def reject_initiation(payload, channel_id, message_id, **kwargs):
            if message_id == 43:
                raise ipy.errors.HTTPException(MagicMock(status=503))
            return await original_edit(payload, channel_id, message_id, **kwargs)

        self.case.bot.http.edit_message.side_effect = reject_initiation
        with patch.object(war_start.match_commands, "free_clan", new=AsyncMock()), \
                patch.object(war_start, "schedule_channel_name_edit"):
            with self.assertRaises(ipy.errors.HTTPException):
                await war_start.abort_war_start(ctx, war_start.WarAbort(self.case.match, 0, message))
        self.assertEqual(self.case.persisted["times.0"], None)
        self.assertEqual(self.case.persisted["old_msgs.0"], 43)
        self.assert_store_retained(0, store, "helper_stored_undo", ready=True)

    async def test_stale_live_abort_does_not_retire_newer_publication(self) -> None:
        await self.complete_live()
        self.case.match.persistent_host_clans = list(self.case.match.host_clans)
        self.case.persisted.update({"times": [2000, None], "hosts": list(self.case.match.hosts),
                                    "host_clans": list(self.case.match.host_clans), "old_msgs": [44, None]})
        message = self.case.bot.cache.place_message_data(deepcopy(self.case.message_data))
        ctx = MagicMock(spec=ipy.ComponentContext, bot=self.case.bot, channel=self.case.ctx.channel)
        self.case.bot.http.edit_message.reset_mock()
        with patch.object(live_start.match_commands, "free_clan", new=AsyncMock()) as free:
            self.assertFalse(await live_start.abort_live_start(ctx, live_start.LiveAbort(self.case.match, message)))
        self.case.bot.http.edit_message.assert_not_awaited()
        free.assert_not_awaited()
        self.assertEqual(self.case.persisted["old_msgs"], [44, None])

    async def test_live_restart_replaces_aborted_public_status(self) -> None:
        await self.complete_live()
        self.messages[80]["embeds"][0].update({"title": "**Match Aborted**", "description": "Aborted",
                                               "color": 0xBE0000})
        self.messages[80]["components"] = []
        request = self.case.live_request()
        with patch.object(live_start.match_commands, "Match", new=MagicMock(find_one=AsyncMock(return_value=self.case.match))), \
                patch.object(live_start, "schedule_channel_name_edit"):
            await live_start.complete_live_start(self.case.bot, self.case.ctx, request)
        self.assertEqual(self.messages[80]["embeds"][0]["title"], "**Match Started**")
        self.assertEqual(self.messages[80]["embeds"][0]["color"], 0x9EDE73)
        self.assertIn("Spectate", self.messages[80]["embeds"][0]["description"])
        self.assertEqual(self.messages[80]["components"][0]["components"][0]["custom_id"],
                         "live_spectate|TOKEN|1000")
