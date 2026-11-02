import unittest

import anyio

from common.players.data_boundary import PlayerDataBoundary, PlayerDataReset


class TestPlayerDataBoundary(unittest.IsolatedAsyncioTestCase):
    async def test_independent_writes_overlap_and_reset_waits_for_both(self) -> None:
        boundary = PlayerDataBoundary()
        generation = boundary.generation
        entered = [anyio.Event(), anyio.Event()]
        release = [anyio.Event(), anyio.Event()]
        reset_entered = anyio.Event()

        async def write(index: int) -> None:
            async with boundary.write(generation):
                entered[index].set()
                await release[index].wait()

        async def reset() -> None:
            async with boundary.reset():
                reset_entered.set()

        with anyio.fail_after(2):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(write, 0)
                tasks.start_soon(write, 1)
                await entered[0].wait()
                await entered[1].wait()
                tasks.start_soon(reset)
                while boundary.generation == generation:
                    await anyio.lowlevel.checkpoint()
                self.assertFalse(reset_entered.is_set())
                release[0].set()
                await anyio.lowlevel.checkpoint()
                self.assertFalse(reset_entered.is_set())
                release[1].set()
            self.assertTrue(reset_entered.is_set())

    async def test_request_queued_before_reset_is_rejected_after_token_unlocks(self) -> None:
        boundary = PlayerDataBoundary()
        token_lock = anyio.Lock()
        queued = anyio.Event()
        outcomes: list[str] = []

        async def old_request() -> None:
            generation = boundary.generation
            queued.set()
            async with token_lock:
                with self.assertRaises(PlayerDataReset):
                    async with boundary.write(generation):
                        self.fail("A request with pre-reset input was admitted")
                outcomes.append("rejected")

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                async with token_lock:
                    tasks.start_soon(old_request)
                    await queued.wait()
                    async with boundary.reset():
                        self.assertEqual(outcomes, [])
        self.assertEqual(outcomes, ["rejected"])

    async def test_pending_reset_rejects_new_writes_and_duplicate_reset(self) -> None:
        boundary = PlayerDataBoundary()
        async with boundary.reset():
            with self.assertRaises(PlayerDataReset):
                async with boundary.write(boundary.generation):
                    self.fail("Write entered maintenance")
            with self.assertRaises(PlayerDataReset):
                async with boundary.reset():
                    self.fail("A second reset entered maintenance")
        async with boundary.write(boundary.generation):
            self.assertEqual(boundary.generation, 1)

    async def test_cancelled_writer_releases_its_lease(self) -> None:
        boundary = PlayerDataBoundary()
        entered = anyio.Event()

        async def write() -> None:
            async with boundary.write(boundary.generation):
                entered.set()
                await anyio.sleep_forever()

        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(write)
                await entered.wait()
                tasks.cancel_scope.cancel()
        with anyio.fail_after(2):
            async with boundary.reset():
                self.assertEqual(boundary.generation, 1)

    async def test_cancelled_waiting_reset_releases_maintenance(self) -> None:
        boundary = PlayerDataBoundary()
        cancel_scopes: list[anyio.CancelScope] = []
        reset_finished = anyio.Event()

        async def reset() -> None:
            with anyio.CancelScope() as scope:
                cancel_scopes.append(scope)
                async with boundary.reset():
                    self.fail("Reset entered while an active write was held")
            reset_finished.set()

        with anyio.fail_after(2):
            async with boundary.write(boundary.generation):
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(reset)
                    while boundary.generation == 0:
                        await anyio.lowlevel.checkpoint()
                    cancel_scopes[0].cancel()
                    await reset_finished.wait()
                    async with boundary.write(boundary.generation):
                        self.assertEqual(boundary.generation, 1)
            async with boundary.reset():
                self.assertEqual(boundary.generation, 2)

    async def test_failed_reset_releases_maintenance_and_invalidates_old_requests(self) -> None:
        boundary = PlayerDataBoundary()
        old_generation = boundary.generation
        with self.assertRaises(LookupError):
            async with boundary.reset():
                raise LookupError("injected reset failure")
        with self.assertRaises(PlayerDataReset):
            async with boundary.write(old_generation):
                self.fail("Old request survived reset generation change")
        async with boundary.write(boundary.generation):
            self.assertEqual(boundary.generation, 1)
