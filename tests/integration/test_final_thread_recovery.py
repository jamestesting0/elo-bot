from unittest.mock import AsyncMock, MagicMock

import interactions as ipy

from tests.integration._final_fixture import FinalScenarioCase
from cogs.base_store.storage import clear_match_base_stores
from common.finals.hosting import accept_final_start
from common.finals.lifecycle import confirm_bracket, resolve_final
from common.finals.matches import bind_thread, prepare_match
from common.finals.recovery import recover_final
from common.finals.results import FinalResultSubmission, submit_result
from common.tokens.bets import open_market
from elobot.db import BaseStore, Clan, Helper, Match
from elobot.db.token_models import FinalBettingMarket


class TestFinalThreadRecovery(FinalScenarioCase):
    async def pairing(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1", is_war=False)
        await source.set({"thread": "777", "is_war": False, "hosts": ["host", None], "times": [123, None],
                          "participants.0.base_store": BaseStore(clan_tag="#TEST", slot="Slot 1",
                                                               reservation_id="owned").model_dump(),
                          "participants.0.opponent_bases_stored": True})
        await Clan(tag="#TEST", name="Test", availability_str="Available", available_slots=[]).insert()
        source = await Match.get(source.id)
        await bind_thread(final, source, "777")
        return await resolve_final(), source

    async def submit(self, final, source, *, forfeit=False):
        node = final.nodes["QF1"]
        return await submit_result(FinalResultSubmission(final_id=final.id, node_id="QF1", actor_id="manager",
            operation_key="result", expected_version=0, contestant_revision=node.contestant_revision,
            attempt=node.attempt, scores={user: ["0.00" if forfeit else "3.100" if side == 0 else "2.80"] * 7
                for side, user in enumerate(node.contestants)}, winner=node.contestants[0] if forfeit else None,
            outcome="Forfeit" if forfeit else "Played", reason="No show" if forfeit else None))

    async def release_deleted(self, source):
        await clear_match_base_stores(source, reset_stored_flags=True, reason="thread_deleted")

    def deleted_bot(self):
        channel = MagicMock(send=AsyncMock(return_value=MagicMock(id=900)))
        channel.guild.fetch_member = AsyncMock(return_value=None)
        bot = MagicMock()
        async def fetch(channel_id):
            if channel_id == 777:
                raise ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))
            return channel
        bot.fetch_channel = AsyncMock(side_effect=fetch)
        return bot, channel

    async def test_deleted_unplayed_attempt_releases_exact_owner_and_requires_new_manager_attempt(self):
        final, source = await self.pairing()
        market = await open_market(final, "QF1")
        await accept_final_start(source, "host", player=None)
        bot, _ = self.deleted_bot()
        pending = await recover_final(final, bot=bot, release_deleted=self.release_deleted)
        current = await resolve_final()
        self.assertIn("thread deleted", " ".join(pending))
        self.assertEqual(current.nodes["QF1"].state, "NEEDS_MANAGER")
        self.assertIsNone(current.nodes["QF1"].match_id)
        old = await Match.get(source.id)
        self.assertTrue(old.final_superseded)
        self.assertIsNone(old.participants[0].base_store)
        replacement = await prepare_match(current, "QF1")
        self.assertNotEqual(replacement.id, source.id)
        self.assertEqual(replacement.final_attempt, 2)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
        self.assertEqual((await resolve_final()).nodes["QF1"].first_actual_started_at,
                         current.nodes["QF1"].first_actual_started_at)

    async def test_forbidden_thread_keeps_binding_and_reservation(self):
        final, source = await self.pairing()
        bot = MagicMock(fetch_channel=AsyncMock(side_effect=ipy.errors.Forbidden(
            MagicMock(status=403, reason="Forbidden"))))
        pending = await recover_final(final, bot=bot, release_deleted=self.release_deleted)
        self.assertIn("could not be verified", " ".join(pending))
        self.assertEqual((await resolve_final()).nodes["QF1"].match_id, source.id)
        self.assertEqual((await Match.get(source.id)).participants[0].base_store.reservation_id, "owned")

    async def test_completed_play_releases_only_after_durable_helper_credit_and_retries_once(self):
        final, source = await self.pairing()
        result = await self.submit(final, source)
        observations = []
        async def release(saved):
            observations.append(((await Helper.find_one({"user_id": "host"})).points,
                                 saved.final_result_operation.completed))
            await clear_match_base_stores(saved, reset_stored_flags=True, reason="finished")
        await recover_final(final, release_completed=release)
        await recover_final(final, release_completed=release)
        self.assertEqual(observations, [(3, True)])
        self.assertEqual((await Helper.find_one({"user_id": "host"})).operation_receipts,
                         [f"result:{source.id}:host:live"])
        self.assertIsNone((await Match.get(source.id)).participants[0].base_store)
        self.assertTrue((await resolve_final()).result_progress[result.id].release_completed)

    async def test_forfeit_retains_slot_until_confirmed_thread_deletion_and_publishes_fallback(self):
        final, source = await self.pairing()
        result = await self.submit(final, source, forfeit=True)
        await recover_final(final, release_completed=self.release_deleted)
        self.assertIsNotNone((await Match.get(source.id)).participants[0].base_store)
        bot, channel = self.deleted_bot()
        await recover_final(final, bot=bot, release_deleted=self.release_deleted)
        self.assertIsNone((await Match.get(source.id)).participants[0].base_store)
        progress = (await resolve_final()).result_progress[result.id]
        self.assertTrue(progress.thread_deleted)
        self.assertTrue(progress.published)
        self.assertEqual(progress.channel_id, str(final.channel_id))
        forfeit = channel.send.await_args.kwargs["embed"].fields[1]
        self.assertEqual(forfeit.name, "**Forfeit**")
        self.assertEqual(forfeit.value,
                         f"**[❌Invalid Player](<https://discordapp.com/users/{result.snapshot.winner}>)** wins")

    async def test_saved_handoff_survives_source_document_loss_with_exact_helper_receipt(self):
        final, source = await self.pairing()
        result = await self.submit(final, source)
        await source.delete()
        await recover_final(final)
        await recover_final(final)
        helper = await Helper.find_one({"user_id": "host"})
        self.assertEqual(helper.points, 3)
        self.assertEqual(len(helper.history), 1)
        self.assertTrue((await resolve_final()).result_progress[result.id].completed)

    async def test_restart_binds_only_the_thread_saved_on_the_exact_attempt(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await prepare_match(final, "QF1")
        await source.set({"thread": "777"})
        await recover_final(final)
        node = (await resolve_final()).nodes["QF1"]
        self.assertEqual(node.thread_id, "777")
        self.assertFalse(node.provisioning)
        self.assertEqual(await Match.count(), 1)
