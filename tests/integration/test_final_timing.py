from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

from elo_bot.config.ids import FINAL_CHANNEL, FINAL_NOTIFICATION_ROLE
from elo_bot.discord.embeds import embed_color
from elo_bot.features.hosting.live.live_start import LiveStart, ManualLiveStartReply, start_live_match
from elo_bot.extensions.match import MatchCommands
from elo_bot.features.finals.hosting import accept_final_start
from elo_bot.features.finals.lifecycle import confirm_bracket, final_for_view
from elo_bot.features.finals.matches import bind_thread, prepare_match, schedule_final_match
from elo_bot.features.finals.recovery import mark_deleted_attempt, recover_final
from elo_bot.features.finals.results import FinalResultSubmission, submit_result
from elo_bot.core.finals import FinalError
from elo_bot.features.matches.messages import MatchMessageState, publish_match_message
from elo_bot.features.matches.recovery import _repair_message
from elo_bot.db import Clan, EloFinal, Match
from elo_bot.db.final_models import FinalMatchResult
from elo_bot.db.token_models import FinalBettingMarket
from elo_bot.features.tokens.bets import open_market
from tests.integration._final_fixture import FinalScenarioCase
from tests._patch_sites import patch_sites


class TestFinalTiming(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.source = await prepare_match(self.final, "QF1")

    async def request(self, **changes):
        final = await final_for_view()
        node = final.nodes["QF1"]
        return replace(FinalResultSubmission(final_id=final.id, node_id=node.id, actor_id="manager",
            operation_key=f"timing:{node.result_version}", expected_version=node.result_version,
            contestant_revision=node.contestant_revision, attempt=node.attempt,
            scores={user: ["3.100" if side == 0 else "2.80"] * 7
                    for side, user in enumerate(node.contestants)}), **changes)

    async def race(self, *actions):
        results = [None] * len(actions)

        async def run(index, action):
            try:
                results[index] = await action()
            except FinalError as error:
                results[index] = error

        with anyio.fail_after(5):
            async with anyio.create_task_group() as group:
                for index, action in enumerate(actions):
                    group.start_soon(run, index, action)
        return results

    async def test_live_reschedule_survives_source_cleanup_and_keeps_both_slots(self):
        source = await schedule_final_match(self.source, 1700000000)
        source = await schedule_final_match(source, 1700003600)
        await source.delete()
        node = (await final_for_view()).nodes["QF1"]
        self.assertEqual(node.scheduled_times, [1700003600, 1700003600])
        self.assertFalse(node.scheduled_is_war)
        self.assertIsNone(node.actual_started_at)

    async def test_war_sides_schedule_independently_under_concurrency(self):
        results = await self.race(
            lambda: schedule_final_match(self.source, 1700000000, player=0),
            lambda: schedule_final_match(self.source, 1700007200, player=1),
        )
        self.assertFalse(any(isinstance(result, FinalError) for result in results))
        saved = await Match.get(self.source.id)
        node = (await final_for_view()).nodes["QF1"]
        self.assertEqual(saved.times, [1700000000, 1700007200])
        self.assertEqual(saved.scheduled_times, [1700000000, 1700007200])
        self.assertEqual(node.scheduled_times, [1700000000, 1700007200])

    async def test_failed_schedule_rolls_back_physical_and_pairing_writes(self):
        collection = EloFinal.get_pymongo_collection()
        with patch.object(collection, "replace_one", new=AsyncMock(side_effect=RuntimeError("injected"))):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await schedule_final_match(self.source, 1700000000)
        self.assertEqual((await Match.get(self.source.id)).times, [None, None])
        self.assertEqual((await Match.get(self.source.id)).scheduled_times, [None, None])
        self.assertEqual((await final_for_view()).nodes["QF1"].scheduled_times, [None, None])

    async def test_played_results_are_recorded_without_a_schedule(self):
        await submit_result(await self.request())
        result = await FinalMatchResult.find_one({"node_id": "QF1"})
        self.assertEqual((result.snapshot.outcome, result.snapshot.scheduled_times, result.snapshot.actual_started_at),
                         ("Played", [None, None], None))
        node = (await final_for_view()).nodes["QF1"]
        self.assertEqual((node.result_id, node.winner), (result.id, node.contestants[0]))

    async def test_played_results_need_the_pairings_match(self):
        await (await final_for_view()).set({"nodes.QF1.match_id": None})
        with self.assertRaises(FinalError):
            await submit_result(await self.request())
        self.assertEqual(await FinalMatchResult.count(), 0)

    async def test_war_side_can_start_when_only_the_other_side_is_scheduled(self):
        market = await open_market(await final_for_view(), "QF1")
        source = await schedule_final_match(self.source, 1700000000, player=0)
        with self.assertRaisesRegex(FinalError, "/match war start"):
            await accept_final_start(source, "host", player=None)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        await accept_final_start(source, "host", player=1)
        saved = await Match.get(source.id)
        self.assertEqual(saved.scheduled_times, [1700000000, None])
        self.assertIsNone(saved.final_started_times[0])
        self.assertIsNotNone(saved.final_started_times[1])
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")

    async def test_unscheduled_war_start_cannot_race_a_live_schedule(self):
        source = self.source
        expected = dict(MatchMessageState.capture(source, 0).binding_filter())
        results = await self.race(
            lambda: schedule_final_match(source, 1700003600),
            lambda: accept_final_start(source, "host", player=0, expected_source=expected),
        )
        self.assertEqual(sum(isinstance(result, FinalError) for result in results), 1)
        saved = await Match.get(source.id)
        if saved.final_started_at is not None:
            self.assertTrue(saved.is_war)
            self.assertEqual(saved.scheduled_times, [None, None])
        else:
            self.assertFalse(saved.is_war)
            self.assertEqual(saved.scheduled_times, [1700003600] * 2)

    async def test_accepted_unscheduled_war_start_blocks_switch_to_live(self):
        await accept_final_start(self.source, "host", player=0)
        saved = await Match.get(self.source.id)
        self.assertTrue(saved.is_war)
        self.assertEqual(saved.times, [None, None])
        with self.assertRaises(FinalError):
            await schedule_final_match(saved, 1700003600)
        with self.assertRaisesRegex(FinalError, "/match war start"):
            await accept_final_start(saved, "host", player=None)

    async def test_each_war_side_keeps_its_accepted_start_without_reclosing_market(self):
        source = await self.scheduled_pairing(self.final, "QF1")
        market = await open_market(await final_for_view(), "QF1")
        with patch("elo_bot.features.finals.hosting.time.time", return_value=1800000000):
            await accept_final_start(source, "host", player=1)
        with patch("elo_bot.features.finals.hosting.time.time", return_value=1800000300):
            await accept_final_start(source, "host", player=0)
            await accept_final_start(source, "host", player=1)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_started_times, [1800000300, 1800000000])
        self.assertEqual(saved.final_started_at, 1800000000)
        self.assertEqual(saved.scheduled_times, [1700000000, 1700003600])
        self.assertEqual((await final_for_view()).nodes["QF1"].actual_started_at, 1800000000)
        self.assertEqual((await FinalBettingMarket.get(market.id)).version, 1)

    async def test_start_failure_rolls_back_start_and_market_together(self):
        source = await schedule_final_match(self.source, 1700000000)
        market = await open_market(await final_for_view(), "QF1")
        with patch.object(Match.get_pymongo_collection(), "update_one",
                          new=AsyncMock(side_effect=RuntimeError("injected"))):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await accept_final_start(source, "host", player=None)
        saved = await Match.get(source.id)
        self.assertIsNone(saved.final_started_at)
        self.assertEqual(saved.final_started_times, [None, None])
        self.assertIsNone((await final_for_view()).nodes["QF1"].actual_started_at)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")

    async def test_ambiguous_commits_and_replays_keep_one_schedule_and_accepted_start(self):
        async def ambiguous_commit():
            await self.client.admin.command({"configureFailPoint": "failCommand", "mode": {"times": 1}, "data": {
                "failCommands": ["commitTransaction"],
                "writeConcernError": {"code": 64, "errmsg": "injected ambiguous commit"},
                "errorLabels": ["UnknownTransactionCommitResult"],
            }})

        self.addAsyncCleanup(self.client.admin.command, {"configureFailPoint": "failCommand", "mode": "off"})
        await ambiguous_commit()
        source = await schedule_final_match(self.source, 1700000000)
        source = await schedule_final_match(source, 1700000000)
        market = await open_market(await final_for_view(), "QF1")
        await ambiguous_commit()
        await accept_final_start(source, "host", player=None)
        started_at = (await Match.get(source.id)).final_started_at
        await accept_final_start(source, "host", player=None)
        self.assertIsNotNone(started_at)
        self.assertEqual((await Match.get(source.id)).final_started_times, [started_at, started_at])
        self.assertEqual((await final_for_view()).nodes["QF1"].scheduled_times, [1700000000] * 2)
        self.assertEqual((await FinalBettingMarket.get(market.id)).version, 1)

    async def test_reschedule_and_start_race_accepts_only_one_current_action(self):
        source = await schedule_final_match(self.source, 1700000000)
        expected = dict(MatchMessageState.capture(source, 0).binding_filter())
        results = await self.race(
            lambda: schedule_final_match(source, 1700003600),
            lambda: accept_final_start(source, "host", player=None, expected_source=expected),
        )
        self.assertEqual(sum(isinstance(result, FinalError) for result in results), 1)
        saved = await Match.get(source.id)
        self.assertEqual(saved.scheduled_times, (await final_for_view()).nodes["QF1"].scheduled_times)
        self.assertEqual(saved.scheduled_times, [1700003600] * 2 if saved.final_started_at is None else [1700000000] * 2)

    async def test_deleted_attempt_cannot_reschedule_start_or_supply_replacement_timing(self):
        source = await schedule_final_match(self.source, 1700000000)
        await accept_final_start(source, "host", player=None)
        final = await final_for_view()
        await self.race(lambda: mark_deleted_attempt(final, final.nodes["QF1"], source),
                        lambda: schedule_final_match(source, 1700003600))
        current = await final_for_view()
        replacement = await prepare_match(current, "QF1")
        for action in (lambda: schedule_final_match(source, 1700007200),
                       lambda: accept_final_start(source, "host", player=None)):
            with self.assertRaises(FinalError):
                await action()
        self.assertEqual(replacement.final_attempt, source.final_attempt + 1)
        self.assertIsNone(replacement.final_started_at)
        self.assertIsNone((await final_for_view()).nodes["QF1"].actual_started_at)
        result = await submit_result(await self.request())
        self.assertEqual(result.snapshot.scheduled_times, [1700000000] * 2)
        self.assertIsNone(result.snapshot.actual_started_at)

    async def test_correction_retains_official_timing_after_mutation_and_source_deletion(self):
        source = await schedule_final_match(self.source, 1700000000)
        with patch("elo_bot.features.finals.hosting.time.time", return_value=1800000000):
            await accept_final_start(source, "host", player=None)
        initial = await submit_result(await self.request())
        await source.delete()
        await recover_final(await final_for_view())
        final = await final_for_view()
        await final.set({"nodes.QF1.scheduled_times": [123, 456], "nodes.QF1.actual_started_at": 789})
        corrected = await submit_result(await self.request(reason="Corrected score"))
        self.assertEqual(corrected.snapshot.scheduled_times, [1700000000, 1700000000])
        self.assertEqual(corrected.snapshot.actual_started_at, 1800000000)
        self.assertEqual(corrected.previous_id, initial.id)
        self.assertEqual((await FinalMatchResult.get(initial.id)).snapshot.actual_started_at, 1800000000)

    async def test_historical_unknown_timing_stays_unknown_on_correction(self):
        source = await schedule_final_match(self.source, 1700000000)
        initial = await submit_result(await self.request())
        await FinalMatchResult.get_pymongo_collection().update_one({"_id": initial.id}, {"$unset": {
            "snapshot.scheduled_times": "", "snapshot.actual_started_at": "",
        }})
        await source.set({"final_started_at": 1800000000})
        await recover_final(await final_for_view())
        corrected = await submit_result(await self.request(reason="Historical correction"))
        self.assertEqual(corrected.snapshot.scheduled_times, [None, None])
        self.assertIsNone(corrected.snapshot.actual_started_at)

    async def test_unscheduled_forfeit_has_no_played_schedule_or_start(self):
        request = await self.request(outcome="Forfeit", reason="No show")
        request = replace(request, scores={user: ["0.00"] * 7 for user in request.scores},
                          winner=next(iter(request.scores)))
        result = await submit_result(request)
        self.assertEqual(result.snapshot.scheduled_times, [None, None])
        self.assertIsNone(result.snapshot.actual_started_at)

    async def test_schedule_callbacks_commit_before_failed_publication_without_success(self):
        for node_id, player, thread in (("QF1", None, 777), ("QF2", 1, 778)):
            with self.subTest(player=player):
                source = await prepare_match(self.final, node_id)
                await source.set({"thread": str(thread)})
                bot = MagicMock(ext={}, fetch_user=AsyncMock(return_value=MagicMock(mention="<@1>")))
                cog = MatchCommands(bot)
                ctx_type = ipy.SlashContext if player is None else ipy.ContextMenuContext
                ctx = MagicMock(spec=ctx_type, send=AsyncMock(), defer=AsyncMock())
                ctx.channel.id = thread
                if player is not None:
                    ctx.target = MagicMock(spec=ipy.Message, content="<t:1700000000>",
                                           author=MagicMock(id=int(source.participants[player].user_id)))

                async def fail(*args, **kwargs):
                    expected = [1700000000] * 2 if player is None else [None, 1700000000]
                    self.assertEqual((await Match.get(source.id)).scheduled_times, expected)
                    self.assertEqual((await final_for_view()).nodes[node_id].scheduled_times, expected)
                    raise ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))

                with patch("elo_bot.extensions.match.hosting.time", return_value=1699999000), \
                        patch_sites(('elo_bot.extensions.match.lifecycle.append_helper_task', 'elo_bot.extensions.match.hosting.append_helper_task'), new=AsyncMock()), \
                        patch_sites(('elo_bot.extensions.match.hosting.publish_match_message', 'elo_bot.extensions.match.settings.publish_match_message'), new=AsyncMock(side_effect=fail)):
                    with self.assertRaises(ipy.errors.Forbidden):
                        if player is None:
                            await cog.match_schedule.callback.func(cog, ctx, timestamp="1700000000")
                        else:
                            await cog.match_war_schedule_message.callback.func(cog, ctx)
                ctx.send.assert_not_awaited()

    async def test_live_schedule_pings_the_final_role_when_creating_its_public_announcement(self):
        await self.source.set({"thread": "777"})
        thread_message = MagicMock(id=51, edit=AsyncMock(), pin=AsyncMock())
        thread = MagicMock(id=777, send=AsyncMock(return_value=thread_message))
        announcement = MagicMock(id=52, _channel_id=FINAL_CHANNEL, components=[], edit=AsyncMock())
        channel = MagicMock(id=FINAL_CHANNEL, send=AsyncMock(return_value=announcement),
                            fetch_message=AsyncMock(return_value=announcement))
        bot = MagicMock(ext={}, fetch_channel=AsyncMock(return_value=channel))
        cog = MatchCommands(bot)
        ctx = MagicMock(spec=ipy.SlashContext, channel=thread, send=AsyncMock(), defer=AsyncMock())
        ctx.guild.fetch_member = AsyncMock(return_value=None)
        role_mention = f"<@&{FINAL_NOTIFICATION_ROLE}>"

        with (
            patch("elo_bot.extensions.match.hosting.time", return_value=1_699_999_000),
            patch("elo_bot.extensions.match.hosting.append_helper_task", new_callable=AsyncMock),
            patch("elo_bot.extensions.match.hosting.choose_tip", new=AsyncMock(return_value="Tip")) as choose_tip,
            patch("elo_bot.extensions.match.hosting.schedule_channel_name_edit"),
        ):
            await cog.match_schedule.callback.func(cog, ctx, timestamp="1700000000")
            first = await Match.get(self.source.id)
            self.assertEqual(first.public_message_publication.embeds[0]["title"], "**QF1 · Match Scheduled**")
            self.assertEqual(first.public_message_publication.embeds[0]["author"]["name"], "Season 7 Final")
            self.assertEqual(first.public_message_publication.embeds[0]["color"], embed_color())
            scout = MagicMock(spec=ipy.SlashContext, channel=thread, send=AsyncMock(), defer=AsyncMock())
            scout.author.id = int(first.participants[0].user_id)
            scout.author.has_role.return_value = False
            with patch("elo_bot.extensions.match.bases.time.time", return_value=1_699_997_000):
                await cog.match_base_scout.callback.func(cog, scout)
            self.assertIn("<t:1699998200:R>", scout.send.await_args.args[0])
            announcement.content = f'{role_mention}\n💡 **TIP:** "An old tip."'
            await cog.match_schedule.callback.func(cog, ctx, timestamp="1700003600")

        bot.fetch_channel.assert_awaited_with(FINAL_CHANNEL)
        channel.send.assert_awaited_once()
        choose_tip.assert_not_awaited()
        self.assertEqual(channel.send.await_args.args[0], role_mention)
        self.assertEqual(channel.send.await_args.kwargs["allowed_mentions"].roles, [FINAL_NOTIFICATION_ROLE])
        self.assertNotIn(role_mention, thread.send.await_args_list[0].args[0])
        self.assertEqual(announcement.edit.await_args.kwargs["content"], role_mention)
        self.assertEqual(announcement.edit.await_args.kwargs["allowed_mentions"], {"parse": []})
        rescheduled = await Match.get(self.source.id)
        self.assertEqual(rescheduled.msg, first.msg)
        self.assertEqual(rescheduled.scheduled_times, [1_700_003_600] * 2)
        self.assertEqual(rescheduled.public_message_publication.embeds[0]["title"], "**QF1 · Match Rescheduled**")
        self.assertEqual(rescheduled.public_message_publication.embeds[0]["author"]["name"], "Season 7 Final")
        self.assertEqual(rescheduled.public_message_publication.embeds[0]["color"], embed_color())

    async def test_live_final_announcement_keeps_match_code_through_recovery_and_end(self):
        await self.source.set({"thread": "777"})
        await accept_final_start(self.source, "9", player=None)
        source = await Match.get(self.source.id)
        announcement = MagicMock(id=52, _channel_id=FINAL_CHANNEL, edit=AsyncMock())
        channel = MagicMock(id=FINAL_CHANNEL, send=AsyncMock(return_value=announcement))
        await publish_match_message(MagicMock(), source, MatchMessageState.capture(source, 0),
                                    embed=ipy.Embed(title="**Match Started**"), public_channel=channel)

        saved = await Match.get(source.id)
        publication = saved.public_message_publication
        self.assertFalse(publication.pending)
        announcement.embeds = [ipy.Embed.from_dict(publication.embeds[0])]
        self.assertEqual(announcement.embeds[0].title, "**QF1 · Match Started**")
        self.assertEqual(announcement.embeds[0].author.name, "Season 7 Final")
        announcement.edit.reset_mock()
        await _repair_message(announcement, bound=True, ended=False, active=True)
        announcement.edit.assert_not_awaited()
        await _repair_message(announcement, bound=True, ended=True, active=False)
        ended = announcement.edit.await_args.kwargs["embeds"][0]
        self.assertEqual(ended.title, "**QF1 · Match Ended**")
        self.assertEqual(ended.author.name, "Season 7 Final")

    async def test_foreign_source_identity_cannot_schedule_start_or_record_scores(self):
        source = await schedule_final_match(self.source, 1700000000)
        original = await Match.get_pymongo_collection().find_one({"_id": source.id})
        request = await self.request()
        for changes in (
            {"final_id": "000000000000000000000001"},
            {"final_node_id": "QF2"},
            {"final_contestant_revision": source.final_contestant_revision + 1},
            {"final_attempt": source.final_attempt + 1},
            {"participants": [participant.model_dump() for participant in reversed(source.participants)]},
        ):
            with self.subTest(changes=changes):
                await Match.get_pymongo_collection().update_one({"_id": source.id}, {"$set": changes})
                for action in (lambda: schedule_final_match(source, 1700003600),
                               lambda: accept_final_start(source, "host", player=None),
                               lambda: submit_result(request)):
                    with self.assertRaises(FinalError):
                        await action()
                self.assertEqual(await FinalMatchResult.count(), 0)
                self.assertIsNone((await final_for_view()).nodes["QF1"].actual_started_at)
                await Match.get_pymongo_collection().replace_one({"_id": source.id}, original)

    async def test_scheduled_live_start_accepts_preassigned_host_before_failed_publication(self):
        source = await schedule_final_match(self.source, 1700000000)
        await source.set({"thread": "777", "hosts": ["9", None]})
        await bind_thread(self.final, source, "777")
        await Clan(tag="#TIMING", name="Timing", availability_str="Available").insert()
        bot = MagicMock()
        ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        ctx.author.id = 9
        ctx.channel_id = 777
        ctx.channel.id = 777
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(mention="<@1>"))
        ctx.channel.send = AsyncMock(side_effect=ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden")))
        with self.assertRaises(ipy.errors.Forbidden):
            await start_live_match(bot, ctx, LiveStart(match=source, host=MagicMock(id=9),
                clan=MagicMock(tag="#TIMING"), player1=MagicMock(), player2=MagicMock(),
                player1_elo=2000, player2_elo=2000, reply=ManualLiveStartReply(slot_instruction="")))
        saved = await Match.get(source.id)
        self.assertIsNotNone(saved.final_started_at)
        self.assertEqual(saved.scheduled_times, [1700000000, 1700000000])
        self.assertEqual(saved.final_started_times, [saved.final_started_at, saved.final_started_at])

    async def test_unscheduled_live_start_commits_hosting_as_live(self):
        source = self.source
        await source.set({"thread": "777"})
        await bind_thread(self.final, source, "777")
        await Clan(tag="#TIMING", name="Timing", availability_str="Available").insert()
        market = await open_market(await final_for_view(), "QF1")
        ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock())
        ctx.author.id = 9
        ctx.channel.id = ctx.channel_id = 777
        ctx.guild.fetch_member = AsyncMock(return_value=MagicMock(mention="<@1>"))
        ctx.channel.send = AsyncMock(side_effect=ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden")))
        with self.assertRaises(ipy.errors.Forbidden):
            await start_live_match(MagicMock(), ctx, LiveStart(match=source, host=MagicMock(id=9),
                clan=MagicMock(tag="#TIMING"), player1=MagicMock(), player2=MagicMock(),
                player1_elo=2000, player2_elo=2000, reply=ManualLiveStartReply(slot_instruction="")))
        saved = await Match.get(source.id)
        self.assertFalse(saved.is_war)
        self.assertEqual(saved.scheduled_times, [None, None])
        self.assertEqual(saved.hosts[0], "9")
        self.assertIsNotNone(saved.final_hosting_times[0])
        self.assertEqual(saved.final_started_times, [saved.final_started_at, saved.final_started_at])
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "CLOSED")
