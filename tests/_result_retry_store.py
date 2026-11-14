import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch
from beanie import Link, PydanticObjectId
from beanie.odm.fields import ExpressionField, LinkInfo, LinkTypes
from beanie.odm.settings.document import DocumentSettings
from bson import DBRef
from pymongo.results import UpdateResult
from elobot.db import BaseLayout, BaseLink, Match, MatchDistrictStats, MatchPlayerStats, Participant, Player, RecordedMatch
from pymongo.errors import DuplicateKeyError
from pymongo.results import DeleteResult
from elobot.config.ids import MATCH_CHANNEL
from elobot.db import Helper
from elobot.db.result_models import MatchResultOperation, ResultRecordSnapshot
from tests._patch_sites import patch_sites
import elobot.extensions.match as match_module
from tests._patch_sites import MATCH_MODULE_FAMILY, patch_family
import elobot.extensions.match.results as match_results_module





def read_field(document, path):
    value = document
    for part in path.split("."):
        if value is None:
            return None
        value = value[int(part)] if isinstance(value, list) else value.get(part)
    return value


def write_field(document, path, value) -> None:
    parts = path.split(".")
    target = document
    for part in parts[:-1]:
        target = target[int(part)] if isinstance(target, list) else target.setdefault(part, {})
    if isinstance(target, list):
        target[int(parts[-1])] = deepcopy(value)
    else:
        target[parts[-1]] = deepcopy(value)


def apply_operations(document, updates) -> None:
    for field, value in updates.get("$set", {}).items():
        write_field(document, field, value)
    for field, value in updates.get("$inc", {}).items():
        write_field(document, field, (read_field(document, field) or 0) + value)
    for field, value in updates.get("$max", {}).items():
        write_field(document, field, max(read_field(document, field) or value, value))
    for field, value in updates.get("$push", {}).items():
        write_field(document, field, (read_field(document, field) or []) + [value])
    for field, value in updates.get("$addToSet", {}).items():
        current = read_field(document, field) or []
        if value not in current:
            write_field(document, field, current + [value])


def matches(document, query) -> bool:
    for expression, expected in query.items():
        field = str(expression)
        if field == "$and":
            if not all(matches(document, branch) for branch in expected):
                return False
            continue
        if field == "$or":
            if not any(matches(document, branch) for branch in expected):
                return False
            continue
        value = read_field(document, field)
        if isinstance(expected, dict) and "$ne" in expected:
            if value == expected["$ne"] or isinstance(value, list) and expected["$ne"] in value:
                return False
        elif isinstance(value, list) and not isinstance(expected, list):
            if expected not in value:
                return False
        elif value != expected:
            return False
    return True


class BaseQuery:
    def __init__(self, state):
        self.state = state

    def __await__(self):
        return self.fetch().__await__()

    async def fetch(self) -> BaseLayout:
        document = BaseLayout.model_validate(deepcopy(self.state))
        document._save_state()
        return document

    async def update(self, updates, **_kwargs) -> UpdateResult:
        apply_operations(self.state, updates)
        return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

DISTRICTS = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]

RESULTS = {"player1": "3.100\n3.100\n3.100", "player2": "2.90\n2.90\n2.90"}

def apply_update(stored, update) -> None:
    stored.update(deepcopy(update.get("$set", {})))
    for field, value in update.get("$inc", {}).items():
        stored[field] = stored.get(field, 0) + value
    for field, value in update.get("$max", {}).items():
        stored[field] = max(stored.get(field, value), value)
    for field, value in update.get("$addToSet", {}).items():
        if value not in stored[field]:
            stored[field].append(deepcopy(value))
    for field, value in update.get("$pull", {}).items():
        stored[field] = [item for item in stored[field] if item != value]

