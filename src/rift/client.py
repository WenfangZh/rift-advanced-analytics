from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from rift.config import Settings
from rift.ratelimit import RateLimiter

log = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]


class RiotAPIError(Exception):
    def __init__(self, status: int, path: str, body: str = "") -> None:
        super().__init__(f"Riot API {status} for {path}: {body[:200]}")
        self.status = status
        self.path = path


class RiotAuthError(RiotAPIError):
    """401/403: the key is missing, invalid, or expired (dev keys expire after 24h)."""


class RiotClient:
    """Async Riot API client with per-routing-value rate limiting and retries.

    Riot enforces application rate limits separately for each routing value, so
    the platform host (euw1) and the regional host (europe) get their own limiter.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        sleep: Sleep = asyncio.sleep,
        limiter_factory: Callable[[], RateLimiter] | None = None,
    ) -> None:
        if not settings.api_key:
            raise RiotAuthError(0, "", "RIOT_API_KEY is not set")
        self.settings = settings
        self._sleep = sleep
        self._limiter_factory = limiter_factory or (
            lambda: RateLimiter(settings.rate_limits, sleep=sleep)
        )
        self._limiters: dict[str, RateLimiter] = {}
        self._http = httpx.AsyncClient(
            headers={"X-Riot-Token": settings.api_key, "Accept": "application/json"},
            timeout=settings.request_timeout,
            transport=transport,
        )
        self.request_count = 0

    async def __aenter__(self) -> RiotClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    def limiter(self, routing: str) -> RateLimiter:
        if routing not in self._limiters:
            self._limiters[routing] = self._limiter_factory()
        return self._limiters[routing]

    async def get(self, routing: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """GET a JSON resource. Returns None on 404."""
        url = f"https://{routing}.api.riotgames.com{path}"
        limiter = self.limiter(routing)
        backoff = 1.0
        for attempt in range(self.settings.max_retries + 1):
            self.request_count += 1
            try:
                async with limiter.slot():
                    response = await self._http.get(url, params=params)
                    limiter.update_from_headers(
                        response.headers.get("X-App-Rate-Limit"),
                        response.headers.get("X-App-Rate-Limit-Count"),
                    )
            except httpx.TransportError as exc:
                if attempt == self.settings.max_retries:
                    raise
                log.warning("transport error on %s (%s), retrying in %.1fs", path, exc, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue

            status = response.status_code
            if status == 200:
                return response.json()
            if status == 404:
                return None
            if status in (401, 403):
                raise RiotAuthError(status, path, response.text)
            if status == 429:
                retry_after = float(response.headers.get("Retry-After", backoff))
                limiter.block_for(retry_after)
                log.warning(
                    "429 on %s (%s limit), backing off %.1fs",
                    path,
                    response.headers.get("X-Rate-Limit-Type", "service"),
                    retry_after,
                )
                backoff = min(backoff * 2, 60)
                continue
            if status >= 500 and attempt < self.settings.max_retries:
                log.warning("%s on %s, retrying in %.1fs", status, path, backoff)
                await self._sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            raise RiotAPIError(status, path, response.text)
        raise RiotAPIError(429, path, "retries exhausted")

    async def league_list(self, tier: str, queue: str) -> Any:
        endpoint = {
            "CHALLENGER": "challengerleagues",
            "GRANDMASTER": "grandmasterleagues",
            "MASTER": "masterleagues",
        }[tier]
        return await self.get(self.settings.platform, f"/lol/league/v4/{endpoint}/by-queue/{queue}")

    async def league_entries(self, queue: str, tier: str, division: str, page: int) -> Any:
        return await self.get(
            self.settings.platform,
            f"/lol/league/v4/entries/{queue}/{tier}/{division}",
            {"page": page},
        )

    async def league_entries_by_puuid(self, puuid: str) -> Any:
        return await self.get(self.settings.platform, f"/lol/league/v4/entries/by-puuid/{puuid}")

    async def champion_masteries(self, puuid: str) -> Any:
        return await self.get(
            self.settings.platform, f"/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}"
        )

    async def match_ids(
        self, puuid: str, *, queue: int, count: int, start_time: int | None = None
    ) -> list[str]:
        params: dict[str, Any] = {"queue": queue, "type": "ranked", "start": 0, "count": count}
        if start_time is not None:
            params["startTime"] = start_time
        result = await self.get(
            self.settings.region, f"/lol/match/v5/matches/by-puuid/{puuid}/ids", params
        )
        return result or []

    async def match(self, match_id: str) -> Any:
        return await self.get(self.settings.region, f"/lol/match/v5/matches/{match_id}")

    async def timeline(self, match_id: str) -> Any:
        return await self.get(self.settings.region, f"/lol/match/v5/matches/{match_id}/timeline")
