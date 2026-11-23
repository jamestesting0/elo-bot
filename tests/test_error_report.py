import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.app import errors
from elo_bot.discord import runtime


class TestErrorReport(unittest.IsolatedAsyncioTestCase):
    async def test_long_player_report_reaches_owner_and_bug_report_channel(self) -> None:
        error = RuntimeError("x" * 5000 + "TAIL")
        ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), kwargs={}, custom_id="store-bases")
        ctx.author.mention = "<@1>"
        ctx.author.id = 1
        response = MagicMock(send=AsyncMock(), defer=AsyncMock())
        channel = MagicMock(spec=ipy.GuildText)
        bot = MagicMock(
            wait_for_component=AsyncMock(return_value=SimpleNamespace(ctx=response)),
            fetch_channel=AsyncMock(return_value=channel),
        )
        bot.owner.send = AsyncMock()
        with patch.object(runtime, "bot", bot, create=True), redirect_stderr(io.StringIO()):
            await errors.report_error(ctx, error)

        dm = bot.owner.send.await_args.kwargs
        self.assertLessEqual(len(dm["embed"].description), 4096)
        self.assertIn("TAIL", dm["embed"].description)
        self.assertTrue(dm["embed"].description.endswith("\n```"))
        self.assertEqual(dm["components"].label, "Respond")
        response.send.assert_awaited_once()
        bot.fetch_channel.assert_awaited_once_with(1566842840716152962)
        channel.send.assert_awaited_once()
        copy = channel.send.await_args.kwargs
        self.assertEqual(copy["embed"].to_dict(), dm["embed"].to_dict())
        self.assertNotIn("components", copy)

    async def test_automatic_errors_reach_owner_and_bug_report_channel(self) -> None:
        for handler in (errors.on_error, errors.on_autocomplete_error):
            with self.subTest(handler=handler.callback.__name__):
                channel = MagicMock(spec=ipy.GuildText)
                bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
                bot.owner.send = AsyncMock()
                event = MagicMock(
                    ctx=MagicMock(send=AsyncMock()),
                    error=ValueError("Field value cannot exceed 1024 characters"),
                )
                with patch.object(runtime, "bot", bot, create=True), redirect_stderr(io.StringIO()):
                    await handler.callback(event)

                bot.owner.send.assert_awaited_once()
                dm = bot.owner.send.await_args.kwargs
                self.assertIn(str(event.error), dm["embed"].description)
                self.assertTrue(dm["silent"])
                bot.fetch_channel.assert_awaited_once_with(1566842840716152962)
                channel.send.assert_awaited_once()
                self.assertEqual(channel.send.await_args.kwargs["embed"].to_dict(), dm["embed"].to_dict())

    async def test_rejected_channel_post_is_logged_and_preserves_the_owner_dm(self) -> None:
        channel = MagicMock(spec=ipy.GuildText)
        channel.send.side_effect = ipy.errors.Forbidden(MagicMock(status=403, reason="Forbidden"))
        bot = MagicMock(fetch_channel=AsyncMock(return_value=channel))
        bot.owner.send = AsyncMock()
        event = MagicMock(error=ValueError("Synthetic error"))
        with patch.object(runtime, "bot", bot, create=True), redirect_stderr(io.StringIO()) as log:
            await errors.on_error.callback(event)

        channel.send.assert_awaited_once()
        bot.owner.send.assert_awaited_once()
        self.assertIn("Synthetic error", bot.owner.send.await_args.kwargs["embed"].description)
        self.assertIn("1566842840716152962", log.getvalue())
