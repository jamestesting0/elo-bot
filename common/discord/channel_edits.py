import asyncio  # noqa: ANYIO_OK - Discord name edits must outlive the interaction that scheduled them
from typing import Final

import interactions as ipy


_PENDING_CHANNEL_NAME_EDITS: Final[set[asyncio.Task[None]]] = set()
_CHANNEL_NAME_EDIT_TASKS: Final[dict[int, asyncio.Task[None]]] = {}
_LATEST_CHANNEL_NAMES: Final[dict[int, str]] = {}


async def _edit_channel_name(channel: ipy.BaseChannel, channel_id: int) -> None:
    while True:
        channel_name = _LATEST_CHANNEL_NAMES[channel_id]
        try:
            await channel.edit(name=channel_name)
        except (ipy.errors.HTTPException, ipy.errors.TooManyChanges):
            return
        if _LATEST_CHANNEL_NAMES[channel_id] == channel_name:
            return


def _finish_channel_name_edit(channel_id: int, task: asyncio.Task[None]) -> None:
    _PENDING_CHANNEL_NAME_EDITS.discard(task)
    if _CHANNEL_NAME_EDIT_TASKS.get(channel_id) is task:
        _CHANNEL_NAME_EDIT_TASKS.pop(channel_id)
        _LATEST_CHANNEL_NAMES.pop(channel_id)
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        task.get_loop().call_exception_handler({
            "message": "Background channel name edit failed",
            "exception": error,
            "task": task,
        })


def schedule_channel_name_edit(channel: ipy.BaseChannel, status_prefix: str) -> asyncio.Task[None]:
    channel_id = int(channel.id)
    suffix = channel.name.partition(" ")[2]
    _LATEST_CHANNEL_NAMES[channel_id] = f"{status_prefix} {suffix}"

    task = _CHANNEL_NAME_EDIT_TASKS.get(channel_id)
    if task is not None and not task.done():
        return task

    task = asyncio.create_task(_edit_channel_name(channel, channel_id))
    _CHANNEL_NAME_EDIT_TASKS[channel_id] = task
    _PENDING_CHANNEL_NAME_EDITS.add(task)
    task.add_done_callback(lambda finished_task: _finish_channel_name_edit(channel_id, finished_task))
    return task
