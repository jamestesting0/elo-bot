import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy
from pymongo.errors import OperationFailure

from cogs.self_commands import SelfCommands
from cogs.war_hosting import helper_store, result_store
from common.constants import MATCH_CHANNEL, RED_COLOR, SUCCESS_EMOJI
from common.matches.recovery import _repair_message
from tests import _helper_score_fixture as helper_fixture
from tests import _result_retry_store as retry_fixture


class TestLiveResultPublication(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = retry_fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.runtime.match["is_war"] = False
        self.announcement = MagicMock(
            id=42, embeds=[ipy.Embed(title="**Match Started**")],
            components=[ipy.ActionRow(ipy.Button(style=ipy.ButtonStyle.SECONDARY, label="Spectate",
                                                custom_id="live_spectate|TOKEN|100"))],
            edit=AsyncMock(),
        )
        self.summary = MagicMock(id=901, edit=AsyncMock())
        self.replacement = MagicMock(id=902, edit=AsyncMock())
        self.channel = MagicMock(fetch_message=AsyncMock(return_value=self.announcement),
                                 send=AsyncMock(return_value=self.replacement))
        self.runtime.thread.send.return_value = self.summary
        self.runtime.thread.fetch_message.return_value = self.summary
        self.runtime.state.bot.fetch_channel.side_effect = (
            lambda channel_id: self.channel if channel_id == MATCH_CHANNEL else self.runtime.thread
        )
        self.ctx = MagicMock(message=None, send=AsyncMock())

    async def finalize(self) -> bool:
        return await self.runtime.cog._finalize_match_results_locked("TOKEN", ctx=self.ctx)

    async def test_live_results_update_original_public_embed_after_durable_binding(self) -> None:
        # Given: the live announcement is already bound to the match.
        async def edit(**payload) -> None:
            self.assertTrue(self.runtime.match["result_operation"]["completed"])
            self.assertEqual(self.runtime.match["result_operation"]["public_message_id"], 42)
            self.assertFalse(self.runtime.match["result_operation"]["published"])
            self.assertFalse(self.ctx.send.await_count)
            self.announcement.embeds = [payload["embed"]]
            self.announcement.components = payload["components"]

        self.announcement.edit.side_effect = edit
        # When: both live results are finalized.
        self.assertTrue(await self.finalize())
        # Then: the original public message contains the recorded results with no Spectate control.
        self.channel.fetch_message.assert_awaited_once_with(42)
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.runtime.match["msg"], "42")
        self.assertTrue(self.runtime.match["result_operation"]["published"])
        self.assertEqual(self.announcement.embeds[0].title, "**Match has Ended**")
        self.assertEqual(self.announcement.embeds[0].color, RED_COLOR)
        self.assertEqual(self.announcement.embeds[0].description,
                         self.runtime.thread.send.await_args.kwargs["embed"].description)
        self.assertEqual(self.announcement.components, [])
        await _repair_message(self.announcement, bound=True, ended=True, active=True)
        self.announcement.edit.assert_awaited_once()

    async def test_public_edit_failure_retries_original_without_repeating_results(self) -> None:
        # Given: the original public message cannot be edited on the first attempt.
        self.announcement.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.finalize())
        self.assertEqual(self.runtime.match["result_operation"]["public_message_id"], 42)
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.assertIn("incomplete", self.ctx.send.await_args.args[0])
        records = deepcopy(self.runtime.state.records)
        players = deepcopy(self.runtime.state.players)
        self.announcement.edit.side_effect = None
        # When: recovery retries publication.
        self.assertTrue(await self.finalize())
        # Then: the same message receives the result without duplicate persistence or publication.
        self.assertEqual(self.runtime.match["msg"], "42")
        self.assertTrue(self.runtime.match["result_operation"]["published"])
        self.assertEqual(self.runtime.state.records, records)
        self.assertEqual(self.runtime.state.players, players)
        self.assertEqual(self.announcement.edit.await_args.kwargs["components"], [])
        self.runtime.thread.send.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_public_binding_failure_preserves_announcement_and_retries(self) -> None:
        # Given: durable binding of the announcement to the result fails.
        def fail_binding(updates) -> None:
            if updates.get("$set", {}).get("result_operation.public_message_id") == 42:
                raise OperationFailure("injected binding failure")

        self.runtime.before_match_update = fail_binding
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.finalize())
        self.assertIsNone(self.runtime.match["result_operation"]["public_message_id"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.announcement.edit.assert_not_awaited()
        self.assertEqual(self.runtime.match["msg"], "42")
        self.runtime.before_match_update = lambda _updates: None
        # When: binding becomes available again.
        self.assertTrue(await self.finalize())
        # Then: publication completes using the original announcement.
        self.assertEqual(self.runtime.match["result_operation"]["public_message_id"], 42)
        self.announcement.edit.assert_awaited_once()
        self.channel.send.assert_not_awaited()

    async def test_live_store_callback_publishes_results_before_success(self) -> None:
        # Given: Helper serves complete live scores over a local HTTP boundary.
        http = helper_fixture.HelperScoreState(self)
        await http.asyncSetUp()
        self.addAsyncCleanup(http.asyncTearDown)
        result_store.RESULT_AUTOSTORE_LOCKS.clear()
        result_store.RESULT_FINALIZATION_LOCKS.clear()
        bot = self.runtime.state.bot
        bot.user.id = 999
        bot.wait_for_component = http.bot.wait_for_component
        bot.fetch_user = AsyncMock(side_effect=self.runtime.state.users)
        bot.get_ext.side_effect = lambda name: self.runtime.cog if name == "MatchCommands" else None
        self.runtime.cog.helper_api_base_url = http.runtime.helper_api_base_url
        self.runtime.cog.helper_api_key = "test-key"
        self.runtime.match["host_clans"] = ["#CLAN", None]
        tags = ("#PPY099", "#LLQ288")
        districts = retry_fixture.DISTRICTS
        for side, user_id in enumerate(("111", "222")):
            self.runtime.state.players[user_id]["tag"] = tags[side]
            self.runtime.match["participants"][side]["result"] = [None] * 3
        body = http.job_document("succeeded")
        body["type"] = "fetch_live_score"
        body["result"] = {"scores": {
            "player_1": {"player_tag": tags[0], "scores": {d: http.score() for d in districts}},
            "player_2": {"player_tag": tags[1], "scores": {d: http.score(2, 90) for d in districts}},
        }}
        http.status_responses = [(200, body)]
        ctx = http.make_context()
        ctx.author.id = 111
        ctx.channel.id = 321
        ctx.defer = AsyncMock()
        progress_message = ctx.send.return_value

        async def send(content: str, **_kwargs) -> ipy.Message:
            if content.startswith(str(SUCCESS_EMOJI)):
                self.assertTrue(self.runtime.match["result_operation"]["published"])
                self.assertEqual(self.runtime.match["result_operation"]["public_message_id"], 42)
                self.announcement.edit.assert_awaited_once()
            return progress_message

        ctx.send.side_effect = send
        cog = SelfCommands(bot)
        # When: a participant invokes the registered live-result callback.
        with patch.object(helper_store.time, "time", return_value=200):
            await cog.self_store_result_live.callback.func(cog, ctx)
        # Then: the HTTP scores, recorded result and original public embed agree before success is shown.
        self.assertEqual(http.create_payloads[0]["type"], "fetch_live_score")
        self.assertEqual(self.runtime.match["result_operation"]["response"], retry_fixture.RESULTS)
        self.assertEqual(self.announcement.edit.await_args.kwargs["embed"].title, "**Match has Ended**")
        self.assertEqual(self.announcement.edit.await_args.kwargs["components"], [])
        self.assertTrue(ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))
        self.channel.send.assert_not_awaited()
        self.assertEqual(self.runtime.match["msg"], "42")

    async def test_changed_public_binding_does_not_edit_stale_announcement(self) -> None:
        # Given: another action replaces the match's public message before binding.
        def replace_binding(updates) -> None:
            if updates.get("$set", {}).get("result_operation.public_message_id") == 42:
                self.runtime.match["msg"] = "99"

        self.runtime.before_match_update = replace_binding
        # When: results try to use the earlier announcement.
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.finalize())
        # Then: the conditional binding preserves the newer identity and leaves publication pending.
        self.assertEqual(self.runtime.match["msg"], "99")
        self.assertIsNone(self.runtime.match["result_operation"]["public_message_id"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.announcement.edit.assert_not_awaited()
        self.channel.send.assert_not_awaited()

    async def test_deleted_live_announcement_creates_bound_result_replacement(self) -> None:
        # Given: Discord confirms that the original public announcement was deleted.
        self.channel.fetch_message.side_effect = ipy.errors.NotFound(MagicMock(status=404, reason="Not Found"))
        # When: the complete live results are finalized.
        self.assertTrue(await self.finalize())
        # Then: a replacement contains the results and is bound durably.
        self.channel.fetch_message.assert_awaited_once_with(42)
        self.channel.send.assert_awaited_once()
        self.assertEqual(self.channel.send.await_args.kwargs["embed"].title, "**Match has Ended**")
        self.assertEqual(self.channel.send.await_args.kwargs["components"], [])
        self.assertEqual(self.runtime.match["msg"], "902")
        self.assertEqual(self.runtime.match["result_operation"]["public_message_id"], 902)
        self.assertTrue(self.runtime.match["result_operation"]["published"])

    async def test_unavailable_announcement_does_not_send_duplicate(self) -> None:
        # Given: Discord cannot confirm whether the existing announcement is available.
        self.channel.fetch_message.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        # When: publication tries to fetch it.
        with self.assertLogs(level="ERROR"):
            self.assertFalse(await self.finalize())
        # Then: results remain recorded and publication stays pending, with no duplicate public message.
        self.assertTrue(self.runtime.match["result_operation"]["completed"])
        self.assertFalse(self.runtime.match["result_operation"]["published"])
        self.assertEqual(self.runtime.match["msg"], "42")
        self.channel.send.assert_not_awaited()
        self.announcement.edit.assert_not_awaited()
