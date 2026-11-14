import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import interactions as ipy
from pymongo.errors import OperationFailure

from elobot.config.ids import MATCH_CHANNEL, STAFF_ROLE
from elobot.config.emojis import SUCCESS_EMOJI
from elobot.db import Match
from tests import _result_retry_store as fixture


class TestMatchHostAndResultClear(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.runtime = fixture.ResultRetryState(self)
        self.runtime.setUp()
        self.match = self.runtime.match
        self.ctx = MagicMock(defer=AsyncMock(), send=AsyncMock(), author=MagicMock(id=999),
                             channel_id=321, channel=MagicMock(id=321, fetch_message=AsyncMock()))
        self.host = MagicMock(id=888, mention="<@888>", display_name="Host", roles=[MagicMock(id=STAFF_ROLE)])
        self.message = MagicMock(id=90, _channel_id=321, channel=self.ctx.channel, components=[],
                                 embeds=[ipy.Embed()], edit=AsyncMock())
        self.ctx.channel.fetch_message.return_value = self.message
        self.public = MagicMock(id=42, _channel_id=MATCH_CHANNEL, components=[], embeds=[ipy.Embed()], edit=AsyncMock())
        self.runtime.state.bot.fetch_channel = AsyncMock(return_value=MagicMock(
            id=MATCH_CHANNEL, fetch_message=AsyncMock(return_value=self.public)))

    def schedule(self) -> None:
        self.match.update(is_war=False, times=[100, 100], old_msgs=[90, 90])
        for participant in self.match["participants"]:
            participant["result"] = [None] * 3

    async def set_host(self) -> None:
        await self.runtime.cog.match_set_host.callback.func(self.runtime.cog, self.ctx, self.host)

    async def clear(self, player: int = 0) -> None:
        await self.runtime.cog.match_result_clear.callback.func(self.runtime.cog, self.ctx, player)

    async def test_host_is_persisted_for_both_sides_before_publication(self) -> None:
        # Given
        self.schedule()
        async def check_saved(**_kwargs):
            self.assertEqual(self.match["hosts"], ["888", "888"])
        self.message.edit.side_effect = check_saved
        # When
        await self.set_host()
        # Then
        self.assertEqual(self.match["hosts"], ["888", "888"])
        self.assertEqual(self.message.edit.await_args.kwargs["embed"]["fields"][1]["value"],
                         "**[Host](<https://discordapp.com/users/888>)**")
        self.assertIn("**[Host](<https://discordapp.com/users/888>)**", self.public.embeds[0].description)
        self.assertTrue(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_host_write_failure_never_publishes_success(self) -> None:
        self.schedule()
        self.runtime.before_match_write.side_effect = OperationFailure("injected write failure")
        with self.assertRaises(OperationFailure):
            await self.set_host()
        self.message.edit.assert_not_awaited()
        self.ctx.send.assert_not_awaited()

    async def test_host_publication_failure_preserves_saved_host(self) -> None:
        self.schedule()
        self.message.edit.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.set_host()
        self.assertEqual(self.match["hosts"], ["888", "888"])
        self.ctx.send.assert_not_awaited()

    async def test_stale_host_assignment_does_not_overwrite_newer_host(self) -> None:
        self.schedule()
        self.runtime.before_match_update = lambda _updates: self.match.update(hosts=["777", "777"])
        await self.set_host()
        self.assertEqual(self.match["hosts"], ["777", "777"])
        self.message.edit.assert_not_awaited()
        self.assertFalse(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_clear_preserves_other_player_and_reservation_fields(self) -> None:
        # Given
        self.match["submitters"] = ["999", "888"]
        self.match["participants"][0]["base_store"] = {
            "clan_tag": "#CLAN", "slot": "Slot 1", "stored_at": 100, "reservation_id": "reservation",
        }
        before = deepcopy(self.match)
        async def check_saved(*_args, **_kwargs):
            self.assertEqual(self.match["participants"][0]["result"], [None] * 3)
        self.ctx.send.side_effect = check_saved
        # When
        await self.clear()
        # Then
        before["participants"][0]["result"] = [None] * 3
        before["submitters"][0] = None
        self.assertEqual(self.match, before)
        self.assertEqual(self.ctx.send.await_args.args[0], f"{SUCCESS_EMOJI} Cleared <@111>'s result.")

    async def test_clear_player_two_and_repeat_are_safe(self) -> None:
        before = deepcopy(self.match["participants"][0])
        await self.clear(1)
        await self.clear(1)
        self.assertEqual(self.match["participants"][1]["result"], [None] * 3)
        self.assertEqual(self.match["participants"][0], before)

    async def test_clear_rejects_participant_who_is_not_selected_sides_host(self) -> None:
        self.ctx.author.id = 111
        before = deepcopy(self.match)
        await self.clear()
        self.assertEqual(self.match, before)
        self.assertFalse(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_clear_write_failure_does_not_report_success(self) -> None:
        before = deepcopy(self.match)
        self.runtime.before_match_write.side_effect = OperationFailure("injected write failure")
        with self.assertRaises(OperationFailure):
            await self.clear()
        self.assertEqual(self.match, before)
        self.ctx.send.assert_not_awaited()

    async def test_clear_rejects_changed_result_at_write_boundary(self) -> None:
        self.runtime.before_match_update = lambda _updates: self.match["participants"][0].update(
            result=["1.50"] * 3)
        await self.clear()
        self.assertEqual(self.match["participants"][0]["result"], ["1.50"] * 3)
        self.assertFalse(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_clear_rejects_finalization_claim_at_write_boundary(self) -> None:
        async def claim(_query, _updates):
            from elobot.features.results.operations import begin_result_operation
            self.runtime.before_match_write = AsyncMock()
            match = await Match.find_one({"token": "TOKEN"})
            assert match is not None
            await begin_result_operation(match, fixture.RESULTS)
        self.runtime.before_match_write = AsyncMock(side_effect=claim)
        before = deepcopy(self.match["participants"])
        await self.clear()
        self.assertEqual(self.match["participants"], before)
        self.assertIn("being recorded", self.ctx.send.await_args.args[0])

    async def test_clear_rejects_previously_recorded_result(self) -> None:
        self.match["recorded_match_id"] = "11"
        before = deepcopy(self.match)
        await self.clear()
        self.assertEqual(self.match, before)
        self.assertIn("/match resubmit", self.ctx.send.await_args.args[0])

    async def test_missing_match_does_not_write(self) -> None:
        self.ctx.channel.id = 987
        before = deepcopy(self.match)
        await self.clear()
        self.assertEqual(self.match, before)
        self.assertIn("Unable to fetch", self.ctx.send.await_args.args[0])

    async def test_clear_does_not_restore_deleted_match(self) -> None:
        self.runtime.before_match_update = lambda _updates: self.match.update(thread=None, times=[None, None])
        before = deepcopy(self.match["participants"])
        await self.clear()
        self.assertEqual(self.match["participants"], before)
        self.assertIsNone(self.match["thread"])
        self.assertFalse(self.ctx.send.await_args.args[0].startswith(str(SUCCESS_EMOJI)))

    async def test_clear_publication_failure_preserves_cleared_result(self) -> None:
        self.ctx.send.side_effect = ipy.errors.HTTPException(MagicMock(status=503, reason="Unavailable"))
        with self.assertRaises(ipy.errors.HTTPException):
            await self.clear()
        self.assertEqual(self.match["participants"][0]["result"], [None] * 3)

    async def test_clear_rejects_pending_completed_and_cancelled_operations(self) -> None:
        from elobot.features.results.operations import begin_result_operation
        match = await Match.find_one({"token": "TOKEN"})
        assert match is not None
        await begin_result_operation(match, fixture.RESULTS)
        for completed, cancelled, reason in (
            (False, False, "being recorded"),
            (True, False, "/match resubmit"),
            (True, True, "deleted and will not be recreated"),
        ):
            with self.subTest(completed=completed, cancelled=cancelled):
                self.match["result_operation"].update(completed=completed, cancelled=cancelled)
                before = deepcopy(self.match)
                await self.clear()
                self.assertEqual(self.match, before)
                self.assertIn(reason, self.ctx.send.await_args.args[0])
