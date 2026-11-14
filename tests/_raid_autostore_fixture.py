from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elobot.db import BaseLink, Match, Participant

from copy import deepcopy
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.results import UpdateResult

from cogs.base_store import preparation, raid_task, self_service
from cogs.base_store.channel_context import ScheduledBaseStoreContext
from cogs.base_store.models import SelfBaseStoreRequest
from cogs.match_commands import MatchCommands
from elobot.config.ids import RAID_AUTOSTORE_LOGS_CHANNEL
from tests._stored_start_fixture import terminal_job


class RaidAutostoreFixture(IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.enterContext(patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        ))
        self.persisted = Match.model_construct(
            id=PydanticObjectId(), token="RAID", thread="100", matched_time=1,
            final_districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            participants=[Participant(user_id="111"), Participant(user_id="222")],
        ).model_dump()
        self.alive = True
        self.trace = []
        self.slots = {str(choice.value): list(BASE_STORE_SLOTS) for choice in HOST_CLAN_CHOICES[:3]}
        self.collection = MagicMock(update_one=AsyncMock(side_effect=self.update))
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.collection))
        self.enterContext(patch.object(Match, "find_one", new=AsyncMock(side_effect=self.find)))
        lock = anyio.Lock()
        for module in (preparation, raid_task):
            self.enterContext(patch.object(module, "BASE_STORE_RESERVATION_LOCK", lock))
        self.bot = ipy.Client()
        self.bot.cache.guild_cache[50] = MagicMock(id=50, fetch_member=AsyncMock(
            side_effect=lambda user_id: ipy.User(
                client=self.bot, id=user_id, username=f"Player {user_id}", avatar=None,
            ),
        ))
        self.bot._user = ipy.User(client=self.bot, id=999, username="Bot", avatar=None)
        self.channel = self.bot.cache.place_channel_data({
            "id": "100", "guild_id": "50", "type": 11, "name": "Match Thread", "parent_id": "10",
        })
        self.bot.cache.place_channel_data({
            "id": str(RAID_AUTOSTORE_LOGS_CHANNEL), "guild_id": "50", "type": 0, "name": "raid-autostore-logs",
        })
        self.messages = {}
        self.bot.http.create_message = AsyncMock(side_effect=self.create_message)
        self.bot.http.edit_message = AsyncMock(side_effect=self.edit_message)
        self.runtime = MatchCommands(self.bot)
        self.runtime._base_store_sync_slots = AsyncMock(side_effect=self.slot_map)
        self.runtime._base_store_choose_bases = AsyncMock(side_effect=self.choose)
        self.bot.coc_client = MagicMock(get_clan=AsyncMock(side_effect=lambda tag: MagicMock(
            tag=tag, name="Helper Clan", share_link="https://clan.example/",
        )))
        self.helper = self.enterContext(patch.object(self_service.stored_bases, "store_bases", new=AsyncMock(
            side_effect=self.helper_success,
        )))
        self.interactive = self.enterContext(patch.object(self_service.helper_base_store, "store_bases_interactively",
                                                       new_callable=AsyncMock))
        self.availability = self.enterContext(patch.object(self_service.helper_base_store_availability,
                                                         "confirm_store_bases_availability", new_callable=AsyncMock))
        self.ctx = ScheduledBaseStoreContext(self.bot, self.channel)

    def get(self, path):
        value = self.persisted
        for part in ("id" if path == "_id" else path).split("."):
            if value is None:
                return None
            value = value[int(part)] if isinstance(value, list) else value.get(part)
        return value

    def matches(self, query) -> bool:
        return self.alive and all(
            self.get(key) != expected["$ne"] if isinstance(expected, dict) and "$ne" in expected
            else self.get(key) == expected
            for key, expected in query.items()
        )

    async def find(self, query) -> Match | None:
        if not self.matches(query):
            return None
        snapshot = Match.model_validate(deepcopy(self.persisted))
        snapshot._save_state()
        return snapshot

    async def update(self, query, update) -> UpdateResult:
        matched = self.matches(query)
        if matched:
            for path, value in update["$set"].items():
                parts = path.split(".")
                target = self.persisted
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                target[parts[-1]] = deepcopy(value)
            self.trace.append("claim" if "participants.0.base_store" in update["$set"] else "bind")
        return UpdateResult({"n": int(matched), "nModified": int(matched)}, acknowledged=True)

    async def slot_map(self, _tag=None):
        slots = deepcopy(self.slots)
        if self.alive:
            for participant in self.persisted["participants"]:
                store = participant["base_store"]
                if store and store["slot"] in slots.get(store["clan_tag"], []):
                    slots[store["clan_tag"]].remove(store["slot"])
        return slots

    async def choose(self, _match, player) -> None:
        self.persisted["participants"][1 - player]["bases"] = [
            BaseLink(link=f"https://link.clashofclans.com/{index}", image="image").model_dump() for index in range(3)
        ]

    async def helper_success(self, _runtime, _requested_by, _payload):
        self.trace.append("helper")
        self.assertIsNotNone(self.persisted["participants"][0]["base_store"])
        return terminal_job("store_bases", "succeeded")

    async def create_message(self, payload, channel_id, *, files=None):
        message_id = str(900 + len(self.messages))
        self.trace.append("pending" if payload.get("embeds") else "notice")
        self.messages[message_id] = {
            "id": message_id, "channel_id": str(channel_id), "guild_id": "50", "type": 0,
            "author": {"id": "999", "username": "Bot", "discriminator": "0", "avatar": None},
            "timestamp": "2026-10-09T06:00:00+00:00", "embeds": [], "components": [],
            **deepcopy(payload),
        }
        return deepcopy(self.messages[message_id])

    async def edit_message(self, payload, _channel_id, message_id, *, files=None):
        self.trace.append("edit")
        self.messages[str(message_id)].update(deepcopy(payload))
        return deepcopy(self.messages[str(message_id)])

    async def store(self) -> None:
        snapshot = await self.find({"token": "RAID"})
        await self_service.store_self_bases(
            self.runtime, self.ctx, SelfBaseStoreRequest(snapshot, 0, raid_autostore=True),
        )

    def assert_unreserved(self) -> None:
        self.assertIsNone(self.persisted["participants"][0]["base_store"])
        self.assertFalse(self.persisted["participants"][0]["opponent_bases_stored"])
        self.helper.assert_not_awaited()

    def stored_message(self):
        return self.messages["900"]

    def log_text(self) -> str:
        return "".join(message["embeds"][0]["description"] for message in self.messages.values()
                       if message["channel_id"] == str(RAID_AUTOSTORE_LOGS_CHANNEL))

def make_match(thread: str = "100") -> Match:
    return Match.model_construct(
        token=thread, thread=thread, matched_time=1,
        participants=[Participant(user_id="111"), Participant(user_id="222")],
    )
