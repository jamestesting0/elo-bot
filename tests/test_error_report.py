import io
import unittest
from contextlib import redirect_stderr
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import interactions as ipy

from elo_bot.app import errors
from elo_bot.discord import runtime


class TestErrorReport(unittest.IsolatedAsyncioTestCase):
    async def test_long_traceback_still_reaches_owner(self) -> None:
        error = RuntimeError("x" * 5000 + "TAIL")
        for module in (errors,):
            with self.subTest(module=module.__name__):
                ctx = MagicMock(spec=ipy.ComponentContext, send=AsyncMock(), kwargs={}, custom_id="store-bases")
                ctx.author.mention = "<@1>"
                ctx.author.id = 1
                response = MagicMock(send=AsyncMock(), defer=AsyncMock())
                bot = MagicMock(wait_for_component=AsyncMock(return_value=SimpleNamespace(ctx=response)))
                bot.owner.send = AsyncMock()
                with patch.object(runtime, "bot", bot, create=True), redirect_stderr(io.StringIO()):
                    await module.report_error(ctx, error)

                description = bot.owner.send.await_args.kwargs["embed"].description
                self.assertLessEqual(len(description), 4096)
                self.assertIn("TAIL", description)
                self.assertTrue(description.endswith("\n```"))
                response.send.assert_awaited_once()
