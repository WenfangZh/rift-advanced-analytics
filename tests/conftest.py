from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import httpx
import pytest

from rift.archive import Archive
from rift.config import Settings
from tests import factory

POOL = [f"puuid-{i:02d}" for i in range(20)]
TIERS = ["DIAMOND", "MASTER", "DIAMOND", "GRANDMASTER", "EMERALD"]


class FakeRiot:
    """In-memory stand-in for the Riot API, served through httpx.MockTransport."""

    def __init__(self, n_matches: int = 6) -> None:
        self.matches = {}
        for m in range(n_matches):
            match_id = f"EUW1_{7000 + m}"
            players = [POOL[(m * 3 + k) % len(POOL)] for k in range(10)]
            self.matches[match_id] = factory.make_match(match_id, players, blue_wins=m % 2 == 0)
        self.calls: Counter[str] = Counter()
        self.status_override: dict[str, list[int]] = {}

    def tier_of(self, puuid: str) -> str | None:
        idx = POOL.index(puuid)
        return None if idx % 7 == 6 else TIERS[idx % len(TIERS)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        host = request.url.host
        kind = re.sub(r"puuid-\d+|EUW1_\d+", "{}", path)
        self.calls[kind] += 1
        for pattern, statuses in self.status_override.items():
            if pattern in path and statuses:
                return httpx.Response(statuses.pop(0), headers={"Retry-After": "1"})
        headers = {"X-App-Rate-Limit": "1000:1,100000:120", "X-App-Rate-Limit-Count": "1:1,1:120"}
        body = self.route(host, path, request.url.params)
        if body is None:
            return httpx.Response(404, json={"status": {"status_code": 404}}, headers=headers)
        return httpx.Response(200, content=json.dumps(body), headers=headers)

    def route(self, host: str, path: str, params) -> object | None:
        if host == "euw1.api.riotgames.com":
            if m := re.fullmatch(r"/lol/league/v4/(\w+)leagues/by-queue/RANKED_SOLO_5x5", path):
                tier = m.group(1).upper()
                members = {"CHALLENGER": POOL[0:1], "GRANDMASTER": POOL[3:4], "MASTER": POOL[1:2]}
                return {
                    "tier": tier,
                    "entries": [
                        {"puuid": p, "rank": "I", "leaguePoints": 500} for p in members[tier]
                    ],
                }
            if m := re.fullmatch(r"/lol/league/v4/entries/RANKED_SOLO_5x5/DIAMOND/(\w+)", path):
                if m.group(1) == "I" and params.get("page") == "1":
                    return [{"puuid": POOL[2], "tier": "DIAMOND", "rank": "I", "leaguePoints": 10}]
                return []
            if m := re.fullmatch(r"/lol/league/v4/entries/by-puuid/(.+)", path):
                tier = self.tier_of(m.group(1))
                return [factory.solo_entry(tier, "II")] if tier else []
            if m := re.fullmatch(
                r"/lol/champion-mastery/v4/champion-masteries/by-puuid/(.+)", path
            ):
                return factory.masteries(
                    {c: 10_000 * (i + 1) for i, (c, _) in enumerate(factory.CHAMPIONS)}
                )
        if host == "europe.api.riotgames.com":
            if m := re.fullmatch(r"/lol/match/v5/matches/by-puuid/(.+)/ids", path):
                ids = [
                    mid
                    for mid, mt in self.matches.items()
                    if m.group(1) in mt["metadata"]["participants"]
                ]
                return ids[: int(params.get("count", 20))]
            if m := re.fullmatch(r"/lol/match/v5/matches/(EUW1_\d+)/timeline", path):
                return factory.make_timeline(m.group(1)) if m.group(1) in self.matches else None
            if m := re.fullmatch(r"/lol/match/v5/matches/(EUW1_\d+)", path):
                return self.matches.get(m.group(1))
        return None


async def no_sleep(_: float) -> None:
    return None


@pytest.fixture
def fake_riot() -> FakeRiot:
    return FakeRiot()


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        api_key="RGAPI-test-key",
        data_dir=tmp_path / "data",
        rate_limits=[(1000, 1.0)],
        concurrency=2,
    )


@pytest.fixture
def populated_raw(tmp_path: Path) -> Path:
    """A raw archive with two matches, timelines, manifests and snapshots."""
    raw = tmp_path / "raw"
    archive = Archive(raw)
    for n, match_id in enumerate(["EUW1_1", "EUW1_2"]):
        match = factory.make_match(match_id, POOL[:10], blue_wins=n == 0)
        patch = archive.write_match(match)
        archive.write_timeline(patch, match_id, factory.make_timeline(match_id))
        archive.write_manifest(
            patch,
            match_id,
            {
                "match_id": match_id,
                "patch": patch,
                "crawled_at": factory.GAME_START_MS + 3_600_000,
                "has_timeline": True,
                "participants": [
                    {
                        "puuid": p,
                        "participant_id": i + 1,
                        "champion_id": factory.CHAMPIONS[i][0],
                        "rank_fetched_at": 1_000 + i,
                        "mastery_fetched_at": 2_000 + i,
                    }
                    for i, p in enumerate(POOL[:10])
                ],
            },
        )
    tiers = [
        "DIAMOND",
        "DIAMOND",
        "MASTER",
        "DIAMOND",
        None,
        "DIAMOND",
        "MASTER",
        "MASTER",
        "DIAMOND",
        "EMERALD",
    ]
    for i, p in enumerate(POOL[:10]):
        entries = [factory.solo_entry(tiers[i], "II" if i % 2 else "I")] if tiers[i] else []
        archive.append_snapshot("ranks", {"puuid": p, "fetched_at": 1_000 + i, "entries": entries})
        archive.append_snapshot("ranks", {"puuid": p, "fetched_at": 1_000 + i, "entries": entries})
        played = factory.CHAMPIONS[i][0]
        champs = {played: 100_000 * (i + 1), 1: 50_000} if i != 4 else {1: 50_000}
        archive.append_snapshot(
            "masteries",
            {"puuid": p, "fetched_at": 2_000 + i, "masteries": factory.masteries(champs)},
        )
    archive.close()
    return raw
