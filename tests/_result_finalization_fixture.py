import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import anyio
import interactions as ipy
from beanie import PydanticObjectId
from pymongo.results import UpdateResult
import elobot.extensions.match as match_commands
from elobot.extensions.match import MatchCommands
from elobot.features.hosting.war import result_store
from elobot.config.ids import MATCH_CHANNEL
from elobot.db import BaseStore, Match, Participant
from elobot.db.player_models import MatchPlayerStats
from elobot.db.result_models import ResultRecordSnapshot






VALID_RESULTS = ["3.100", "2.90", "1.80"]
PLAYER2_RESULTS = ["3.100", "1.70", "1.80"]
type RecordArgument = ipy.Guild | ipy.Client | str | dict[str, str] | list[str | None]


def make_result_match(*, complete: bool = True, stored_bases: bool = False) -> Match:
    first_results = VALID_RESULTS.copy() if complete else ["3.100", None, None]
    match = Match.model_construct(
        id=PydanticObjectId("000000000000000000000001"),
        token="TOKEN",
        thread="321",
        is_war=True,
        host_clans=["#ONE", "#TWO"],
        hosts=["999", None],
        times=[123, None],
        matched_time=123,
        final_districts=["D1", "D2", "D3"],
        participants=[
            Participant(user_id="111", result=first_results),
            Participant(user_id="222", result=PLAYER2_RESULTS.copy()),
        ],
    )
    if stored_bases:
        for index, participant in enumerate(match.participants):
            participant.base_store = BaseStore(
                clan_tag=("#ONE", "#TWO")[index], slot=str(index + 1), reservation_id=f"stored-{index}", ready=True,
            )
            participant.opponent_bases_stored = True
    return match


def release_result_stores(match: Match, *, reset_stored_flags: bool, bot: ipy.Client, reason: str) -> None:
    assert reason == "finished" and reset_stored_flags
    assert match.result_operation is not None and match.result_operation.completed
    for participant in match.participants:
        participant.base_store = None
        participant.opponent_bases_stored = False


def record_result(match: Match) -> tuple[str, str]:
    operation = match.result_operation
    assert operation is not None
    operation.record = ResultRecordSnapshot(
        id="1",
        time=123,
        operation_key=f"{match.id}:{operation.key}",
        players={
            "111": MatchPlayerStats(
                stars=6, destruction=270, opponent_stars=5, opponent_destruction=250,
                opponent="222", elo_change=5, won=True,
            ),
            "222": MatchPlayerStats(
                stars=5, destruction=250, opponent_stars=6, opponent_destruction=270,
                opponent="111", elo_change=-5, won=False,
            ),
        },
    )
    return "+5", "-5"


