import unittest
from unittest.mock import AsyncMock, patch

import anyio

from elobot.extensions import match as match_commands
from elobot.features.base_store.models import BaseStorePreparationFailure, PreparedBaseStore


from tests._match_cleanup_fixture import ThreadReservationStateMixin

class TestThreadReservationSerialization(ThreadReservationStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_button_rechecks_a_reservation_created_during_countdown(self) -> None:
        await self.reserve_during_countdown("button")

    async def test_slash_rechecks_a_reservation_created_during_countdown(self) -> None:
        await self.reserve_during_countdown("slash")

    async def test_expiry_rechecks_a_reservation_created_during_activity_refresh(self) -> None:
        async def refresh(**_kwargs):
            if self.case.channel.fetch_messages.await_count == 2:
                self.assertIsInstance(await self.reserve(), PreparedBaseStore)
            return [self.old_message]
        self.case.channel.fetch_messages = AsyncMock(side_effect=refresh)
        await self.invoke("expiry")
        self.case.channel.delete.assert_not_awaited()
        self.assertTrue(self.case.runtime.alive)
        self.assertIsNotNone(self.case.runtime.fixture.match["participants"][0]["base_store"])

    async def test_discord_delete_and_native_cleanup_exclude_new_reservation_claims(self) -> None:
        deleting, resume_delete, preparing = anyio.Event(), anyio.Event(), anyio.Event()
        outcomes: list[PreparedBaseStore | BaseStorePreparationFailure] = []

        async def delete(**_kwargs) -> None:
            deleting.set()
            await resume_delete.wait()

        async def prepare() -> None:
            preparing.set()
            outcomes.append(await self.reserve())

        self.case.channel.delete = AsyncMock(side_effect=delete)
        self.enterContext(patch.object(match_commands.asyncio, "sleep", new=AsyncMock()))
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.invoke, "button")
                await deleting.wait()
                locked_during_discord = self.reservation_lock.locked()
                tasks.start_soon(prepare)
                await preparing.wait()
                await anyio.lowlevel.checkpoint()
                before_delete_finished = list(outcomes)
                resume_delete.set()
        self.assertTrue(locked_during_discord)
        self.assertEqual(before_delete_finished, [])
        self.assertFalse(self.case.runtime.alive)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], BaseStorePreparationFailure)
        self.assertIsNone(self.case.runtime.fixture.match["participants"][0]["base_store"])
