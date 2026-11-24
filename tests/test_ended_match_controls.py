import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import interactions as ipy

from elo_bot.config.presentation import RED_COLOR
from elo_bot.features.matches.recovery import _repair_match, _repair_message
from elo_bot.db import Match, Participant


class TestEndedMatchControls(unittest.IsolatedAsyncioTestCase):
    async def test_recorded_live_match_recovery_removes_thread_hosting_controls(self) -> None:
        message = SimpleNamespace(id=43, embeds=[ipy.Embed(title="**Match Ended**", color=RED_COLOR)],
                                  components=["hosting controls"], edit=AsyncMock())
        async def history(**_kwargs):
            yield message
        channel = SimpleNamespace(history=history)
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=channel))
        match = Match.model_construct(thread="42", old_msgs=[43, None], times=[123, None], is_war=False,
            result_operation=SimpleNamespace(completed=True),
            participants=[Participant(user_id="1"), Participant(user_id="2")])

        await _repair_match(bot, match)

        self.assertEqual(message.edit.await_args.kwargs["components"], [])

    async def test_completed_match_recovery_preserves_existing_controls(self) -> None:
        message = SimpleNamespace(id=43, embeds=[ipy.Embed(title="**Match Started**", description="Updating…")],
                                  components=["original controls"], edit=AsyncMock())
        async def history(**_kwargs):
            yield message
        channel = SimpleNamespace(history=history)
        bot = SimpleNamespace(fetch_channel=AsyncMock(return_value=channel))
        match = Match.model_construct(thread="42", old_msgs=[43, None], times=[123, 123],
            participants=[Participant(user_id="1", result=["3.100"] * 3),
                          Participant(user_id="2", result=["3.100"] * 3)])

        await _repair_match(bot, match)

        payload = message.edit.await_args.kwargs
        self.assertNotIn("components", payload)
        self.assertEqual(payload["embeds"][0]["title"], "**Match Ended**")
        self.assertEqual(payload["embeds"][0]["description"], "")

    async def test_result_summary_recovery_keeps_delete_thread_button(self) -> None:
        message = SimpleNamespace(id=44, embeds=[ipy.Embed(title="**Match has Ended**", description="Results")],
                                  components=["delete thread"], edit=AsyncMock())

        await _repair_message(message, bound=True, ended=True, active=False)

        message.edit.assert_not_awaited()
