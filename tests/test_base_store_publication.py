from collections.abc import Mapping
from copy import deepcopy
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from beanie import PydanticObjectId
from beanie.odm.settings.document import DocumentSettings
from pydantic import JsonValue
from pymongo.errors import PyMongoError
from pymongo.results import UpdateResult

from cogs.base_store.models import BaseStorePresentationRequest
from cogs.base_store.presentation import build_helper_store_components, build_manual_store_components
from cogs.base_store.publication import publish_base_store_message
from common.constants import ERROR_EMOJI, SUCCESS_EMOJI
from database_models import BaseStore, Match, Participant


class TestBaseStorePublication(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        settings = patch.object(
            Match, "_document_settings", DocumentSettings(name="matches", use_state_management=True),
        )
        settings.start()
        self.addCleanup(settings.stop)
        self.store = BaseStore(clan_tag="#CLAN", slot="Slot 2", stored_at=3_000_000_000,
                               reservation_id="CURRENT", ready=False)
        self.match = Match.model_construct(
            id=PydanticObjectId(), token="TOKEN", matched_time=1, thread="123",
            participants=[Participant(user_id="111", base_store=self.store, opponent_bases_stored=True),
                          Participant(user_id="222")],
        )
        self.match._save_state()
        clan = MagicMock()
        clan.name, clan.tag, clan.share_link = "A Clan", "#CLAN", "https://clan.example"
        self.request = BaseStorePresentationRequest(self.match, 0, "999", clan)
        self.components = build_helper_store_components(self.request)
        self.trace: list[str] = []
        self.message = MagicMock(spec=ipy.Message)
        self.message.id, self.message.embeds = 456, []
        self.message.edit = AsyncMock(side_effect=self.edit_message)
        self.ctx = MagicMock(spec=ipy.SlashContext)
        self.ctx.channel = MagicMock()
        self.ctx.channel.guild.fetch_member = AsyncMock(
            side_effect=lambda user_id: MagicMock(id=user_id, display_name=f"Member {user_id}"),
        )
        self.ctx.channel.send = AsyncMock(side_effect=self.send_message)
        self.ctx.send = AsyncMock()
        self.collection = MagicMock()
        self.collection.update_one = AsyncMock(side_effect=self.update_reservation)
        collection_patch = patch.object(Match, "get_pymongo_collection", return_value=self.collection)
        collection_patch.start()
        self.addCleanup(collection_patch.stop)
        self.find = AsyncMock(return_value=self.match)
        find_patch = patch.object(Match, "find_one", new=self.find)
        find_patch.start()
        self.addCleanup(find_patch.stop)

    async def send_message(self, *, embed: ipy.Embed, components: list[ipy.Button]) -> MagicMock:
        self.trace.append("pending")
        self.assertEqual(components, [])
        self.message.embeds = [deepcopy(embed)]
        return self.message

    async def edit_message(self, *, embed: ipy.Embed | dict[str, JsonValue] | None = None,
                           embeds: list[ipy.Embed] | None = None, components: list[ipy.Button]) -> None:
        self.trace.append("enabled" if components else "unavailable")
        self.message.embeds = [
            ipy.Embed.from_dict(deepcopy(item)) if isinstance(item, dict) else deepcopy(item)
            for item in (embeds if embeds is not None else [embed])
        ]
        self.message.components = components

    async def update_reservation(self, query: Mapping[str, JsonValue | PydanticObjectId],
                                 update: Mapping[str, Mapping[str, int]]) -> UpdateResult:
        self.trace.append("persist")
        return UpdateResult({"n": 1, "nModified": 1}, acknowledged=True)

    def assert_unavailable(self) -> None:
        self.assertEqual(self.message.components, [])
        self.assertEqual(self.message.embeds[0].title, "**Base Storage Unavailable**")
        self.assertFalse(self.message.embeds[0].description)
        self.assertTrue(self.ctx.send.await_args.kwargs["ephemeral"])
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_success_enables_controls_only_after_message_identity_is_persisted(self) -> None:
        # Given: an existing stored reservation has no message binding.
        # When: its success message is published.
        result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: the only enabled message is bound to the committed reservation.
        self.assertTrue(result)
        self.assertEqual(self.trace, ["pending", "persist", "enabled"])
        self.assertEqual(self.store.message_id, 456)
        self.assertIs(self.store.ready, True)
        self.assertEqual(self.collection.update_one.await_args.args[1], {"$set": {
            "participants.0.base_store.message_id": 456, "participants.0.base_store.ready": True,
        }})
        self.find.assert_awaited_once_with(self.match.base_store_filter(0))
        self.assertEqual(self.message.embeds[0].title, "**Bases Stored**")
        self.assertFalse(self.ctx.channel.send.await_args.kwargs["embed"].description)
        self.assertFalse(self.message.embeds[0].description)
        self.assertEqual(self.message.embeds[0].fields[-1].value, "Slot 2")
        self.assertEqual([button.custom_id for button in self.message.components],
                         ["helper_stored_start|TOKEN|0|CURRENT", "helper_stored_undo|TOKEN|0|CURRENT"])
        self.ctx.send.assert_not_awaited()

    async def test_replaced_reservation_cannot_publish_success(self) -> None:
        # Given: the reservation was replaced before the pending message could bind.
        self.collection.update_one.side_effect = None
        self.collection.update_one.return_value = UpdateResult({"n": 0}, acknowledged=True)
        # When: publication attempts the conditional binding.
        result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: the visible message stays present with failure and no controls.
        self.assertFalse(result)
        self.assertEqual(self.trace, ["pending", "unavailable"])
        self.assertIsNone(self.store.message_id)
        self.assertIs(self.store.ready, False)
        self.find.assert_not_awaited()
        self.assert_unavailable()

    async def test_manual_slot_reservation_does_not_claim_bases_were_copied(self) -> None:
        # Given: a helper reserves a slot before manually copying the bases.
        request = BaseStorePresentationRequest(self.match, 0, "111", self.request.clan, bases_stored=False)
        components = build_manual_store_components(request, "Slot 2")
        # When: the reserved slot's controls are published.
        result = await publish_base_store_message(self.ctx, request, components)
        # Then: the visible state describes the committed reservation and identifies its controls.
        self.assertTrue(result)
        self.assertEqual(self.message.embeds[0].title, "**Base Slot Reserved**")
        self.assertFalse(self.message.embeds[0].description)
        self.assertEqual([button.custom_id for button in self.message.components],
                         ["start_match|0|#CLAN|Slot 2|CURRENT", "undo_base_storage|TOKEN|0|CURRENT"])

    async def test_incomplete_publication_keeps_reservation_unready_with_undo_only(self) -> None:
        # Given: Helper stopped after possibly copying part of the requested bases.
        request = BaseStorePresentationRequest(self.match, 0, "999", self.request.clan,
                                               failure_reason="Copying did not complete.")
        components = build_helper_store_components(request)[1:]
        # When: the recovery message is bound to that held reservation.
        result = await publish_base_store_message(self.ctx, request, components)
        # Then: the published recovery state remains incomplete and cannot enable Start.
        self.assertTrue(result)
        self.assertIs(self.store.ready, False)
        self.assertEqual(self.store.message_id, 456)
        self.assertEqual(self.collection.update_one.await_args.args[1], {"$set": {
            "participants.0.base_store.message_id": 456, "participants.0.base_store.ready": False,
        }})
        self.assertEqual(self.message.embeds[0].title, "**Base Storage Incomplete**")
        self.assertFalse(self.message.embeds[0].description)
        self.assertEqual([button.custom_id for button in self.message.components],
                         ["helper_stored_undo|TOKEN|0|CURRENT"])

    async def test_reservation_loss_during_success_edit_retires_controls(self) -> None:
        # Given: a release wins while Discord is enabling the stored-base message.
        self.find.return_value = None
        # When: publication performs its final durable reservation check.
        result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: publication reports failure and removes those controls.
        self.assertFalse(result)
        self.assertEqual(self.trace, ["pending", "persist", "enabled", "unavailable"])
        self.assert_unavailable()

    async def test_database_failure_preserves_slot_and_reports_unavailable(self) -> None:
        # Given: MongoDB cannot confirm the message binding.
        self.collection.update_one.side_effect = PyMongoError("write failed")
        # When: publication reaches the database boundary.
        with self.assertLogs(level="ERROR"):
            result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: it never frees possibly stored bases or displays success.
        self.assertFalse(result)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        self.assertIsNone(self.store.message_id)
        self.assert_unavailable()

    async def test_discord_send_failure_preserves_reservation_with_bound_undo_recovery(self) -> None:
        # Given: Discord rejects the initial public message.
        self.ctx.channel.send.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        self.ctx.send.return_value = self.message

        async def edit_interaction(_message, **kwargs):
            await self.edit_message(**kwargs)

        self.ctx.edit = AsyncMock(side_effect=edit_interaction)
        # When: publication cannot create its pending message.
        with self.assertLogs(level="ERROR"):
            result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: the reservation stays held and the interaction provides a bound Undo control.
        self.assertFalse(result)
        self.collection.update_one.assert_awaited_once()
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertEqual(self.store.message_id, 456)
        self.assertFalse(self.store.ready)
        self.ctx.edit.assert_awaited_once()
        self.assertEqual([button.custom_id for button in self.message.components],
                         ["helper_stored_undo|TOKEN|0|CURRENT"])
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])
        self.assertNotIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_discord_success_edit_failure_does_not_release_stored_bases(self) -> None:
        # Given: binding commits but Discord fails to display the controls.
        self.message.edit.side_effect = [ipy.errors.HTTPException(response=MagicMock(status=500)), None]
        # When: publication handles the failed success edit.
        with self.assertLogs(level="ERROR"):
            result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: it holds the reservation and attempts to leave an inactive failure message.
        self.assertFalse(result)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertEqual(self.store.message_id, 456)
        self.assertEqual([button.custom_id for button in self.message.edit.await_args.kwargs["components"]],
                         ["helper_stored_undo|TOKEN|0|CURRENT"])
        self.assertFalse(self.store.ready)
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])

    async def test_failure_to_retire_public_message_still_reports_interaction_failure(self) -> None:
        # Given: Discord rejects both the success edit and the inactive fallback edit.
        self.message.edit.side_effect = ipy.errors.HTTPException(response=MagicMock(status=500))
        # When: publication cannot update its public message.
        with self.assertLogs(level="ERROR"):
            result = await publish_base_store_message(self.ctx, self.request, self.components)
        # Then: the requester receives failure while the physical slot remains held.
        self.assertFalse(result)
        self.assertIs(self.match.participants[0].base_store, self.store)
        self.assertIn(str(ERROR_EMOJI), self.ctx.send.await_args.args[0])
