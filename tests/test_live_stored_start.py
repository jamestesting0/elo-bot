import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.features.base_store.publication import mark_store_reserved
from elobot.features.hosting.live import live_start
from elobot.features.hosting.live import helper_live
from elobot.features.hosting.war.helper_host_job import run_helper_host_job
from elobot.features.base_store.live import live_store_components
from tests._live_base_fixture import CLAN, DISTRICTS, PAIR, LiveBaseFixture


class TestLiveStoredStart(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.f = LiveBaseFixture()
        await self.f.setup(self)
        self.message = self.f.add_pair()
        self.match = await self.f.find({})
        self.message.embeds = [ipy.Embed(
            title="**Bases Stored**",
            fields=[ipy.EmbedField(name="**Slots**", value="<@111> — Slot 1\n<@222> — Slot 2")],
        )]
        self.message.components = [ipy.ActionRow(*live_store_components(self.match, PAIR, allow_start=True))]
        self.request = helper_live.AutomaticLiveHost(
            match=self.match, clan=self.f.clan, host=self.f.bot.user,
            player1=SimpleNamespace(tag="#PPY099"), player2=SimpleNamespace(tag="#LLQ288"),
            player1_elo=1000, player2_elo=1000, scout=False, requested_by="111",
            allow_opponent_cancel=True, stored_reservation_id=PAIR,
        )
        self.availability = self.enterContext(patch.object(helper_live, "confirm_helper_availability",
                                                         new=AsyncMock(return_value=True)))
        self.choose = self.enterContext(patch.object(helper_live.match_commands, "choose_bases", new_callable=AsyncMock))
        self.slots = self.enterContext(patch.object(helper_live.live_eligibility, "clan_has_all_base_store_slots",
                                                  new=AsyncMock(return_value=False)))

    async def test_accepted_storage_uses_exact_stored_job_without_recopying(self):
        self.f.jobs(["succeeded"], "host_live_match_on_stored")
        start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=self.f.message()))
        async def run(runtime, ctx, request):
            self.assertEqual(request.additional_cancel_user_ids, frozenset({"222"}))
            self.assertTrue(helper_live.match_commands.RESOURCE_MATCH_SIDE_LOCKS[("LIVE", 1)].locked())
            await run_helper_host_job(runtime, ctx, request)
        with patch.object(live_start, "start_live_match", new=start), patch.object(helper_live, "run_helper_host_job", new=run):
            await helper_live.host_live_automatically(self.f.runtime, self.f.ctx, self.request)
        self.assertEqual(self.f.http.create_payloads, [{
            "type": "host_live_match_on_stored", "requested_by": "111", "payload": {
                "host_clan_tag": CLAN, "player_1_tag": "#PPY099", "player_2_tag": "#LLQ288",
                "districts": DISTRICTS, "remove_bases_after": False,
            },
        }])
        self.assertEqual(start.await_args.args[2].stored_reservation_id, PAIR)
        self.availability.assert_awaited_once_with(
            self.f.runtime, self.f.ctx, "host_live_match_on_stored", public_progress=False,
        )
        self.choose.assert_not_awaited()
        self.slots.assert_not_awaited()
        self.assertFalse(helper_live.match_commands.RESOURCE_MATCH_SIDE_LOCKS[("LIVE", 1)].locked())

    async def test_public_progress_is_forwarded_to_availability_prompt(self):
        self.f.jobs(["succeeded"], "host_live_match_on_stored")
        start = AsyncMock(return_value=live_start.LiveStartSucceeded(message=self.f.message()))
        with patch.object(live_start, "start_live_match", new=start), \
                patch.object(helper_live, "run_helper_host_job", new=AsyncMock()):
            await helper_live.host_live_automatically(
                self.f.runtime, self.f.ctx, replace(self.request, public_progress=True),
            )
        self.availability.assert_awaited_once_with(
            self.f.runtime, self.f.ctx, "host_live_match_on_stored", public_progress=True,
        )

    async def test_changed_pair_prevents_local_start_and_helper_job(self):
        self.f.document["participants"][1]["base_store"]["reservation_id"] = "a" * 16
        with patch.object(live_start, "start_live_match", new_callable=AsyncMock) as start:
            await helper_live.host_live_automatically(self.f.runtime, self.f.ctx, self.request)
        start.assert_not_awaited()
        self.assertFalse(self.f.http.create_payloads)
        self.availability.assert_not_awaited()

    async def test_local_start_condition_requires_both_current_ready_reservations(self):
        async def claim(_lease, match, index, **kwargs):
            control = kwargs["expected_control"]
            self.assertEqual(control.binding_filter()["participants.1.base_store.reservation_id"], PAIR)
            self.assertTrue(self.f.matches(control.binding_filter()))
            self.f.document["participants"][1]["base_store"] = None
            self.assertFalse(self.f.matches(control.binding_filter()))
            return False
        request = live_start.LiveStart(
            match=self.match, host=self.f.bot.user, clan=self.f.clan,
            player1=self.request.player1, player2=self.request.player2,
            player1_elo=1000, player2_elo=1000, scout=False,
            reply=live_start.AutomaticLiveStartReply(), stored_reservation_id=PAIR,
        )
        with patch.object(live_start.match_commands, "claim_resource_clan", new=claim):
            outcome = await live_start.start_live_match(self.f.bot, self.f.ctx, request)
        self.assertIsInstance(outcome, live_start.LiveStartClanUnavailable)
        self.assertFalse(any(self.f.document["times"]))

    async def test_started_pair_keeps_shared_undo_and_both_reservations(self):
        for index in (0, 1):
            self.assertTrue(await mark_store_reserved(self.f.bot, self.match, index))
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.assertFalse(any(p["base_store"]["ready"] for p in self.f.document["participants"]))
        self.assertEqual([b.custom_id for b in self.message.components[0].components], [
            f"helper_stored_live_start|LIVE|{PAIR}", f"helper_stored_live_undo|LIVE|{PAIR}",
        ])
        self.assertEqual(self.message.embeds[0].title, "**Bases Stored**")
        self.assertEqual(self.message.embeds[0].fields[0].value,
                         "**[Player 111](<https://discordapp.com/users/111>)**: Slot 1\n"
                         "**[Player 222](<https://discordapp.com/users/222>)**: Slot 2")

    async def test_aborted_live_start_keeps_pair_and_shared_undo(self):
        self.f.document.update(is_war=False, times=[100, None], hosts=["999", None],
                               host_clans=[CLAN, None], old_msgs=[700, None])
        self.match = await self.f.find({})
        started = MagicMock(id=700, delete=AsyncMock(), edit=AsyncMock(), embeds=[])
        with patch.object(live_start.match_commands, "free_clan", new_callable=AsyncMock), patch.object(
            live_start, "schedule_channel_name_edit",
        ):
            aborted = await live_start.abort_live_start(self.f.ctx, live_start.LiveAbort(self.match, started))
        self.assertTrue(aborted)
        started.delete.assert_not_awaited()
        self.assertEqual(started.edit.await_args.kwargs["embeds"][0].title, "**Match Aborted**")
        self.assertEqual(started.edit.await_args.kwargs["components"], [])
        self.assertEqual(self.f.document["old_msgs"], [700, None])
        self.assertTrue(all(p["base_store"] for p in self.f.document["participants"]))
        self.assertFalse(any(self.f.document["times"]))
        self.assertEqual([b.custom_id for b in self.message.components[0].components],
                         [f"helper_stored_live_start|LIVE|{PAIR}", f"helper_stored_live_undo|LIVE|{PAIR}"])
