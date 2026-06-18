"""Port core/util/semaphore.ts. Semafor FIFO; acquire() zwraca funkcje release
(slot przekazywany bezposrednio nastepnemu czekajacemu, jak w oryginale).

UWAGA: semafor jest tworzony PER MODUL orchestratora (4 niezalezne instancje
po CLAUDE_MAX_CONCURRENT), nie globalnie - tak dziala oryginal.
"""

import asyncio
from collections import deque
from collections.abc import Callable


class Semaphore:
    def __init__(self, capacity: int) -> None:
        self._available = capacity
        self._waiters: deque[asyncio.Future[None]] = deque()

    async def acquire(self) -> Callable[[], None]:
        if self._available > 0:
            self._available -= 1
            return self._release
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        await fut
        return self._release

    def _release(self) -> None:
        while self._waiters:
            fut = self._waiters.popleft()
            if not fut.done():
                fut.set_result(None)
                return
        self._available += 1

    @property
    def queued(self) -> int:
        return len(self._waiters)
