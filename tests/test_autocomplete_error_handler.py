import unittest
from unittest.mock import AsyncMock, MagicMock

import coc
import interactions as ipy

from elobot.app import errors


class TestAutocompleteErrorHandler(unittest.IsolatedAsyncioTestCase):
    handlers = (errors.on_autocomplete_error,)

    @staticmethod
    def http_error(code):
        response = MagicMock(status=400, reason="Bad Request")
        return ipy.errors.HTTPException(
            response,
            response_data={"code": code, "message": "interaction unavailable"},
        )

    async def test_does_not_respond_to_unavailable_interaction(self):
        for handler in self.handlers:
            for code in (10062, 40060):
                with self.subTest(handler=handler.callback.__module__, code=code):
                    ctx = MagicMock()
                    ctx.send = AsyncMock()
                    event = MagicMock(ctx=ctx, error=self.http_error(code))

                    await handler.callback(event)

                    ctx.send.assert_not_awaited()

    async def test_swallows_unavailable_interaction_from_fallback_response(self):
        for handler in self.handlers:
            for code in (10062, 40060):
                with self.subTest(handler=handler.callback.__module__, code=code):
                    ctx = MagicMock()
                    ctx.send = AsyncMock(side_effect=self.http_error(code))
                    event = MagicMock(ctx=ctx, error=coc.errors.GatewayError())

                    await handler.callback(event)

                    ctx.send.assert_awaited_once()

    async def test_reraises_other_fallback_http_errors(self):
        for handler in self.handlers:
            with self.subTest(handler=handler.callback.__module__):
                ctx = MagicMock()
                ctx.send = AsyncMock(side_effect=self.http_error(50013))
                event = MagicMock(ctx=ctx, error=coc.errors.GatewayError())

                with self.assertRaises(ipy.errors.HTTPException):
                    await handler.callback(event)