class ResultFinalizationFixture(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        result_store.RESULT_FINALIZATION_LOCKS.clear()
        self.bot = MagicMock()
        self.cog = MatchCommands(self.bot)
        self.guild = MagicMock()
        self.guild.fetch_member = AsyncMock(side_effect=lambda user_id: MagicMock(
            id=int(user_id), display_name=f"Player {user_id}",
        ))
        self.thread = MagicMock()
        self.thread.name = "🟢 Match TOKEN"
        self.thread.send = AsyncMock(return_value=MagicMock(id=901, edit=AsyncMock()))
        self.thread.fetch_message = AsyncMock()
        self.thread.edit = AsyncMock(side_effect=self._finish_thread)
        self.thread_renamed = anyio.Event()
        self.match_channel = MagicMock()
        self.match_channel.send = AsyncMock(return_value=MagicMock(id=902, edit=AsyncMock()))
        self.bot.fetch_guild = AsyncMock(return_value=self.guild)
        self.bot.fetch_channel = AsyncMock(side_effect=self._fetch_channel)
        self.save_changes = self.enterContext(patch.object(Match, "save_changes", new_callable=AsyncMock))
        self.operation_collection = MagicMock(
            update_one=AsyncMock(return_value=UpdateResult({"n": 1, "nModified": 1}, acknowledged=True))
        )
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.operation_collection))
        self.clear_stores = self._async_boundary("clear_match_base_stores")
        self.clear_stores.side_effect = release_result_stores
        self.award_helper = self._async_boundary("award_helper_once")
        self.autoqueue = self._async_boundary("autoqueue_completed_match_participants")
        self._async_boundary("invite_eligible_players")
        self.record_bases = self._async_boundary("record_base_stats")
        self.free_clans = self._async_boundary("free_match_clans")
        self._async_boundary("remove_helper_task")
        self._async_boundary("fetch_user_match_id_choices")
        self._async_boundary("choose_tip").return_value = "tip"
        self.match = make_result_match(stored_bases=True)
        self.match_model = self.enterContext(patch.object(match_commands, "Match"))
        self.match_model.find_one = AsyncMock(side_effect=lambda _query: self.match)
        self.player_model = self.enterContext(patch.object(match_commands, "Player"))
        self.record = self._async_boundary("record_match")

        def record(*_args: RecordArgument) -> tuple[str, str]:
            return record_result(self.match)

        self.record.side_effect = record
        self.lifecycle = MagicMock()
        for name in ("award_helper", "record_bases", "record", "clear_stores", "free_clans"):
            self.lifecycle.attach_mock(getattr(self, name), name)
        self.lifecycle.attach_mock(self.operation_collection.update_one, "checkpoint")

    def _async_boundary(self, name: str) -> AsyncMock:
        return self.enterContext(patch.object(match_commands, name, new_callable=AsyncMock))

    async def _finish_thread(self, *, name: str) -> None:
        self.thread.name = name
        self.thread_renamed.set()

    async def _fetch_channel(self, channel_id: int) -> MagicMock:
        if channel_id == 321:
            return self.thread
        if channel_id == MATCH_CHANNEL:
            return self.match_channel
        raise AssertionError(f"unexpected channel id: {channel_id}")

    def _delay_rename(self) -> tuple[anyio.Event, anyio.Event, anyio.Event]:
        started, release, finished = anyio.Event(), anyio.Event(), anyio.Event()

        async def delayed_rename(*, name: str) -> None:
            started.set()
            with anyio.fail_after(5):
                await release.wait()
            self.thread.name = name
            finished.set()

        self.thread.edit.side_effect = delayed_rename
        return started, release, finished


from copy import deepcopy
from pymongo.results import DeleteResult, InsertOneResult
from elobot.extensions import data as data_commands
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.players.data_boundary import PLAYER_DATA_BOUNDARY
from elobot.features.results.manual import manual_result_key
from elobot.db import Player, RecordedMatch
from tests._result_retry_store import apply_operations, matches
from tests._result_retry_store import apply_update
from tests import _result_retry_store as result_fixture


RESULTS = result_fixture.RESULTS