def matches_query(stored, query) -> bool:
    for field, expected in query.items():
        value = stored.get(field)
        if isinstance(expected, dict) and "$ne" in expected:
            if expected["$ne"] in value:
                return False
        elif field == "matches" and isinstance(expected, DBRef):
            if expected not in value:
                return False
        elif value != expected:
            return False
    return True


class RecordStateMixin:
    def setUp(self) -> None:
        for model in (BaseLayout, Match, Player, RecordedMatch):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
            for field in model.model_fields:
                self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.enterContext(patch.object(Player, "_link_fields", {"matches": LinkInfo(
            field_name="matches", lookup_field_name="matches", document_class=RecordedMatch, link_type=LinkTypes.LIST,
        )}))
        recorded_collection = MagicMock()
        recorded_collection.name = "recorded_matches"
        self.enterContext(patch.object(RecordedMatch, "get_pymongo_collection", return_value=recorded_collection))
        self.records = {"10": self.make_record("10")}
        self.players = {}
        for user_id in ("111", "222"):
            player = Player.model_construct(id=user_id, user_id=user_id, tag="#TAG", elo=1000, highest_elo=1000,
                                             wins=int(user_id == "111"), matches=[self.records["10"]])
            player._save_state()
            self.players[user_id] = deepcopy(player.get_saved_state())
        self.bot = MagicMock(get_ext=MagicMock(return_value=None))
        self.users = [MagicMock(display_name=user_id, send=AsyncMock()) for user_id in self.players]
        self.before_insert = lambda: None
        self.after_player_read = lambda _player: None
        self.before_player_update = lambda _query, _update: None
        self.lookup_reverse = False
        self.player_collection = MagicMock(update_one=AsyncMock(side_effect=self.update_player))
        self.player_collection.name = "players"
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=self.player_collection))
        self.enterContext(patch.object(Player, "find_one", side_effect=self.find_player))
        self.enterContext(patch.object(RecordedMatch, "find", side_effect=lambda query, **_kwargs: MagicMock(
            to_list=AsyncMock(return_value=[self.records[record_id] for record_id in query["_id"]["$in"]
                                           if record_id in self.records]),
        )))

        async def legacy_player_update(document, updates, **_kwargs):
            apply_update(self.players[document.user_id], dict(updates))
            return document

        async def insert_record(document, **_kwargs):
            self.before_insert()
            self.records[document.id] = document.model_copy(deep=True)
            return document

        self.enterContext(patch.object(Player, "update", new=legacy_player_update))
        self.enterContext(patch.object(RecordedMatch, "insert", new=insert_record))
        query = MagicMock()
        query.sort.return_value.first_or_none = AsyncMock(return_value=self.records["10"])
        self.enterContext(patch.object(RecordedMatch, "find_all", return_value=query))
        self.enterContext(patch.object(Match, "find_one", new=AsyncMock(return_value=None)))
        self.enterContext(patch('elobot.features.results.recording.fetch_member_otherwise_user', new=AsyncMock(side_effect=self.users)))
        self.choice_refresh = self.enterContext(patch_sites(('elobot.extensions.match.results.fetch_user_match_id_choices', 'elobot.features.results.recording.fetch_user_match_id_choices'), new_callable=AsyncMock))
        self.invite = self.enterContext(patch_sites(('elobot.extensions.match.results.invite_eligible_players', 'elobot.features.results.recording.invite_eligible_players'), new_callable=AsyncMock))

    def make_record(self, record_id: str) -> RecordedMatch:
        return RecordedMatch.model_construct(id=record_id, time=1, players={
            user_id: MatchPlayerStats(stars=stars * 3, destruction=destruction * 3, opponent_stars=(5 - stars) * 3,
                opponent_destruction=(190 - destruction) * 3, opponent=opponent, elo_change=change, won=change > 0,
                districts={district: MatchDistrictStats(stars=stars, destruction=destruction) for district in DISTRICTS})
            for user_id, opponent, stars, destruction, change in (("111", "222", 3, 100, 20), ("222", "111", 2, 90, -20))
        })

    async def find_player(self, query, fetch_links: bool = False, **_kwargs) -> Player:
        stored = deepcopy(self.players[query.get("user_id", query.get("_id"))])
        stored["id"] = stored.pop("_id")
        if fetch_links:
            stored["matches"] = [self.records[ref.id].model_copy(deep=True) for ref in stored["matches"]
                                 if ref.id in self.records]
            if self.lookup_reverse:
                stored["matches"].reverse()
        else:
            stored["matches"] = [Link(ref, RecordedMatch) for ref in stored["matches"]]
        player = Player.model_construct(**stored)
        player._save_state()
        self.after_player_read(player)
        return player

    async def update_player(self, query, update, **_kwargs) -> UpdateResult:
        self.assertLess(self.player_collection.update_one.await_count, 10, "history guard did not stabilize")
        self.before_player_update(query, update)
        stored = self.players[query.get("user_id", query.get("_id"))]
        matched = matches_query(stored, query)
        if matched:
            apply_update(stored, update)
        return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

    async def invoke_record(self) -> None:
        await match_results_module.record_match(MagicMock(), self.bot, "111", "222", RESULTS, DISTRICTS)

    def prepare_resubmit(self) -> MagicMock:
        match = Match.model_construct(id=PydanticObjectId(), token="TOKEN", matched_time=1, final_districts=DISTRICTS,
            msg=42, thread="321", participants=[Participant(user_id="111", result=RESULTS["player1"].splitlines(),
                bases=[BaseLink(link="base", image="image"), None, None]),
                Participant(user_id="222", result=RESULTS["player2"].splitlines())])
        match._save_state()
        match_state = deepcopy(match.get_saved_state())
        base = BaseLayout.model_construct(id=PydanticObjectId(), district=DISTRICTS[0], link="base", image_url="image",
            quality="Good", expiration_date=100.0, records=[["222", 2, 90, 10.0], ["222", 2, 90, 20.0]])
        base._save_state()
        self.base = deepcopy(base.get_saved_state())
        self.after_base_read = lambda: None

        async def find_base(_query, **_kwargs) -> BaseLayout:
            values = deepcopy(self.base)
            values["id"] = values.pop("_id")
            document = BaseLayout.model_construct(**values)
            document._save_state()
            self.after_base_read()
            return document

        async def update_base(query, updates, **_kwargs) -> UpdateResult:
            matched = matches_query(self.base, query)
            if matched:
                apply_update(self.base, updates)
            return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

        async def legacy_base_update(document, updates, **_kwargs):
            apply_update(self.base, dict(updates))
            return document

        async def replace_record(*_args):
            self.records["11"] = self.make_record("11")
            match.result_operation.record = ResultRecordSnapshot.model_validate(self.records["11"].model_dump())
            match_state["result_operation"]["record"] = match.result_operation.record.model_dump()
            for player in self.players.values():
                player["matches"].append(self.records["11"].to_ref())
            return "+20", "-20"

        async def update_match(query, updates, **_kwargs) -> UpdateResult:
            matched = matches(match_state, query)
            if matched:
                apply_operations(match_state, updates)
                match.result_operation = MatchResultOperation.model_validate(match_state["result_operation"])
            return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

        self.enterContext(patch.object(Match, "find_one", new=AsyncMock(return_value=match)))
        self.enterContext(patch.object(Match, "save_changes", new=AsyncMock()))
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=MagicMock(
            update_one=AsyncMock(side_effect=update_match))))
        self.enterContext(patch.object(RecordedMatch, "delete", new=AsyncMock()))
        self.enterContext(patch.object(BaseLayout, "find_one", side_effect=find_base))
        self.enterContext(patch.object(BaseLayout, "update", new=legacy_base_update))
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=MagicMock(
            update_one=AsyncMock(side_effect=update_base))))
        self.enterContext(patch('elobot.extensions.match.results.record_match', new=AsyncMock(side_effect=replace_record)))
        self.enterContext(patch('elobot.extensions.match.results.record_base_stats', new=AsyncMock()))
        self.enterContext(patch_sites(('elobot.extensions.match.lifecycle.clear_match_base_stores', 'elobot.extensions.match.results.clear_match_base_stores'), new=AsyncMock()))
        self.enterContext(patch_sites(('elobot.extensions.match.lifecycle.free_match_clans', 'elobot.extensions.match.results.free_match_clans'), new=AsyncMock()))
        self.enterContext(patch_sites(('elobot.extensions.match.results.choose_tip', 'elobot.extensions.match.hosting.choose_tip', 'elobot.features.matches.tips.choose_tip'), new=AsyncMock(return_value="Test tip")))
        channel = MagicMock(fetch_message=AsyncMock(return_value=MagicMock(edit=AsyncMock())),
                            send=AsyncMock(return_value=MagicMock(id=901, edit=AsyncMock())))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        return MagicMock(custom_id="resubmit_modal|TOKEN", defer=AsyncMock(), send=AsyncMock(),
                         guild=MagicMock(fetch_member=AsyncMock(side_effect=lambda user_id: MagicMock(
                             id=int(user_id), display_name=f"Player {user_id}",
                         ))))

    async def invoke_resubmit(self, ctx: MagicMock) -> None:
        cog = match_module.MatchCommands(self.bot)
        await cog.resubmit_modal.callback.func(cog, ctx, **RESULTS)


