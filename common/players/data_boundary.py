from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio


class PlayerDataReset(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "Player data was reset or is being reset. Check the current match before trying again."
        )


class PlayerDataBoundary:
    def __init__(self) -> None:
        self._condition = anyio.Condition()
        self._generation = 0
        self._active_writes = 0
        self._resetting = False

    @property
    def generation(self) -> int:
        return self._generation

    @asynccontextmanager
    async def write(self, expected_generation: int) -> AsyncIterator[None]:
        admitted = False
        try:
            async with self._condition:
                if self._resetting or expected_generation != self._generation:
                    raise PlayerDataReset()
                self._active_writes += 1
                admitted = True
            yield
        finally:
            if admitted:
                with anyio.CancelScope(shield=True):
                    async with self._condition:
                        self._active_writes -= 1
                        self._condition.notify_all()

    @asynccontextmanager
    async def reset(self) -> AsyncIterator[None]:
        claimed = False
        try:
            async with self._condition:
                if self._resetting:
                    raise PlayerDataReset()
                self._resetting = True
                self._generation += 1
                claimed = True
                while self._active_writes:
                    await self._condition.wait()
            yield
        finally:
            if claimed:
                with anyio.CancelScope(shield=True):
                    async with self._condition:
                        self._resetting = False
                        self._condition.notify_all()


PLAYER_DATA_BOUNDARY = PlayerDataBoundary()
