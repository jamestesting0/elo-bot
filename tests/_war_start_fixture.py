from collections.abc import Mapping
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import coc
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue
from pymongo.errors import PyMongoError
from pymongo.results import UpdateResult

from elo_bot.features.base_store import storage as base_store_storage
from elo_bot.features.hosting.war import war_start
from elo_bot.db import Match, Participant
from tests._patch_sites import MATCH_MODULE_FAMILY, patch_family


class WarStartFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True))
        settings.start()
        self.addCleanup(settings.stop)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="42", is_war=True,
            times=[1000, None], hosts=["999", None], host_clans=["#CLAN", None],
            old_msgs=[None, None], message_publications=[None, None], participants=[Participant(user_id="100"), Participant(user_id="200")],
        )
        self.match._save_state()
        self.persisted: dict[str, JsonValue | PydanticObjectId] = {
            "_id": self.match.id, "is_war": True, "msg": None,
            "participants.0.user_id": "100", "participants.1.user_id": "200",
            "times.0": 1000, "times.1": None, "hosts.0": "999", "hosts.1": None,
            "host_clans.0": "#CLAN", "host_clans.1": None, "old_msgs.0": None, "old_msgs.1": None,
        }
        self.trace: list[str] = []
        self.bot = ipy.Client()
        self.bot.cache.place_channel_data({"id": "42", "type": 0, "name": "match", "guild_id": "50"})
        self.ctx = ipy.SlashContext(self.bot)
        self.ctx.channel_id, self.ctx.guild_id = 42, 50
        self.ctx.send = AsyncMock()
        self.bot.fetch_user = AsyncMock(return_value=ipy.User(client=self.bot, id=100, username="Player", avatar=None))
        self.message_data: dict[str, JsonValue] = {
            "id": "43", "channel_id": "42", "guild_id": "50",
            "author": {"id": "999", "username": "EloBot", "discriminator": "0", "avatar": None},
            "content": "", "timestamp": "2026-09-06T00:00:00+00:00", "type": 0,
            "embeds": [], "components": [],
        }
        self.bot.http.create_message = AsyncMock(side_effect=self.create_message)
        self.bot.http.edit_message = AsyncMock(side_effect=self.edit_message)
        self.bot.http.pin_message = AsyncMock(side_effect=lambda *_args: self.trace.append("pin"))
        self.bot.http.get_message = AsyncMock()
        self.bot.http.delete_message = AsyncMock()
        self.collection = MagicMock()
        self.collection.find_one_and_update = AsyncMock(side_effect=PyMongoError("binding failed"))
        self.collection.update_one = AsyncMock(side_effect=self.update_message_binding)
        self.collection.find_one = AsyncMock(side_effect=self.find_message_binding)
        collection_patch = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection_patch.start()
        self.addCleanup(collection_patch.stop)
        for boundary in (
                patch_family(MATCH_MODULE_FAMILY, "send_bases", new_callable=AsyncMock),
                patch_family(MATCH_MODULE_FAMILY, "remove_helper_task", new_callable=AsyncMock),
                patch.object(base_store_storage, "clear_participant_base_store", new_callable=AsyncMock),
        ):
            boundary.start()
            self.addCleanup(boundary.stop)

    async def create_message(self, payload: dict[str, JsonValue], _channel_id: int, *, files=None):
        self.trace.append("send")
        self.message_data.update(deepcopy(payload))
        return deepcopy(self.message_data)

    async def edit_message(self, payload: dict[str, JsonValue], _channel_id: int, _message_id: int, *, files=None):
        self.trace.append("edit")
        self.message_data.update(deepcopy(payload))
        return deepcopy(self.message_data)

    async def update_message_binding(
            self, query: Mapping[str, JsonValue | PydanticObjectId], update: Mapping[str, Mapping[str, JsonValue]],
    ) -> UpdateResult:
        self.trace.append("bind")
        matched = all(
            key not in self.persisted if value == {"$exists": False} else self.persisted.get(key) == value
            for key, value in query.items()
        )
        if matched:
            self.persisted.update(update["$set"])
        return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

    async def find_message_binding(
            self, query: Mapping[str, JsonValue | PydanticObjectId], _projection: Mapping[str, int],
    ) -> Mapping[str, PydanticObjectId] | None:
        self.trace.append("read")
        return {"_id": self.match.id} if all(self.persisted.get(key) == value for key, value in query.items()) else None

    def war_request(self) -> war_start.WarStart:
        return war_start.WarStart(
            match=self.match, player=0, host=ipy.User(client=self.bot, id=999, username="Host", avatar=None),
            clan=coc.Clan(data={"tag": "#CLAN", "name": "A Clan"}, client=None),
            coc_client=MagicMock(), reply=war_start.SelfHostedWarStartReply(),
        )
