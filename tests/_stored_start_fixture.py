import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

import elobot.features.base_store.storage as base_store_storage
import elobot.extensions.match as match_commands
import elobot.features.hosting.war.helper_base_store_availability as helper_base_store_availability
from elobot.extensions.match import MatchCommands
from elobot.features.hosting.war import stored_bases
from elobot.config.game import BASE_STORE_SLOTS, HOST_CLAN_CHOICES
from elobot.db import BaseLink, BaseStore, Participant


HUMAN_ID = "123456789012345678"
PLAYER_ID = "100"
OPPONENT_ID = "200"
TOKEN = "SELFSTORE"
THREAD_ID = "12345"
FIRST_CLAN_TAG = str(HOST_CLAN_CHOICES[0].value)
SECOND_CLAN_TAG = str(HOST_CLAN_CHOICES[1].value)
DISTRICTS = ["Barbarian Camp", "Wizard Valley", "Capital Peak"]
BASE_LINKS = [
    "https://link.clashofclans.com/en?action=OpenLayout&id=one",
    "https://link.clashofclans.com/en?action=OpenLayout&id=two",
    "https://link.clashofclans.com/en?action=OpenLayout&id=three",
]


def terminal_job(job_type: str, status: str) -> stored_bases.HelperStoredBaseTerminalJob:
    return stored_bases.HelperStoredBaseTerminalJob.model_validate({
        "id": "0123456789abcdef0123456789abcdef",
        "type": job_type,
        "status": status,
        "created_at": 100.0,
        "started_at": 101.0,
        "finished_at": 102.0,
        "events": [],
        "result": None,
        "error": "device failed" if status == "failed" else None,
    })


class StoredStartFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        match_commands.RESOURCE_CLAN_LOCKS.clear()
        match_commands.RESOURCE_MATCH_SIDE_LOCKS.clear()
        match_commands._RESOURCE_CLAN_RESERVATIONS.set({})
        match_commands._RESOURCE_MATCH_SIDE_RESERVATIONS.set({})
        self.bot = MagicMock()
        self.bot.user = MagicMock(id=9999, mention="<@9999>")
        self.bot.owner = MagicMock(id=9998)
        self.bot.fetch_user = AsyncMock(side_effect=lambda user_id: MagicMock(id=user_id, mention=f"<@{user_id}>"))
        self.clans = {
            FIRST_CLAN_TAG: SimpleNamespace(
                tag=FIRST_CLAN_TAG,
                name=str(HOST_CLAN_CHOICES[0].name),
                share_link="https://clan.example/first",
            ),
            SECOND_CLAN_TAG: SimpleNamespace(
                tag=SECOND_CLAN_TAG,
                name=str(HOST_CLAN_CHOICES[1].name),
                share_link="https://clan.example/second",
            ),
        }
        self.bot.coc_client.get_clan = AsyncMock(side_effect=lambda tag: self.clans[tag])
        self.cog = MatchCommands(self.bot)
        self.confirm_store_bases_availability = AsyncMock(return_value=True)
        self.availability_patch = patch.object(
            helper_base_store_availability,
            "confirm_store_bases_availability",
            new=self.confirm_store_bases_availability,
        )
        self.availability_patch.start()
        self.addCleanup(self.availability_patch.stop)
        self.host_availability_patch = patch.object(
            helper_base_store_availability,
            "confirm_host_war_match_on_stored_availability",
            new=AsyncMock(return_value=True),
        )
        self.host_availability_patch.start()
        self.addCleanup(self.host_availability_patch.stop)
        self.remove_availability_patch = patch.object(
            helper_base_store_availability,
            "confirm_remove_bases_availability",
            new=AsyncMock(return_value=True),
        )
        self.remove_availability_patch.start()
        self.addCleanup(self.remove_availability_patch.stop)
        retirement_patch = patch.object(base_store_storage, "retire_stored_base_message", new_callable=AsyncMock)
        retirement_patch.start()
        self.addCleanup(retirement_patch.stop)

    @staticmethod
    def make_match(*, stored: bool = False) -> MagicMock:
        player = Participant(
            user_id=PLAYER_ID,
            opponent_bases_stored=stored,
            base_store=(
                BaseStore(clan_tag=FIRST_CLAN_TAG, slot="Slot 2", stored_at=None)
                if stored else None
            ),
        )
        opponent = Participant(
            user_id=OPPONENT_ID,
            bases=[BaseLink(link=link, image=f"https://image.example/{index}")
                   for index, link in enumerate(BASE_LINKS)],
        )
        match = MagicMock(
            token=TOKEN,
            thread=THREAD_ID,
            is_war=True,
            is_autowin=False,
            result_operation=None,
            autowin_winner_id=None,
            times=[None, None],
            hosts=[None, None],
            host_clans=[None, None],
            old_msgs=[None, None],
            final_districts=list(DISTRICTS),
            participants=[player, opponent],
        )
        match.save_changes = AsyncMock()
        match.get_pymongo_collection.return_value.update_one = AsyncMock(
            return_value=MagicMock(matched_count=1),
        )

        async def clear_base_store(index: int, reset_stored_flag: bool = False) -> bool:
            match.participants[index].base_store = None
            if reset_stored_flag:
                match.participants[index].opponent_bases_stored = False
            return True

        async def attach_base_store_message(index: int, message_id: int, *, ready: bool = True) -> bool:
            store = match.participants[index].base_store
            if store is None:
                return False
            store.message_id = message_id
            store.ready = ready
            return True

        match.clear_base_store = AsyncMock(side_effect=clear_base_store)
        match.attach_base_store_message = AsyncMock(side_effect=attach_base_store_message)
        match.base_store_filter.side_effect = lambda index: {
            "token": TOKEN,
            f"participants.{index}.base_store.reservation_id": match.participants[index].base_store.reservation_id,
            f"participants.{index}.base_store.message_id": match.participants[index].base_store.message_id,
            f"participants.{index}.base_store.ready": match.participants[index].base_store.ready,
        }
        return match

    @staticmethod
    def make_slash_context() -> ipy.SlashContext:
        ctx = MagicMock(spec=ipy.SlashContext)
        ctx.author = MagicMock(id=HUMAN_ID, mention=f"<@{HUMAN_ID}>")
        ctx.channel = MagicMock(id=int(THREAD_ID), name="Match Thread")
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.guild = ctx.guild
        message = MagicMock(spec=ipy.Message)
        message.id, message.embeds = 4242, []
        message.edit = AsyncMock()
        ctx.channel.send = AsyncMock(return_value=message)
        ctx.send = AsyncMock()
        return ctx

    @staticmethod
    def make_component_context(action: str) -> ipy.ComponentContext:
        ctx = MagicMock(spec=ipy.ComponentContext)
        ctx.custom_id = f"helper_stored_{action}|{TOKEN}|0"
        ctx.author = MagicMock(id=HUMAN_ID, mention=f"<@{HUMAN_ID}>")
        ctx.channel = MagicMock(id=int(THREAD_ID), name="Match Thread")
        ctx.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: SimpleNamespace(id=user_id, display_name=f"Player {user_id}"),
        )
        ctx.channel.guild = ctx.guild
        ctx.send = AsyncMock()
        ctx.defer = AsyncMock()
        ctx.message = MagicMock()
        ctx.message.id = 4242
        ctx.message.embeds = [ipy.Embed(title="**Bases Stored**")]
        ctx.message.delete = AsyncMock()
        ctx.message.edit = AsyncMock()
        ctx.message.components = []
        return ctx

    @staticmethod
    def slot_map(_clan_tag: str | None = None, *, bot: ipy.Client | None = None) -> dict[str, list[str]]:
        return {
            FIRST_CLAN_TAG: ["Slot 2"],
            SECOND_CLAN_TAG: list(BASE_STORE_SLOTS),
        }
