from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from rift.archive import Archive, read_json_gz
from rift.client import RiotAuthError, RiotClient
from rift.config import (
    APEX_TIERS,
    DIAMOND_DIVISIONS,
    RANKED_SOLO_QUEUE,
    RANKED_SOLO_QUEUE_ID,
    Settings,
)
from rift.state import CrawlState, now_ms

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
CRAWLABLE_TIERS = {"DIAMOND", *APEX_TIERS}


@dataclass
class CrawlResult:
    done: int = 0
    skipped: int = 0
    failed: int = 0
    requests: int = 0
    stop_reason: str = ""


def solo_entry(entries: list[dict] | None) -> dict | None:
    for entry in entries or []:
        if entry.get("queueType") == RANKED_SOLO_QUEUE:
            return entry
    return None


class Crawler:
    def __init__(
        self, settings: Settings, client: RiotClient, state: CrawlState, archive: Archive
    ) -> None:
        self.settings = settings
        self.client = client
        self.state = state
        self.archive = archive
        self._claim_lock = asyncio.Lock()
        self._inflight: set[str] = set()
        self._claimed = 0
        self._max: int | None = None
        self._frontier_exhausted = False
        self.result = CrawlResult()

    async def seed(self, *, more_diamond: bool = True) -> int:
        """Add players from the apex ladders (refreshed daily) and the next page(s)
        of each Diamond division. Returns the number of players seen."""
        seen = 0
        stale_before = now_ms() - 24 * HOUR_MS
        for tier in APEX_TIERS:
            cursor = self.state.seed_cursor(tier)
            if cursor and (cursor["seeded_at"] or 0) >= stale_before:
                continue
            league = await self.client.league_list(tier, RANKED_SOLO_QUEUE)
            if league is None:
                continue
            self.archive.write_league_page(tier.lower(), league)
            for entry in league.get("entries", []):
                seen += self._add_seed_player(entry, tier)
            self.state.set_seed_cursor(tier, None, exhausted=False)
            log.info("seeded %s: %d entries", tier, len(league.get("entries", [])))

        if not more_diamond:
            return seen
        for division in DIAMOND_DIVISIONS:
            name = f"DIAMOND_{division}"
            cursor = self.state.seed_cursor(name)
            if cursor and cursor["exhausted"]:
                continue
            page = cursor["next_page"] if cursor else 1
            for _ in range(self.settings.diamond_pages_per_seed):
                entries = await self.client.league_entries(
                    RANKED_SOLO_QUEUE, "DIAMOND", division, page
                )
                if not entries:
                    self.state.set_seed_cursor(name, page, exhausted=True)
                    break
                self.archive.write_league_page(f"diamond_{division}_p{page}", entries)
                for entry in entries:
                    seen += self._add_seed_player(entry, "DIAMOND")
                page += 1
                self.state.set_seed_cursor(name, page, exhausted=False)
                log.info("seeded DIAMOND %s page %d: %d entries", division, page - 1, len(entries))
        return seen

    def _add_seed_player(self, entry: dict, tier: str) -> int:
        puuid = entry.get("puuid")
        if not puuid:
            return 0
        self.state.upsert_player(
            puuid, "ladder", entry.get("tier", tier), entry.get("rank"), entry.get("leaguePoints")
        )
        return 1

    async def run(self, max_matches: int | None = None) -> CrawlResult:
        self._max = max_matches
        try:
            if self.state.next_player(now_ms()) is None:
                await self.seed()
            async with asyncio.TaskGroup() as tg:
                for _ in range(max(1, self.settings.concurrency)):
                    tg.create_task(self._worker())
        except* RiotAuthError as group:
            self.result.stop_reason = f"auth error: {group.exceptions[0]}"
            raise group.exceptions[0] from None
        finally:
            self.result.requests = self.client.request_count
        if not self.result.stop_reason:
            self.result.stop_reason = (
                "max matches reached"
                if max_matches is not None and self._claimed >= max_matches
                else "frontier exhausted"
            )
        return self.result

    async def _worker(self) -> None:
        while (match_id := await self._claim()) is not None:
            try:
                status = await self.process_match(match_id)
            except (RiotAuthError, asyncio.CancelledError):
                raise
            except Exception as exc:
                log.exception("match %s failed", match_id)
                self.state.fail_match(match_id, repr(exc))
                self.result.failed += 1
            else:
                if status == "done":
                    self.result.done += 1
                else:
                    self.result.skipped += 1
            finally:
                self._inflight.discard(match_id)

    async def _claim(self) -> str | None:
        async with self._claim_lock:
            while True:
                if self._max is not None and self._claimed >= self._max:
                    return None
                rows = self.state.pending_matches(self._inflight)
                if rows:
                    match_id = rows[0]["match_id"]
                    self._inflight.add(match_id)
                    self._claimed += 1
                    return match_id
                if not await self._expand_frontier():
                    return None

    async def _expand_frontier(self) -> bool:
        if self._frontier_exhausted:
            return False
        recrawl_before = now_ms() - int(self.settings.player_recrawl_hours * HOUR_MS)
        player = self.state.next_player(recrawl_before)
        if player is None:
            await self.seed()
            player = self.state.next_player(recrawl_before)
        if player is None:
            self._frontier_exhausted = True
            return False
        start_times = [self.settings.match_start_time]
        if player["last_crawled_at"]:
            start_times.append(player["last_crawled_at"] // 1000)
        start_time = max((t for t in start_times if t is not None), default=None)
        crawled_at = now_ms()
        ids = await self.client.match_ids(
            player["puuid"],
            queue=RANKED_SOLO_QUEUE_ID,
            count=self.settings.matches_per_player,
            start_time=start_time,
        )
        added = self.state.enqueue_matches(ids, player["puuid"])
        self.state.mark_player_crawled(player["puuid"], crawled_at)
        log.info("player %s…: %d ids, %d new", player["puuid"][:8], len(ids), added)
        return True

    async def process_match(self, match_id: str) -> str:
        row = self.state.get_match(match_id)
        patch = row["patch"]
        if patch is None:
            match = await self.client.match(match_id)
            if match is None:
                self.state.finish_match(match_id, "missing")
                return "missing"
            patch = self.archive.write_match(match)
            self.state.set_match_fetched(match_id, patch)
        else:
            match = read_json_gz(self.archive.entity_path("matches", patch, match_id))

        info = match["info"]
        if info.get("queueId") != RANKED_SOLO_QUEUE_ID:
            self.state.finish_match(match_id, "skipped")
            return "skipped"

        has_timeline = bool(row["has_timeline"])
        if not has_timeline:
            timeline = await self.client.timeline(match_id)
            if timeline is not None:
                self.archive.write_timeline(patch, match_id, timeline)
                self.state.set_timeline_fetched(match_id)
                has_timeline = True

        participants = [p for p in info["participants"] if p.get("puuid")]
        lookups = await asyncio.gather(*(self._lookup_player(p["puuid"]) for p in participants))
        manifest = {
            "match_id": match_id,
            "patch": patch,
            "crawled_at": now_ms(),
            "has_timeline": has_timeline,
            "participants": [
                {
                    "puuid": p["puuid"],
                    "participant_id": p.get("participantId"),
                    "champion_id": p.get("championId"),
                    "rank_fetched_at": rank_at,
                    "mastery_fetched_at": mastery_at,
                }
                for p, (rank_at, mastery_at) in zip(participants, lookups, strict=True)
            ],
        }
        self.archive.write_manifest(patch, match_id, manifest)
        self.state.finish_match(match_id)
        log.info("match %s done (patch %s)", match_id, patch)
        return "done"

    async def _lookup_player(self, puuid: str) -> tuple[int, int]:
        rank_at, mastery_at = await asyncio.gather(self._rank(puuid), self._mastery(puuid))
        return rank_at, mastery_at

    async def _rank(self, puuid: str) -> int:
        fresh_after = now_ms() - int(self.settings.rank_ttl_hours * HOUR_MS)
        cached = self.state.cached_rank(puuid, fresh_after)
        if cached:
            return cached["fetched_at"]
        entries: list[dict[str, Any]] = await self.client.league_entries_by_puuid(puuid) or []
        fetched_at = now_ms()
        self.archive.append_snapshot(
            "ranks", {"puuid": puuid, "fetched_at": fetched_at, "entries": entries}
        )
        solo = solo_entry(entries)
        self.state.store_rank(puuid, fetched_at, solo)
        if solo and solo.get("tier") in CRAWLABLE_TIERS:
            self.state.upsert_player(
                puuid, "match", solo["tier"], solo.get("rank"), solo.get("leaguePoints")
            )
        return fetched_at

    async def _mastery(self, puuid: str) -> int:
        fresh_after = now_ms() - int(self.settings.mastery_ttl_hours * HOUR_MS)
        cached = self.state.cached_mastery(puuid, fresh_after)
        if cached:
            return cached["fetched_at"]
        masteries = await self.client.champion_masteries(puuid) or []
        fetched_at = now_ms()
        self.archive.append_snapshot(
            "masteries", {"puuid": puuid, "fetched_at": fetched_at, "masteries": masteries}
        )
        self.state.store_mastery(puuid, fetched_at)
        return fetched_at
