import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.app import errors
from elo_bot.config.emojis import ERROR_EMOJI
from elo_bot.discord.concurrency import CHANNEL_USER_CONCURRENCY_BUCKET


class TestComponentErrorHandler(unittest.IsolatedAsyncioTestCase):
    async def test_max_concurrency_responds_privately_without_reporting_bug(self) -> None:
        for module in (errors,):
            for bucket, label in (
                (ipy.Buckets.USER, "user"),
                (ipy.Buckets.CHANNEL, "channel"),
                (ipy.Buckets.DEFAULT, "user"),
                (CHANNEL_USER_CONCURRENCY_BUCKET, "channel and user"),
            ):
                with self.subTest(module=module.__name__, bucket=bucket):
                    ctx = MagicMock(spec=ipy.ComponentContext)
                    ctx.send = AsyncMock()
                    error = ipy.errors.MaxConcurrencyReached(MagicMock(), ipy.MaxConcurrency(1, bucket))
                    event = ipy.events.ComponentError(ctx=ctx, error=error)

                    with patch.object(module, "report_error", new_callable=AsyncMock) as report_error:
                        await module.on_component_error.callback(event)

                    ctx.send.assert_awaited_once_with(
                        f"{ERROR_EMOJI} This command can only be used 1 time(s) at once per {label}.",
                        ephemeral=True,
                    )
                    report_error.assert_not_awaited()

    async def test_unexpected_error_still_reports_bug(self) -> None:
        for module in (errors,):
            with self.subTest(module=module.__name__):
                ctx = MagicMock(spec=ipy.ComponentContext)
                ctx.send = AsyncMock()
                error = RuntimeError("unexpected callback failure")
                event = ipy.events.ComponentError(ctx=ctx, error=error)

                with patch.object(module, "report_error", new_callable=AsyncMock) as report_error:
                    await module.on_component_error.callback(event)

                report_error.assert_awaited_once_with(ctx, error)
                ctx.send.assert_not_awaited()
