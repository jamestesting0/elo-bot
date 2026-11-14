import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.errors import OperationFailure

import elobot.extensions.preset as preset_commands
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import BaseLayout, Player
from tests._patch_sites import patch_sites
import elobot.extensions.match.lifecycle as match_lifecycle_module


class TestPresetBaseRemoval(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model in (Player, BaseLayout):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=model.Settings.name, use_state_management=True,
            )))
        base = BaseLayout.model_construct(
            id=PydanticObjectId(), district="Barbarian Camp", link="https://example.test/base",
            image_url="https://example.test/base.png", expiration_date=9_999_999_999,
            quality="Private", builder_id="111",
        )
        self.player = Player.model_construct(
            id="111", user_id="111", tag="#PLAYER", elo=1500, highest_elo=1500,
            base_presets={base.district: base, "Wizard Valley": None},
        )
        self.player._save_state()
        self.persisted = deepcopy(self.player.get_saved_state())
        self.events: list[str] = []
        self.collection = MagicMock()
        self.collection.find_one_and_update = AsyncMock(side_effect=self.persist_update)
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=self.collection))
        self.player_model = self.enterContext(patch.object(preset_commands, "Player"))
        self.player_model.find_one = AsyncMock(return_value=self.player)
        self.bot = MagicMock()
        self.cog = preset_commands.PresetCommands(self.bot)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.author = MagicMock(id=111)
        self.ctx.defer = AsyncMock()
        self.ctx.send = AsyncMock(side_effect=self.send_success)

    async def persist_update(self, query, update, **_kwargs):
        self.assertEqual(query, {"_id": "111"})
        for field, value in update["$set"].items():
            target = self.persisted
            *parts, last = field.split(".")
            for part in parts:
                target = target[part]
            target[last] = deepcopy(value)
        self.events.append("persist")
        return deepcopy(self.persisted)

    async def send_success(self, content: str, *, ephemeral: bool) -> None:
        self.assertEqual(self.persisted["base_presets"], {})
        self.assertEqual(content, f"{SUCCESS_EMOJI} You have successfully removed **all** your base presets.")
        self.assertTrue(ephemeral)
        self.events.append("success")

    async def test_remove_all_clears_persisted_presets_before_success(self) -> None:
        # Given a persisted preset and a legacy null entry, run the registered command.
        await self.cog.preset_base_remove_all.callback.func(self.cog, self.ctx)

        # Then a fresh player has no presets and success follows the database write.
        self.assertEqual(Player.model_validate(deepcopy(self.persisted)).base_presets, {})
        self.assertEqual(self.events, ["persist", "success"])
        self.assertEqual(self.collection.find_one_and_update.await_args.args[1], {
            "$set": {"base_presets": {}},
        })

    async def test_remove_all_database_failure_preserves_presets_without_success(self) -> None:
        # Given MongoDB rejects the removal.
        before = deepcopy(self.persisted)
        self.collection.find_one_and_update.side_effect = OperationFailure("write failed")

        # When removal fails, no success may be published.
        with self.assertRaisesRegex(OperationFailure, "write failed"):
            await self.cog.preset_base_remove_all.callback.func(self.cog, self.ctx)

        self.assertEqual(self.persisted, before)
        self.ctx.send.assert_not_awaited()

    async def test_new_match_after_remove_all_has_no_preset_bases_or_note(self) -> None:
        # Given removal completed before a new match is confirmed.
        await self.cog.preset_base_remove_all.callback.func(self.cog, self.ctx)
        districts = ["Barbarian Camp", "Balloon Lagoon", "Dragon Cliffs"]
        participants = [
            MagicMock(user_id=user_id, districts=districts, submit_bases=False, bases=[None] * 3)
            for user_id in ("111", "222")
        ]
        match = MagicMock(participants=participants, base_submission=True, thread=None)

        async def save_match() -> None:
            self.assertEqual([p.bases for p in participants], [[None] * 3, [None] * 3])
            self.events.append("save match")

        match.save_changes = AsyncMock(side_effect=save_match)
        match.get_pymongo_collection.return_value.update_one = AsyncMock(return_value=MagicMock(matched_count=1))
        message = MagicMock(id=456, content="<@111>\n<@222>", edit=AsyncMock(), pin=AsyncMock())

        async def send_match(*_args, **_kwargs):
            self.assertEqual(self.events[-1], "save match")
            self.events.append("publish match")
            return message

        thread = MagicMock(id=321, send=AsyncMock(side_effect=send_match))
        channel = MagicMock(create_private_thread=AsyncMock(return_value=thread))
        self.bot.fetch_channel = AsyncMock(return_value=channel)
        self.bot.fetch_user = AsyncMock(side_effect=[MagicMock(mention=f"<@{p.user_id}>") for p in participants])
        fresh_player = Player.model_validate(deepcopy(self.persisted))
        opponent = Player.model_construct(id="222", user_id="222", tag="#OTHER", elo=1500, highest_elo=1500)

        # When match creation reloads the removed presets from the persisted record.
        with (
            patch_sites(('elobot.features.hosting.live.live_start.Match', 'elobot.features.hosting.war.war_start.Match', 'elobot.extensions.match.adapters.Match', 'elobot.extensions.match.lifecycle.Match', 'elobot.extensions.match.bases.Match', 'elobot.extensions.match.base_store.Match', 'elobot.extensions.match.results.Match', 'elobot.extensions.match.hosting.Match', 'elobot.extensions.match.dashboards.Match', 'elobot.features.queue.state.Match', 'elobot.features.matches.occupancy.Match', 'elobot.features.matches.resources.Match', 'elobot.features.matches.lifecycle.Match', 'elobot.features.matches.attack_bases.Match', 'elobot.features.results.recording.Match')) as match_model,
            patch_sites(('elobot.features.hosting.war.war_start.Player', 'elobot.extensions.match.queue.Player', 'elobot.extensions.match.lifecycle.Player', 'elobot.extensions.match.results.Player', 'elobot.extensions.match.hosting.Player', 'elobot.features.queue.state.Player', 'elobot.features.queue.matching.Player', 'elobot.features.matches.lifecycle.Player', 'elobot.features.matches.attack_bases.Player', 'elobot.features.results.recording.Player')) as player_model,
        ):
            match_model.find_one = AsyncMock(return_value=match)
            player_model.find_one = AsyncMock(side_effect=[fresh_player, opponent])
            await match_lifecycle_module.confirm_match(self.bot, "TOKEN")

        # Then neither stored match bases nor the published embed claims presets remain.
        self.assertIsNot(fresh_player, self.player)
        self.assertEqual([p.bases for p in participants], [[None] * 3, [None] * 3])
        embed = thread.send.await_args.kwargs["embed"]
        self.assertFalse(any("has preset bases" in field.value for field in embed.fields))
        self.assertEqual(self.events, ["persist", "success", "save match", "publish match"])
