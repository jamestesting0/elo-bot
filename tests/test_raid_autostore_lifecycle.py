from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.base_store import raid_task
from elobot.db import Match
from tests._raid_autostore_fixture import RaidAutostoreFixture
from tests._stored_start_fixture import terminal_job


class TestRaidAutostoreLifecycle(RaidAutostoreFixture):
    async def test_success_is_persisted_before_real_discord_controls_are_enabled(self) -> None:
        await self.store()

        store = self.persisted["participants"][0]["base_store"]
        self.assertTrue(store["ready"])
        self.assertEqual(store["message_id"], 900)
        self.assertEqual(self.trace, ["claim", "pending", "bind", "helper", "bind", "edit", "notice"])
        message = self.stored_message()
        self.assertEqual(message["embeds"][0]["title"], "**Bases Stored**")
        controls = message["components"][0]["components"]
        self.assertEqual([control["custom_id"] for control in controls],
                         [f"helper_stored_start|RAID|0|{store['reservation_id']}",
                          f"helper_stored_undo|RAID|0|{store['reservation_id']}"])
        self.assertFalse(message["embeds"][0]["description"])
        self.interactive.assert_not_awaited()
        self.availability.assert_not_awaited()
        self.assertEqual(self.helper.await_args.args[1], "999")
        self.assertEqual(len(self.helper.await_args.args[2].base_links), 3)

    async def test_started_opponent_side_is_left_alone_while_this_side_is_stored(self) -> None:
        self.persisted["times"][1], self.persisted["hosts"][1] = 123, "888"

        await self.store()

        self.assertTrue(self.persisted["participants"][0]["base_store"]["ready"])
        self.assertIsNone(self.persisted["participants"][1]["base_store"])
        self.assertEqual((self.persisted["times"], self.persisted["hosts"]), ([None, 123], [None, "888"]))
        claim = self.collection.update_one.await_args_list[0].args[0]
        self.assertEqual((claim["times.1"], claim["hosts.1"]), (123, "888"))
        self.assertEqual(self.stored_message()["embeds"][0]["title"], "**Bases Stored**")

    async def test_start_during_base_selection_prevents_reservation_and_helper_work(self) -> None:
        async def start_during_selection(_match, _player):
            self.persisted["hosts"][1] = "888"

        self.runtime._base_store_choose_bases.side_effect = start_during_selection

        await self.store()

        self.assert_unreserved()
        self.collection.update_one.assert_not_awaited()
        self.assertEqual(len(self.messages), 1)
        self.assertFalse(self.stored_message()["components"])

    async def test_atomic_claim_rejects_a_start_committed_after_the_last_read(self) -> None:
        async def start_before_claim(query, update):
            self.persisted["times"][1] = 123
            return await self.update(query, update)

        self.collection.update_one.side_effect = start_before_claim

        await self.store()

        self.assert_unreserved()
        query = self.collection.update_one.await_args.args[0]
        self.assertIsNone(query["times.1"])
        self.assertIsNone(query["hosts.1"])
        self.assertFalse(self.stored_message()["components"])

    async def test_incomplete_generated_bases_do_not_consume_a_slot(self) -> None:
        self.runtime._base_store_choose_bases.side_effect = None

        await self.store()

        self.assert_unreserved()
        self.collection.update_one.assert_not_awaited()

    async def test_capacity_is_rechecked_after_base_selection(self) -> None:
        async def consume_capacity(match, player):
            await self.choose(match, player)
            tags = list(self.slots)
            self.slots = {tags[0]: [], tags[1]: ["Slot 1"], tags[2]: ["Slot 2"]}

        self.runtime._base_store_choose_bases.side_effect = consume_capacity

        await self.store()

        self.assert_unreserved()
        self.collection.update_one.assert_not_awaited()
        self.assertEqual(sum(map(len, self.slots.values())), 2)

    async def test_failed_persistence_never_requests_helper_or_publishes_success(self) -> None:
        self.collection.update_one.side_effect = PyMongoError("write failed")

        with self.assertRaises(PyMongoError):
            await self.store()

        self.assert_unreserved()
        self.assertEqual(self.messages, {})

    async def test_helper_failure_retains_exact_slot_with_only_undo_enabled(self) -> None:
        self.helper.side_effect = None
        self.helper.return_value = terminal_job("store_bases", "failed")

        await self.store()

        store = self.persisted["participants"][0]["base_store"]
        self.assertIsNotNone(store)
        self.assertFalse(store["ready"])
        self.assertEqual(store["message_id"], 900)
        message = self.stored_message()
        self.assertEqual(message["embeds"][0]["title"], "**Base Storage Incomplete**")
        self.assertEqual([control["custom_id"] for control in message["components"][0]["components"]],
                         [f"helper_stored_undo|RAID|0|{store['reservation_id']}"])
        self.assertTrue(all(str(SUCCESS_EMOJI) not in message.get("content", "") for message in self.messages.values()))

    async def test_publication_failure_preserves_committed_storage_and_reports_incomplete(self) -> None:
        async def fail_first_edit(payload, channel_id, message_id, *, files=None):
            if self.bot.http.edit_message.await_count == 1:
                raise ipy.errors.Forbidden(AsyncMock(status=403, reason="Forbidden"))
            return await self.edit_message(payload, channel_id, message_id)

        self.bot.http.edit_message.side_effect = fail_first_edit

        with self.assertLogs(level="ERROR"):
            await self.store()

        store = self.persisted["participants"][0]["base_store"]
        self.assertIsNotNone(store)
        self.assertFalse(store["ready"])
        self.assertEqual(self.stored_message()["embeds"][0]["title"], "**Base Storage Incomplete**")
        self.assertEqual(len(self.stored_message()["components"][0]["components"]), 1)

    async def test_deleted_match_cannot_publish_success_after_helper_completes(self) -> None:
        async def deleted_during_helper(*_args):
            self.alive = False
            return terminal_job("store_bases", "succeeded")

        self.helper.side_effect = deleted_during_helper

        await self.store()

        self.assertFalse(self.alive)
        self.assertEqual(self.stored_message()["embeds"][0]["title"], "**Base Storage Unavailable**")
        self.assertEqual(self.stored_message()["components"], [])

    async def test_duplicate_run_preserves_existing_reservation_and_message(self) -> None:
        await self.store()
        before = self.persisted["participants"][0]["base_store"].copy()
        self.helper.reset_mock()
        self.bot.http.create_message.reset_mock()
        self.collection.update_one.reset_mock()

        await self.store()

        self.assertEqual(self.persisted["participants"][0]["base_store"], before)
        self.helper.assert_not_awaited()
        self.bot.http.create_message.assert_not_awaited()
        self.collection.update_one.assert_not_awaited()

    async def test_busy_side_is_skipped_without_changing_messages_or_storage(self) -> None:
        self.runtime._base_store_try_reserve_side = AsyncMock(return_value=None)

        await self.store()

        self.assert_unreserved()
        self.assertEqual(self.messages, {})

    async def test_failed_pending_message_binding_never_requests_helper(self) -> None:
        async def decline_binding(query, update):
            if "participants.0.base_store.message_id" in update["$set"]:
                return await self.update({"token": "CHANGED"}, update)
            return await self.update(query, update)

        self.collection.update_one.side_effect = decline_binding

        await self.store()

        store = self.persisted["participants"][0]["base_store"]
        self.assertIsNotNone(store)
        self.assertFalse(store["ready"])
        self.helper.assert_not_awaited()
        self.assertEqual(self.stored_message()["embeds"][0]["title"], "**Base Storage Unavailable**")
        self.assertEqual(self.stored_message()["components"], [])

    async def test_cancellation_retains_bound_pending_message_and_releases_action_lock(self) -> None:
        entered = anyio.Event()

        async def parked_helper(*_args):
            entered.set()
            await anyio.sleep_forever()

        self.helper.side_effect = parked_helper

        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.store)
                await entered.wait()
                tasks.cancel_scope.cancel()

        store = self.persisted["participants"][0]["base_store"]
        self.assertFalse(store["ready"])
        self.assertEqual(store["message_id"], 900)
        self.assertEqual(self.stored_message()["embeds"][0]["title"], "**Base Storage**")
        self.assertEqual(self.stored_message()["components"], [])
        lease = await self.runtime._base_store_try_reserve_side("RAID", 0)
        self.assertIsNotNone(lease)
        lease.release()

    async def test_registered_task_stores_opponents_separately_through_real_discord_http(self) -> None:
        snapshot = await self.find({"token": "RAID"})

        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[snapshot]))), \
                patch.object(raid_task.random, "shuffle", side_effect=lambda players: None):
            await self.runtime.raid_weekend_auto_store.callback.func(self.runtime)

        stores = [participant["base_store"] for participant in self.persisted["participants"]]
        self.assertTrue(all(store["ready"] for store in stores))
        self.assertNotEqual(stores[0]["clan_tag"], stores[1]["clan_tag"])
        self.assertEqual(self.helper.await_count, 2)
        self.assertEqual(sum(map(len, (await self.slot_map()).values())), 4)
        self.assertTrue(all(self.messages[str(store["message_id"])]["embeds"][0]["title"] == "**Bases Stored**"
                            for store in stores))
        self.assertEqual(self.log_text(), (
            "Checked **1** match(es) with an unstarted side.\n"
            f"Stored <#100> side 1 in `{stores[0]['clan_tag']}` {stores[0]['slot']}.\n"
            f"Stored <#100> side 2 in `{stores[1]['clan_tag']}` {stores[1]['slot']}.\n"
        ))

    async def test_registered_task_randomly_selects_one_player_without_consuming_reserve(self) -> None:
        self.slots = {tag: ["Slot 1"] for tag in self.slots}
        self.helper.side_effect = None
        self.helper.return_value = terminal_job("store_bases", "succeeded")
        snapshot = await self.find({"token": "RAID"})

        with patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(return_value=[snapshot]))), \
                patch.object(raid_task.random, "shuffle", side_effect=lambda players: players.reverse()):
            await self.runtime.raid_weekend_auto_store.callback.func(self.runtime)

        self.assertIsNone(self.persisted["participants"][0]["base_store"])
        self.assertTrue(self.persisted["participants"][1]["base_store"]["ready"])
        slots = await self.slot_map()
        self.assertEqual(sum(map(len, slots.values())), 2)
        self.assertEqual(sum(map(bool, slots.values())), 2)
        self.helper.assert_awaited_once()
