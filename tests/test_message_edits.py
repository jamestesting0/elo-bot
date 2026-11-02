import asyncio  # noqa: ANYIO_OK - overlap check uses the same runtime task type as Discord edits
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import anyio
import interactions as ipy

from common.discord import message_edits
from common.discord.message_edits import edit_channel_message


class TestChannelMessageEdits(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = 0.0
        self.slept: list[float] = []
        self.enterContext(patch.object(message_edits, "_now", new=self._monotonic))
        self.enterContext(patch.object(message_edits, "_sleep", new=self._sleep))

    def _monotonic(self) -> float:
        return self.clock

    async def _sleep(self, delay: float) -> None:
        self.slept.append(delay)
        self.clock += delay

    def _message(self, channel_id: int, message_id: int = 1) -> MagicMock:
        message = MagicMock(spec=ipy.Message)
        message.id = message_id
        message._channel_id = channel_id
        message.edit = AsyncMock()
        return message

    async def test_five_edits_in_the_same_channel_do_not_wait(self) -> None:
        message = self._message(123)
        for index in range(5):
            await edit_channel_message(message, content=str(index))
            self.clock += 0.01

        self.assertEqual(self.slept, [])
        self.assertEqual(message.edit.await_count, 5)

    async def test_sixth_edit_in_the_same_channel_waits_for_the_bucket(self) -> None:
        message = self._message(123)
        for index in range(6):
            await edit_channel_message(message, content=str(index))
            self.clock += 0.01

        self.assertEqual(len(self.slept), 1)
        self.assertGreater(self.slept[0], 1.0)
        self.assertEqual(message.edit.await_count, 6)

    async def test_other_channels_do_not_share_the_bucket(self) -> None:
        first = self._message(123)
        second = self._message(456)
        for index in range(5):
            await edit_channel_message(first, content=str(index))
            self.clock += 0.01
        await edit_channel_message(second, content="other")

        self.assertEqual(self.slept, [])
        second.edit.assert_awaited_once_with(content="other")

    async def test_same_channel_edits_never_overlap(self) -> None:
        message = self._message(123)
        started = anyio.Event()
        release = anyio.Event()
        second_requested = anyio.Event()
        active = 0
        max_active = 0
        channel_state = message_edits._channel_edit_state(123)
        requests = 0

        def get_channel_state(_channel_id):
            nonlocal requests
            requests += 1
            if requests == 2:
                second_requested.set()
            return channel_state

        async def delayed_edit(**_kwargs) -> None:
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                if message.edit.await_count == 1:
                    started.set()
                    async with asyncio.timeout(5):
                        await release.wait()
            finally:
                active -= 1

        message.edit.side_effect = delayed_edit
        with patch.object(message_edits, "_channel_edit_state", side_effect=get_channel_state):
            async with asyncio.timeout(5):
                first = asyncio.create_task(edit_channel_message(message, content="one"))
                await started.wait()
                second = asyncio.create_task(edit_channel_message(message, content="two"))
                try:
                    await second_requested.wait()
                    self.assertEqual(max_active, 1)
                finally:
                    release.set()
                    await asyncio.gather(first, second)
        self.assertEqual(max_active, 1)
        self.assertEqual(message.edit.await_count, 2)


if __name__ == "__main__":
    unittest.main()
