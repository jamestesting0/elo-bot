from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Concatenate, ParamSpec, TypeVar
from weakref import WeakValueDictionary

import anyio
import interactions as ipy


_LOCKS: WeakValueDictionary[str, anyio.Lock] = WeakValueDictionary()
_OWNED: ContextVar[frozenset[tuple[int, str]]] = ContextVar("match_message_locks", default=frozenset())
_P = ParamSpec("_P")
_T = TypeVar("_T")
_R = TypeVar("_R")


@asynccontextmanager
async def match_message_lock(thread: str | int) -> AsyncIterator[None]:
    """Serialize publication and recovery; nested calls in the same task may reenter."""
    key = str(thread)
    owner = (anyio.get_current_task().id, key)
    if owner in _OWNED.get():
        yield
        return
    lock = _LOCKS.setdefault(key, anyio.Lock())
    async with lock:
        token = _OWNED.set(_OWNED.get() | {owner})
        try:
            yield
        finally:
            _OWNED.reset(token)


def coordinated_match_message(
        function: Callable[Concatenate[_T, ipy.BaseContext, _P], Awaitable[_R]],
) -> Callable[Concatenate[_T, ipy.BaseContext, _P], Awaitable[_R]]:
    """Guard channel-scoped callbacks before they read or mutate the match run."""
    @wraps(function)
    async def guarded(first: _T, ctx: ipy.BaseContext, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        async with match_message_lock(ctx.channel.id):
            return await function(first, ctx, *args, **kwargs)
    return guarded
