import unittest
from unittest.mock import AsyncMock, patch

import interactions as ipy
from pymongo.results import UpdateResult

import cogs.match_commands as match_commands
from cogs.base_store.controls import resolve_stored_base_control
from cogs.base_store.models import StoredBaseActionRequest, StoredBaseControlFailure, StoredBaseControlState
from cogs.war_hosting import helper_base_store, stored_bases, war_start
from common.constants import SUCCESS_EMOJI
from database_models import Match


from tests._stored_base_controls_fixture import StoredBaseControlsStateMixin

class TestStoredBaseControls(StoredBaseControlsStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_stale_nonce_and_message_identity_are_rejected(self) -> None:
        # Given: each control refers to a different reservation or public message.
        for nonce, message_id in (("OLD", 456), ("CURRENT", 789), (None, 456)):
            with self.subTest(nonce=nonce, message_id=message_id):
                request = StoredBaseActionRequest("TOKEN", 0, nonce, message_id)
                # When: the control resolves the active reservation.
                result = await resolve_stored_base_control(request)
                # Then: it cannot target the replacement's stored bases.
                self.assertIsInstance(result, StoredBaseControlFailure)

    async def test_matching_control_resolves_exact_current_reservation(self) -> None:
        # Given: a control carries both current identities.
        request = StoredBaseActionRequest("TOKEN", 0, "CURRENT", 456)
        # When: it resolves the reservation.
        result = await resolve_stored_base_control(request)
        # Then: its numeric slot and clan belong to that reservation.
        self.assertIsInstance(result, StoredBaseControlState)
        self.assertEqual((result.clan_tag, result.slot, result.numeric_slot), ("#CLAN", "Slot 2", 2))

    async def test_legacy_control_accepts_only_legacy_reservation(self) -> None:
        # Given: preexisting reservations have neither nonce nor message identity.
        self.store.reservation_id, self.store.message_id = None, None
        # When: an old persistent button resolves it.
        result = await resolve_stored_base_control(StoredBaseActionRequest("TOKEN", 0, None, 456))
        # Then: the shipped legacy control remains usable.
        self.assertIsInstance(result, StoredBaseControlState)
        self.assertEqual(self.store.message_id, 456)
        self.collection.update_one.assert_awaited_once()
        self.assertEqual(self.collection.update_one.await_args.args[1], {"$set": {
            "participants.0.base_store.message_id": 456, "participants.0.base_store.ready": True,
        }})

    async def test_stale_helper_buttons_never_start_or_remove_replacement_bases(self) -> None:
        # Given: old Start and Undo buttons remain visible after a replacement.
        for action in ("start", "undo"):
            with self.subTest(action=action):
                self.ctx.custom_id = f"helper_stored_{action}|TOKEN|0|OLD"
                self.ctx.send.reset_mock()
                callback = getattr(self.cog, f"helper_stored_{action}_button")
                with (
                    patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                                 new_callable=AsyncMock) as start,
                    patch.object(helper_base_store, "remove_bases_interactively", new_callable=AsyncMock) as remove,
                ):
                    # When: the registered callback receives the stale click.
                    await callback.callback.func(self.cog, self.ctx)
                # Then: no remote job touches the replacement and the user sees failure.
                start.assert_not_awaited()
                remove.assert_not_awaited()
                self.assert_failed_without_success()
                self.assertEqual(self.ctx.message.edit.await_args.kwargs["components"], [])

    async def test_stale_manual_buttons_never_start_or_clear_replacement_reservation(self) -> None:
        # Given: both old manual controls carry a replaced nonce.
        for action, custom_id in (("start_match", "start_match|0|#CLAN|Slot 2|OLD"),
                                  ("undo_base_storage", "undo_base_storage|TOKEN|0|OLD")):
            with self.subTest(action=action):
                self.ctx.custom_id = custom_id
                self.ctx.send.reset_mock()
                callback = getattr(self.cog, f"{action}_button")
                with (
                    patch.object(war_start, "start_war", new_callable=AsyncMock) as start,
                    patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock) as clear,
                ):
                    # When: the registered manual callback receives the stale click.
                    await callback.callback.func(self.cog, self.ctx)
                # Then: current data and the match remain untouched.
                start.assert_not_awaited()
                clear.assert_not_awaited()
                self.assert_failed_without_success()

    async def test_helper_undo_cannot_claim_success_when_conditional_clear_loses(self) -> None:
        # Given: Helper removed the old bases but a replacement wins the database update.
        self.ctx.custom_id = "helper_stored_undo|TOKEN|0|CURRENT"
        terminal = stored_bases.HelperStoredBaseTerminalJob.model_validate({
            "id": "0123456789abcdef0123456789abcdef", "type": "remove_bases", "status": "succeeded",
            "created_at": 100, "started_at": 101, "finished_at": 102, "events": [], "result": None, "error": None,
        })
        with (
            patch.object(helper_base_store, "remove_bases_interactively", new=AsyncMock(return_value=terminal)),
            patch.object(self.cog, "_base_store_clear_participant", new=AsyncMock(return_value=False)) as clear,
        ):
            # When: Undo tries to release its exact reservation.
            await self.cog.helper_stored_undo_button.callback.func(self.cog, self.ctx)
        # Then: a failed conditional clear cannot become an Undo success.
        clear.assert_awaited_once()
        self.assert_failed_without_success()
        self.assertIs(self.match.participants[0].base_store, self.store)

    async def test_manual_undo_cannot_claim_success_when_conditional_clear_loses(self) -> None:
        # Given: the reservation changes immediately after the manual click is validated.
        self.ctx.custom_id = "undo_base_storage|TOKEN|0|CURRENT"
        with patch.object(match_commands, "clear_participant_base_store", new=AsyncMock(return_value=False)) as clear:
            # When: manual Undo attempts its conditional clear.
            await self.cog.undo_base_storage_button.callback.func(self.cog, self.ctx)
        # Then: it preserves the replacement and responds with failure.
        clear.assert_awaited_once()
        self.assert_failed_without_success()
        self.assertIs(self.match.participants[0].base_store, self.store)

    async def test_incomplete_storage_rejects_helper_and_manual_start(self) -> None:
        # Given: a failed or cancelled copy has a held reservation with incomplete bases.
        self.store.ready = False
        for action, custom_id in (("helper_stored_start", "helper_stored_start|TOKEN|0|CURRENT"),
                                  ("start_match", "start_match|0|#CLAN|Slot 2|CURRENT")):
            with self.subTest(action=action):
                self.ctx.custom_id = custom_id
                self.ctx.send.reset_mock()
                callback = getattr(self.cog, f"{action}_button")
                with (
                    patch.object(helper_base_store, "host_war_match_on_stored_interactively",
                                 new_callable=AsyncMock) as helper_start,
                    patch.object(war_start, "start_war", new_callable=AsyncMock) as local_start,
                ):
                    # When: a Start callback receives the incomplete reservation.
                    await callback.callback.func(self.cog, self.ctx)
                # Then: neither physical hosting nor a durable match start can run.
                helper_start.assert_not_awaited()
                local_start.assert_not_awaited()
                self.assert_failed_without_success()
                self.assertIs(self.match.participants[0].base_store, self.store)

    async def test_finished_incomplete_storage_allows_undo_after_helper_removal_succeeds(self) -> None:
        # Given: partially copied bases remain held in an incomplete reservation.
        self.store.ready = False
        for participant in self.match.participants:
            participant.result = ["3.100", "2.80", "1.50"]
        self.ctx.custom_id = "helper_stored_undo|TOKEN|0|CURRENT"
        terminal = stored_bases.HelperStoredBaseTerminalJob.model_validate({
            "id": "0123456789abcdef0123456789abcdef", "type": "remove_bases", "status": "succeeded",
            "created_at": 100, "started_at": 101, "finished_at": 102, "events": [], "result": None, "error": None,
        })
        with (
            patch.object(helper_base_store, "remove_bases_interactively",
                         new=AsyncMock(return_value=terminal)) as remove,
            patch.object(self.cog, "_base_store_clear_participant", new=AsyncMock(return_value=True)) as clear,
        ):
            # When: the user removes those partially copied bases.
            await self.cog.helper_stored_undo_button.callback.func(self.cog, self.ctx)
        # Then: confirmed removal can release the reservation and retire the message.
        remove.assert_awaited_once()
        clear.assert_awaited_once_with(self.match, 0, reset_stored_flag=True, reason="undone")
        self.assertEqual(self.ctx.message.edit.await_args.kwargs["components"], [])
        payload = self.ctx.message.edit.await_args.kwargs["embeds"][0]
        self.assertEqual(payload["title"] if isinstance(payload, dict) else payload.title, "**Base Storage Undone**")
        self.assertFalse(payload.get("description") if isinstance(payload, dict) else payload.description)
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_finished_legacy_manual_undo_binds_before_clearing_reservation(self) -> None:
        # Given: a legacy reservation has no persisted public message identity.
        self.store.reservation_id, self.store.message_id = None, None
        for participant in self.match.participants:
            participant.result = ["3.100", "2.80", "1.50"]
        self.ctx.custom_id = "undo_base_storage|TOKEN|0"

        async def clear_bound_reservation(match: Match, player: int, *, reset_stored_flag: bool,
                                          bot: ipy.Client, reason: str) -> bool:
            self.assertEqual(match.participants[player].base_store.message_id, 456)
            self.assertEqual(reason, "undone")
            return True

        with patch.object(match_commands, "clear_participant_base_store",
                          new=AsyncMock(side_effect=clear_bound_reservation)) as clear:
            # When: the old manual Undo control is used.
            await self.cog.undo_base_storage_button.callback.func(self.cog, self.ctx)
        # Then: persisted binding precedes clearing and supplies the message to retire.
        self.collection.update_one.assert_awaited_once()
        query, update = self.collection.update_one.await_args.args
        self.assertIsNone(query["participants.0.base_store.message_id"])
        self.assertEqual(update["$set"]["participants.0.base_store.message_id"], 456)
        clear.assert_awaited_once_with(
            self.match, 0, reset_stored_flag=True, bot=self.bot, reason="undone",
        )
        self.assertIn(str(SUCCESS_EMOJI), self.ctx.send.await_args.args[0])

    async def test_legacy_manual_undo_binding_loss_cannot_clear_or_claim_success(self) -> None:
        # Given: the legacy reservation changes before its message can be bound.
        self.store.reservation_id, self.store.message_id = None, None
        self.collection.update_one.return_value = UpdateResult({"n": 0}, acknowledged=True)
        self.ctx.custom_id = "undo_base_storage|TOKEN|0"
        with patch.object(match_commands, "clear_participant_base_store", new_callable=AsyncMock) as clear:
            # When: the old Undo control loses its conditional binding update.
            await self.cog.undo_base_storage_button.callback.func(self.cog, self.ctx)
        # Then: neither clearing nor a success response can follow that failed binding.
        clear.assert_not_awaited()
        self.assertIsNone(self.store.message_id)
        self.assert_failed_without_success()
