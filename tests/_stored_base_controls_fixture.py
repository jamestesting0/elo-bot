from unittest.mock import AsyncMock, MagicMock, patch
import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.fields import ExpressionField
from beanie.odm.settings.document import DocumentSettings
from pymongo.results import UpdateResult
import elobot.extensions.match as match_commands
from elobot.features.hosting.war import helper_base_store_availability
from elobot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from elobot.db import BaseStore, Match, Participant


class StoredBaseControlsStateMixin:
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        for field in ("token", "thread"):
            expression_patch = patch.object(Match, field, ExpressionField(field), create=True)
            expression_patch.start()
            self.addCleanup(expression_patch.stop)
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 2", stored_at=3_000_000_000,
                               reservation_id="CURRENT", message_id=456)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="123",
            final_districts=["Barbarian Camp", "Wizard Valley", "Capital Peak"],
            participants=[Participant(user_id="111", base_store=self.store, opponent_bases_stored=True),
                          Participant(user_id="222")],
        )
        self.bot = MagicMock()
        self.bot.user = MagicMock(id=999)
        self.cog = match_commands.MatchCommands(self.bot)
        self.cog._base_store_try_reserve_side = AsyncMock(return_value=MagicMock())
        self.ctx = MagicMock(spec=ipy.ComponentContext)
        self.ctx.author = MagicMock(id=111)
        self.ctx.channel = MagicMock(id=123)
        self.ctx.message = MagicMock(spec=ipy.Message)
        self.ctx.message.id = 456
        self.ctx.message.embeds = [ipy.Embed(title="**Bases Stored**")]
        self.ctx.message.components = []
        self.ctx.message.edit = AsyncMock()
        self.ctx.message.delete = AsyncMock()
        self.ctx.send, self.ctx.defer = AsyncMock(), AsyncMock()
        self.find = AsyncMock(return_value=self.match)
        find_patch = patch.object(Match, "find_one", new=self.find)
        find_patch.start()
        self.addCleanup(find_patch.stop)
        self.collection = MagicMock()
        self.collection.update_one = AsyncMock(return_value=UpdateResult({"n": 1}, acknowledged=True))
        collection_patch = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection_patch.start()
        self.addCleanup(collection_patch.stop)
        for method in ("confirm_host_war_match_on_stored_availability", "confirm_remove_bases_availability"):
            availability_patch = patch.object(helper_base_store_availability, method, new=AsyncMock(return_value=True))
            availability_patch.start()
            self.addCleanup(availability_patch.stop)

    def assert_failed_without_success(self) -> None:
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])
        self.ctx.message.delete.assert_not_awaited()
