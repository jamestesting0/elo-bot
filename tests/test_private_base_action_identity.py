import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import Document, PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pymongo.results import DeleteResult, UpdateResult

import elobot.extensions.base as base_commands
import elobot.extensions.preset as preset_commands
import elobot.features.bases.layouts.private as layouts_private
import elobot.features.bases.layouts.queries as layouts_queries
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import BaseLayout, Player
import elobot.extensions.base as base_module


BASE_LINK = "https://link.clashofclans.com/en?action=OpenLayout&id=TH13%3ACC%3A1%3AAAAAA"


class TestPrivateBaseActionIdentity(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        for model, name in ((BaseLayout, "base_layouts"), (Player, "players")):
            self.enterContext(patch.object(model, "_document_settings", DocumentSettings(
                name=name, use_state_management=True,
            )))
        self.shown = BaseLayout.model_construct(
            id=PydanticObjectId(), district="Barbarian Camp", link=BASE_LINK,
            image_url="https://example.invalid/a.png", quality="Private", builder_id="111",
            expiration_date=3_000_000_000, records=[],
        )
        self.replacement = self.shown.model_copy(update={
            "id": PydanticObjectId(), "link": BASE_LINK.replace("AAAAA", "BBBBB"),
        })
        self.player = Player.model_construct(id="111", user_id="111", tag="#P", elo=1000, highest_elo=1000)
        self.player._save_state()
        self.ctx = MagicMock(spec=ipy.ComponentContext)
        self.ctx.author = SimpleNamespace(id=111, mention="<@111>")
        self.ctx.message = MagicMock(spec=ipy.Message)
        self.ctx.message.interaction = SimpleNamespace(user=111)
        self.ctx.guild.fetch_member = AsyncMock(
            return_value=SimpleNamespace(id=111, display_name="Builder"),
        )
        self.ctx.message.embeds = [base_commands.construct_base_embed(
            self.shown, names={"111": "**[Builder](<https://discordapp.com/users/111>)**"},
        )]
        self.ctx.message.components = [ipy.ActionRow(ipy.Button(
            style=ipy.ButtonStyle.SUCCESS, label="Preset Add", custom_id="p_base_preset|Any|0",
        ))]
        self.ctx.defer, self.ctx.send, self.ctx.edit = AsyncMock(), AsyncMock(), AsyncMock()
        self.base_model = MagicMock()
        self.query = self.base_model.find.return_value
        self.query.sort.return_value.to_list = AsyncMock(return_value=[self.replacement])
        self.base_model.find_one = AsyncMock(return_value=None)
        self.player_model = MagicMock()
        self.player_model.find_one = AsyncMock(return_value=self.player)
        for module, name, value in ((base_commands, "BaseLayout", self.base_model),
                                    (layouts_queries, "BaseLayout", self.base_model),
                                    (layouts_private, "BaseLayout", self.base_model),
                                    (base_commands, "Player", self.player_model),
                                    (layouts_private, "Player", self.player_model),
                                    (preset_commands, "Player", self.player_model)):
            self.enterContext(patch.object(module, name, value))
        self.collection = MagicMock()
        self.collection.name = BaseLayout.Settings.name
        self.collection.delete_one = AsyncMock(return_value=DeleteResult({"n": 1}, acknowledged=True))
        self.enterContext(patch.object(BaseLayout, "get_pymongo_collection", return_value=self.collection))
        self.player_collection = MagicMock()
        self.player_collection.name = Player.Settings.name
        self.player_collection.update_one = AsyncMock(return_value=UpdateResult({"n": 0}, acknowledged=True))
        self.enterContext(patch.object(Player, "get_pymongo_collection", return_value=self.player_collection))
        self.player_set = AsyncMock(return_value=self.player)
        self.enterContext(patch.object(Player, "set", new=self.player_set))
        self.cog = base_module.BaseCommands(MagicMock())

    async def click_private(self, command: str) -> None:
        with patch.object(base_commands, "update_private_base_message", new=AsyncMock()):
            await getattr(self.cog, command).callback.func(self.cog, self.ctx)

    def enable_player_write_boundary(self) -> None:
        self.persisted_player = deepcopy(self.player.get_saved_state())
        self.before_player_write = None

        def matches(query):
            for field, expected in query.items():
                if field == "$or":
                    if not any(matches(option) for option in expected):
                        return False
                    continue
                current = self.persisted_player
                for part in field.split("."):
                    current = current.get(part) if isinstance(current, dict) else None
                if current != expected:
                    return False
            return True

        def apply(query, update):
            if self.before_player_write is not None:
                self.before_player_write()
                self.before_player_write = None
            if not matches(query):
                return False
            for field, value in update["$set"].items():
                target = self.persisted_player
                *parts, last = field.split(".")
                for part in parts:
                    target = target.setdefault(part, {})
                target[last] = value
            return True

        async def update_one(query, update, **_kwargs):
            return UpdateResult({"n": int(apply(query, update))}, acknowledged=True)

        async def find_one_and_update(query, update, **_kwargs):
            return deepcopy(self.persisted_player) if apply(query, update) else None

        self.player_collection.update_one = AsyncMock(side_effect=update_one)
        self.player_collection.find_one_and_update = AsyncMock(side_effect=find_one_and_update)
        self.enterContext(patch.object(Player, "set", Document.set))
        self.enterContext(patch.object(Player, "find_one", classmethod(Document.find_one.__func__)))

    async def test_legacy_delete_never_targets_a_replacement_at_the_same_index(self) -> None:
        # Given: the shown base was removed and another base now occupies index zero.
        self.ctx.custom_id = "p_base_delete|Any|0"
        # When: the old Delete button is clicked.
        await self.click_private("p_base_delete")
        # Then: the replacement is untouched and the user receives no deletion success.
        self.collection.delete_one.assert_not_awaited()
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_legacy_preset_never_targets_a_replacement_at_the_same_index(self) -> None:
        # Given: the shown base is no longer the base at index zero.
        self.ctx.custom_id = "p_base_preset|Any|0"
        # When: its old Preset button is clicked.
        await self.click_private("p_base_preset")
        # Then: no preset changes to the replacement.
        self.player_set.assert_not_awaited()
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_private_view_controls_include_the_shown_document_identity(self) -> None:
        # Given: the current page displays a particular persisted base.
        self.query.sort.return_value.to_list.return_value = [self.shown]
        # When: its controls are rendered through the real SDK objects.
        await base_commands.update_private_base_message(self.ctx, "Any", 0)
        # Then: mutation buttons bind the shown base instead of only a mutable list index.
        row = self.ctx.edit.await_args.kwargs["components"]
        mutation_ids = [button.custom_id for button in row.components[2:]]
        self.assertEqual(mutation_ids, [
            f"p_base_preset|Any|0|{self.shown.id}|add", f"p_base_delete|Any|0|{self.shown.id}",
        ])

    async def test_valid_legacy_delete_resolves_the_displayed_link(self) -> None:
        # Given: an older message still refers to an existing owned base.
        self.ctx.custom_id = "p_base_delete|Any|0"
        self.base_model.find_one.return_value = self.shown
        # When: its original Delete control is used.
        await self.click_private("p_base_delete")
        # Then: the displayed link identifies the deleted document.
        self.assertEqual(self.base_model.find_one.await_args.args[0], {
            "quality": "Private", "builder_id": "111", "link": self.shown.link,
        })
        self.assertEqual(self.collection.delete_one.await_args.args[0], {
            "_id": self.shown.id, "builder_id": "111", "quality": "Private", "district": self.shown.district,
        })
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_bound_delete_uses_identity_when_list_position_changes(self) -> None:
        # Given: another base now occupies the index recorded by a newer message.
        self.ctx.custom_id = f"p_base_delete|Any|0|{self.shown.id}"
        self.base_model.find_one.return_value = self.shown
        # When: the bound Delete control is clicked.
        await self.click_private("p_base_delete")
        # Then: only the document identified by the button is deleted.
        self.assertEqual(self.base_model.find_one.await_args.args[0], {
            "quality": "Private", "builder_id": "111", "_id": self.shown.id,
        })
        self.assertEqual(self.collection.delete_one.await_args.args[0], {
            "_id": self.shown.id, "builder_id": "111", "quality": "Private", "district": self.shown.district,
        })

    async def test_valid_legacy_preset_saves_the_displayed_base(self) -> None:
        # Given: a legacy page's displayed base still belongs to this player.
        self.ctx.custom_id = "p_base_preset|Any|0"
        self.base_model.find_one.return_value = self.shown
        # When: the old Preset control is used.
        await self.click_private("p_base_preset")
        # Then: it persists that base even if list ordering has changed.
        self.player_set.assert_awaited_once_with({f"base_presets.{self.shown.district}": self.shown})
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_stale_preset_remove_does_not_replace_a_new_preset(self) -> None:
        # Given: another action replaced the preset after this Remove button was shown.
        self.ctx.custom_id = "p_base_preset|Any|0"
        self.ctx.message.components[0].components[0].label = "Preset Remove"
        self.base_model.find_one.return_value = self.shown
        self.player.base_presets = {self.shown.district: self.replacement}
        self.player._save_state()
        # When: the stale Remove button is clicked.
        await self.click_private("p_base_preset")
        # Then: it cannot reinstate the old preset or remove the replacement.
        self.player_set.assert_not_awaited()
        self.assertEqual(self.player.base_presets[self.shown.district].id, self.replacement.id)

    async def test_stale_preset_add_does_not_remove_an_already_added_base(self) -> None:
        # Given: another page already added this base while this Add button stayed visible.
        self.ctx.custom_id = "p_base_preset|Any|0"
        self.base_model.find_one.return_value = self.shown
        self.player.base_presets = {self.shown.district: self.shown}
        self.player._save_state()
        # When: the original Add button is clicked.
        await self.click_private("p_base_preset")
        # Then: adding remains idempotent instead of toggling the base away.
        self.player_set.assert_awaited_once_with({f"base_presets.{self.shown.district}": self.shown})
        self.assertEqual(self.player.base_presets[self.shown.district].id, self.shown.id)

    async def test_refresh_after_deleting_last_page_selects_remaining_base(self) -> None:
        # Given: one base remains after deleting the old page at index one.
        self.query.sort.return_value.to_list.return_value = [self.shown]
        # When: the list refreshes using that old index.
        await base_commands.update_private_base_message(self.ctx, "Any", 1)
        # Then: it shows the remaining base and leaves a usable bound control.
        self.assertIn(self.shown.link, self.ctx.edit.await_args.kwargs["embed"].description)

    async def test_remove_all_presets_explicitly_writes_empty_mapping(self) -> None:
        # Given: the player has a persisted base preset.
        self.player.base_presets = {self.shown.district: self.shown}
        self.player._save_state()
        cog = preset_commands.PresetCommands(MagicMock())
        # When: the command removes all presets.
        await cog.preset_base_remove_all.callback.func(cog, self.ctx)
        # Then: Mongo receives an explicit clear, even though Beanie ignores removed dict keys.
        self.assertEqual(self.player_set.await_args.args[0], {"base_presets": {}})

    async def test_builder_edit_writes_only_builder_identity(self) -> None:
        # Given: this snapshot predates independent defense records and a renewal.
        self.base_model.find_one.return_value = self.shown
        self.collection.find_one_and_update = AsyncMock(return_value=self.shown.model_dump(by_alias=True))
        builder = SimpleNamespace(id=222, bot=False, mention="<@222>")
        # When: an operator changes only the builder.
        await self.cog.base_set_builder.callback.func(self.cog, self.ctx, BASE_LINK, builder)
        # Then: neither records nor the expiration can be overwritten by that edit.
        update = self.collection.find_one_and_update.await_args.args[1]
        self.assertEqual(update, {"$set": {"builder_id": "222"}})

    async def test_remove_and_delete_preserve_preset_replaced_at_write_boundary(self) -> None:
        for command in ("p_base_preset", "p_base_delete"):
            with self.subTest(command=command):
                # Given: the callback reads preset A before another command writes B.
                self.base_model.find_one.return_value = self.shown
                self.player.base_presets = {self.shown.district: self.shown}
                self.player._save_state()
                self.enable_player_write_boundary()
                self.before_player_write = lambda: self.persisted_player["base_presets"].update({
                    self.shown.district: self.replacement.to_ref(),
                })
                suffix = "|remove" if command == "p_base_preset" else ""
                self.ctx.custom_id = f"{command}|Any|0|{self.shown.id}{suffix}"
                with patch.object(base_commands, "update_private_base_message", new=AsyncMock()):
                    # When: the actual model reaches its Mongo write boundary.
                    await getattr(self.cog, command).callback.func(self.cog, self.ctx)
                # Then: removal of A cannot clear the replacement B.
                self.assertEqual(self.persisted_player["base_presets"][self.shown.district], self.replacement.to_ref())
                self.assertEqual(self.player_collection.update_one.await_args.args[0], {
                    "_id": self.player.id, "$or": [
                        {f"base_presets.{self.shown.district}._id": self.shown.id},
                        {f"base_presets.{self.shown.district}": self.shown.to_ref()},
                    ],
                })

    async def test_add_reasserts_intent_when_same_base_snapshot_is_outdated(self) -> None:
        # Given: the loaded snapshot says A while another write replaces its stored preset with B.
        self.base_model.find_one.return_value = self.shown
        self.player.base_presets = {self.shown.district: self.shown}
        self.player._save_state()
        self.enable_player_write_boundary()

        async def fetch_snapshot(*_args, **_kwargs):
            self.persisted_player["base_presets"][self.shown.district] = self.replacement.model_dump(by_alias=True)
            return self.player

        self.player_model.find_one.side_effect = fetch_snapshot
        self.ctx.custom_id = f"p_base_preset|Any|0|{self.shown.id}|add"
        with patch.object(base_commands, "update_private_base_message", new=AsyncMock()):
            # When: the user explicitly requests Add A.
            await self.cog.p_base_preset.callback.func(self.cog, self.ctx)
        # Then: the callback writes A despite the stale same-base snapshot.
        self.assertEqual(self.persisted_player["base_presets"][self.shown.district]["_id"], self.shown.id)
        self.assertEqual(set(self.player_collection.find_one_and_update.await_args.args[1]["$set"]), {
            f"base_presets.{self.shown.district}",
        })
        restored = Player.model_validate(self.persisted_player).base_presets[self.shown.district]
        self.assertEqual((restored.id, restored.expiration_date), (self.shown.id, self.shown.expiration_date))

    async def test_remove_matches_current_snapshot_and_legacy_reference(self) -> None:
        for representation in ("snapshot", "reference"):
            with self.subTest(representation=representation):
                self.base_model.find_one.return_value = self.shown
                self.player.base_presets = {self.shown.district: self.shown}
                self.player._save_state()
                self.enable_player_write_boundary()
                if representation == "reference":
                    self.persisted_player["base_presets"][self.shown.district] = self.shown.to_ref()
                self.persisted_player["base_presets"]["Skeleton Park"] = self.replacement.to_ref()
                self.ctx.custom_id = f"p_base_preset|Any|0|{self.shown.id}|remove"
                with patch.object(base_commands, "update_private_base_message", new=AsyncMock()):
                    await self.cog.p_base_preset.callback.func(self.cog, self.ctx)
                self.assertIsNone(self.persisted_player["base_presets"][self.shown.district])
                self.assertEqual(self.persisted_player["base_presets"]["Skeleton Park"], self.replacement.to_ref())

    async def test_delete_requires_current_private_owner_and_district_at_write(self) -> None:
        for changed in ({"builder_id": "222"}, {"quality": "Good"}, {"district": "Skeleton Park"}):
            with self.subTest(changed=changed):
                # Given: staff change the loaded base before its delete reaches Mongo.
                self.base_model.find_one.return_value = self.shown
                row = self.shown.model_dump(by_alias=True)
                deleted = False

                async def delete_one(query, **_kwargs):
                    nonlocal deleted
                    row.update(changed)
                    deleted = all(row.get(field) == value for field, value in query.items())
                    return DeleteResult({"n": int(deleted)}, acknowledged=True)

                self.collection.delete_one.side_effect = delete_one
                self.ctx.send.reset_mock()
                self.ctx.custom_id = f"p_base_delete|Any|0|{self.shown.id}"
                with patch.object(base_commands, "update_private_base_message", new=AsyncMock()):
                    # When: the old owner clicks the bound Delete control.
                    await self.cog.p_base_delete.callback.func(self.cog, self.ctx)
                # Then: the changed base survives and the callback does not report deletion.
                self.assertFalse(deleted)
                self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])
                self.assertEqual(self.ctx.edit.await_args.kwargs["components"], [])
