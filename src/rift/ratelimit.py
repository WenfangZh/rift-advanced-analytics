from __future__ import annotations

import asyncio
import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from rift.config import parse_rate_limits

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]
Slot = list[float]

IN_FLIGHT = math.inf


class RateLimiter:
    """Sliding-log limiter enforcing several (limit, period) windows at once.

    A slot is held from the moment a request is sent until its response arrives,
    and it is timestamped with the completion time. The server receives the
    request somewhere in between, so counting from completion guarantees that no
    `period`-long interval of server receipts holds more than `limit` requests,
    whatever connection setup or latency jitter does to send order.
    """

    def __init__(
        self,
        windows: list[tuple[int, float]],
        *,
        clock: Clock = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        margin: float = 0.05,
        poll: float = 0.05,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._margin = margin
        self._poll = poll
        self._lock = asyncio.Lock()
        self._blocked_until = 0.0
        self._windows: dict[float, tuple[int, list[Slot]]] = {}
        self.set_windows(windows)

    @property
    def windows(self) -> list[tuple[int, float]]:
        return sorted((limit, period) for period, (limit, _) in self._windows.items())

    def set_windows(self, windows: list[tuple[int, float]]) -> None:
        old = self._windows
        history = max((log for _, log in old.values()), key=len, default=[])
        self._windows = {
            period: (limit, old[period][1] if period in old else list(history))
            for limit, period in windows
        }

    def update_from_headers(self, limit_header: str | None, count_header: str | None) -> None:
        """Adopt the limits the server advertises and catch up with its counters.

        The count header reveals requests made with the same key by another
        process (or a previous run) that our local log does not know about.
        """
        if limit_header:
            windows = parse_rate_limits(limit_header)
            if sorted(windows) != self.windows:
                self.set_windows(windows)
        if count_header:
            now = self._clock()
            for count, period in parse_rate_limits(count_header):
                if period not in self._windows:
                    continue
                _, log = self._windows[period]
                self._prune(log, period, now)
                log.extend([now] for _ in range(count - len(log)))

    def block_for(self, seconds: float) -> None:
        self._blocked_until = max(self._blocked_until, self._clock() + seconds)

    @staticmethod
    def _prune(log: list[Slot], period: float, now: float) -> None:
        log[:] = [slot for slot in log if slot[0] > now - period]

    def _wait_time(self, now: float) -> float:
        wait = self._blocked_until - now
        for period, (limit, log) in self._windows.items():
            self._prune(log, period, now)
            if len(log) >= limit:
                frees_at = sorted(slot[0] for slot in log)[len(log) - limit]
                wait = max(
                    wait,
                    self._poll if frees_at == IN_FLIGHT else frees_at + period - now + self._margin,
                )
        return wait

    async def acquire(self) -> Slot:
        async with self._lock:
            while True:
                wait = self._wait_time(self._clock())
                if wait <= 0:
                    slot = [IN_FLIGHT]
                    for _, log in self._windows.values():
                        log.append(slot)
                    return slot
                await self._sleep(wait)

    def release(self, slot: Slot) -> None:
        slot[0] = self._clock()

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        held = await self.acquire()
        try:
            yield
        finally:
            self.release(held)
