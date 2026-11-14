import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elobot.app import errors
from elobot.config.emojis import ERROR_EMOJI


class TestCommandErrorHandler(unittest.IsolatedAsyncioTestCase):
    handlers = ((errors.on_command_error, errors),)

    async def test_max_concurrency_message_describes_limit_and_bucket(self) -> None:
        for handler, module in self.handlers:
            for bucket, label in (
                    (ipy.Buckets.USER, "user"),
                    (ipy.Buckets.GUILD, "guild"),
                    (ipy.Buckets.CHANNEL, "channel"),
                    (ipy.Buckets.MEMBER, "member"),
                    (ipy.Buckets.CATEGORY, "category"),
                    (ipy.Buckets.ROLE, "role"),
                    (ipy.Buckets.DEFAULT, "user"),
            ):
                with self.subTest(handler=handler.callback.__module__, bucket=bucket):
                    ctx = MagicMock(spec=ipy.SlashContext)
                    ctx.send = AsyncMock()
                    max_concurrency = ipy.MaxConcurrency(2, bucket)
                    error = ipy.errors.MaxConcurrencyReached(MagicMock(), max_concurrency)
                    event = MagicMock(ctx=ctx, error=error)

                    with patch.object(module, "report_error", new_callable=AsyncMock) as report_error:
                        await handler.callback(event)

                    message = ctx.send.await_args.args[0].lower()
                    self.assertTrue(message.startswith(str(ERROR_EMOJI)))
                    self.assertIn("2 time(s)", message)
                    self.assertIn(f"per {label}", message)
                    self.assertTrue(ctx.send.await_args.kwargs["ephemeral"])
                    report_error.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
