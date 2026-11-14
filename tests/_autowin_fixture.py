import unittest
from copy import deepcopy
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure
from pymongo.results import UpdateResult
from elobot.db import Helper, Match, Participant, Player
from tests._result_retry_store import read_field, write_field
from pymongo.results import DeleteResult
from elobot.extensions import data as data_commands
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import RecordedMatch
from tests._result_retry_store import matches
from tests._patch_sites import patch_sites
import elobot.extensions.match as match_module


class AutowinStateMixin:
    def setUp(self) -> None:
        for model in (Match, Player, Helper):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
        for model, field in ((Match, "thread"), (Player, "user_id"), (Helper, "user_id"),
                             (Helper, "points"), (Helper, "history")):
            self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.match = Match.model_construct(id=PydanticObjectId(), token="AUTOWIN", thread="321", matched_time=1000.0,
                                           participants=[Participant(user_id="111"), Participant(user_id="222")])
        self.rows = {
            Match: [self.match.model_dump(by_alias=True)],
            Player: [Player.model_construct(id=user_id, user_id=user_id, tag="#TAG", elo=1000,
                                           highest_elo=1000, wins=7).model_dump(by_alias=True)
                     for user_id in ("111", "222")],
            Helper: [Helper.model_construct(id=PydanticObjectId(), user_id="900").model_dump(by_alias=True)],
        }
        self.fail_player: str | None = None
        self.concurrent_elo = False
        self.fail_helper_after_write = False
        self.events: list[str] = []
        self.bot = MagicMock()
        self.users = {user_id: MagicMock(spec=ipy.User, id=int(user_id), mention=f"<@{user_id}>", send=AsyncMock())
                      for user_id in ("111", "222")}
        self.bot.fetch_user = AsyncMock(side_effect=lambda user_id: self.users[str(user_id)])
        self.cog = match_module.MatchCommands(self.bot)

        def loaded(model, query):
            row = next((row for row in self.rows[model] if self.matches(row, query)), None)
            if row is None:
                return None
            document = model.model_validate(deepcopy(row))
            document._save_state()
            return document

        async def update(model, query, operation, **kwargs):
            if model is Player and self.concurrent_elo:
                self.concurrent_elo = False
                self.rows[Player][0]["elo"] += 20
            row = next((row for row in self.rows[model] if self.matches(row, query)), None)
            if row is None and kwargs.get("upsert"):
                row = {"_id": PydanticObjectId(), **query, **operation.get("$setOnInsert", {})}
                self.rows[model].append(row)
            if row is None:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            if model is Player and row["user_id"] == self.fail_player:
                self.fail_player = None
                raise OperationFailure("injected second player write failure")
            self.apply(row, operation)
            self.events.append(model.__name__)
            if model is Helper and "$inc" in operation and self.fail_helper_after_write:
                self.fail_helper_after_write = False
                raise OperationFailure("injected response loss after helper credit")
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        async def find_row(model, query, projection=None, **_kwargs):
            row = next((row for row in self.rows[model] if self.matches(row, query)), None)
            if row is None:
                return None
            if projection is not None:
                self.assertEqual(projection, {"_id": 1})
                return {"_id": deepcopy(row["_id"])}
            return deepcopy(row)

        for model in (Match, Player, Helper):
            collection = MagicMock()
            collection.name = model.Settings.name
            collection.update_one = AsyncMock(side_effect=partial(update, model))
            collection.find_one = AsyncMock(side_effect=partial(find_row, model))
            self.enterContext(patch.object(model, "get_pymongo_collection", return_value=collection))
            if model is not Helper:
                self.enterContext(patch.object(model, "find_one", new=AsyncMock(
                    side_effect=lambda query, _model=model, **_kwargs: loaded(_model, query),
                )))

        async def legacy_set(document, updates, **_kwargs):
            await update(type(document), {"_id": document.id}, {"$set": dict(updates)})
            return document

        async def legacy_credit(*operations, **_kwargs):
            combined = {}
            for operation in operations:
                combined.update(dict(operation))
            await update(Helper, {"user_id": "900"}, combined)

        self.enterContext(patch.object(Player, "set", new=legacy_set))
        self.enterContext(patch.object(Match, "set", new=legacy_set))
        self.enterContext(patch.object(Helper, "find_one", return_value=MagicMock(upsert=AsyncMock(side_effect=legacy_credit))))
        self.free_clans = self.enterContext(patch_sites(('elobot.extensions.match.lifecycle.free_match_clans', 'elobot.extensions.match.results.free_match_clans'), new=AsyncMock()))
        self.cleanup = self.enterContext(patch_sites(('elobot.extensions.match.lifecycle.remove_helper_task', 'elobot.extensions.match.results.remove_helper_task', 'elobot.extensions.match.result_store.remove_helper_task', 'elobot.features.matches.resources.remove_helper_task'), new=AsyncMock()))
        self.autoqueue = self.enterContext(patch_sites(('elobot.extensions.match.results.autoqueue_completed_match_participants', 'elobot.extensions.match.result_store.autoqueue_completed_match_participants'), new=AsyncMock()))
        self.schedule = self.enterContext(patch_sites(('elobot.extensions.match.results.schedule_channel_name_edit', 'elobot.extensions.match.hosting.schedule_channel_name_edit', 'elobot.extensions.match.settings.schedule_channel_name_edit'), new=MagicMock()))
        self.invite = self.enterContext(patch_sites(('elobot.extensions.match.results.invite_eligible_players', 'elobot.extensions.match.result_store.invite_eligible_players', 'elobot.features.results.recording.invite_eligible_players'), new=AsyncMock()))

    @staticmethod
    def value(row, path):
        return read_field(row, path)

    @classmethod
    def matches(cls, row, query) -> bool:
        for field, expected in query.items():
            if str(field) == "$or":
                if not any(cls.matches(row, alternative) for alternative in expected):
                    return False
                continue
            value = cls.value(row, field)
            if isinstance(expected, dict):
                if "$ne" in expected and (expected["$ne"] in value if isinstance(value, list) else value == expected["$ne"]):
                    return False
                if "$exists" in expected and (value is not None) != expected["$exists"]:
                    return False
            elif (expected not in value if isinstance(value, list) and not isinstance(expected, list) else value != expected):
                return False
        return True

    @classmethod
    def apply(cls, row, operation) -> None:
        for path, value in operation.get("$set", {}).items():
            write_field(row, path, value)
        for path, value in operation.get("$inc", {}).items():
            row[path] = row.get(path, 0) + value
        for path, value in operation.get("$max", {}).items():
            row[path] = max(row.get(path, value), value)
        for path, value in operation.get("$addToSet", {}).items():
            if value not in row.setdefault(path, []):
                row[path].append(deepcopy(value))
        for path, value in operation.get("$push", {}).items():
            row.setdefault(path, []).append(deepcopy(value))

    def context(self) -> MagicMock:
        ctx = MagicMock(author=MagicMock(id=999, mention="<@999>"), channel=MagicMock(id=321, send=AsyncMock()),
                        defer=AsyncMock(), send=AsyncMock())
        return ctx

    async def invoke(self, ctx: MagicMock, winner: str = "111") -> None:
        await self.cog.match_autowin.callback.func(self.cog, ctx, self.users[winner], reason="No show")

    def elos(self) -> list[int]:
        return [row["elo"] for row in self.rows[Player]]


