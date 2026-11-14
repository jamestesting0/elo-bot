from collections import deque
from weakref import WeakKeyDictionary
import asyncio  # noqa: ANYIO_OK - per-loop locks must match the Discord runtime
import time
from typing import Any, Final

import anyio
import interactions as ipy


# Discord reports PATCH /channels/{channel_id}/messages/{message_id} as limit 5 with
# x-ratelimit-reset-after of 1 second. Stay slightly under that shared channel bucket.
_EDIT_LIMIT: Final = 5
_EDIT_WINDOW: Final = 1.1

_CHANNEL_MESSAGE_EDIT_STATE: WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[int, tuple[asyncio.Lock, deque[float]]]
] = WeakKeyDictionary()


async def _sleep(delay: float) -> None:
    await anyio.sleep(delay)


def _now() -> float:
    return time.monotonic()


def _channel_edit_state(channel_id: int) -> tuple[asyncio.Lock, deque[float]]:
    loop = asyncio.get_running_loop()
    channels = _CHANNEL_MESSAGE_EDIT_STATE.get(loop)
    if channels is None:
        channels = {}
        _CHANNEL_MESSAGE_EDIT_STATE[loop] = channels
    state = channels.get(channel_id)
    if state is None:
        state = (asyncio.Lock(), deque())
        channels[channel_id] = state
    return state


def _message_channel_id(message: ipy.Message) -> int:
    for value in (getattr(message, "_channel_id", None), getattr(getattr(message, "channel", None), "id", None)):
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return id(message)


async def edit_channel_message(message: ipy.Message, **kwargs: Any) -> Any:
    lock, times = _channel_edit_state(_message_channel_id(message))
    async with lock:
        now = _now()
        while times and now - times[0] >= _EDIT_WINDOW:
            times.popleft()
        if len(times) >= _EDIT_LIMIT:
            await _sleep(_EDIT_WINDOW - (now - times[0]))
            now = _now()
            while times and now - times[0] >= _EDIT_WINDOW:
                times.popleft()
        try:
            return await message.edit(**kwargs)
        finally:
            times.append(_now())
