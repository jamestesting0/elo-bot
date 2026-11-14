import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import anyio
import interactions as ipy
from cogs import match_commands, thread_commands
from elobot.config.ids import LVL2_ROLES, MATCH_CHANNEL
from elobot.db import Match
from tests import _result_finalization_fixture as result_fixture
from copy import deepcopy
from cogs.base_store import storage
from elobot.db import BaseStore
from cogs.base_store import preparation
from cogs.base_store.models import BaseStorePreparationFailure, BaseStorePreparationRequest, PreparedBaseStore
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.results import DeleteResult, UpdateResult
from cogs import challenge_commands
from elobot.db import Participant


class PendingMatchStateMixin:
    def setUp(self) -> None:
        self.runtime = result_fixture.ResetFinalizationState(self)
        self.runtime.setUp()
        self.thread = thread_commands.ThreadCommands(self.runtime.fixture.state.bot)
        self.runtime.fixture.state.bot.http.get_channel = AsyncMock(
            side_effect=ipy.errors.NotFound(MagicMock(status=404)),
        )
        self.free = self.enterContext(patch.object(thread_commands, "free_match_clans", new=AsyncMock()))
        self.enterContext(patch.object(thread_commands, "remove_helper_task", new=AsyncMock()))

    async def exercise_thread_event(self, scenario: str) -> None:
        self.runtime.scenario = scenario
        errors: list[str] = []
        async def finalize() -> None:
            try:
                await self.runtime.run_callback()
            except AttributeError as error:
                errors.append(str(error))
        with anyio.fail_after(3):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(finalize)
                await self.runtime.paused.wait()
                event = MagicMock(thread=MagicMock(id=321, parent_channel=MagicMock(id=MATCH_CHANNEL)))
                await self.thread.on_thread_delete.callback(self.thread, event)
                retained_after_event = self.runtime.alive
                self.runtime.resume.set()
        self.assertTrue(retained_after_event)
        self.assertEqual(errors, [])
        self.free.assert_not_awaited()
        self.assertTrue(self.runtime.fixture.match["result_operation"]["published"])
        self.assertEqual([row["elo"] for row in self.runtime.fixture.state.players.values()], [1033, 972])


class ReservedThreadStateMixin:
    def setUp(self) -> None:
        self.fixture = PendingMatchState(self)
        self.fixture.setUp()
        self.runtime = self.fixture.runtime
        self.store = BaseStore(clan_tag="#HELD", slot="Slot 1", reservation_id="kept").model_dump()
        self.runtime.fixture.match["participants"][0].update(
            base_store=deepcopy(self.store), opponent_bases_stored=True)
        self.channel = MagicMock(id=321, parent_channel=MagicMock(id=MATCH_CHANNEL), delete=AsyncMock())
        self.channel.name = "🔴 Match Thread"
        self.ctx = MagicMock(custom_id="delete_thread|TOKEN", defer=AsyncMock(),
            send=AsyncMock(return_value=MagicMock(id=123)), channel=self.channel,
            author=MagicMock(id=999, roles=[MagicMock(id=LVL2_ROLES[0])]))
        confirm = MagicMock(custom_id="confirm", channel=self.channel, edit_origin=AsyncMock())
        self.runtime.fixture.state.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=confirm))
        self.clear = self.enterContext(patch.object(
            match_commands, "clear_match_base_stores", new=AsyncMock(wraps=storage.clear_match_base_stores),
        ))
        self.enterContext(patch.object(thread_commands, "clear_match_base_stores", new=self.clear))
        self.enterContext(patch.object(storage, "sync_base_store_slots", new=AsyncMock()))

    def assert_held(self) -> None:
        self.assertTrue(self.runtime.alive)
        self.assertEqual(self.runtime.fixture.match["participants"][0]["base_store"], self.store)
        self.assertTrue(self.runtime.fixture.match["participants"][0]["opponent_bases_stored"])
        self.clear.assert_not_awaited()


