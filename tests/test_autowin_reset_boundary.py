import unittest

import anyio
from pymongo.results import UpdateResult

from common.constants import SUCCESS_EMOJI
from common.players.data_boundary import PLAYER_DATA_BOUNDARY
from database_models import Match, Player


from tests._autowin_fixture import AutowinResetStateMixin

class TestAutowinResetBoundary(AutowinResetStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_reset_waits_for_both_autowin_rating_writes_and_completion(self) -> None:
        generation = PLAYER_DATA_BOUNDARY.generation
        paused = anyio.Event()
        resume = anyio.Event()
        collection = Player.get_pymongo_collection()
        original_update = collection.update_one.side_effect

        async def update(query, operation, **kwargs) -> UpdateResult:
            if query["user_id"] == "222":
                paused.set()
                await resume.wait()
            return await original_update(query, operation, **kwargs)

        collection.update_one.side_effect = update
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.autowin)
                await paused.wait()
                tasks.start_soon(self.reset)
                while PLAYER_DATA_BOUNDARY.generation == generation:
                    await anyio.lowlevel.checkpoint()
                self.assertEqual(self.fixture.elos(), [1005, 1000])
                self.reset_ctx.send.assert_not_awaited()
                resume.set()
        self.assertEqual(self.fixture.elos(), [1000, 1000])
        self.assertEqual(self.fixture.rows[Match], [])
        self.assertEqual([row["wins"] for row in self.fixture.rows[Player]], [0, 0])
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertTrue(self.reset_ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertLess(self.events.index("autowin:success"), self.events.index("reset:delete-match"))

    async def test_autowin_deferred_across_reset_rejects_the_stale_request(self) -> None:
        async def reset_during_defer(**_kwargs) -> None:
            await self.reset()

        self.ctx.defer.side_effect = reset_during_defer
        await self.fixture.invoke(self.ctx)
        self.assertIn("Player data was reset or is being reset", self.ctx.send.await_args.args[0])
        self.assertEqual(self.fixture.elos(), [1000, 1000])
        for user in self.fixture.users.values():
            user.send.assert_not_awaited()

    async def test_autowin_requested_during_reset_is_rejected(self) -> None:
        async with PLAYER_DATA_BOUNDARY.reset():
            await self.fixture.invoke(self.ctx)
        self.assertIn("Player data was reset or is being reset", self.ctx.send.await_args.args[0])
        self.assertEqual(self.fixture.elos(), [1000, 1000])
        Player.get_pymongo_collection().update_one.assert_not_awaited()
