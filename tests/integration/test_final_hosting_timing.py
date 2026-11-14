import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from cogs import match_commands
from elobot.features.base_store.controls import start_stored_bases
from elobot.features.base_store.models import StoredBaseActionRequest
from elobot.features.hosting.live.live_start import (
    LiveAbort, LiveStart, LiveStartBusy, LiveStartClanUnavailable, ManualLiveStartReply, abort_live_start,
    start_live_match,
)
from cogs.self_commands import SelfCommands
from elobot.integrations.helper_api import (
    HelperEta, HelperEtaReader, HelperHostLiveMatchPayload, HelperHostWarMatchPayload,
    HelperJobCreateRequest, HelperJobCreator, HelperJobCreateRejected,
)
from elobot.features.hosting.war.helper_host_job import HelperHostJobRequest, run_helper_host_job
from elobot.features.hosting.war.war_start import WarAbort, abort_war_start
from elobot.features.finals.lifecycle import confirm_bracket, resolve_final
from elobot.features.finals.hosting import accept_final_start
from elobot.features.finals.matches import bind_thread, prepare_match, schedule_final_match
from elobot.features.finals.types import FinalError
from elobot.features.matches.messages import MatchMessageState
from elobot.features.matches.timing import hosting_started
from elobot.features.tokens.bets import open_market
from elobot.db import BaseStore, Clan, Match
from elobot.db.match_models import ExpectedLiveStartState
from elobot.db.token_models import FinalBettingMarket
from tests.integration._final_fixture import FinalScenarioCase


