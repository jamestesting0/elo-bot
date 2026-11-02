import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from pymongo.errors import PyMongoError

from cogs.base_store import repository, storage
from common import utils
from database_models import Clan, Match
from tests._live_base_fixture import LiveBaseFixture


class TestLiveBaseCleanup(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = LiveBaseFixture()
        await self.f.setup(self)
        self.message = self.f.add_pair()
        self.message.embeds = [ipy.Embed(
            title="**Bases Stored**",
            fields=[ipy.EmbedField(name="**Slots**", value="<@111> — Slot 1\n<@222> — Slot 2")],
        )]
        self.f.document["participants"][0]["base_store"]["stored_at"] = 900
        self.f.document["participants"][1]["base_store"]["stored_at"] = 1100

    def assert_only_older_slot_released(self, name="Player 222"):
        self.assertIsNone(self.f.document["participants"][0]["base_store"])
        self.assertIsNotNone(self.f.document["participants"][1]["base_store"])
        self.assertEqual(self.message.embeds[0].title, "**Base Storage Incomplete**")
        self.assertEqual(self.message.embeds[0].fields[0].value,
                         f"**[{name}](<https://discordapp.com/users/222>)** — Slot 2")
        self.assertEqual([b.label for b in self.message.components[0].components], ["Undo"])

    async def test_reconciliation_keeps_newer_slot_and_shared_recovery_control(self):
        with patch.object(repository, "latest_completed_raid_weekend_end_timestamp", return_value=1000):
            await repository.clear_expired_base_store_reservations([await self.f.find({})], bot=self.f.bot)
        self.assert_only_older_slot_released()

    async def test_weekend_task_keeps_newer_slot_and_shared_recovery_control(self):
        self.f.channel.guild.fetch_member.side_effect = None
        self.f.channel.guild.fetch_member.return_value = None
        async def matches():
            return [await self.f.find({})]
        with (
            patch.object(utils, "latest_completed_raid_weekend_end_timestamp", return_value=1000),
            patch.object(Match, "find", return_value=MagicMock(to_list=AsyncMock(side_effect=matches))),
            patch.object(Clan, "find_all", return_value=MagicMock(to_list=AsyncMock(return_value=[]))),
        ):
            await utils.clear_all_base_store_slots(bot=self.f.bot)
        self.assert_only_older_slot_released("❌Invalid Player")

    async def test_confirmed_thread_deletion_releases_pair_without_helper_or_message_edits(self):
        with patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=self.f.slots)):
            await storage.clear_match_base_stores(await self.f.find({}), reset_stored_flags=True,
                                                 reason="thread_deleted", bot=self.f.bot)
        self.assertTrue(all(p["base_store"] is None for p in self.f.document["participants"]))
        self.message.edit.assert_not_awaited()
        self.assertFalse(self.f.http.create_payloads)

    async def test_match_wide_release_edits_shared_live_message_once_per_phase(self) -> None:
        with patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=self.f.slots)):
            await storage.clear_match_base_stores(
                await self.f.find({}), reset_stored_flags=True, reason="undone", bot=self.f.bot,
            )

        self.assertTrue(all(participant["base_store"] is None for participant in self.f.document["participants"]))
        self.assertEqual(self.message.edit.await_count, 2)
        self.assertEqual(self.message.embeds[0].title, "**Base Storage Undone**")
        self.assertEqual(self.message.embeds[0].fields[0].value, "<@111> — Slot 1\n<@222> — Slot 2")
        self.assertEqual(self.message.components, [])

    async def test_slot_sync_failure_restores_shared_undo_for_retained_slot(self) -> None:
        error = PyMongoError("slot sync failed")
        with patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=error)):
            with self.assertRaises(PyMongoError) as raised:
                await storage.clear_match_base_stores(
                    await self.f.find({}), reset_stored_flags=True, reason="undone", bot=self.f.bot,
                )
        self.assertIs(raised.exception, error)
        self.assert_only_older_slot_released()

    async def test_second_release_conflict_restores_shared_undo_for_retained_slot(self) -> None:
        original = Match.clear_base_store

        async def clear(match, player, **kwargs):
            return False if player == 1 else await original(match, player, **kwargs)

        with patch.object(Match, "clear_base_store", new=clear), patch.object(
            storage, "sync_base_store_slots", new=AsyncMock(side_effect=self.f.slots),
        ):
            with self.assertRaises(storage.BaseStoreReleaseConflict):
                await storage.clear_match_base_stores(
                    await self.f.find({}), reset_stored_flags=True, reason="undone", bot=self.f.bot,
                )
        self.assert_only_older_slot_released()

    async def test_cancelled_cleanup_restores_shared_undo_before_propagating(self) -> None:
        cancelled = anyio.get_cancelled_exc_class()
        with patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=cancelled())):
            with self.assertRaises(cancelled):
                await storage.clear_match_base_stores(
                    await self.f.find({}), reset_stored_flags=True, reason="undone", bot=self.f.bot,
                )
        self.assert_only_older_slot_released()

    async def test_failed_final_publication_keeps_committed_release_and_logs_recovery_context(self) -> None:
        original_edit = self.message.edit.side_effect
        error = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))

        async def edit(**kwargs):
            if self.message.edit.await_count == 2:
                raise error
            return await original_edit(**kwargs)

        self.message.edit.side_effect = edit
        with patch.object(storage, "sync_base_store_slots", new=AsyncMock(side_effect=self.f.slots)):
            with self.assertLogs(level="ERROR") as logs, self.assertRaises(ipy.errors.HTTPException) as raised:
                await storage.clear_match_base_stores(
                    await self.f.find({}), reset_stored_flags=True, reason="undone", bot=self.f.bot,
                )
        self.assertIs(raised.exception, error)
        self.assertTrue(all(p["base_store"] is None for p in self.f.document["participants"]))
        self.assertIn(f"message {self.message.id} for match LIVE", logs.output[0])
        self.assertEqual(self.message.embeds[0].title, "**Releasing Base Storage**")
        self.assertEqual(self.message.components, [])