class AutowinResetStateMixin:
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        self.fixture = AutowinState(self)
        self.fixture.setUp()
        for field in Player.model_fields:
            self.enterContext(patch.object(Player, field, ExpressionField(field), create=True))
        self.ctx = self.fixture.context()
        self.reset_ctx = MagicMock(defer=AsyncMock(), send=AsyncMock())
        self.reset_cog = data_commands.DataCommands(self.fixture.bot)
        self.events: list[str] = []

        async def autowin_sent(message, **_kwargs) -> None:
            if message.startswith(str(SUCCESS_EMOJI)):
                self.events.append("autowin:success")

        async def reset_sent(message, **_kwargs) -> None:
            if message.startswith(str(SUCCESS_EMOJI)):
                self.events.append("reset:success")

        self.ctx.send.side_effect = autowin_sent
        self.reset_ctx.send.side_effect = reset_sent

        async def list_matches() -> list[Match]:
            return [Match.model_validate(deepcopy(row)) for row in self.fixture.rows[Match]]

        async def delete_match(query, **_kwargs) -> DeleteResult:
            expected = deepcopy(query)
            expected.pop("participants")
            rows = self.fixture.rows[Match]
            candidate = next((row for row in rows if matches(row, expected)), None)
            if candidate:
                self.assertTrue(all(participant["base_store"] is None for participant in candidate["participants"]))
                rows.remove(candidate)
                self.events.append("reset:delete-match")
            return DeleteResult({"n": int(candidate is not None)}, acknowledged=True)

        async def reset_players(update, **_kwargs) -> UpdateResult:
            for row in self.fixture.rows[Player]:
                self.fixture.apply(row, {operator: {str(key): value for key, value in fields.items()}
                                         for operator, fields in dict(update).items()})
            self.events.append("reset:players")
            return UpdateResult({"n": 2, "nModified": 2}, acknowledged=True)

        self.enterContext(patch.object(Match, "find_all", return_value=MagicMock(to_list=AsyncMock(side_effect=list_matches))))
        Match.get_pymongo_collection().delete_one = AsyncMock(side_effect=delete_match)
        self.enterContext(patch.object(Player, "find_all", return_value=MagicMock(update=AsyncMock(side_effect=reset_players))))
        self.enterContext(patch.object(RecordedMatch, "delete_all", new=AsyncMock()))
        self.enterContext(patch.object(data_commands, "free_match_clans", new=AsyncMock()))

    async def reset(self) -> None:
        await self.reset_cog.data_reset_all.callback.func(self.reset_cog, self.reset_ctx)

    async def autowin(self) -> None:
        await self.fixture.invoke(self.ctx)


class AutowinState(AutowinStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual


class AutowinResetState(AutowinResetStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertEqual = test.assertEqual
        self.assertTrue = test.assertTrue