class TestFinalHostingTiming(FinalScenarioCase):
    async def pairing(self, *, live=False, stored=False):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await self.scheduled_pairing(final, "QF1", is_war=not live)
        await source.set({"thread": "777"})
        if stored:
            for side in ((0, 1) if live else (0,)):
                store = BaseStore(clan_tag="#REVIEW", slot=f"Slot {side + 1}", reservation_id="a" * 16,
                                  message_id=555, ready=True, is_live=live)
                await source.set({f"participants.{side}.base_store": store.model_dump(),
                                  f"participants.{side}.opponent_bases_stored": True})
        await bind_thread(final, source, "777")
        await Clan(tag="#REVIEW", name="Review", availability_str="Available").insert()
        return final, await Match.get(source.id)

    def transport(self, source, *, component=False):
        bot = MagicMock(ext={})
        bot.user.id = 42
        bot.coc_client.get_clan = AsyncMock(return_value=MagicMock(tag="#REVIEW", name="Review"))
        bot.fetch_user = AsyncMock(return_value=MagicMock(mention="<@8>"))
        ctx = MagicMock(spec=ipy.ComponentContext if component else ipy.SlashContext,
                        send=AsyncMock(), defer=AsyncMock())
        ctx.channel.id = 777
        ctx.channel_id = 777
        ctx.author.id = int(source.participants[0].user_id)
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(mention="<@1>"))
        ctx.channel.fetch_pinned_messages = AsyncMock(return_value=[MagicMock(embeds=[ipy.Embed(title="Match")])])
        ctx.channel.send = AsyncMock(return_value=MagicMock(id=900, created_at=datetime.now(UTC),
            edit=AsyncMock(), embeds=[ipy.Embed(title="Live Match Request")]))
        runtime = match_commands.MatchCommands(bot)
        runtime.helper_api_base_url = "http://127.0.0.1:1"
        runtime.helper_api_key = "test-only"
        bot.get_ext.return_value = runtime
        return bot, ctx, runtime

    async def check_legacy_failed_helper_cleanup(self, *, live, newer_start=False):
        _, source = await self.pairing(live=live)
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"old_msgs": [900, None]},
            "$unset": {"final_hosting_times": "", "final_legacy_hosting_evidence": ""},
        })
        original = await Match.get(source.id)
        bot, ctx, runtime = self.transport(original)
        ctx.bot = bot
        await accept_final_start(original, str(ctx.author.id), clan_tag="#REVIEW", player=None if live else 0)
        accepted_at = (await Match.get(original.id)).final_started_at
        reservation = await match_commands.try_reserve_resource_clan("#REVIEW")
        self.assertTrue(await match_commands.claim_resource_clan(
            reservation, original, 0, player2_index=1 if live else None,
            expected_control=MatchMessageState.capture(original, 0),
            updates={"times.0": 1800000000, "hosts.0": "42"},
        ))
        self.assertNotIn("final_hosting_times", original.model_fields_set)
        if newer_start:
            await Match.get_pymongo_collection().update_one(
                {"_id": original.id}, {"$set": {"final_hosting_times.0": 1800000001}},
            )
        message = MagicMock(id=900, embeds=[ipy.Embed(title="Started")], edit=AsyncMock())
        if live:
            payload = HelperHostLiveMatchPayload(host_clan_tag="#REVIEW", player_1_tag="#P1", player_2_tag="#P2",
                                                base_links_for_player_1=["one"], base_links_for_player_2=["two"])
            async def abort(msg):
                return await abort_live_start(ctx, LiveAbort(match=original, message=msg))
            module = "elobot.features.hosting.live.live_start"
            job_type = "host_live_match"
        else:
            payload = HelperHostWarMatchPayload(host_clan_tag="#REVIEW", player_tag="#P1", base_links=["one"], slot=1)
            async def abort(msg):
                return await abort_war_start(ctx, WarAbort(match=original, player=0, message=msg))
            module = "elobot.features.hosting.war.war_start"
            job_type = "host_war_match"
        request = HelperHostJobRequest(
            create_request=HelperJobCreateRequest(job_type=job_type, requested_by="1", payload=payload),
            requester_id="1", started_message=message, abort_started=abort,
        )
        with patch.object(HelperJobCreator, "create", new=AsyncMock(
                return_value=HelperJobCreateRejected(error="rejected"))), \
                patch(f"{module}.mark_store_reserved", new=AsyncMock(return_value=True)), \
                patch(f"{module}.schedule_channel_name_edit"):
            await run_helper_host_job(runtime, ctx, request)
        saved = await Match.get(original.id)
        self.assertEqual(saved.final_started_at, accepted_at)
        if newer_start:
            self.assertEqual(saved.times[0], 1800000000)
            self.assertEqual(saved.final_hosting_times[0], 1800000001)
            self.assertEqual(saved.host_clans[0], "#REVIEW")
            self.assertNotEqual((await Clan.find_one({"tag": "#REVIEW"})).availability_str, "Available")
            message.edit.assert_not_awaited()
            return
        self.assertIsNone(saved.times[0])
        self.assertIsNone(saved.host_clans[0])
        self.assertEqual((await Clan.find_one({"tag": "#REVIEW"})).availability_str, "Available")
        self.assertFalse(hosting_started(saved, 0))
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        message.edit.assert_awaited_once()

    async def test_legacy_war_helper_rejection_aborts_original_match(self):
        await self.check_legacy_failed_helper_cleanup(live=False)

    async def test_legacy_live_helper_rejection_aborts_original_match(self):
        await self.check_legacy_failed_helper_cleanup(live=True)

    async def test_stale_legacy_war_helper_rejection_preserves_newer_start(self):
        await self.check_legacy_failed_helper_cleanup(live=False, newer_start=True)

    async def test_stale_legacy_live_helper_rejection_preserves_newer_start(self):
        await self.check_legacy_failed_helper_cleanup(live=True, newer_start=True)

    async def test_scheduled_self_live_callback_publishes_opponent_request(self):
        _, source = await self.pairing(live=True)
        bot, ctx, _ = self.transport(source)
        cog = SelfCommands(bot)
        with patch.object(cog.live_requests, "schedule_expiry"):
            await cog.self_host_live.callback.func(cog, ctx)
        ctx.channel.send.assert_awaited_once()
        self.assertEqual(ctx.channel.send.await_args.kwargs["embed"].title, "Live Match Request")
        self.assertEqual(ctx.channel.send.await_args.kwargs["content"], "<@8>")
        self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
        self.assertIsNone((await Match.get(source.id)).final_started_at)

    async def test_scheduled_stored_live_callback_keeps_reservation_in_request(self):
        _, source = await self.pairing(live=True, stored=True)
        bot, ctx, _ = self.transport(source, component=True)
        ctx.custom_id = f"helper_stored_live_start|{source.token}|{'a' * 16}"
        ctx.message.id = 555
        cog = SelfCommands(bot)
        with patch.object(cog.live_requests, "schedule_expiry"):
            await cog.self_stored_live_start.callback.func(cog, ctx)
        ctx.channel.send.assert_awaited_once()
        message = ctx.channel.send.return_value
        self.assertEqual(ctx.channel.send.await_args.kwargs["embed"].title, "Live Match Request")
        self.assertTrue(all(button.custom_id.endswith("a" * 16) for button in message.components[0].components))
        self.assertEqual((await Match.get(source.id)).participants[1].base_store.reservation_id, "a" * 16)

    async def test_scheduled_self_war_callback_reaches_helper_without_losing_schedule(self):
        _, source = await self.pairing(stored=True)
        bot, ctx, _ = self.transport(source)
        cog = SelfCommands(bot)
        with patch.object(HelperEtaReader, "read", new=AsyncMock(return_value=HelperEta(
                estimate_start_at=1, estimate_end_at=2))), patch.object(HelperJobCreator, "create",
                new=AsyncMock(return_value=HelperJobCreateRejected(error="Test rejection"))) as create:
            await cog.self_host_war.callback.func(cog, ctx)
        create.assert_awaited_once()
        self.assertEqual((await Match.get(source.id)).times, source.scheduled_times)
        self.assertIn("could not confirm", ctx.send.await_args.args[0])

    async def test_scheduled_stored_helper_rejection_preserves_accepted_time_and_allows_retry(self):
        _, source = await self.pairing(stored=True)
        _, ctx, runtime = self.transport(source, component=True)
        market = await open_market(await resolve_final(), "QF1")
        request = StoredBaseActionRequest(source.token, 0, "a" * 16, 555)
        with patch.object(HelperEtaReader, "read", new=AsyncMock(return_value=HelperEta(
                estimate_start_at=1, estimate_end_at=2))), patch.object(HelperJobCreator, "create",
                new=AsyncMock(return_value=HelperJobCreateRejected(error="Test rejection"))) as create:
            with patch("elobot.features.finals.hosting.time.time", return_value=1800000000):
                await start_stored_bases(runtime, ctx, request)
            with patch("elobot.features.finals.hosting.time.time", return_value=1800003600):
                await start_stored_bases(runtime, ctx, request)
        self.assertEqual(create.await_count, 2)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_started_at, 1800000000)
        self.assertEqual(saved.times, source.scheduled_times)
        self.assertEqual(saved.hosts, [None, None])
        self.assertFalse(hosting_started(saved, 0))
        self.assertEqual(saved.participants[0].base_store.reservation_id, "a" * 16)
        self.assertEqual((await FinalBettingMarket.get(market.id)).version, 1)

    async def test_scheduled_failed_clan_claim_can_retry_until_hosting_commits(self):
        _, source = await self.pairing(live=True)
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {"$unset": {"final_hosting_times": ""}})
        source = await Match.get(source.id)
        bot, ctx, _ = self.transport(source)
        start = LiveStart(match=source, host=bot.user, clan=MagicMock(tag="#REVIEW", name="Review"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=2000, player2_elo=2000,
            scout=False, reply=ManualLiveStartReply(slot_instruction=""))
        with patch("cogs.match_commands.claim_resource_clan", new=AsyncMock(return_value=False)):
            first = await start_live_match(bot, ctx, start)
        self.assertIsInstance(first, LiveStartClanUnavailable)
        accepted_at = (await Match.get(source.id)).final_started_at
        self.assertIsNotNone(accepted_at)
        ctx.channel.send.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        with self.assertRaises(ipy.errors.Forbidden):
            await start_live_match(bot, ctx, start)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_started_at, accepted_at)
        self.assertEqual(saved.final_hosting_times, [saved.times[0], saved.times[0]])
        self.assertTrue(hosting_started(saved))
        self.assertIsInstance(await start_live_match(bot, ctx, start), LiveStartBusy)
        expected = ExpectedLiveStartState(tuple(saved.times), tuple(saved.hosts),
                                          tuple(saved.host_clans), tuple(saved.old_msgs))
        self.assertTrue(await saved.abort_started_live_match(expected))
        self.assertFalse(hosting_started(saved))
        self.assertEqual((await Match.get(source.id)).final_hosting_times, [None, None])
        self.assertEqual(saved.final_started_at, accepted_at)

    async def test_live_clan_occupancy_failure_rolls_back_commit_marker_but_keeps_acceptance(self):
        _, source = await self.pairing(live=True)
        bot, ctx, _ = self.transport(source)
        start = LiveStart(match=source, host=bot.user, clan=MagicMock(tag="#REVIEW", name="Review"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=2000, player2_elo=2000,
            scout=False, reply=ManualLiveStartReply(slot_instruction=""))
        with patch("cogs.match_commands.occupy_clan", new=AsyncMock(side_effect=RuntimeError("occupancy failed"))):
            with self.assertRaisesRegex(RuntimeError, "occupancy failed"):
                await start_live_match(bot, ctx, start)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_hosting_times, [None, None])
        self.assertEqual(saved.times, source.scheduled_times)
        self.assertFalse(hosting_started(saved))
        self.assertIsNotNone(saved.final_started_at)

    async def test_live_claim_cannot_overwrite_state_changed_after_acceptance(self):
        _, source = await self.pairing(live=True)
        bot, ctx, _ = self.transport(source)
        start = LiveStart(match=source, host=bot.user, clan=MagicMock(tag="#REVIEW", name="Review"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=2000, player2_elo=2000,
            scout=False, reply=ManualLiveStartReply(slot_instruction=""))

        async def accept_then_change(*args, **kwargs):
            await accept_final_start(*args, **kwargs)
            await Match.get_pymongo_collection().update_one({"_id": source.id}, {"$set": {"old_msgs.0": 123}})

        with patch("elobot.features.hosting.live.live_start.accept_final_start", new=accept_then_change):
            outcome = await start_live_match(bot, ctx, start)
        self.assertIsInstance(outcome, LiveStartClanUnavailable)
        saved = await Match.get(source.id)
        self.assertEqual(saved.old_msgs[0], 123)
        self.assertEqual(saved.times, source.scheduled_times)
        self.assertEqual(saved.final_hosting_times, [None, None])
        self.assertIsNotNone(saved.final_started_at)
        ctx.channel.send.assert_not_awaited()

    async def test_committed_start_matching_scheduled_second_is_still_active(self):
        _, source = await self.pairing(live=True)
        bot, ctx, _ = self.transport(source)
        ctx.channel.send.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        start = LiveStart(match=source, host=bot.user, clan=MagicMock(tag="#REVIEW", name="Review"),
            player1=MagicMock(), player2=MagicMock(), player1_elo=2000, player2_elo=2000,
            scout=False, reply=ManualLiveStartReply(slot_instruction=""))
        with patch("elobot.features.hosting.live.live_start.datetime") as clock:
            clock.now.return_value.timestamp.return_value = source.scheduled_times[0]
            with self.assertRaises(ipy.errors.Forbidden):
                await start_live_match(bot, ctx, start)
        saved = await Match.get(source.id)
        self.assertEqual(saved.times, saved.scheduled_times)
        self.assertTrue(hosting_started(saved))
        self.assertEqual(saved.final_hosting_times, saved.scheduled_times)

    async def test_unhosted_legacy_times_require_explicit_schedule_without_backfill(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await prepare_match(final, "QF1")
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"times": [1600000000, 1600003600]}, "$unset": {"scheduled_times": ""},
        })
        source = await Match.get(source.id)
        self.assertNotIn("scheduled_times", source.model_fields_set)
        self.assertEqual(source.scheduled_times, [None, None])
        source = await schedule_final_match(source, 1700000000, player=0)
        self.assertEqual(source.scheduled_times, [1700000000, None])
        self.assertEqual((await resolve_final()).nodes["QF1"].scheduled_times, [1700000000, None])
        self.assertIsNone(source.final_started_at)
        source = await schedule_final_match(source, 1700007200, player=1)
        self.assertEqual(source.scheduled_times, [1700000000, 1700007200])

    async def test_active_legacy_hosting_is_preserved_when_new_timing_fields_are_absent(self):
        final = await confirm_bracket(await self.roster(8), "manager")
        source = await prepare_match(final, "QF1")
        for active in ({"hosts": ["42", None]}, {"host_clans": ["#REVIEW", None]},
                       {"final_started_at": 1600000000}):
            with self.subTest(active=active):
                await Match.get_pymongo_collection().update_one({"_id": source.id}, {
                    "$set": {"times": [1600000000, 1600003600], "hosts": [None, None],
                             "host_clans": [None, None], "final_started_at": None, **active},
                    "$unset": {"scheduled_times": ""},
                })
                legacy = await Match.get(source.id)
                before = await Match.get_pymongo_collection().find_one({"_id": source.id})
                self.assertTrue(hosting_started(legacy, 0))
                with self.assertRaises(FinalError):
                    await schedule_final_match(legacy, 1700000000, player=0)
                self.assertEqual(await Match.get_pymongo_collection().find_one({"_id": source.id}), before)

    async def test_legacy_scheduled_hosting_without_marker_remains_ambiguous(self):
        _, source = await self.pairing()
        for active in ({"hosts": ["42", None]}, {"host_clans": ["#REVIEW", None]}):
            with self.subTest(active=active):
                await Match.get_pymongo_collection().update_one({"_id": source.id}, {
                    "$set": {"hosts": [None, None], "host_clans": [None, None], **active},
                    "$unset": {"final_hosting_times": ""},
                })
                legacy = await Match.get(source.id)
                before = await Match.get_pymongo_collection().find_one({"_id": source.id})
                self.assertTrue(hosting_started(legacy, 0))
                with self.assertRaises(FinalError):
                    await schedule_final_match(legacy, 1700000000, player=0)
                self.assertEqual(await Match.get_pymongo_collection().find_one({"_id": source.id}), before)

    async def test_stale_abort_preserves_changed_hosting_commitment(self):
        _, source = await self.pairing()
        for live in (False, True):
            with self.subTest(live=live):
                await source.set({"is_war": not live, "old_msgs": [777, 777],
                                  "final_hosting_times": [100, 100]})
                stale = await Match.get(source.id)
                await Match.get_pymongo_collection().update_one(
                    {"_id": source.id}, {"$set": {"final_hosting_times": [200, 200]}})
                if live:
                    expected = ExpectedLiveStartState(tuple(stale.times), tuple(stale.hosts),
                                                      tuple(stale.host_clans), tuple(stale.old_msgs))
                    aborted = await stale.abort_started_live_match(expected)
                else:
                    aborted = await stale.abort_started_war_side(
                        0, stale.times[0], stale.hosts[0], stale.host_clans[0], stale.old_msgs[0])
                self.assertFalse(aborted)
                saved = await Match.get(source.id)
                self.assertEqual(saved.final_hosting_times, [200, 200])
                self.assertEqual(saved.times, source.times)

    async def test_legacy_side_abort_preserves_other_sides_ambiguous_hosting(self):
        _, source = await self.pairing()
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"hosts": ["host0", "host1"], "host_clans": ["#OLD0", "#OLD1"],
                     "old_msgs": [101, 102]},
            "$unset": {"final_hosting_times": ""},
        })
        legacy = await Match.get(source.id)
        self.assertTrue(hosting_started(legacy, 0))
        self.assertTrue(await legacy.abort_started_war_side(
            1, legacy.times[1], legacy.hosts[1], legacy.host_clans[1], legacy.old_msgs[1]))
        saved = await Match.get(source.id)
        self.assertTrue(hosting_started(saved, 0))
        self.assertFalse(hosting_started(saved, 1))
        self.assertEqual(saved.final_hosting_times, [None, None])
        self.assertEqual(saved.hosts[0], "host0")
        self.assertEqual(saved.host_clans[0], "#OLD0")

    async def test_legacy_side_claim_preserves_other_side_without_fabricating_commitment(self):
        _, source = await self.pairing()
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"hosts": ["host0", None], "host_clans": ["#OLD0", None]},
            "$unset": {"final_hosting_times": ""},
        })
        legacy = await Match.get(source.id)
        reservation = await match_commands.try_reserve_resource_clan("#REVIEW")
        self.assertIsNotNone(reservation)
        self.assertTrue(await match_commands.claim_resource_clan(
            reservation, legacy, 1, expected_control=MatchMessageState.capture(legacy, 1),
            updates={"times.1": 1800000000, "hosts.1": "host1"},
        ))
        saved = await Match.get(source.id)
        self.assertTrue(hosting_started(saved, 0))
        self.assertTrue(hosting_started(saved, 1))
        self.assertEqual(saved.final_hosting_times, [None, 1800000000])
        self.assertEqual(saved.host_clans, ["#OLD0", "#REVIEW"])

    async def test_legacy_initialization_uses_current_row_and_stale_abort_cannot_clear_it(self):
        _, source = await self.pairing()
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"old_msgs": [101, 102]}, "$unset": {"final_hosting_times": ""},
        })
        stale = await Match.get(source.id)
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"hosts.0": "new-host", "host_clans.0": "#NEW", "old_msgs.1": 103},
        })
        self.assertFalse(await stale.abort_started_war_side(
            1, stale.times[1], stale.hosts[1], stale.host_clans[1], stale.old_msgs[1]))
        saved = await Match.get(source.id)
        self.assertTrue(hosting_started(saved, 0))
        self.assertFalse(hosting_started(saved, 1))
        self.assertEqual(saved.final_hosting_times, [None, None])
        self.assertEqual(saved.host_clans[0], "#NEW")
        self.assertEqual(saved.old_msgs[1], 103)

    async def test_legacy_initialization_does_not_commit_unhosted_or_preassigned_side(self):
        _, source = await self.pairing()
        for side_time, host in ((source.times[0], None), (None, "assigned-host")):
            with self.subTest(side_time=side_time, host=host):
                await Match.get_pymongo_collection().update_one({"_id": source.id}, {
                    "$set": {"times": [side_time, source.times[1]], "hosts": [host, "host1"],
                             "host_clans": [None, "#OLD1"], "old_msgs": [101, 102]},
                    "$unset": {"final_hosting_times": "", "final_legacy_hosting_evidence": ""},
                })
                legacy = await Match.get(source.id)
                self.assertFalse(hosting_started(legacy, 0))
                self.assertTrue(await legacy.abort_started_war_side(
                    1, legacy.times[1], legacy.hosts[1], legacy.host_clans[1], legacy.old_msgs[1]))
                saved = await Match.get(source.id)
                self.assertFalse(hosting_started(saved, 0))
                self.assertEqual(saved.final_hosting_times, [None, None])

    async def test_concurrent_legacy_side_aborts_do_not_restore_cleared_evidence(self):
        _, source = await self.pairing()
        await Match.get_pymongo_collection().update_one({"_id": source.id}, {
            "$set": {"hosts": ["host0", "host1"], "host_clans": ["#OLD0", "#OLD1"],
                     "old_msgs": [101, 102]},
            "$unset": {"final_hosting_times": "", "final_legacy_hosting_evidence": ""},
        })
        first, second = await asyncio.gather(Match.get(source.id), Match.get(source.id))
        outcomes = await asyncio.gather(
            first.abort_started_war_side(0, first.times[0], first.hosts[0], first.host_clans[0], first.old_msgs[0]),
            second.abort_started_war_side(1, second.times[1], second.hosts[1], second.host_clans[1],
                                          second.old_msgs[1]),
        )
        self.assertEqual(outcomes, [True, True])
        saved = await Match.get(source.id)
        self.assertEqual(saved.times, [None, None])
        self.assertEqual(saved.final_legacy_hosting_evidence, [False, False])
        self.assertFalse(hosting_started(saved))
