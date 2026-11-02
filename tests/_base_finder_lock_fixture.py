import asyncio

import anyio


class ContendedLock:
    def __init__(self) -> None:
        self.lock = anyio.Lock()
        self.contended = asyncio.Event()

    async def acquire(self) -> None:
        if self.lock.locked():
            self.contended.set()
        await self.lock.acquire()

    def release(self) -> None:
        self.lock.release()

    def statistics(self) -> anyio.LockStatistics:
        return self.lock.statistics()

    async def __aenter__(self) -> None:
        await self.acquire()

    async def __aexit__(self, *_exc) -> None:
        self.release()
