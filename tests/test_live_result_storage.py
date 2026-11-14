import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
from beanie import PydanticObjectId
from pymongo.errors import PyMongoError

from elo_bot.features.hosting.live import result_store as live_store
from elo_bot.extensions.match import MatchCommands
from elo_bot.extensions.self_service import SelfCommands
from elo_bot.features.hosting.war import result_store
from elo_bot.features.hosting.war import helper_store
from elo_bot.config.emojis import ERROR_EMOJI, SUCCESS_EMOJI
from tests import _helper_score_fixture as helper_tests


DISTRICTS = ["Wizard Valley", "Balloon Lagoon", "Dragon Cliffs"]
TAGS = ["#PPY099", "#LLQ288"]
RESULTS = [["3.100"] * 3, ["2.85"] * 3]


class TestLiveResultStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.http = helper_tests.HelperScoreState(self)
        await self.http.asyncSetUp()
        self.addAsyncCleanup(self.http.asyncTearDown)
        result_store.RESULT_AUTOSTORE_LOCKS.clear()
        result_store.RESULT_FINALIZATION_LOCKS.clear()
        self.bot = self.http.bot
        self.bot.user.id = 999
        self.bot.fetch_user = AsyncMock(return_value=MagicMock(send=AsyncMock()))
        self.runtime = MatchCommands(self.bot)
        self.runtime.helper_api_base_url = self.http.runtime.helper_api_base_url
        self.runtime.helper_api_key = "test-key"
        self.runtime._finalize_match_results_locked = AsyncMock(return_value=True)
        self.bot.get_ext.return_value = self.runtime
        self.cog = SelfCommands(self.bot)
        self.ctx = self.http.make_context()
        self.ctx.channel.id = 321
        self.ctx.defer = AsyncMock()
        self.source = self.make_match()
        self.fresh = self.make_match()
        self.body = self.http.job_document("succeeded")
        self.body["type"] = "fetch_live_score"
        self.body["result"] = {"scores": {
            "player_1": {"player_tag": TAGS[0], "scores": {d: self.http.score() for d in DISTRICTS}},
            "player_2": {"player_tag": TAGS[1], "scores": {d: self.http.score(2, 85) for d in DISTRICTS}},
        }}
        self.http.status_responses = [(200, self.body)]
        self.database = {}

        async def persist(_query, operation):
            self.database.update(operation["$set"])
            return SimpleNamespace(matched_count=1)

        self.write = AsyncMock(side_effect=persist)
        self.fresh.get_pymongo_collection.return_value.update_one = self.write

    @staticmethod
    def make_match() -> MagicMock:
        return MagicMock(
            id=PydanticObjectId("0123456789abcdef01234567"), token="LIVE", thread="321", is_war=False,
            times=[100, None], hosts=["999", None], host_clans=["#CLAN", None], old_msgs=[555, None],
            final_districts=DISTRICTS.copy(), submitters=[None, None], result_operation=None,
            autowin_winner_id=None, autowin_completed=False,
            participants=[MagicMock(user_id=helper_tests.REQUESTER_ID, result=[None] * 3),
                          MagicMock(user_id="222", result=[None] * 3)],
        )

    async def invoke(self) -> None:
        with (
            patch("elo_bot.extensions.self_service.Match") as command_model,
            patch.object(live_store, "Match") as model,
            patch.object(helper_store, "Player") as players,
            patch.object(helper_store.time, "time", return_value=200),
        ):
            command_model.find_one = AsyncMock(return_value=self.source)
            model.find_one = AsyncMock(side_effect=[self.source, self.fresh])
            players.find_one = AsyncMock(side_effect=[SimpleNamespace(tag=tag) for tag in TAGS])
            await self.cog.self_store_result_live.callback.func(self.cog, self.ctx)

    async def test_callback_posts_live_payload_and_commits_both_before_success(self) -> None:
        self.ctx.author.id = 222

        async def finalize(token):
            self.assertEqual(token, "LIVE")
            self.assertEqual(self.database["participants.0.result"], RESULTS[0])
            self.assertEqual(self.database["participants.1.result"], RESULTS[1])
            self.assertFalse(any(str(c.args[0]).startswith(str(SUCCESS_EMOJI)) for c in self.ctx.send.await_args_list))
            return True

        self.runtime._finalize_match_results_locked.side_effect = finalize
        await self.invoke()

        self.assertEqual(self.http.eta_types, ["fetch_live_score"])
        self.assertEqual(self.http.create_payloads, [{
            "type": "fetch_live_score", "requested_by": "222", "payload": {
                "host_clan_tag": "#CLAN", "player_1_tag": TAGS[0], "player_2_tag": TAGS[1],
                "attacks_started_after": -200.0, "attacks_ended_before": 500.0,
                "districts": DISTRICTS, "max_allowed_attacks": dict.fromkeys(DISTRICTS, 2),
            },
        }])
        self.ctx.defer.assert_awaited_once_with(ephemeral=False)
        self.assertTrue(self.ctx.send.await_args_list)
        self.assertTrue(all(call.kwargs.get("ephemeral") is False for call in self.ctx.send.await_args_list))
        self.write.assert_awaited_once()
        query = self.write.await_args.args[0]
        self.assertFalse(query["is_war"])
        self.assertEqual(query["times.0"], 100)
        self.assertEqual(query["participants.1.user_id"], "222")
        self.assertEqual(self.database["submitters.0"], "999")
        self.assertEqual(self.database["submitters.1"], "999")
        self.assertEqual([p.result for p in self.fresh.participants], RESULTS)
        self.assertIn("Both live match results were stored.", self.ctx.send.await_args.args[0])
        self.assertEqual(self.ctx.edit.await_args.kwargs["components"], [])
        self.assertEqual(self.bot.fetch_user.await_count, 2)
        self.runtime._finalize_match_results_locked.assert_awaited_once_with("LIVE")

    async def test_incomplete_other_player_preserves_both_results(self) -> None:
        self.body["result"]["scores"]["player_2"]["scores"][DISTRICTS[0]] = None
        await self.invoke()
        self.write.assert_not_awaited()
        self.runtime._finalize_match_results_locked.assert_not_awaited()
        self.assertIn("Neither result was changed.", self.ctx.send.await_args.args[0])

    async def test_mixed_attacker_challenge_is_not_trusted(self) -> None:
        self.body["result"]["scores"]["player_1"]["scores"][DISTRICTS[0]]["same_attacker_for_all_attacks"] = False
        await self.invoke()
        self.write.assert_not_awaited()

    async def test_swapped_player_tags_are_rejected(self) -> None:
        self.body["result"]["scores"]["player_1"]["player_tag"] = TAGS[1]
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("different players", self.ctx.edit.await_args.kwargs["content"])

    async def test_miner_violation_on_other_player_reports_staff_without_writes(self) -> None:
        self.body["result"]["scores"]["player_2"]["scores"][DISTRICTS[0]]["attacks"][0]["miner_count"] = 4
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("<@222>", self.ctx.channel.send.await_args.args[0])
        self.assertIn("miner-limit violation", self.ctx.edit.await_args.kwargs["content"])

    async def test_helper_cancellation_preserves_results(self) -> None:
        self.body.update(status="cancelled", result=None)
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("cancelled", self.ctx.edit.await_args.kwargs["content"])
        self.assertEqual(self.ctx.edit.await_args.kwargs["components"], [])

    async def test_helper_failure_without_scores_preserves_results(self) -> None:
        self.body.update(status="failed", result=None, error="device offline")
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("device offline", self.ctx.edit.await_args.kwargs["content"])

    async def test_restarted_live_match_rejects_stale_scores(self) -> None:
        self.fresh.times[0] = 101
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("match changed", self.ctx.send.await_args.args[0])

    async def test_changed_other_participant_rejects_stale_scores(self) -> None:
        self.fresh.participants[1].user_id = "333"
        await self.invoke()
        self.write.assert_not_awaited()

    async def test_concurrent_result_operation_blocks_persistence(self) -> None:
        self.fresh.result_operation = SimpleNamespace(cancelled=False, completed=False)
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("being recorded", self.ctx.send.await_args.args[0])

    async def test_conflicting_existing_score_preserves_results(self) -> None:
        self.fresh.participants[1].result[0] = "1.10"
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertIn("does not match", self.ctx.send.await_args.args[0])

    async def test_database_failure_does_not_announce_success(self) -> None:
        self.write.side_effect = PyMongoError("offline")
        await self.invoke()
        self.assertEqual(self.fresh.participants[0].result, [None] * 3)
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(ERROR_EMOJI)))
        self.runtime._finalize_match_results_locked.assert_not_awaited()

    async def test_conditional_write_miss_does_not_announce_success(self) -> None:
        self.write.side_effect = None
        self.write.return_value = SimpleNamespace(matched_count=0)
        await self.invoke()
        self.assertEqual(self.fresh.participants[1].result, [None] * 3)
        self.runtime._finalize_match_results_locked.assert_not_awaited()

    async def test_incomplete_finalization_reports_saved_results_without_success_controls(self) -> None:
        self.runtime._finalize_match_results_locked.return_value = False
        await self.invoke()
        self.assertEqual(self.database["participants.1.result"], RESULTS[1])
        self.assertIn("Both results were saved, but", self.ctx.send.await_args.args[0])
        self.bot.fetch_user.assert_not_awaited()

    async def test_success_publication_failure_keeps_durable_results(self) -> None:
        original_send = self.ctx.send

        async def send(content, **kwargs):
            if content.startswith(str(SUCCESS_EMOJI)):
                raise RuntimeError("publication failed")
            return await original_send(content, **kwargs)

        self.ctx.send = AsyncMock(side_effect=send)
        with self.assertRaisesRegex(RuntimeError, "publication failed"):
            await self.invoke()
        self.assertEqual(self.database["participants.0.result"], RESULTS[0])
        self.assertEqual(self.database["participants.1.result"], RESULTS[1])
        self.runtime._finalize_match_results_locked.assert_awaited_once()

    async def test_two_participants_share_one_job_and_finalize_once(self) -> None:
        entered_finalization = anyio.Event()
        finish_finalization = anyio.Event()
        second_started = anyio.Event()
        self.source.get_pymongo_collection.return_value.update_one = self.write

        async def finalize(_token):
            entered_finalization.set()
            await finish_finalization.wait()
            self.source.result_operation = SimpleNamespace(cancelled=False, completed=True)
            return True

        async def second_request():
            second_started.set()
            await self.runtime.store_self_live_result(self.ctx, self.source)

        self.runtime._finalize_match_results_locked.side_effect = finalize
        with (
            patch.object(live_store, "Match") as model,
            patch.object(helper_store, "Player") as players,
            patch.object(helper_store.time, "time", return_value=200),
        ):
            model.find_one = AsyncMock(return_value=self.source)
            players.find_one = AsyncMock(side_effect=[SimpleNamespace(tag=tag) for tag in TAGS])
            with anyio.fail_after(5):
                async with anyio.create_task_group() as group:
                    group.start_soon(self.runtime.store_self_live_result, self.ctx, self.source)
                    await entered_finalization.wait()
                    group.start_soon(second_request)
                    await second_started.wait()
                    self.assertEqual(model.find_one.await_count, 2)
                    finish_finalization.set()
        self.assertEqual(len(self.http.create_payloads), 1)
        self.write.assert_awaited_once()
        self.runtime._finalize_match_results_locked.assert_awaited_once()
        self.assertIn("already recorded", self.ctx.send.await_args.args[0])

    async def test_changed_match_mode_before_queueing_creates_no_job(self) -> None:
        fresh = self.make_match()
        fresh.is_war = True
        with patch.object(live_store, "Match") as model:
            model.find_one = AsyncMock(return_value=fresh)
            await self.runtime.store_self_live_result(self.ctx, self.source)
        self.assertFalse(self.http.create_payloads)
        self.write.assert_not_awaited()

    async def test_wrong_response_shape_creates_no_durable_success(self) -> None:
        self.body["result"] = {"scores": {district: self.http.score() for district in DISTRICTS}}
        await self.invoke()
        self.write.assert_not_awaited()
        self.runtime._finalize_match_results_locked.assert_not_awaited()
        self.assertTrue(self.ctx.edit.await_args.kwargs["content"].startswith(str(ERROR_EMOJI)))

    async def test_helper_rejection_is_public(self) -> None:
        self.http.create_status = 503
        self.http.create_body = 'unavailable'
        await self.invoke()
        self.write.assert_not_awaited()
        self.assertTrue(self.ctx.send.await_args_list)
        self.assertTrue(all(call.kwargs.get('ephemeral') is False for call in self.ctx.send.await_args_list))

    async def test_helper_availability_failure_is_public(self) -> None:
        self.http.eta_start = None
        await self.invoke()
        self.assertFalse(self.http.create_payloads)
        self.ctx.send.assert_awaited_once()
        self.assertIs(self.ctx.send.await_args.kwargs['ephemeral'], False)

    async def test_public_queue_prompt_only_accepts_requester(self) -> None:
        from elo_bot.features.hosting.war.confirmation import HelperQueueDecision

        self.http.eta_start = 400
        component = MagicMock(custom_id=HelperQueueDecision.CONTINUE, edit_origin=AsyncMock())
        component.author.id = self.ctx.author.id

        async def wait_for_component(**kwargs):
            if 'timeout' in kwargs:
                check = kwargs['check']
                self.assertTrue(check(SimpleNamespace(ctx=component)))
                self.assertFalse(check(SimpleNamespace(ctx=SimpleNamespace(author=SimpleNamespace(id='outsider')))))
                return SimpleNamespace(ctx=component)
            with anyio.fail_after(5):
                await anyio.Event().wait()

        self.bot.wait_for_component.side_effect = wait_for_component
        await self.invoke()
        self.assertTrue(all(call.kwargs.get('ephemeral') is False for call in self.ctx.send.await_args_list))
        component.edit_origin.assert_awaited_once()
        self.write.assert_awaited_once()
