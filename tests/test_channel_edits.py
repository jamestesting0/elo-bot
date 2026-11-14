import asyncio  # noqa: ANYIO_OK - verifies the asyncio task lifecycle used by the Discord runtime
import unittest
from unittest.mock import AsyncMock, MagicMock

import anyio
import interactions as ipy

from elobot.discord.channel_edits import _PENDING_CHANNEL_NAME_EDITS, schedule_channel_name_edit


class TestChannelNameEdits(unittest.IsolatedAsyncioTestCase):
    async def test_scheduled_edit_returns_while_rate_limited_task_is_pending(self) -> None:
        # Given a channel edit that cannot finish yet
        channel = MagicMock(spec=ipy.ThreadChannel)
        channel.id = 123
        channel.name = "🟢 Match TOKEN"
        started = anyio.Event()
        release = anyio.Event()

        async def delayed_edit(*, name: str) -> None:
            started.set()
            with anyio.fail_after(5):
                await release.wait()

        channel.edit = AsyncMock(side_effect=delayed_edit)

        # When the name edit is scheduled
        task = schedule_channel_name_edit(channel, "🔴")
        with anyio.fail_after(1):
            await started.wait()

        # Then the caller is free while the retained edit continues in the background
        self.assertFalse(task.done())
        self.assertIn(task, _PENDING_CHANNEL_NAME_EDITS)
        release.set()
        async with asyncio.timeout(5):
            await task
        await anyio.sleep(0)
        self.assertNotIn(task, _PENDING_CHANNEL_NAME_EDITS)
        channel.edit.assert_awaited_once_with(name="🔴 Match TOKEN")

    async def test_same_channel_burst_never_overlaps_and_only_applies_latest_pending_name(self) -> None:
        # Given one channel whose first name edit remains in flight
        channel = MagicMock(spec=ipy.ThreadChannel)
        channel.id = 123
        channel.name = "🟡 Match TOKEN"
        first_started = anyio.Event()
        release_first = anyio.Event()
        attempted_names = []
        active_edits = 0
        max_active_edits = 0

        async def delayed_edit(*, name: str) -> None:
            nonlocal active_edits, max_active_edits
            attempted_names.append(name)
            active_edits += 1
            max_active_edits = max(max_active_edits, active_edits)
            try:
                if len(attempted_names) == 1:
                    first_started.set()
                    with anyio.fail_after(5):
                        await release_first.wait()
            finally:
                active_edits -= 1

        channel.edit = AsyncMock(side_effect=delayed_edit)

        # When two newer states are requested before the first edit completes
        tasks = [schedule_channel_name_edit(channel, "🔵")]
        with anyio.fail_after(1):
            await first_started.wait()
        tasks.append(schedule_channel_name_edit(channel, "🟢"))
        tasks.append(schedule_channel_name_edit(channel, "🔴"))

        try:
            await anyio.sleep(0)

            # Then no second Discord PATCH starts while the first is active
            self.assertEqual(attempted_names, ["🔵 Match TOKEN"])
        finally:
            release_first.set()
            async with asyncio.timeout(5):
                await asyncio.gather(*tasks, return_exceptions=True)

        # And the obsolete middle state is coalesced into the latest requested name
        self.assertEqual(max_active_edits, 1)
        self.assertEqual(attempted_names, ["🔵 Match TOKEN", "🔴 Match TOKEN"])

    async def test_expected_discord_edit_failures_finish_cleanly(self) -> None:
        # Given Discord rejects a background rename after handling its rate limit
        response = MagicMock(status=429, reason="Too Many Requests")
        errors = (
            ipy.errors.HTTPException(response),
            ipy.errors.TooManyChanges(),
        )

        # When each expected error completes its scheduled task
        for error in errors:
            channel = MagicMock(spec=ipy.ThreadChannel)
            channel.id = 123
            channel.name = "🟢 Match TOKEN"
            channel.edit = AsyncMock(side_effect=error)
            task = schedule_channel_name_edit(channel, "🔴")
            async with asyncio.timeout(5):
                await task
            await anyio.sleep(0)

            # Then the error does not escape and the task is no longer retained
            self.assertNotIn(task, _PENDING_CHANNEL_NAME_EDITS)

    async def test_unexpected_failure_is_reported_and_released(self) -> None:
        # Given an unexpected failure in the detached Discord edit
        channel = MagicMock(spec=ipy.ThreadChannel)
        channel.id = 123
        channel.name = "🟢 Match TOKEN"
        channel.edit = AsyncMock(side_effect=RuntimeError("unexpected edit failure"))
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        contexts = []
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        try:
            # When the scheduled task fails
            task = schedule_channel_name_edit(channel, "🔴")
            with self.assertRaisesRegex(RuntimeError, "unexpected edit failure"), anyio.fail_after(5):
                await task
            await anyio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        # Then its exception is observed once and the task is released
        self.assertNotIn(task, _PENDING_CHANNEL_NAME_EDITS)
        self.assertEqual(len(contexts), 1)
        self.assertIs(contexts[0]["exception"], task.exception())

    async def test_cancelled_edit_is_released_without_error_reporting(self) -> None:
        # Given a retained channel edit that is still waiting
        channel = MagicMock(spec=ipy.ThreadChannel)
        channel.id = 123
        channel.name = "🟢 Match TOKEN"
        started = anyio.Event()
        release = anyio.Event()

        async def delayed_edit(*, name: str) -> None:
            started.set()
            with anyio.fail_after(5):
                await release.wait()

        channel.edit = AsyncMock(side_effect=delayed_edit)
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        contexts = []
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        try:
            # When the scheduled task is cancelled
            task = schedule_channel_name_edit(channel, "🔴")
            with anyio.fail_after(1):
                await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError), anyio.fail_after(5):
                await task
            await anyio.sleep(0)
        finally:
            loop.set_exception_handler(previous_handler)

        # Then cancellation removes retention without reporting an operational failure
        self.assertNotIn(task, _PENDING_CHANNEL_NAME_EDITS)
        self.assertEqual(contexts, [])


if __name__ == "__main__":
    unittest.main()
