from copy import deepcopy
from functools import partial
from datetime import datetime, UTC
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from bson import BSON
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings

from cogs.base_store import live_service
from cogs.match_commands import MatchCommands
import cogs.match_commands as match_commands
from cogs.war_hosting import helper_base_store_availability
from common.constants import HOST_CLAN_CHOICES
from database_models import BaseLink, BaseStore, Match, Participant
from tests import _helper_stored_bases_fixture as transport


CLAN = str(HOST_CLAN_CHOICES[0].value)
DISTRICTS = ["Barbarian Camp", "Wizard Valley", "Capital Peak"]
PAIR = "1234567890abcdef"


class LiveBaseFixture:
    async def setup(self, case) -> None:
        self.http = transport.StoredBaseJobState(case)
        await self.http.asyncSetUp()
        case.addAsyncCleanup(self.http.asyncTearDown)
        case.enterContext(patch.object(Match, "_document_settings", DocumentSettings(
            name="matches", use_state_management=True,
        )))
        self.document = Match.model_construct(
            id=PydanticObjectId(), token="LIVE", matched_time=1, thread="900",
            participants=[Participant(user_id=str(user), bases=[
                BaseLink(link=f"https://base.example/{user}/{i}", image="image") for i in range(3)
            ]) for user in (111, 222)], final_districts=DISTRICTS,
        ).model_dump()
        self.writes = []
        self.write = AsyncMock(side_effect=self.update)
        self.collection = MagicMock(update_one=self.write, find_one=AsyncMock(side_effect=self.find))
        case.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.collection))
        case.enterContext(patch.object(Match, "find_one", new=AsyncMock(side_effect=self.find)))
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands._RESOURCE_CLAN_RESERVATIONS.set({})
        match_commands._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})
        self.bot = MagicMock()
        self.bot.user.id = 999
        self.bot.user.display_name = "Helper"
        self.bot.user.mention = "<@999>"
        self.bot.owner.id = 998
        self.bot.wait_for_component = AsyncMock(side_effect=self.forever)
        self.bot.fetch_user = AsyncMock(side_effect=lambda uid: MagicMock(id=uid, mention=f"<@{uid}>"))
        self.runtime = MatchCommands(self.bot)
        self.runtime.helper_api_base_url = self.http.runtime.helper_api_base_url
        self.runtime.helper_api_key = "test-key"
        self.runtime._base_store_choose_bases = AsyncMock()
        self.runtime._base_store_sync_slots = AsyncMock(side_effect=self.slots)
        self.runtime._base_store_is_clan_available = AsyncMock(return_value=True)
        self.clan = SimpleNamespace(tag=CLAN, name="Stored Clan", share_link="https://clan.example")
        self.runtime.coc_client.get_clan = AsyncMock(return_value=self.clan)
        self.messages = {}
        self.channel = MagicMock(id=900, send=AsyncMock(side_effect=self.send_public))
        self.channel.fetch_message = AsyncMock(side_effect=lambda mid: self.messages[mid])
        self.bot.fetch_channel = AsyncMock(return_value=self.channel)
        self.bot.cache.fetch_message = AsyncMock(side_effect=lambda _cid, mid, **kw: self.messages[mid])
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Player {user_id}"),
        )
        self.channel.guild = self.ctx.guild
        self.ctx.author = MagicMock(id=111, mention="<@111>")
        self.ctx.channel = self.channel
        self.ctx.bot = self.bot
        self.ctx.send = AsyncMock(side_effect=lambda *args, **kw: self.message())
        self.ctx.edit = AsyncMock(side_effect=self.edit)
        self.ctx.defer = AsyncMock()
        case.enterContext(patch.object(live_service, "confirm_store_bases_availability", new=AsyncMock(return_value=True)))
        case.enterContext(patch.object(helper_base_store_availability, "confirm_remove_bases_availability",
                                      new=AsyncMock(return_value=True)))

    @staticmethod
    async def forever(*args, **kwargs):
        with anyio.fail_after(5):
            await anyio.Event().wait()

    def get(self, key):
        value = self.document
        for part in ("id" if key == "_id" else key).split("."):
            if value is None:
                return None
            value = value[int(part)] if isinstance(value, list) else value.get(part)
        return value

    def matches(self, query):
        for key, expected in query.items():
            value = self.get(key)
            if isinstance(expected, dict) and "$ne" in expected:
                if value == expected["$ne"]:
                    return False
            elif isinstance(expected, dict) and "$size" in expected:
                if not isinstance(value, list) or len(value) != expected["$size"]:
                    return False
            elif isinstance(expected, (dict, list)):
                if BSON.encode({"value": value}) != BSON.encode({"value": expected}):
                    return False
            elif value != expected:
                return False
        return True

    async def find(self, query, *args):
        if not self.matches(query):
            return None
        match = Match.model_validate(deepcopy(self.document))
        match._save_state()
        return match

    async def update(self, query, operation):
        self.writes.append((deepcopy(query), deepcopy(operation)))
        if not self.matches(query):
            return SimpleNamespace(matched_count=0)
        for key, value in operation["$set"].items():
            parent = self.document
            parts = key.split(".")
            for part in parts[:-1]:
                parent = parent[int(part)] if isinstance(parent, list) else parent[part]
            if isinstance(parent, list):
                parent[int(parts[-1])] = deepcopy(value)
            else:
                parent[parts[-1]] = deepcopy(value)
        return SimpleNamespace(matched_count=1)

    async def slots(self, clan_tag=None, **kwargs):
        occupied = [p["base_store"]["slot"] for p in self.document["participants"] if p["base_store"]]
        return {CLAN: [slot for slot in ("Slot 1", "Slot 2") if slot not in occupied]}

    def message(self):
        message = MagicMock(id=len(self.messages) + 500, flags=0, created_at=datetime.now(UTC),
                            embeds=[], components=[], content=None)
        message.edit = AsyncMock(side_effect=partial(self.edit, message))
        self.messages[message.id] = message
        return message

    async def edit(self, message, **kwargs):
        if "embed" in kwargs:
            message.embeds = [ipy.Embed.from_dict(deepcopy(kwargs["embed"]))
                              if isinstance(kwargs["embed"], dict) else kwargs["embed"]]
        if "embeds" in kwargs:
            message.embeds = [
                ipy.Embed.from_dict(deepcopy(item)) if isinstance(item, dict) else item
                for item in kwargs["embeds"]
            ]
        if "content" in kwargs:
            message.content = kwargs["content"]
        if "components" in kwargs:
            components = kwargs["components"]
            if isinstance(components, ipy.ActionRow):
                components = [components]
            elif components and isinstance(components[0], ipy.Button):
                components = [ipy.ActionRow(*components)]
            message.components = components
        return message

    async def send_public(self, *args, **kwargs):
        return await self.edit(self.message(), **kwargs)

    def add_pair(self, ready=True):
        message = self.message()
        for index, participant in enumerate(self.document["participants"]):
            participant["base_store"] = BaseStore(
                clan_tag=CLAN, slot=f"Slot {index+1}", reservation_id=PAIR,
                stored_at=int(datetime.now(UTC).timestamp()), ready=ready, is_live=True, message_id=message.id,
            ).model_dump()
            participant["opponent_bases_stored"] = True
        return message

    def jobs(self, statuses, kind="store_bases"):
        self.http.status_responses = [(200, self.http.job_document(kind, status)) for status in statuses]
