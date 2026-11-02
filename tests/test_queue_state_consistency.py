import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from beanie import PydanticObjectId
from beanie.odm.fields import LinkInfo, LinkTypes
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue

from cogs.match_commands import add_player_to_queue, normalize_queue_timestamps, remove_player_from_queue
from database_models import Player, Queue


class TestQueueStateConsistency(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Player, Queue):
            settings = patch.object(model, "_document_settings", DocumentSettings(
                name=model.__name__, use_state_management=True,
                state_management_replace_objects=getattr(model.Settings, "state_management_replace_objects", False),
            ))
            settings.start()
            self.addCleanup(settings.stop)
        collection = patch.object(Player, "get_pymongo_collection", return_value=SimpleNamespace(name="players"))
        collection.start()
        self.addCleanup(collection.stop)
        links = patch.object(Queue, "_link_fields", {
            "players": LinkInfo(field_name="players", lookup_field_name="players", document_class=Player,
                                link_type=LinkTypes.LIST),
        })
        links.start()
        self.addCleanup(links.stop)
        self.player = Player.model_construct(id="111", user_id="111", tag="#A", elo=1000, highest_elo=1000)
        self.queue = Queue.model_construct(
            id=PydanticObjectId(), tier=3, players=[self.player], entered_at={"111": 10.0},
        )
        self.queue._save_state()

    async def _updates(self) -> dict[str, JsonValue]:
        database = AsyncMock(return_value=self.queue)
        with patch.object(Queue, "set", new=database):
            await self.queue.save_changes()
        database.assert_awaited_once()
        return database.await_args.args[0]

    async def test_dequeue_removes_timestamp_so_rejoin_gets_a_new_age(self) -> None:
        # Given: Mongo contains an existing membership and its initial enqueue time.
        persisted = deepcopy(self.queue.get_saved_state())

        # When: the real removal helper and model save persist a dequeue.
        remove_player_from_queue(self.queue, "111")
        updates = await self._updates()
        persisted.update(updates)

        # Then: reloading and rejoining cannot reuse the old timestamp.
        reloaded = Queue.model_construct(
            id=self.queue.id, tier=3, players=persisted["players"], entered_at=persisted["entered_at"],
        )
        add_player_to_queue(reloaded, self.player, entered_at=1000.0)
        self.assertEqual(reloaded.entered_at, {"111": 1000.0})

    async def test_normalization_clears_orphan_timestamps_from_storage(self) -> None:
        # Given: a legacy queue has a timestamp for a player who is no longer queued.
        self.queue.entered_at["222"] = 1.0
        self.queue._save_state()

        # When: normalization removes the orphan entry.
        self.assertTrue(normalize_queue_timestamps(self.queue, now=1000.0))
        updates = await self._updates()

        # Then: the exact surviving map is persisted, including its deletion.
        self.assertEqual(updates, {"entered_at": {"111": 10.0}})

    async def test_timestamp_addition_keeps_unchanged_memberships_out_of_the_write(self) -> None:
        # Given: a queued player lacks its timestamp.
        self.queue.entered_at = {}
        self.queue._save_state()

        # When: normalization fills the timestamp.
        self.assertTrue(normalize_queue_timestamps(self.queue, now=1000.0))
        updates = await self._updates()

        # Then: only the timestamp map is replaced.
        self.assertEqual(updates, {"entered_at": {"111": 1000.0}})