class ThreadReservationStateMixin:
    def setUp(self) -> None:
        self.case = ReservedThreadState(self)
        self.case.setUp()
        for participant in self.case.runtime.fixture.match["participants"]:
            participant.update(result=[None, None, None], base_store=None, opponent_bases_stored=False)
        self.reservation_lock = anyio.Lock()
        for module in (preparation, match_commands, thread_commands):
            self.enterContext(patch.object(module, "BASE_STORE_RESERVATION_LOCK", self.reservation_lock, create=True))
        self.preparer = MagicMock(
            _base_store_sync_slots=AsyncMock(return_value={"#HELD": ["Slot 1"]}),
            _base_store_choose_bases=AsyncMock(),
        )
        self.request = BaseStorePreparationRequest(
            "321", 0, MagicMock(tag="#HELD", name="Offline Clan"), "Slot 1")
        self.old_message = MagicMock(created_at=datetime.now() - timedelta(days=2))

    async def asyncSetUp(self) -> None:
        await anyio.lowlevel.checkpoint()

    async def reserve(self) -> PreparedBaseStore | BaseStorePreparationFailure:
        return await preparation.prepare_base_store(self.preparer, self.request)

    async def invoke(self, callback: str) -> None:
        if callback == "slash":
            cog = self.case.fixture.thread
            await cog.thread_delete.callback.func(cog, self.case.ctx)
        elif callback == "button":
            cog = self.case.runtime.fixture.cog
            await cog.delete_thread_button.callback.func(cog, self.case.ctx)
        else:
            self.case.runtime.fixture.state.bot.fetch_channel = AsyncMock(return_value=MagicMock(
                fetch_all_threads=AsyncMock(return_value=MagicMock(threads=[self.case.channel]))))
            cog = self.case.runtime.fixture.cog
            await cog.inactive_threads_delete.callback.func(cog)

    async def reserve_during_countdown(self, callback: str) -> None:
        async def countdown(_seconds: float) -> None:
            self.assertFalse(self.reservation_lock.locked())
            self.assertIsInstance(await self.reserve(), PreparedBaseStore)
        self.enterContext(patch.object(match_commands.asyncio, "sleep", new=countdown))
        await self.invoke(callback)
        self.case.channel.delete.assert_not_awaited()
        self.assertTrue(self.case.runtime.alive)
        self.assertIsNotNone(self.case.runtime.fixture.match["participants"][0]["base_store"])
        self.assertIn("reserved bases", self.case.ctx.send.await_args.args[0])


