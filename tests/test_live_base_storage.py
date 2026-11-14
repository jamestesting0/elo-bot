import unittest
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from pymongo.errors import PyMongoError

from elobot.features.base_store import live_service
from elobot.features.base_store import live_publication
from elobot.features.base_store.live import live_pair_ready, ready_live_reservation_id
from tests._live_base_fixture import CLAN, LiveBaseFixture


class TestLiveBaseStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = LiveBaseFixture()
        await self.f.setup(self)

    async def store(self):
        await live_service.store_self_live_bases(self.f.runtime, self.f.ctx, await self.f.find({"token": "LIVE"}))

    async def test_stores_opponents_bases_in_two_slots_then_publishes_both_players(self):
        self.f.jobs(["succeeded", "succeeded"])
        await self.store()
        self.assertEqual(self.f.http.create_payloads, [{
            "type": "store_bases", "requested_by": "111", "payload": {
                "host_clan_tag": CLAN, "slot": index + 1,
                "base_links": [f"https://base.example/{user}/{i}" for i in range(3)],
            },
        } for index, user in enumerate((222, 111))])
        first_write = self.f.writes[0][1]["$set"]
        self.assertIn("participants.0.base_store", first_write)
        self.assertIn("participants.1.base_store", first_write)
        self.assertFalse(first_write["participants.0.base_store"]["ready"])
        stores = [p["base_store"] for p in self.f.document["participants"]]
        self.assertEqual(stores[0]["reservation_id"], stores[1]["reservation_id"])
        self.assertEqual(stores[0]["message_id"], stores[1]["message_id"])
        message = self.f.messages[stores[0]["message_id"]]
        self.assertEqual(message.content, "<@111> <@222>")
        self.assertEqual(message.embeds[0].title, "**Bases Stored**")
        self.assertFalse(self.f.channel.send.await_args.kwargs["embed"].description)
        self.assertFalse(message.embeds[0].description)
        self.assertEqual(message.embeds[0].fields[0].value,
                         "**[Player 111](<https://discordapp.com/users/111>)**\n"
                         "**[Player 222](<https://discordapp.com/users/222>)**")
        self.assertEqual(message.embeds[0].fields[3].value,
                         "**[Player 111](<https://discordapp.com/users/111>)** — Slot 1\n"
                         "**[Player 222](<https://discordapp.com/users/222>)** — Slot 2")
        self.assertEqual([b.label for b in message.components[0].components], ["Start Match", "Undo"])
        self.assertTrue(message.components[0].components[0].custom_id.startswith("helper_stored_live_start|"))
        self.assertTrue(live_pair_ready(await self.f.find({}), stores[0]["reservation_id"]))
        self.assertEqual(ready_live_reservation_id(await self.f.find({})), stores[0]["reservation_id"])
        self.assertEqual((await self.f.slots())[CLAN], [])

    async def test_second_job_failure_retains_both_reservations_without_start(self):
        self.f.jobs(["succeeded", "failed"])
        await self.store()
        stores = [p["base_store"] for p in self.f.document["participants"]]
        self.assertEqual([s["ready"] for s in stores], [True, False])
        self.assertEqual((await self.f.slots())[CLAN], [])
        message = self.f.messages[stores[0]["message_id"]]
        self.assertEqual(message.embeds[0].title, "**Base Storage Incomplete**")
        self.assertFalse(message.embeds[0].description)
        self.assertEqual(message.embeds[0].fields[3].value,
                         "**[Player 111](<https://discordapp.com/users/111>)** — Slot 1\n"
                         "**[Player 222](<https://discordapp.com/users/222>)** — Slot 2")
        self.assertEqual([b.label for b in message.components[0].components], ["Undo"])

    async def test_first_job_cancel_prevents_second_job_but_retains_pair(self):
        self.f.jobs(["cancelled"])
        await self.store()
        self.assertEqual(len(self.f.http.create_payloads), 1)
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))

    async def test_failed_claim_prevents_helper_jobs(self):
        self.f.write.side_effect = PyMongoError("offline")
        await self.store()
        self.assertFalse(self.f.http.create_payloads)
        self.assertTrue(all(p["base_store"] is None for p in self.f.document["participants"]))

    async def test_publication_failure_keeps_successful_reservations_and_retry_recovers(self):
        self.f.jobs(["succeeded", "succeeded"])
        self.f.channel.send.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        await self.store()
        self.assertTrue(all(p["base_store"]["ready"] for p in self.f.document["participants"]))
        self.f.channel.send.side_effect = self.f.send_public
        await self.store()
        self.assertEqual(len(self.f.http.create_payloads), 2)
        self.assertEqual(len(self.f.channel.send.await_args_list), 2)
        self.assertIn("Live stored-base controls were restored", self.f.ctx.send.await_args.args[0])

    async def test_no_clan_with_both_slots_prevents_storage(self):
        self.f.runtime._base_store_sync_slots = AsyncMock(return_value={CLAN: ["Slot 2"]})
        await self.store()
        self.assertFalse(self.f.http.create_payloads)
        self.assertFalse(self.f.writes)

    async def test_stale_pair_during_publication_cannot_enable_start(self):
        message = self.f.add_pair()
        source = await self.f.find({})
        original = message.edit.side_effect
        async def edit(**kwargs):
            result = await original(**kwargs)
            if kwargs.get("components"):
                self.f.document["participants"][1]["base_store"] = None
            return result
        message.edit.side_effect = edit
        self.assertFalse(await live_publication.publish_live_storage(self.f.runtime, self.f.ctx, source))
        self.assertEqual(message.components, [])
        self.assertEqual(message.embeds[0].title, "**Base Storage Unavailable**")
        self.assertFalse(message.embeds[0].description)


    async def test_recovery_during_active_storage_does_not_change_message_binding(self):
        self.f.add_pair()
        lease = await self.f.runtime._base_store_try_reserve_side("LIVE", 1)
        try:
            await self.store()
        finally:
            lease.release()
        self.assertFalse(self.f.http.create_payloads)
        self.assertFalse(self.f.writes)
        self.assertIn("already in progress", self.f.ctx.send.await_args.args[0])

    async def test_ready_live_reservation_id_requires_the_named_pair(self):
        match = await self.f.find({})
        self.assertIsNone(ready_live_reservation_id(match))
        self.f.add_pair()
        match = await self.f.find({})
        pair = match.participants[0].base_store.reservation_id
        self.assertEqual(ready_live_reservation_id(match), pair)
        self.assertEqual(ready_live_reservation_id(match, pair), pair)
        self.assertIsNone(ready_live_reservation_id(match, "a" * 16))
        match.participants[1].base_store.ready = False
        self.assertIsNone(ready_live_reservation_id(match, pair))
