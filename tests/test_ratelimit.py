from __future__ import annotations

import asyncio

from rift.ratelimit import RateLimiter


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


async def take(limiter: RateLimiter) -> None:
    async with limiter.slot():
        pass


def make(windows, clock: FakeClock) -> RateLimiter:
    return RateLimiter(windows, clock=clock, sleep=clock.sleep, margin=0.0)


def test_short_window_blocks_21st_request():
    clock = FakeClock()
    limiter = make([(20, 1.0), (100, 120.0)], clock)

    async def go():
        for _ in range(20):
            await take(limiter)
        assert clock.now == 0.0
        await take(limiter)

    asyncio.run(go())
    assert clock.now == 1.0


def test_long_window_enforced():
    clock = FakeClock()
    limiter = make([(20, 1.0), (100, 120.0)], clock)
    stamps = []

    async def go():
        for _ in range(101):
            await take(limiter)
            stamps.append(clock.now)

    asyncio.run(go())
    assert stamps[99] == 4.0
    assert stamps[100] == 120.0
    for i in range(len(stamps)):
        in_second = [t for t in stamps if stamps[i] <= t < stamps[i] + 1.0]
        in_window = [t for t in stamps if stamps[i] <= t < stamps[i] + 120.0]
        assert len(in_second) <= 20
        assert len(in_window) <= 100


def test_headers_update_windows_and_counts():
    clock = FakeClock()
    limiter = make([(20, 1.0), (100, 120.0)], clock)
    limiter.update_from_headers("10:1,50:60", None)
    assert limiter.windows == [(10, 1.0), (50, 60.0)]

    limiter.update_from_headers(None, "3:1,50:60")

    async def go():
        await take(limiter)

    asyncio.run(go())
    assert clock.now == 60.0


def test_block_for_retry_after():
    clock = FakeClock()
    limiter = make([(20, 1.0)], clock)
    limiter.block_for(7)

    asyncio.run(take(limiter))
    assert clock.now == 7.0


def test_concurrent_acquires_respect_limit():
    clock = FakeClock()
    limiter = make([(5, 1.0)], clock)
    stamps = []

    async def worker():
        await take(limiter)
        stamps.append(clock.now)

    async def go():
        await asyncio.gather(*(worker() for _ in range(12)))

    asyncio.run(go())
    assert sorted(stamps) == [0.0] * 5 + [1.0] * 5 + [2.0] * 2


def test_in_flight_requests_hold_their_slot_until_completion():
    clock = FakeClock()
    limiter = RateLimiter([(2, 1.0)], clock=clock, sleep=clock.sleep, margin=0.0, poll=0.25)

    async def go():
        first = await limiter.acquire()
        second = await limiter.acquire()
        clock.now = 5.0
        waiter = asyncio.create_task(limiter.acquire())
        await asyncio.sleep(0)
        assert not waiter.done()
        limiter.release(first)
        limiter.release(second)
        await waiter

    asyncio.run(go())
    assert clock.now >= 6.0
