import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock

import interactions as ipy

from elo_bot.discord import decorators
from elo_bot.discord.decorators import ignore_repeated_clicks

CREATED = datetime(2026, 11, 19, 12)


def click(messages: dict[int, SimpleNamespace], user: int = 9) -> MagicMock:
    """A click on message 5; `messages` plays the client's message cache, so a reply can replace the version."""
    ctx = MagicMock(spec=ipy.ComponentContext, defer=AsyncMock(), author=SimpleNamespace(id=user))
    type(ctx).message = PropertyMock(side_effect=lambda: messages.get(5))
    return ctx


def version(edited: int | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=5, timestamp=CREATED,
                           edited_timestamp=None if edited is None else CREATED + timedelta(seconds=edited))


class Handler:
    def __init__(self, messages, *, edits_origin: bool, gate: asyncio.Event | None = None):
        self.messages, self.edits_origin, self.gate = messages, edits_origin, gate
        self.handled: list[MagicMock] = []
        self.entered = asyncio.Event()

    @ignore_repeated_clicks
    async def callback(self, ctx):
        self.handled.append(ctx)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.edits_origin:
            self.messages[5] = version(len(self.handled))


class TestIgnoreRepeatedClicks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.addCleanup(decorators._REPLACED_VERSIONS.clear)
        self.addCleanup(decorators._CLICKS_IN_FLIGHT.clear)
        self.messages = {5: version()}

    async def test_second_click_while_the_first_runs_is_acknowledged_without_acting(self):
        handler = Handler(self.messages, edits_origin=True, gate=asyncio.Event())
        first, second = click(self.messages), click(self.messages)
        async with asyncio.timeout(5):
            running = asyncio.create_task(handler.callback(first))
            await handler.entered.wait()
            await handler.callback(second)
            handler.gate.set()
            await running
        self.assertEqual(handler.handled, [first])
        second.defer.assert_awaited_once_with(edit_origin=True)

    async def test_late_click_on_a_replaced_view_is_ignored_but_the_new_view_works(self):
        handler = Handler(self.messages, edits_origin=True)
        old = self.messages[5]
        await handler.callback(click(self.messages))
        replaced = self.messages[5]
        # A click made before the view was redrawn brings that old version with it into the cache.
        self.messages[5] = old
        stale = click(self.messages)
        await handler.callback(stale)
        self.assertEqual(len(handler.handled), 1)
        stale.defer.assert_awaited_once_with(edit_origin=True)
        self.messages[5] = replaced
        current = click(self.messages)
        await handler.callback(current)
        self.assertEqual(handler.handled[-1], current)

    async def test_buttons_that_leave_the_message_unchanged_can_be_clicked_again(self):
        # A button that opens a modal or sends a new message must still work after the modal is dismissed.
        handler = Handler(self.messages, edits_origin=False)
        await handler.callback(click(self.messages))
        await handler.callback(click(self.messages))
        self.assertEqual(len(handler.handled), 2)

    async def test_another_users_click_on_the_same_message_is_handled(self):
        handler = Handler(self.messages, edits_origin=True, gate=asyncio.Event())
        async with asyncio.timeout(5):
            running = asyncio.create_task(handler.callback(click(self.messages, user=1)))
            await handler.entered.wait()
            other = asyncio.create_task(handler.callback(click(self.messages, user=2)))
            handler.gate.set()
            await asyncio.gather(running, other)
        self.assertEqual(sorted(click.author.id for click in handler.handled), [1, 2])
