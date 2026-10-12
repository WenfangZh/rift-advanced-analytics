from __future__ import annotations

import asyncio

import httpx
import pytest

from rift.archive import Archive
from rift.client import RiotAuthError, RiotClient
from rift.crawler import Crawler
from rift.db import build_db
from rift.state import CrawlState
from tests.conftest import no_sleep


def crawl(settings, fake_riot, max_matches):
    async def go():
        state = CrawlState(settings.state_path)
        archive = Archive(settings.raw_dir)
        try:
            async with RiotClient(
                settings, transport=httpx.MockTransport(fake_riot.handler), sleep=no_sleep
            ) as client:
                return await Crawler(settings, client, state, archive).run(max_matches)
        finally:
            archive.close()
            state.close()

    return asyncio.run(go())


def test_crawl_fetches_matches_timelines_and_context(settings, fake_riot):
    result = crawl(settings, fake_riot, 3)
    assert result.done == 3
    assert result.stop_reason == "max matches reached"
    assert fake_riot.calls["/lol/match/v5/matches/{}"] == 3
    assert fake_riot.calls["/lol/match/v5/matches/{}/timeline"] == 3
    assert fake_riot.calls["/lol/league/v4/challengerleagues/by-queue/RANKED_SOLO_5x5"] == 1

    counts = build_db(settings.raw_dir, settings.db_path)
    assert counts["matches"] == 3
    assert counts["participants"] == 30
    assert counts["timeline_frames"] == 90


def test_crawl_is_resumable_and_idempotent(settings, fake_riot):
    crawl(settings, fake_riot, 2)
    rank_calls = fake_riot.calls["/lol/league/v4/entries/by-puuid/{}"]
    result = crawl(settings, fake_riot, 100)
    assert result.done == 4
    assert result.stop_reason == "frontier exhausted"
    assert fake_riot.calls["/lol/match/v5/matches/{}"] == 6
    assert fake_riot.calls["/lol/match/v5/matches/{}/timeline"] == 6
    distinct_players = len(
        {p for m in fake_riot.matches.values() for p in m["metadata"]["participants"]}
    )
    assert fake_riot.calls["/lol/league/v4/entries/by-puuid/{}"] == distinct_players
    assert (
        fake_riot.calls["/lol/champion-mastery/v4/champion-masteries/by-puuid/{}"]
        == distinct_players
    )
    assert rank_calls < distinct_players

    again = crawl(settings, fake_riot, 100)
    assert again.done == 0
    assert fake_riot.calls["/lol/match/v5/matches/{}"] == 6

    counts = build_db(settings.raw_dir, settings.db_path)
    assert counts["matches"] == 6
    assert counts["rank_snapshots"] == distinct_players


def test_crawl_snowballs_diamond_plus_players(settings, fake_riot):
    crawl(settings, fake_riot, 100)
    state = CrawlState(settings.state_path)
    tiers = {r["tier"] for r in state.conn.execute("SELECT tier FROM players")}
    sources = {r["source"] for r in state.conn.execute("SELECT source FROM players")}
    state.close()
    assert tiers <= {"DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"}
    assert sources == {"ladder", "match"}


def test_interrupted_match_resumes_without_refetching(settings, fake_riot):
    fake_riot.status_override["/timeline"] = [500] * 6
    settings.max_retries = 0
    settings.concurrency = 1
    result = crawl(settings, fake_riot, 1)
    assert result.failed == 1
    assert fake_riot.calls["/lol/match/v5/matches/{}"] == 1

    fake_riot.status_override.clear()
    result = crawl(settings, fake_riot, 1)
    assert result.done == 1
    assert fake_riot.calls["/lol/match/v5/matches/{}"] == 1
    assert fake_riot.calls["/lol/match/v5/matches/{}/timeline"] == 2


def test_auth_error_stops_crawl(settings, fake_riot):
    fake_riot.status_override["/lol/league"] = [401] * 10
    with pytest.raises(RiotAuthError):
        crawl(settings, fake_riot, 5)
