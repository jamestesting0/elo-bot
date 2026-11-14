from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

from elobot.features.hosting.live.live_start import LiveStart, ManualLiveStartReply, start_live_match
from elobot.extensions.match import MatchCommands
from elobot.features.finals.hosting import accept_final_start
from elobot.features.finals.lifecycle import confirm_bracket, resolve_final
from elobot.features.finals.matches import bind_thread, prepare_match, schedule_final_match
from elobot.features.finals.recovery import mark_deleted_attempt, recover_final
from elobot.features.finals.results import FinalResultSubmission, submit_result
from elobot.core.finals import FinalError
from elobot.features.matches.messages import MatchMessageState
from elobot.db import Clan, EloFinal, Match
from elobot.db.final_models import FinalMatchResult
from elobot.db.token_models import FinalBettingMarket
from elobot.features.tokens.bets import open_market
from tests.integration._final_fixture import FinalScenarioCase
from tests._patch_sites import patch_sites


class TestFinalTiming(FinalScenarioCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.final = await confirm_bracket(await self.roster(8), "manager")
        self.source = await prepare_match(self.final, "QF1")

    async def request(self, **changes):
        final = await resolve_final()
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
        node = (await resolve_final()).nodes["QF1"]
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
        node = (await resolve_final()).nodes["QF1"]
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
        self.assertEqual((await resolve_final()).nodes["QF1"].scheduled_times, [None, None])

    async def test_unscheduled_or_partly_scheduled_played_results_do_not_write(self):
        for side in (None, 0):
            if side is not None:
                self.source = await schedule_final_match(self.source, 1700000000, player=side)
            with self.subTest(side=side), self.assertRaisesRegex(FinalError, "Schedule both sides"):
                await submit_result(await self.request())
        self.assertEqual(await FinalMatchResult.count(), 0)
        self.assertIsNone((await resolve_final()).nodes["QF1"].result_id)
        self.assertEqual((await Match.get(self.source.id)).participants[0].result, [None] * 7)

    async def test_acceptance_requires_matching_live_or_requested_war_schedule(self):
        market = await open_market(await resolve_final(), "QF1")
        for player in (None, 0, 1):
            with self.subTest(player=player), self.assertRaises(FinalError):
                await accept_final_start(self.source, "host", player=player)
        source = await schedule_final_match(self.source, 1700000000, player=0)
        for player in (None, 1):
            with self.subTest(player=player), self.assertRaises(FinalError):
                await accept_final_start(source, "host", player=player)
        self.assertEqual((await FinalBettingMarket.get(market.id)).state, "OPEN")
        self.assertIsNone((await Match.get(source.id)).final_started_at)

    async def test_each_war_side_keeps_its_accepted_start_without_reclosing_market(self):
        source = await self.scheduled_pairing(self.final, "QF1")
        market = await open_market(await resolve_final(), "QF1")
        with patch("elobot.features.finals.hosting.time.time", return_value=1800000000):
            await accept_final_start(source, "host", player=1)
        with patch("elobot.features.finals.hosting.time.time", return_value=1800000300):
            await accept_final_start(source, "host", player=0)
            await accept_final_start(source, "host", player=1)
        saved = await Match.get(source.id)
        self.assertEqual(saved.final_started_times, [1800000300, 1800000000])
        self.assertEqual(saved.final_started_at, 1800000000)
        self.assertEqual(saved.scheduled_times, [1700000000, 1700003600])
        self.assertEqual((await resolve_final()).nodes["QF1"].actual_started_at, 1800000000)
        self.assertEqual((await FinalBettingMarket.get(market.id)).version, 1)

    async def test_start_failure_rolls_back_start_and_market_together(self):
        source = await schedule_final_match(self.source, 1700000000)
        market = await open_market(await resolve_final(), "QF1")
        with patch.object(Match.get_pymongo_collection(), "update_one",
                          new=AsyncMock(side_effect=RuntimeError("injected"))):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                await accept_final_start(source, "host", player=None)
        saved = await Match.get(source.id)
        self.assertIsNone(saved.final_started_at)
        self.assertEqual(saved.final_started_times, [None, None])
        self.assertIsNone((await resolve_final()).nodes["QF1"].actual_started_at)
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
        market = await open_market(await resolve_final(), "QF1")
        await ambiguous_commit()
        await accept_final_start(source, "host", player=None)
        started_at = (await Match.get(source.id)).final_started_at
        await accept_final_start(source, "host", player=None)
        self.assertIsNotNone(started_at)
        self.assertEqual((await Match.get(source.id)).final_started_times, [started_at, started_at])
        self.assertEqual((await resolve_final()).nodes["QF1"].scheduled_times, [1700000000] * 2)
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
        self.assertEqual(saved.scheduled_times, (await resolve_final()).nodes["QF1"].scheduled_times)
        self.assertEqual(saved.scheduled_times, [1700003600] * 2 if saved.final_started_at is None else [1700000000] * 2)

    async def test_deleted_attempt_cannot_reschedule_start_or_supply_replacement_timing(self):
        source = await schedule_final_match(self.source, 1700000000)
        await accept_final_start(source, "host", player=None)
        final = await resolve_final()
        await self.race(lambda: mark_deleted_attempt(final, final.nodes["QF1"], source),
                        lambda: schedule_final_match(source, 1700003600))
        current = await resolve_final()
        replacement = await prepare_match(current, "QF1")
        for action in (lambda: schedule_final_match(source, 1700007200),
                       lambda: accept_final_start(source, "host", player=None)):
            with self.assertRaises(FinalError):
                await action()
        self.assertEqual(replacement.final_attempt, source.final_attempt + 1)
        self.assertIsNone(replacement.final_started_at)
        self.assertIsNone((await resolve_final()).nodes["QF1"].actual_started_at)
        result = await submit_result(await self.request())
        self.assertEqual(result.snapshot.scheduled_times, [1700000000] * 2)
        self.assertIsNone(result.snapshot.actual_started_at)

    async def test_correction_retains_official_timing_after_mutation_and_source_deletion(self):
        source = await schedule_final_match(self.source, 1700000000)
        with patch("elobot.features.finals.hosting.time.time", return_value=1800000000):
            await accept_final_start(source, "host", player=None)
        initial = await submit_result(await self.request())
        await source.delete()
        await recover_final(await resolve_final())
        final = await resolve_final()
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
        await recover_final(await resolve_final())
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
                ctx = MagicMock(spec=ipy.SlashContext, send=AsyncMock(), defer=AsyncMock())
                ctx.channel.id = thread

                async def fail(*args, **kwargs):
                    expected = [1700000000] * 2 if player is None else [None, 1700000000]
                    self.assertEqual((await Match.get(source.id)).scheduled_times, expected)
                    self.assertEqual((await resolve_final()).nodes[node_id].scheduled_times, expected)
                    raise ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))

                with patch_sites(('elobot.extensions.match.lifecycle.append_helper_task', 'elobot.extensions.match.hosting.append_helper_task'), new=AsyncMock()), \
                        patch_sites(('elobot.extensions.match.hosting.publish_match_message', 'elobot.extensions.match.settings.publish_match_message'), new=AsyncMock(side_effect=fail)):
                    with self.assertRaises(ipy.errors.Forbidden):
                        if player is None:
                            await cog.match_schedule.callback.func(cog, ctx, timestamp="1700000000")
                        else:
                            await cog.match_war_schedule.callback.func(cog, ctx, player=player, timestamp="1700000000")
                ctx.send.assert_not_awaited()

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
                self.assertIsNone((await resolve_final()).nodes["QF1"].actual_started_at)
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
                player1_elo=2000, player2_elo=2000, scout=False, reply=ManualLiveStartReply(slot_instruction="")))
        saved = await Match.get(source.id)
        self.assertIsNotNone(saved.final_started_at)
        self.assertEqual(saved.scheduled_times, [1700000000, 1700000000])
        self.assertEqual(saved.final_started_times, [saved.final_started_at, saved.final_started_at])
