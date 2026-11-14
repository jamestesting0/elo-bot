import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from pymongo.results import DeleteResult, UpdateResult

from cogs import data_commands
from elobot.config.game import BASE_STORE_SLOTS
from elobot.db import BaseStore, Clan, Match, Participant, Player, RecordedMatch


class DataResetFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from tests._final_empty_storage import empty_final_storage
        empty_final_storage(self)
        for model in (Match, Clan, Player):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
            for field in model.model_fields:
                self.enterContext(patch.object(model, field, ExpressionField(field), create=True))
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="42", host_clans=["#CLAN", None],
            participants=[Participant(user_id="111", opponent_bases_stored=True, base_store=BaseStore(
                clan_tag="#CLAN", slot="Slot 1", stored_at=3_000_000_000,
                reservation_id="STORED", message_id=43, ready=True,
            )), Participant(user_id="222")],
        )
        self.match._save_state()
        self.clan = Clan.model_construct(id=PydanticObjectId(), tag="#CLAN", availability_str="<#42> occupied",
                                         available_slots=[slot for slot in BASE_STORE_SLOTS if slot != "Slot 1"])
        self.clan._save_state()
        self.events: list[str] = []
        self.reject_clear = False
        self.replace_before_delete = False
        self.match_deleted = False
        self.bot = ipy.Client()
        self.bot.cache.place_channel_data({"id": "42", "type": 0, "name": "match", "guild_id": "50"})
        self.message = {
            "id": "43", "channel_id": "42", "guild_id": "50", "type": 0,
            "author": {"id": "60", "username": "EloBot", "discriminator": "0", "avatar": None},
            "content": "", "timestamp": "2026-09-06T00:00:00+00:00",
            "embeds": [{"title": "**Bases Stored**", "description": "Ready to start."}],
            "components": [{"type": 1, "components": [{
                "type": 2, "style": 2, "label": "Start Match", "custom_id": "stored_start",
            }]}],
        }

        async def edit_message(payload, _channel_id, _message_id, **_kwargs):
            self.message.update(deepcopy(payload))
            self.events.append("retire-message")
            return deepcopy(self.message)

        async def clear_reservation(query, update, **_kwargs) -> UpdateResult:
            if self.reject_clear:
                return UpdateResult({"n": 0, "nModified": 0}, acknowledged=True)
            self.assertEqual(query["participants.0.base_store.reservation_id"], "STORED")
            self.match.participants[0].base_store = None
            self.match.participants[0].opponent_bases_stored = False
            self.events.append("clear-reservation")
            return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

        async def update_clan(document: Clan, operation, **_kwargs) -> Clan:
            for field, value in operation["$set"].items():
                setattr(self.clan, field, value)
            self.events.append("free-clan" if "availability_str" in operation["$set"] else "update-clan")
            return document

        async def set_clan(document: Clan, updates, **_kwargs) -> Clan:
            return await update_clan(document, {"$set": updates})

        async def delete_match(query, **_kwargs) -> DeleteResult:
            self.assertEqual(query["_id"], self.match.id)
            self.assertEqual(query["thread"], "42")
            self.assertEqual(query["participants"], {"$not": {"$elemMatch": {"$or": [
                {"base_store": {"$ne": None}}, {"opponent_bases_stored": True},
            ]}}})
            if self.replace_before_delete:
                self.match.participants[0].base_store = BaseStore(
                    clan_tag="#CLAN", slot="Slot 2", reservation_id="NEW", message_id=99, ready=True,
                )
                self.match.participants[0].opponent_bases_stored = True
                return DeleteResult({"n": 0}, acknowledged=True)
            self.match_deleted = True
            self.events.append("delete-matches")
            return DeleteResult({"n": 1}, acknowledged=True)

        self.bot.http.get_message = AsyncMock(side_effect=lambda *_args: deepcopy(self.message))
        self.bot.http.edit_message = AsyncMock(side_effect=edit_message)
        self.cog = data_commands.DataCommands(self.bot)
        matches = MagicMock(to_list=AsyncMock(side_effect=lambda: [self.match.model_copy(deep=True)]))
        self.enterContext(patch.object(Match, "find", return_value=matches))
        self.enterContext(patch.object(Match, "find_all", return_value=matches))
        self.match_collection = MagicMock(
            update_one=AsyncMock(side_effect=clear_reservation),
            delete_one=AsyncMock(side_effect=delete_match),
            find_one=AsyncMock(side_effect=lambda *_a: None if self.match_deleted else {"_id": self.match.id}),
        )
        self.enterContext(patch.object(Match, "get_pymongo_collection", return_value=self.match_collection))
        self.enterContext(patch.object(Clan, "find_one", new=AsyncMock(side_effect=lambda *_a, **_k: self.clan)))
        self.enterContext(patch.object(Clan, "set", new=set_clan))
        self.enterContext(patch.object(Clan, "update", new=update_clan))
        self.reset_players = AsyncMock(side_effect=lambda *_a, **_k: self.events.append("reset-players"))
        self.enterContext(patch.object(Player, "find_all", return_value=MagicMock(update=self.reset_players)))
        self.delete_recorded = self.enterContext(patch.object(RecordedMatch, "delete_all", new=AsyncMock(
            side_effect=lambda: self.events.append("delete-recorded"),
        )))
        self.delete_matches = self.match_collection.delete_one
        self.bulk_delete_matches = self.enterContext(patch.object(Match, "delete_all", new=AsyncMock()))

    async def invoke(self, ctx: MagicMock) -> None:
        await self.cog.data_reset_all.callback.func(self.cog, ctx)