class ResultRetryStateMixin:
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        self.state = RecordState(self)
        self.state.setUp()
        match = Match.model_construct(id=PydanticObjectId(), token="TOKEN", thread="321", matched_time=100,
            final_districts=DISTRICTS, hosts=["999", None], times=[100, None], msg="42",
            participants=[Participant(user_id="111", result=RESULTS["player1"].splitlines(),
                bases=[BaseLink(link="base", image="image"), None, None]),
                Participant(user_id="222", result=RESULTS["player2"].splitlines())])
        match._save_state()
        self.match = deepcopy(match.get_saved_state())
        base = BaseLayout.model_construct(id=PydanticObjectId(), district=DISTRICTS[0], link="base", image_url="image",
            quality="Good", expiration_date=100.0, records=[])
        base._save_state()
        self.base = deepcopy(base.get_saved_state())
        self.helper = {"user_id": "999", "points": 0.0, "history": [], "accounts": [], "operation_receipts": []}
        self.after_base_update = lambda _updates: None
        self.before_match_update = lambda _updates: None
        self.before_match_write = AsyncMock()
        self.enterContext(patch.object(Helper, "_document_settings", DocumentSettings(name="helpers")))
        for field in Helper.model_fields:
            self.enterContext(patch.object(Helper, field, ExpressionField(field), create=True))
        self.enterContext(patch.object(Match, "id", ExpressionField("_id"), create=True))

        async def find_match(query, **_kwargs) -> Match | None:
            if not matches(self.match, query):
                return None
            document = Match.model_validate(deepcopy(self.match))
            document._save_state()
            return document

        async def update_match(document, updates, **_kwargs):
            apply_operations(self.match, dict(updates))
            return document

        def collection_for(document):
            async def update(query, updates, **_kwargs):
                if document is self.match:
                    await self.before_match_write(query, updates)
                    self.before_match_update(updates)
                matched = matches(document, query)
                if matched:
                    apply_operations(document, updates)
                    if document is self.base:
                        self.after_base_update(updates)
                return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)
            return MagicMock(update_one=AsyncMock(side_effect=update), find_one=AsyncMock(
                side_effect=lambda query, _projection=None, **_kwargs: deepcopy(document) if matches(document, query) else None))

        async def insert_record(document, **_kwargs):
            if document.id in self.state.records:
                raise DuplicateKeyError("duplicate numeric record ID")
            self.state.records[document.id] = document.model_copy(deep=True)
            return document

        async def delete_record(document, **_kwargs) -> DeleteResult:
            deleted = self.state.records.pop(document.id, None)
            return DeleteResult({"n": int(deleted is not None)}, acknowledged=True)

        async def helper_upsert(*operations, **_kwargs):
            for operation in operations:
                apply_operations(self.helper, dict(operation))

        self.enterContext(patch.object(Match, "find_one", side_effect=find_match))
        self.enterContext(patch.object(Match, "update", new=update_match))
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=collection_for(self.match)))
        self.enterContext(patch.object(RecordedMatch, "insert", new=insert_record))
        self.enterContext(patch.object(RecordedMatch, "delete", new=delete_record))
        self.enterContext(patch.object(RecordedMatch, "find_one", new=AsyncMock(
            side_effect=lambda query, **_kwargs: self.state.records.get(query.get("_id", query.get("id"))))))
        query = MagicMock()
        query.sort.return_value.first_or_none = AsyncMock(side_effect=lambda: max(
            self.state.records.values(), key=lambda record: int(record.id), default=None))
        self.enterContext(patch.object(RecordedMatch, "find_all", return_value=query))
        self.enterContext(patch.object(BaseLayout, "find_one", side_effect=lambda *_args, **_kwargs: BaseQuery(self.base)))
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=collection_for(self.base)))
        self.enterContext(patch.object(Helper, "find_one", return_value=MagicMock(upsert=AsyncMock(side_effect=helper_upsert))))
        self.enterContext(patch.object(Helper, "get_pymongo_collection", return_value=collection_for(self.helper)))
        self.thread = MagicMock(send=AsyncMock(), fetch_message=AsyncMock(
            return_value=MagicMock(embeds=[], edit=AsyncMock())), name="thread")
        self.thread.name = "🟢 Match TOKEN"
        channel = MagicMock(
            fetch_message=AsyncMock(return_value=MagicMock(id=902, edit=AsyncMock())),
            send=AsyncMock(return_value=MagicMock(id=902, edit=AsyncMock())),
        )
        self.state.bot.fetch_channel = AsyncMock(side_effect=lambda channel_id: channel if channel_id == MATCH_CHANNEL else self.thread)
        self.state.bot.fetch_guild = AsyncMock(return_value=MagicMock(fetch_member=AsyncMock(
            side_effect=lambda user_id: MagicMock(id=int(user_id), display_name=f"Player {user_id}"),
        )))
        self.enterContext(patch('elobot.features.results.recording.fetch_member_otherwise_user', new=AsyncMock(
            side_effect=lambda _guild, _bot, user_id: self.state.users[0 if user_id == "111" else 1])))
        for name in ("free_match_clans", "clear_match_base_stores", "remove_helper_task", "fetch_user_match_id_choices",
                     "autoqueue_completed_match_participants", "choose_tip"):
            self.enterContext(patch_family(MATCH_MODULE_FAMILY, name, new=AsyncMock()))
        match_results_module.choose_tip.return_value = "tip"
        self.enterContext(patch_sites(('elobot.extensions.match.results.schedule_channel_name_edit', 'elobot.extensions.match.hosting.schedule_channel_name_edit'), return_value=MagicMock()))
        self.cog = match_module.MatchCommands(self.state.bot)

    async def invoke_record(self) -> None:
        await match_results_module.record_match(MagicMock(), self.state.bot, "111", "222", RESULTS, DISTRICTS, "TOKEN")


class RecordState(RecordStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertLess = test.assertLess


class ResultRetryState(ResultRetryStateMixin):
    """Shared state whose patches and assertions belong to the calling test."""

    def __init__(self, test: unittest.TestCase) -> None:
        self.enterContext = test.enterContext
        self.addCleanup = test.addCleanup
        self.assertLess = test.assertLess
