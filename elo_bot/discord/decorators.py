import asyncio
import traceback
from collections import OrderedDict
from datetime import datetime
from functools import wraps

import interactions


def lock_files(*file_paths):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # acquire each lock asynchronously
            if isinstance(self, interactions.Client):
                file_locks = self.file_locks
            else:
                file_locks = self.bot.file_locks
            locks = [file_locks[file_path] for file_path in sorted(file_paths)]
            for lock in locks:
                await lock.acquire()

            try:
                return await func(self, *args, **kwargs)  # call the original function
            finally:
                for lock in locks:
                    lock.release()

        return wrapper

    return decorator


def create_async_task(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.create_task(func(*args, **kwargs))

    return wrapper


def reset_cooldown_on_error(command_func):
    @wraps(command_func)
    async def wrapper(self, ctx: interactions.SlashContext, *args, **kwargs):
        try:
            return await command_func(self, ctx, *args, **kwargs)
        except Exception:
            try:
                await command_func.cooldown.reset(ctx)
            except Exception:
                pass  # ignore if reset fails

            traceback.print_exc()
            raise
    return wrapper


def acknowledge_privately(callback):
    """Defer privately before a component or modal callback waits on locks or the channel edit queue.

    Discord fails an interaction that is not answered within 3 seconds; every reply that follows is private.
    """
    @wraps(callback)
    async def acknowledged(owner, ctx: interactions.InteractionContext, *args, **kwargs):
        await ctx.defer(ephemeral=True)
        return await callback(owner, ctx, *args, **kwargs)
    return acknowledged


_CLICKS_IN_FLIGHT: set[tuple[int, int]] = set()
# Per message and user, the newest message version a click already replaced. Bounded so it cannot grow forever.
_REPLACED_VERSIONS: OrderedDict[tuple[int, int], datetime] = OrderedDict()
_REPLACED_VERSIONS_LIMIT = 4096


def _version(message: interactions.Message | None) -> datetime | None:
    return None if message is None else (message.edited_timestamp or message.timestamp)


def ignore_repeated_clicks(callback):
    """Acknowledge a double click without acting on it.

    A second click on a message whose first click is still running, or a click on a version of the message that
    an earlier click of the same user already replaced, would only find its view outdated.
    """
    @wraps(callback)
    async def guarded(owner, ctx: interactions.InteractionContext, *args, **kwargs):
        if not isinstance(ctx, (interactions.ComponentContext, interactions.ModalContext)):
            return await callback(owner, ctx, *args, **kwargs)
        clicked = _version(ctx.message)
        if clicked is None:
            return await callback(owner, ctx, *args, **kwargs)
        key = (int(ctx.message.id), int(ctx.author.id))
        replaced = _REPLACED_VERSIONS.get(key)
        if key in _CLICKS_IN_FLIGHT or replaced is not None and clicked <= replaced:
            await ctx.defer(edit_origin=True)
            return None
        _CLICKS_IN_FLIGHT.add(key)
        try:
            return await callback(owner, ctx, *args, **kwargs)
        finally:
            _CLICKS_IN_FLIGHT.discard(key)
            current = _version(ctx.message)
            # Only a reply that edited this message makes its old buttons stale; a modal or a new message does not.
            if current is not None and current != clicked:
                _REPLACED_VERSIONS[key] = clicked
                _REPLACED_VERSIONS.move_to_end(key)
                while len(_REPLACED_VERSIONS) > _REPLACED_VERSIONS_LIMIT:
                    _REPLACED_VERSIONS.popitem(last=False)
    return guarded
