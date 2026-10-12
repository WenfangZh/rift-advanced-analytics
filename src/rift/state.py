"""Crawl state persisted in SQLite: the player frontier, the match work queue and
the rank/mastery caches. Everything here is safe to re-run: inserts are
idempotent and in-flight work that is interrupted simply stays pending."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
    puuid TEXT PRIMARY KEY,
    tier TEXT,
    division TEXT,
    league_points INTEGER,
    source TEXT NOT NULL,
    discovered_at INTEGER NOT NULL,
    last_crawled_at INTEGER
);
CREATE TABLE IF NOT EXISTS match_queue (
    match_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    patch TEXT,
    has_timeline INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    discovered_from TEXT,
    added_at INTEGER NOT NULL,
    done_at INTEGER
);
CREATE INDEX IF NOT EXISTS match_queue_status ON match_queue(status, added_at);
CREATE TABLE IF NOT EXISTS rank_cache (
    puuid TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL,
    tier TEXT,
    division TEXT,
    league_points INTEGER
);
CREATE TABLE IF NOT EXISTS mastery_cache (
    puuid TEXT PRIMARY KEY,
    fetched_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seed_cursor (
    name TEXT PRIMARY KEY,
    next_page INTEGER,
    exhausted INTEGER NOT NULL DEFAULT 0,
    seeded_at INTEGER
);
"""

MAX_ATTEMPTS = 3


def now_ms() -> int:
    return int(time.time() * 1000)


class CrawlState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def upsert_player(
        self,
        puuid: str,
        source: str,
        tier: str | None = None,
        division: str | None = None,
        league_points: int | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO players (puuid, tier, division, league_points, source, discovered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(puuid) DO UPDATE SET
                tier = coalesce(excluded.tier, tier),
                division = coalesce(excluded.division, division),
                league_points = coalesce(excluded.league_points, league_points)
            """,
            (puuid, tier, division, league_points, source, now_ms()),
        )

    def next_player(self, recrawl_before: int) -> sqlite3.Row | None:
        return self.conn.execute(
            """
            SELECT * FROM players
            WHERE last_crawled_at IS NULL OR last_crawled_at < ?
            ORDER BY last_crawled_at IS NOT NULL, last_crawled_at, random()
            LIMIT 1
            """,
            (recrawl_before,),
        ).fetchone()

    def mark_player_crawled(self, puuid: str, at: int) -> None:
        self.conn.execute("UPDATE players SET last_crawled_at = ? WHERE puuid = ?", (at, puuid))

    def enqueue_matches(self, match_ids: list[str], discovered_from: str) -> int:
        before = self.conn.total_changes
        self.conn.executemany(
            "INSERT OR IGNORE INTO match_queue (match_id, discovered_from, added_at) VALUES (?, ?, ?)",
            [(m, discovered_from, now_ms()) for m in match_ids],
        )
        return self.conn.total_changes - before

    def pending_matches(self, exclude: set[str], limit: int = 1) -> list[sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM match_queue WHERE status = 'pending' ORDER BY added_at, match_id LIMIT ?",
            (limit + len(exclude),),
        ).fetchall()
        return [r for r in rows if r["match_id"] not in exclude][:limit]

    def get_match(self, match_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM match_queue WHERE match_id = ?", (match_id,)
        ).fetchone()

    def set_match_fetched(self, match_id: str, patch: str) -> None:
        self.conn.execute("UPDATE match_queue SET patch = ? WHERE match_id = ?", (patch, match_id))

    def set_timeline_fetched(self, match_id: str) -> None:
        self.conn.execute("UPDATE match_queue SET has_timeline = 1 WHERE match_id = ?", (match_id,))

    def finish_match(self, match_id: str, status: str = "done") -> None:
        self.conn.execute(
            "UPDATE match_queue SET status = ?, done_at = ?, last_error = NULL WHERE match_id = ?",
            (status, now_ms(), match_id),
        )

    def fail_match(self, match_id: str, error: str) -> None:
        self.conn.execute(
            """
            UPDATE match_queue SET attempts = attempts + 1, last_error = ?,
                status = CASE WHEN attempts + 1 >= ? THEN 'failed' ELSE 'pending' END
            WHERE match_id = ?
            """,
            (error[:500], MAX_ATTEMPTS, match_id),
        )

    def cached_rank(self, puuid: str, fresh_after: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM rank_cache WHERE puuid = ? AND fetched_at >= ?", (puuid, fresh_after)
        ).fetchone()

    def store_rank(self, puuid: str, fetched_at: int, entry: dict[str, Any] | None) -> None:
        entry = entry or {}
        self.conn.execute(
            "INSERT OR REPLACE INTO rank_cache VALUES (?, ?, ?, ?, ?)",
            (puuid, fetched_at, entry.get("tier"), entry.get("rank"), entry.get("leaguePoints")),
        )

    def cached_mastery(self, puuid: str, fresh_after: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM mastery_cache WHERE puuid = ? AND fetched_at >= ?", (puuid, fresh_after)
        ).fetchone()

    def store_mastery(self, puuid: str, fetched_at: int) -> None:
        self.conn.execute("INSERT OR REPLACE INTO mastery_cache VALUES (?, ?)", (puuid, fetched_at))

    def seed_cursor(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM seed_cursor WHERE name = ?", (name,)).fetchone()

    def set_seed_cursor(self, name: str, next_page: int | None, exhausted: bool) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO seed_cursor VALUES (?, ?, ?, ?)",
            (name, next_page, int(exhausted), now_ms()),
        )

    def stats(self) -> dict[str, Any]:
        q = self.conn.execute
        return {
            "players": q("SELECT count(*) FROM players").fetchone()[0],
            "players_by_tier": dict(
                q(
                    "SELECT coalesce(tier, '?'), count(*) FROM players GROUP BY 1 ORDER BY 2 DESC"
                ).fetchall()
            ),
            "players_crawled": q(
                "SELECT count(*) FROM players WHERE last_crawled_at IS NOT NULL"
            ).fetchone()[0],
            "match_queue": dict(
                q("SELECT status, count(*) FROM match_queue GROUP BY 1").fetchall()
            ),
            "rank_cache": q("SELECT count(*) FROM rank_cache").fetchone()[0],
            "mastery_cache": q("SELECT count(*) FROM mastery_cache").fetchone()[0],
            "seed_cursors": {
                r["name"]: {"next_page": r["next_page"], "exhausted": bool(r["exhausted"])}
                for r in q("SELECT * FROM seed_cursor ORDER BY name")
            },
        }
