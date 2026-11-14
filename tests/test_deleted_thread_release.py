import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.features.base_store import storage
from elo_bot.features.base_store import repository
from elo_bot.db import BaseStore
from elo_bot.db.result_models import MatchResultOperation
from tests import _match_cleanup_fixture as fixtures


class TestDeletedThreadRelease(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.case = fixtures.MatchCleanupState(self)
        self.case.setUp()
        for index, participant in enumerate(self.case.match.participants):
            participant.base_store = BaseStore(
                clan_tag="#HELD", slot=f"Slot {index + 1}",
                reservation_id=f"held{index}", message_id=80 + index,
            )
            participant.opponent_bases_stored = True
        self.case.match._save_state()
        self.case.persisted = self.case.match.model_dump(by_alias=True)
        self.case.bot.http.get_channel = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404)))

    async def event(self) -> None:
        await self.case.thread_cog.on_thread_delete.callback(
            self.case.thread_cog, MagicMock(thread=self.case.ctx.channel),
        )

    def assert_released(self) -> None:
        self.assertTrue(all(p.base_store is None and not p.opponent_bases_stored
                            for p in self.case.match.participants))
        self.case.collection.update_one.assert_awaited()

    async def test_deleted_event_releases_both_slots_before_deleting_match(self) -> None:
        await self.event()
        self.assert_released()
        self.assertIsNone(self.case.persisted)
        self.case.free.assert_awaited_once()
        self.case.bot.http.get_channel.assert_not_awaited()

    async def test_deleted_event_returns_both_slots_to_the_clan(self) -> None:
        clan = MagicMock(tag="#HELD", available_slots=[], save_changes=AsyncMock())
        with (
            patch.object(storage, "sync_base_store_slots", new=repository.sync_base_store_slots),
            patch.object(repository.Match, "find", return_value=MagicMock(
                to_list=AsyncMock(return_value=[self.case.match]),
            )),
            patch.object(repository, "Clan", new=MagicMock(find_one=AsyncMock(return_value=clan))),
        ):
            await self.event()
        self.assertEqual(clan.available_slots, ["Slot 1", "Slot 2"])
        self.assertEqual(clan.save_changes.await_count, 2)
        self.assert_released()

    async def test_deleted_event_releases_slots_but_retains_pending_result(self) -> None:
        self.case.match.result_operation = MatchResultOperation(key="pending", response={}, created_at=1)
        self.case.persisted["result_operation"] = self.case.match.result_operation.model_dump()
        await self.event()
        self.assert_released()
        self.assertIsNotNone(self.case.persisted)
        self.assertEqual(self.case.persisted["result_operation"]["key"], "pending")

    async def test_deleted_event_releases_legacy_stored_flags(self) -> None:
        self.case.match.participants[1].base_store = None
        self.case.persisted["participants"][1]["base_store"] = None
        await self.event()
        self.assert_released()

    async def test_deleted_event_does_not_require_a_cached_parent_channel(self) -> None:
        self.case.ctx.channel.parent_channel = None
        await self.event()
        self.assert_released()

    async def test_deleted_event_releases_slots_when_thread_is_an_uncached_id(self) -> None:
        await self.case.thread_cog.on_thread_delete.callback(
            self.case.thread_cog, MagicMock(thread=str(self.case.ctx.channel.id)),
        )
        self.assert_released()
        self.assertIsNone(self.case.persisted)
        self.case.free.assert_awaited_once()

    async def test_confirmed_not_found_releases_both_slots(self) -> None:
        await self.case.match_cog.match_data_clean_up.callback.func(self.case.match_cog)
        self.case.bot.http.get_channel.assert_awaited_once_with(42)
        self.assert_released()
        self.assertIsNone(self.case.persisted)

    async def test_cache_miss_does_not_release_an_existing_thread(self) -> None:
        self.case.bot.http.get_channel.side_effect = None
        self.case.bot.http.get_channel.return_value = {"id": "42"}
        before = deepcopy(self.case.persisted)
        await self.case.match_cog.match_data_clean_up.callback.func(self.case.match_cog)
        self.case.bot.http.get_channel.assert_awaited_once_with(42)
        self.assertEqual(self.case.persisted, before)
        self.case.collection.update_one.assert_not_awaited()

    async def test_forbidden_or_transient_lookup_does_not_release_slots(self) -> None:
        for error in (ipy.errors.Forbidden, ipy.errors.HTTPException):
            with self.subTest(error=error):
                self.case.bot.http.get_channel.side_effect = error(MagicMock(status=503))
                before = deepcopy(self.case.persisted)
                with self.assertLogs(level="WARNING"):
                    await self.case.match_cog.match_data_clean_up.callback.func(self.case.match_cog)
                self.assertEqual(self.case.persisted, before)
                self.case.collection.update_one.assert_not_awaited()
