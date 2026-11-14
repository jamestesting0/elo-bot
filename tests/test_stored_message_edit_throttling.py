import unittest
from unittest.mock import patch

from elobot.features.base_store.live_publication import publish_live_storage
from elobot.features.base_store.publication import mark_store_reserved, mark_store_unavailable
from elobot.features.base_store.messages import StoredBaseMessageRetirement, retire_stored_base_message
from elobot.discord import message_edits
from tests._live_base_fixture import LiveBaseFixture


class TestStoredMessageEditThrottling(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.f = LiveBaseFixture()
        await self.f.setup(self)
        self.message = self.f.add_pair()
        self.message._channel_id = 900
        self.clock = 0.0
        self.edit_times: list[float] = []
        self.enterContext(patch.object(message_edits, "_now", new=lambda: self.clock))
        self.enterContext(patch.object(message_edits, "_sleep", new=self.sleep))
        original_edit = self.message.edit.side_effect

        async def edit(**kwargs):
            self.edit_times.append(self.clock)
            return await original_edit(**kwargs)

        self.message.edit.side_effect = edit

    async def sleep(self, delay: float) -> None:
        self.clock += delay

    async def test_reservation_edits_do_not_wait_on_the_channel_budget(self) -> None:
        match = await self.f.find({})
        self.assertTrue(await publish_live_storage(self.f.runtime, self.f.ctx, match))
        for _ in range(3):
            self.assertTrue(await mark_store_reserved(self.f.bot, await self.f.find({}), 0))
        await mark_store_unavailable(self.message)
        await retire_stored_base_message(
            self.f.bot, StoredBaseMessageRetirement("900", self.message.id, "releasing"),
        )

        self.assertEqual(self.edit_times, [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(self.message.embeds[0].title, "**Releasing Base Storage**")
        self.assertEqual(self.message.components, [])
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