class MatchCleanupStateMixin:
    def setUp(self) -> None:
        self.enterContext(patch.object(Match, "_document_settings", DocumentSettings(
            name="matches", use_state_management=True,
        )))
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="42",
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        )
        self.match._save_state()
        self.persisted = self.match.model_dump(by_alias=True)
        self.insert_before_delete = False
        self.reject_clear = False
        self.collection = MagicMock(
            update_one=AsyncMock(side_effect=self.update_one), delete_one=AsyncMock(side_effect=self.delete_one),
            find_one=AsyncMock(side_effect=self.find_one),
        )
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.collection))
        model = MagicMock(find_one=AsyncMock(return_value=self.match))
        model.find.return_value.to_list = AsyncMock(return_value=[self.match])
        for module in (thread_commands, match_commands, challenge_commands):
            self.enterContext(patch.object(module, "Match", new=model))
        self.bot = MagicMock()
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(send=AsyncMock()))
        self.bot.fetch_guild = AsyncMock(return_value=MagicMock(fetch_thread=AsyncMock(return_value=None)))
        self.bot.http.get_channel = AsyncMock(side_effect=ipy.errors.NotFound(MagicMock(status=404)))
        self.thread_cog = thread_commands.ThreadCommands(self.bot)
        self.match_cog = match_commands.MatchCommands(self.bot)
        self.challenge_cog = challenge_commands.ChallengeCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.defer, self.ctx.send = AsyncMock(), AsyncMock(return_value=MagicMock(id=123))
        self.ctx.author.id, self.ctx.channel.id = 111, 42
        self.ctx.channel.name, self.ctx.channel.parent_channel.id = "🔴 Match Thread", MATCH_CHANNEL
        self.ctx.channel.delete = AsyncMock()
        self.ctx.message.components = []
        self.ctx.message.edit, self.ctx.edit_origin = AsyncMock(), AsyncMock()
        self.confirm = MagicMock(custom_id="confirm", channel=self.ctx.channel, defer=AsyncMock(),
                                 edit_origin=AsyncMock(), send=AsyncMock())
        self.bot.wait_for_component = AsyncMock(return_value=MagicMock(ctx=self.confirm))
        self.free = self.enterContext(patch.object(thread_commands, "free_match_clans", new_callable=AsyncMock))
        self.enterContext(patch.object(match_commands, "free_match_clans", new=self.free))
        self.enterContext(patch.object(thread_commands, "remove_helper_task", new_callable=AsyncMock))
        self.enterContext(patch.object(thread_commands.asyncio, "sleep", new_callable=AsyncMock))
        self.enterContext(patch.object(storage, "retire_stored_base_message", new_callable=AsyncMock))
        self.enterContext(patch.object(storage, "sync_base_store_slots", new_callable=AsyncMock))
        self.player = MagicMock(last_cancel=None, save_changes=AsyncMock())
        self.enterContext(patch.object(match_commands, "Player", new=MagicMock(
            find_one=AsyncMock(return_value=self.player),
        )))

    def matches(self, query) -> bool:
        if self.persisted is None:
            return False
        for key, expected in query.items():
            if key == "$and":
                if not all(self.matches(branch) for branch in expected):
                    return False
                continue
            if key == "$or":
                if not any(self.matches(branch) for branch in expected):
                    return False
                continue
            if key == "participants":
                self.assertEqual(expected, {"$not": {"$elemMatch": {"$or": [
                    {"base_store": {"$ne": None}}, {"opponent_bases_stored": True},
                ]}}})
                if any(p["base_store"] or p["opponent_bases_stored"] for p in self.persisted["participants"]):
                    return False
            else:
                current = self.persisted
                for part in key.split("."):
                    current = current[int(part)] if isinstance(current, list) else current.get(part)
                if current != expected:
                    return False
        return True

    def install_replacement(self, *, accepted: bool = False) -> None:
        self.persisted["participants"][0]["base_store"] = BaseStore(
            clan_tag="#NEW", slot="Slot 2", reservation_id="NEW", message_id=99, ready=True,
        ).model_dump()
        self.persisted["participants"][0]["opponent_bases_stored"] = True
        if accepted:
            self.persisted["thread"] = "42"
            self.persisted["participants"][1]["confirmed"] = True

    async def update_one(self, query, update, **_kwargs) -> UpdateResult:
        matched = not self.reject_clear and self.matches(query)
        if matched:
            for field, value in update["$set"].items():
                _, index, key = field.split(".")
                self.persisted["participants"][int(index)][key] = deepcopy(value)
        return UpdateResult({"n": int(matched)}, acknowledged=True)

    async def delete_one(self, query, **_kwargs) -> DeleteResult:
        if self.insert_before_delete:
            self.install_replacement(accepted=True)
        matched = self.matches(query)
        if matched:
            self.persisted = None
        return DeleteResult({"n": int(matched)}, acknowledged=True)

    async def find_one(self, query, *_args, **_kwargs):
        return deepcopy(self.persisted) if self.matches(query) else None

    def make_pre_thread(self) -> None:
        self.match.thread = None
        self.persisted["thread"] = None

    async def accept_during_confirmation(self, **_kwargs):
        self.install_replacement(accepted=True)
        return MagicMock(ctx=self.confirm)

    async def invoke_stale_challenge(self, callback_name: str) -> None:
        self.make_pre_thread()
        self.ctx.custom_id = f"{callback_name}|TOKEN"
        self.bot.wait_for_component.side_effect = self.accept_during_confirmation
        callback = getattr(self.challenge_cog, f"{callback_name}_button")
        await callback.callback.func(self.challenge_cog, self.ctx)


class PendingMatchState(PendingMatchStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
        self.assertFalse = test.assertFalse
        self.assertLess = test.assertLess
        self.assertTrue = test.assertTrue


class ReservedThreadState(ReservedThreadStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
        self.assertFalse = test.assertFalse
        self.assertLess = test.assertLess
        self.assertTrue = test.assertTrue


class ThreadReservationState(ThreadReservationStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
        self.assertFalse = test.assertFalse
        self.assertIn = test.assertIn
        self.assertIsInstance = test.assertIsInstance
        self.assertIsNotNone = test.assertIsNotNone
        self.assertLess = test.assertLess
        self.assertTrue = test.assertTrue


class MatchCleanupState(MatchCleanupStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
