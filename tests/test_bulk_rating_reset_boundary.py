import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure
from pymongo.results import InsertOneResult

from elo_bot.extensions import season as season_commands
from elo_bot.features.hosting.war import result_store
from elo_bot.config.emojis import SUCCESS_EMOJI
from elo_bot.features.players.data_boundary import PLAYER_DATA_BOUNDARY
from elo_bot.db import Player, Season
from tests import _autowin_fixture as autowin_fixture
from tests import _result_finalization_fixture as result_fixture


class TestBulkRatingResetBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = result_fixture.ResetFinalizationState(self)
        self.state.setUp()
        self.reset_kind = "elo"
        self.seasons: list[Season] = []
        self.season_cog = season_commands.SeasonCommands(self.state.fixture.state.bot)

        async def load_players() -> list[Player]:
            return [await self.state.fixture.state.find_player({"user_id": user_id})
                    for user_id in self.state.fixture.state.players]

        async def insert_season(document, **_kwargs) -> InsertOneResult:
            row = deepcopy(document)
            row.setdefault("_id", PydanticObjectId())
            self.seasons.append(Season.model_validate(row))
            self.state.events.append("season:insert")
            return InsertOneResult(row["_id"], acknowledged=True)

        Player.find_all().to_list = AsyncMock(side_effect=load_players)
        seasons = MagicMock()
        seasons.sort.return_value.first_or_none = AsyncMock(return_value=None)
        self.enterContext(patch.object(Season, "_document_settings", DocumentSettings(name="seasons", use_state_management=True)))
        self.enterContext(patch.object(Season, "find_all", return_value=seasons))
        self.season_collection = MagicMock(insert_one=AsyncMock(side_effect=insert_season))
        self.season_collection.name = "seasons"
        self.enterContext(patch.object(Season, "get_pymongo_collection", return_value=self.season_collection))

    async def run_reset(self) -> None:
        if self.reset_kind == "season":
            await self.season_cog.season_reset.callback.func(
                self.season_cog, self.state.reset_ctx, start_date="1", end_date="2",
            )
        else:
            await self.state.reset_cog.data_reset_elo.callback.func(self.state.reset_cog, self.state.reset_ctx, 1200)

    async def exercise(self, writer: str, reset_kind: str = "elo"):
        self.state.scenario = writer
        self.reset_kind = reset_kind
        generation = PLAYER_DATA_BOUNDARY.generation
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.state.run_callback)
                await self.state.paused.wait()
                before_reset = self.state.snapshot()
                tasks.start_soon(self.run_reset)
                while PLAYER_DATA_BOUNDARY.generation == generation and self.state.reset_ctx.send.await_count == 0:
                    await anyio.lowlevel.checkpoint()
                self.assertGreater(PLAYER_DATA_BOUNDARY.generation, generation)
                self.state.reset_ctx.send.assert_not_awaited()
                self.assertEqual(self.state.snapshot(), before_reset)
                self.state.resume.set()
        after_reset = self.state.snapshot()
        self.assertEqual(after_reset["records"], ["10", "11"])
        self.assertTrue(after_reset["match_exists"])
        expected_elos = {"111": 999, "222": 963} if reset_kind == "season" else {"111": 1200, "222": 1200}
        for user_id, player in after_reset["players"].items():
            self.assertEqual(player["elo"], expected_elos[user_id])
            self.assertEqual(player["matches"], ["10", "11"])
        self.assertTrue(self.state.reset_ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertLess(self.state.events.index("result:success"), self.state.events.index("reset:success"))
        snapshots = [{player.player_id: player.elo for player in season.players} for season in self.seasons]
        if reset_kind == "season":
            self.assertEqual(snapshots, [{"111": 1033, "222": 972}])
            self.assertLess(self.state.events.index("result:success"), self.state.events.index("season:insert"))
        return {"scenario": f"{reset_kind}:{writer}", "before_reset": before_reset, "after_reset_success": after_reset,
                "season_snapshots": snapshots, "events": self.state.events,
                "response": self.state.reset_ctx.send.await_args.args[0], "reset_waited_for_writer": True}

    async def test_elo_reset_waits_for_the_finalizers_second_player_write(self) -> None:
        await self.exercise("finalizer_player2")

    async def test_elo_reset_waits_for_a_manual_result(self) -> None:
        await self.exercise("manual_player2")

    async def test_season_reset_archives_complete_ratings_after_a_manual_result(self) -> None:
        await self.exercise("manual_player2", "season")

    async def check_queued_result(self, reset_kind: str) -> None:
        self.reset_kind = reset_kind
        self.state.scenario = "finalizer_player2"
        self.state.resume.set()
        token_lock = result_store.RESULT_FINALIZATION_LOCKS.setdefault("TOKEN", anyio.Lock())
        deferred = anyio.Event()

        async def defer(**_kwargs) -> None:
            deferred.set()

        self.state.ctx.defer.side_effect = defer
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                async with token_lock:
                    tasks.start_soon(self.state.run_callback)
                    await deferred.wait()
                    await self.run_reset()
        self.assertIn("Player data was reset or is being reset", self.state.ctx.send.await_args.args[0])
        self.assertEqual(set(self.state.fixture.state.records), {"10"})
        expected_elo = 980 if reset_kind == "season" else 1200
        self.assertEqual([player["elo"] for player in self.state.fixture.state.players.values()], [expected_elo] * 2)
        self.assertFalse(self.state.paused.is_set())

    async def test_elo_reset_invalidates_a_queued_result_for_the_retained_match(self) -> None:
        await self.check_queued_result("elo")

    async def test_season_reset_invalidates_a_queued_result_for_the_retained_match(self) -> None:
        await self.check_queued_result("season")

    async def test_elo_write_failure_reports_no_success_and_releases_maintenance(self) -> None:
        Player.find_all().update.side_effect = OperationFailure("injected Elo reset failure")
        with self.assertRaises(OperationFailure):
            await self.run_reset()
        self.state.reset_ctx.send.assert_not_awaited()
        async with PLAYER_DATA_BOUNDARY.write(PLAYER_DATA_BOUNDARY.generation):
            self.assertEqual(self.state.fixture.state.players["111"]["elo"], 1000)

    async def test_season_insert_failure_reports_no_success_and_releases_maintenance(self) -> None:
        self.reset_kind = "season"
        self.season_collection.insert_one.side_effect = OperationFailure("injected Season insert failure")
        with self.assertRaises(OperationFailure):
            await self.run_reset()
        self.state.reset_ctx.send.assert_not_awaited()
        async with PLAYER_DATA_BOUNDARY.write(PLAYER_DATA_BOUNDARY.generation):
            self.assertEqual(self.seasons, [])


class TestAutowinBulkRatingResetBoundary(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.state = autowin_fixture.AutowinResetState(self)
        self.state.setUp()

    async def run_reset(self) -> None:
        await self.state.reset_cog.data_reset_elo.callback.func(self.state.reset_cog, self.state.reset_ctx, 1200)

    async def test_elo_reset_waits_for_the_autowin_penalty(self) -> None:
        generation = PLAYER_DATA_BOUNDARY.generation
        paused = anyio.Event()
        resume = anyio.Event()
        collection = Player.get_pymongo_collection()
        original_update = collection.update_one.side_effect

        async def update(query, operation, **kwargs):
            if query["user_id"] == "222":
                paused.set()
                await resume.wait()
            return await original_update(query, operation, **kwargs)

        collection.update_one.side_effect = update
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.state.autowin)
                await paused.wait()
                tasks.start_soon(self.run_reset)
                while PLAYER_DATA_BOUNDARY.generation == generation and self.state.reset_ctx.send.await_count == 0:
                    await anyio.lowlevel.checkpoint()
                self.assertGreater(PLAYER_DATA_BOUNDARY.generation, generation)
                self.state.reset_ctx.send.assert_not_awaited()
                self.assertEqual(self.state.fixture.elos(), [1005, 1000])
                resume.set()
        self.assertEqual(self.state.fixture.elos(), [1200, 1200])
        self.assertLess(self.state.events.index("autowin:success"), self.state.events.index("reset:players"))
        self.assertTrue(self.state.reset_ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_elo_reset_during_defer_rejects_the_old_autowin_request(self) -> None:
        async def reset_during_defer(**_kwargs) -> None:
            await self.run_reset()

        self.state.ctx.defer.side_effect = reset_during_defer
        await self.state.fixture.invoke(self.state.ctx)
        self.assertIn("Player data was reset or is being reset", self.state.ctx.send.await_args.args[0])
        self.assertEqual(self.state.fixture.elos(), [1200, 1200])
        Player.get_pymongo_collection().update_one.assert_not_awaited()
