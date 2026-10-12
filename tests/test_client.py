from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from rift.client import RiotAPIError, RiotAuthError, RiotClient
from rift.config import Settings
from rift.ratelimit import RateLimiter


def run_client(settings: Settings, responses: list[httpx.Response], call):
    sleeps: list[float] = []
    seen: list[httpx.Request] = []
    clock = [0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responses.pop(0)

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def limiter() -> RateLimiter:
        return RateLimiter(settings.rate_limits, clock=lambda: clock[0], sleep=fake_sleep)

    async def go():
        async with RiotClient(
            settings,
            transport=httpx.MockTransport(handler),
            sleep=fake_sleep,
            limiter_factory=limiter,
        ) as client:
            return await call(client), client

    result, client = asyncio.run(go())
    return result, client, sleeps, seen


def test_sends_key_and_routes_hosts(settings):
    ok = [httpx.Response(200, json={"ok": 1}), httpx.Response(200, json=["EUW1_1"])]

    async def call(client):
        a = await client.league_entries_by_puuid("abc")
        b = await client.match_ids("abc", queue=420, count=5)
        return a, b

    result, _, _, seen = run_client(settings, ok, call)
    assert result == ({"ok": 1}, ["EUW1_1"])
    assert seen[0].headers["X-Riot-Token"] == "RGAPI-test-key"
    assert seen[0].url.host == "euw1.api.riotgames.com"
    assert seen[1].url.host == "europe.api.riotgames.com"
    assert seen[1].url.params["queue"] == "420"


def test_429_honours_retry_after(settings):
    responses = [
        httpx.Response(429, headers={"Retry-After": "3", "X-Rate-Limit-Type": "application"}),
        httpx.Response(200, json={"ok": True}),
    ]
    result, client, sleeps, _ = run_client(settings, responses, lambda c: c.match("EUW1_1"))
    assert result == {"ok": True}
    assert sum(sleeps) >= 3
    assert client.request_count == 2


def test_5xx_retried_then_succeeds(settings):
    responses = [httpx.Response(503), httpx.Response(500), httpx.Response(200, json=[1])]
    result, _, sleeps, _ = run_client(settings, responses, lambda c: c.match("EUW1_1"))
    assert result == [1]
    assert sleeps == [1.0, 2.0]


def test_5xx_gives_up(settings):
    settings.max_retries = 1
    responses = [httpx.Response(500), httpx.Response(500)]
    with pytest.raises(RiotAPIError):
        run_client(settings, responses, lambda c: c.match("EUW1_1"))


def test_404_returns_none(settings):
    result, *_ = run_client(settings, [httpx.Response(404)], lambda c: c.match("EUW1_1"))
    assert result is None


def test_auth_error_not_retried(settings):
    with pytest.raises(RiotAuthError):
        run_client(settings, [httpx.Response(403)], lambda c: c.match("EUW1_1"))


def test_app_rate_limit_header_adopted(settings):
    responses = [httpx.Response(200, json={}, headers={"X-App-Rate-Limit": "500:10,30000:600"})]
    _, client, _, _ = run_client(settings, responses, lambda c: c.match("EUW1_1"))
    assert client.limiter("europe").windows == [(500, 10.0), (30000, 600.0)]


def test_missing_key_rejected():
    with pytest.raises(RiotAuthError):
        RiotClient(Settings(api_key=None))


def test_key_not_logged(settings, caplog):
    caplog.set_level(logging.DEBUG)
    responses = [httpx.Response(500), httpx.Response(200, json={})]
    run_client(settings, responses, lambda c: c.match("EUW1_1"))
    assert "RGAPI-test-key" not in caplog.text
    assert "RGAPI-test-key" not in repr(settings)
