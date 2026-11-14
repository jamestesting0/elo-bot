import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.results import UpdateResult

from elobot.features.base_store import storage
from elobot.features.hosting.war import war_start
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.features.matches.cleanup import delete_unreserved_match
from elobot.db import BaseStore


from tests._match_cleanup_fixture import MatchCleanupStateMixin
from tests._patch_sites import patch_sites
import elobot.extensions.match.lifecycle as match_lifecycle_module

class TestMatchCleanupConsistency(MatchCleanupStateMixin, unittest.IsolatedAsyncioTestCase):
    async def test_manual_thread_delete_retains_match_when_discord_delete_fails(self) -> None:
        # Given: Discord rejects deletion after the staff confirmation.
        self.ctx.channel.delete.side_effect = ipy.errors.HTTPException(MagicMock(status=503))
        # When: the real registered command runs.
        with self.assertRaises(ipy.errors.HTTPException):
            await self.thread_cog.thread_delete.callback.func(self.thread_cog, self.ctx)
        # Then: its Match document remains available for retry.
        self.assertIsNotNone(self.persisted)
        self.collection.delete_one.assert_not_awaited()
        self.free.assert_not_awaited()

    async def test_deleted_thread_event_preserves_store_inserted_after_cleanup(self) -> None:
        # Given: a new reservation appears at the last deletion boundary.
        self.insert_before_delete = True
        # When: the actual ThreadDelete listener cleans the old snapshot.
        await self.thread_cog.on_thread_delete.callback(self.thread_cog, MagicMock(thread=self.ctx.channel))
        # Then: the replacement identity survives and its resources are not freed.
        self.assertIsNotNone(self.persisted)
        self.assertEqual(self.persisted["participants"][0]["base_store"]["reservation_id"], "NEW")
        self.free.assert_not_awaited()

    async def test_scheduled_cleanup_preserves_store_inserted_after_cleanup(self) -> None:
        # Given: a missing-thread match gains a replacement before deletion.
        self.insert_before_delete = True
        # When: the registered cleanup task processes it.
        await self.match_cog.match_data_clean_up.callback.func(self.match_cog)
        # Then: its current reservation is retained for a subsequent cleanup attempt.
        self.assertIsNotNone(self.persisted)
        self.free.assert_not_awaited()

    async def test_participant_cas_conflict_stops_thread_cleanup(self) -> None:
        # Given: the loaded store is older than the reservation in MongoDB.
        self.match.participants[0].base_store = BaseStore(clan_tag="#OLD", slot="Slot 1", reservation_id="OLD")
        self.install_replacement()
        self.reject_clear = True
        # When: the real aggregate processes the failed Match CAS.
        with self.assertRaises(storage.BaseStoreReleaseConflict):
            await self.thread_cog.on_thread_delete.callback(self.thread_cog, MagicMock(thread=self.ctx.channel))
        # Then: a rejected release cannot lead to deletion or resource cleanup.
        self.collection.delete_one.assert_not_awaited()
        self.assertIsNotNone(self.persisted)
        self.free.assert_not_awaited()

    async def test_match_cancel_confirmation_cannot_delete_an_accepted_match(self) -> None:
        # Given: acceptance and storage finish while cancellation waits for confirmation.
        self.make_pre_thread()
        self.ctx.custom_id = "match_cancel|TOKEN"
        self.bot.wait_for_component.side_effect = self.accept_during_confirmation
        # When: the old confirmation callback resumes.
        await self.match_cog.match_cancel_button.callback.func(self.match_cog, self.ctx)
        # Then: the active match survives without cancellation notifications or penalties.
        self.assertIsNotNone(self.persisted)
        self.bot.fetch_user.assert_not_awaited()
        self.player.save_changes.assert_not_awaited()
        self.assertNotIn(str(SUCCESS_EMOJI), self.confirm.send.await_args.args[0])


    async def test_challenge_confirmation_preserves_accepted_match(self) -> None:
        for callback in ("challenge_cancel", "challenge_reject"):
            with self.subTest(callback=callback):
                self.persisted = self.match.model_dump(by_alias=True)
                await self.invoke_stale_challenge(callback)
                self.assertIsNotNone(self.persisted)
                self.bot.fetch_user.assert_not_awaited()

    async def test_automatic_challenge_expiry_preserves_accepted_match(self) -> None:
        # Given: the challenge is accepted at the expiry deletion boundary.
        self.make_pre_thread()
        self.insert_before_delete = True
        # When: the registered expiry task runs.
        await self.challenge_cog.auto_reject_challenges.callback.func(self.challenge_cog)
        # Then: neither deletion nor expiry notifications apply to the accepted run.
        self.assertIsNotNone(self.persisted)
        self.bot.fetch_user.assert_not_awaited()

    async def test_automatic_match_expiry_preserves_concurrent_acceptance(self) -> None:
        # Given: the unconfirmed match is accepted at the deletion boundary.
        self.make_pre_thread()
        self.insert_before_delete = True
        # When: the automatic cancellation helper uses its real Match boundary.
        await match_lifecycle_module.cancel_match(self.bot, "TOKEN")
        # Then: the accepted match stays present and receives no expiry notification.
        self.assertIsNotNone(self.persisted)
        self.bot.fetch_user.assert_not_awaited()

    async def test_confirmed_participant_change_prevents_pre_thread_cancellation(self) -> None:
        # Given: acceptance has committed but its Discord thread has not yet been created.
        self.make_pre_thread()
        self.persisted["participants"][1]["confirmed"] = True
        # When: deletion compares the original pre-thread state.
        deleted = await delete_unreserved_match(self.match, pre_thread=True)
        # Then: that confirmation alone prevents deletion.
        self.assertFalse(deleted)
        self.assertIsNotNone(self.persisted)

    async def test_legacy_stored_flag_blocks_final_deletion(self) -> None:
        # Given: a legacy reservation flag appeared after the snapshot was loaded.
        self.persisted["participants"][0]["opponent_bases_stored"] = True
        # When: deletion checks the current reservation state.
        deleted = await delete_unreserved_match(self.match)
        # Then: an unmapped stored slot also prevents deletion.
        self.assertFalse(deleted)
        self.assertIsNotNone(self.persisted)

    async def test_already_deleted_match_is_an_idempotent_cleanup_success(self) -> None:
        # Given: another cleanup worker already removed this exact Match.
        self.persisted = None
        # When: the same cleanup deletion is retried.
        deleted = await delete_unreserved_match(self.match)
        # Then: no stale-record conflict is invented for an absent document.
        self.assertTrue(deleted)

    async def test_manual_thread_deletion_finishes_before_match_deletion(self) -> None:
        # Given: the Discord deletion succeeds for a fully released Match.
        async def delete_thread(**_kwargs):
            self.assertIsNotNone(self.persisted)

        self.ctx.channel.delete.side_effect = delete_thread
        # When: the registered staff command completes.
        await self.thread_cog.thread_delete.callback.func(self.thread_cog, self.ctx)
        # Then: the database deletion and resource release follow Discord success.
        self.assertIsNone(self.persisted)
        self.free.assert_awaited_once_with(self.match)

    async def test_war_abort_retains_unmapped_store_and_reports_unavailable_message(self) -> None:
        # Given: an active match has a held reservation without a mapped message.
        self.match.times, self.match.hosts, self.match.host_clans = [1000, None], ["999", None], ["#OLD", None]
        self.match.old_msgs, self.match.persistent_host_clans = [77, None], ["#OLD", None]
        self.match.participants[0].base_store = BaseStore(clan_tag="#OLD", slot="Slot 1", reservation_id="OLD")
        self.match.participants[0].opponent_bases_stored = True
        store = self.match.participants[0].base_store
        self.collection.update_one.side_effect = [UpdateResult({"n": 1}, True)]
        message = MagicMock(id=77, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with patch_sites(('elobot.extensions.match.result_store.free_clan', 'elobot.extensions.match.dashboards.free_clan', 'elobot.features.matches.resources.free_clan'), new_callable=AsyncMock) as free, \
                patch.object(war_start, "schedule_channel_name_edit") as rename:
            # When: abort commits but cannot update the retained reservation's controls.
            with self.assertRaises(war_start.BaseStoreMessageConflict):
                await war_start.abort_war_start(self.ctx, war_start.WarAbort(self.match, 0, message))
        # Then: the slot stays held, normal abort cleanup completes, and no complete UI is reported.
        self.assertEqual(self.collection.update_one.await_count, 1)
        self.assertIs(self.match.participants[0].base_store, store)
        self.assertTrue(self.match.participants[0].opponent_bases_stored)
        self.assertIsNone(self.match.times[0])
        message.delete.assert_not_awaited()
        self.assertEqual(message.edit.await_args.kwargs["embeds"][0].title, "**Match Aborted**")
        self.assertEqual(message.edit.await_args.kwargs["components"], [])
        self.assertEqual(self.match.old_msgs[0], 77)
        free.assert_awaited_once_with("#OLD", expected_thread=str(self.match.thread))
        rename.assert_not_called()