class ResetFinalizationStateMixin:
    def setUp(self) -> None:
        original_insert = RecordedMatch.insert
        self.fixture = result_fixture.ResultRetryState(self)
        self.fixture.setUp()
        self.alive = True
        self.scenario = ""
        self.lock_key = "TOKEN"
        self.events: list[str] = []
        self.paused = anyio.Event()
        self.resume = anyio.Event()
        self.ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(), message=None,
                             guild=self.fixture.state.bot.fetch_guild.return_value)
        self.reset_ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        self.reset_cog = data_commands.DataCommands(self.fixture.state.bot)
        async def result_sent(message, **_kwargs) -> None:
            if message.startswith(str(SUCCESS_EMOJI)):
                self.events.append("result:success")

        async def reset_sent(message, **_kwargs) -> None:
            if message.startswith(str(SUCCESS_EMOJI)):
                self.events.append("reset:success")

        self.ctx.send.side_effect = result_sent
        self.reset_ctx.send.side_effect = reset_sent
        match_collection = Match.get_pymongo_collection()
        original_match_update = match_collection.update_one.side_effect

        async def update_match(query, updates, **kwargs) -> UpdateResult:
            if not self.alive:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            return await original_match_update(query, updates, **kwargs)

        async def find_record(query, **_kwargs) -> RecordedMatch | None:
            return next((record.model_copy(deep=True) for record in self.fixture.state.records.values()
                         if matches(record.model_dump(by_alias=True), query)), None)

        async def update_record(query, updates, **_kwargs) -> UpdateResult:
            record = await find_record(query)
            if record is None:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            document = deepcopy(record.model_dump(by_alias=True))
            apply_operations(document, updates)
            self.fixture.state.records[record.id] = RecordedMatch.model_validate(document)
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        self.enterContext(patch.object(RecordedMatch, "find_one", new=AsyncMock(side_effect=find_record)))
        self.enterContext(patch.object(Match, "find_one", side_effect=self.find_match))
        self.enterContext(patch.object(Match, "find_all", return_value=MagicMock(
            to_list=AsyncMock(side_effect=self.list_matches))))
        self.enterContext(patch.object(Player, "find_all", return_value=MagicMock(
            update=AsyncMock(side_effect=self.reset_players))))
        self.enterContext(patch.object(data_commands, "free_match_clans", new=AsyncMock()))
        self.enterContext(patch.object(RecordedMatch, "insert", new=original_insert))
        match_collection.update_one.side_effect = update_match
        match_collection.delete_one = AsyncMock(side_effect=self.delete_match)
        match_collection.find_one.side_effect = lambda query, *_args, **_kwargs: (
            deepcopy(self.fixture.match) if self.alive and matches(self.fixture.match, query) else None)
        self.fixture.state.player_collection.update_one.side_effect = self.update_player
        recorded_collection = RecordedMatch.get_pymongo_collection()
        recorded_collection.update_one = AsyncMock(side_effect=update_record)
        recorded_collection.insert_one = AsyncMock(side_effect=self.insert_record)
        recorded_collection.delete_many = AsyncMock(side_effect=self.delete_records)
        RecordedMatch.find_all().delete = AsyncMock(side_effect=self.delete_records)

    def snapshot(self):
        return {
            "match_exists": self.alive,
            "records": sorted(self.fixture.state.records),
            "players": {user_id: {
                "elo": player["elo"], "wins": player["wins"],
                "matches": [ref.id for ref in player["matches"]],
            } for user_id, player in self.fixture.state.players.items()},
        }

    async def find_match(self, query, **_kwargs) -> Match | None:
        if not self.alive or not matches(self.fixture.match, {str(key): value for key, value in query.items()}):
            return None
        document = Match.model_validate(deepcopy(self.fixture.match))
        document._save_state()
        return document

    async def list_matches(self) -> list[Match]:
        document = await self.find_match({"token": "TOKEN"})
        return [document] if document else []

    async def delete_match(self, query, **_kwargs) -> DeleteResult:
        expected = deepcopy(query)
        reservation_filter = expected.pop("participants")
        self.assertEqual(reservation_filter, {"$not": {"$elemMatch": {"$or": [
            {"base_store": {"$ne": None}}, {"opponent_bases_stored": True},
        ]}}})
        unreserved = all(participant.get("base_store") is None and not participant["opponent_bases_stored"]
                         for participant in self.fixture.match["participants"])
        deleted = self.alive and matches(self.fixture.match, expected) and unreserved
        if deleted:
            self.alive = False
            self.events.append("reset:delete-match")
        return DeleteResult({"n": int(deleted)}, acknowledged=True)

    async def reset_players(self, update, **_kwargs) -> UpdateResult:
        for player in self.fixture.state.players.values():
            apply_update(player, {operator: {str(key): value for key, value in values.items()}
                                  for operator, values in dict(update).items()})
        self.events.append("reset:players")
        count = len(self.fixture.state.players)
        return UpdateResult({"n": count, "nModified": count}, acknowledged=True)

    async def delete_records(self, _query=None, **_kwargs) -> DeleteResult:
        count = len(self.fixture.state.records)
        self.fixture.state.records.clear()
        self.events.append("reset:delete-records")
        return DeleteResult({"n": count}, acknowledged=True)

    async def insert_record(self, document, **_kwargs) -> InsertOneResult:
        if self.scenario == "finalizer_insert":
            self.events.append("result:before-record-insert")
            self.paused.set()
            await self.resume.wait()
        recorded = RecordedMatch.model_validate(deepcopy(document))
        self.fixture.state.records[recorded.id] = recorded
        self.events.append("result:insert-record")
        return InsertOneResult(recorded.id, acknowledged=True)

    async def update_player(self, query, update, **kwargs) -> UpdateResult:
        if self.scenario != "finalizer_insert" and query.get("user_id") == "222" and "$addToSet" in update:
            self.events.append("result:before-player2")
            self.paused.set()
            await self.resume.wait()
        result = await self.fixture.state.update_player(query, update, **kwargs)
        self.events.append(f"result:player:{query.get('user_id')}:{result.matched_count}")
        return result

    async def run_callback(self) -> None:
        if self.scenario == "manual_player2":
            self.ctx.author.id = 999
            self.lock_key = manual_result_key("999", ["111", "222"], RESULTS, result_fixture.DISTRICTS)
            await self.fixture.cog.match_submit.callback.func(
                self.fixture.cog, self.ctx, MagicMock(id=111), MagicMock(id=222),
                RESULTS["player1"], RESULTS["player2"], *result_fixture.DISTRICTS,
            )
            return
        is_resubmit = self.scenario == "resubmit_player2"
        callback = self.fixture.cog.resubmit_modal if is_resubmit else self.fixture.cog.result_submit_modal
        self.ctx.custom_id = "resubmit_modal|TOKEN" if is_resubmit else "result_modal|TOKEN"
        response = {"player1": RESULTS["player2"], "player2": RESULTS["player1"]} if is_resubmit else RESULTS
        await callback.callback.func(self.fixture.cog, self.ctx, **response)

    async def run_reset(self) -> None:
        await self.reset_cog.data_reset_all.callback.func(self.reset_cog, self.reset_ctx)

    async def exercise(self, scenario: str):
        self.scenario = scenario
        generation = PLAYER_DATA_BOUNDARY.generation
        with anyio.fail_after(5):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(self.run_callback)
                await self.paused.wait()
                before_reset = self.snapshot()
                self.assertTrue(result_store.RESULT_FINALIZATION_LOCKS[self.lock_key].locked())
                tasks.start_soon(self.run_reset)
                while PLAYER_DATA_BOUNDARY.generation == generation:
                    await anyio.lowlevel.checkpoint()
                self.reset_ctx.send.assert_not_awaited()
                self.assertEqual(self.snapshot(), before_reset)
                self.resume.set()
        after_reset = self.snapshot()
        self.assertFalse(self.alive)
        self.assertEqual(after_reset["records"], [])
        for player in after_reset["players"].values():
            self.assertEqual(player, {"elo": 1000, "matches": [], "wins": 0})
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertTrue(self.reset_ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.assertLess(self.events.index("result:success"), self.events.index("reset:delete-match"))
        return {"scenario": scenario, "before_reset": before_reset, "after_reset_success": after_reset,
                "events": self.events, "reset_response": self.reset_ctx.send.await_args.args[0],
                "result_response": self.ctx.send.await_args.args[0], "reset_waited_for_result": True}


class ResetFinalizationState(ResetFinalizationStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
        self.assertFalse = test.assertFalse
        self.assertLess = test.assertLess
        self.assertTrue = test.assertTrue
