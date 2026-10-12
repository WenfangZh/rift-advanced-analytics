"""Permanent raw JSON archive.

Layout under data/raw/:
  matches/patch=15.20/EUW1_123.json.gz      match-v5 payload
  timelines/patch=15.20/EUW1_123.json.gz    match-v5 timeline payload
  manifests/patch=15.20/EUW1_123.json.gz    which rank/mastery snapshot each participant got
  ranks/date=2026-10-12/<run>.jsonl.gz      league-v4 entries-by-puuid snapshots
  masteries/date=2026-10-12/<run>.jsonl.gz  champion-mastery-v4 snapshots
  leagues/date=2026-10-12/<name>.json.gz    seed league lists / entry pages

Per-entity files are written atomically (tmp + rename). Snapshot streams are
append-only JSONL, flushed after each record so a killed process loses at most
the record being written; readers tolerate a truncated tail.
"""

from __future__ import annotations

import gzip
import json
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any


def patch_from_version(game_version: str | None) -> str:
    if not game_version:
        return "unknown"
    parts = game_version.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else game_version


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def write_json_gz(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    os.replace(tmp, path)


def read_json_gz(path: Path) -> Any:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def iter_jsonl_gz(path: Path) -> Iterator[dict]:
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    return
    except EOFError:
        return


class Archive:
    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self._run_id = f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        self._streams: dict[tuple[str, str], IO[str]] = {}

    def entity_path(self, kind: str, patch: str, match_id: str) -> Path:
        return self.raw_dir / kind / f"patch={patch}" / f"{match_id}.json.gz"

    def write_match(self, match: dict) -> str:
        patch = patch_from_version(match["info"].get("gameVersion"))
        write_json_gz(self.entity_path("matches", patch, match["metadata"]["matchId"]), match)
        return patch

    def write_timeline(self, patch: str, match_id: str, timeline: dict) -> None:
        write_json_gz(self.entity_path("timelines", patch, match_id), timeline)

    def write_manifest(self, patch: str, match_id: str, manifest: dict) -> None:
        write_json_gz(self.entity_path("manifests", patch, match_id), manifest)

    def write_league_page(self, name: str, payload: Any) -> None:
        stamp = datetime.now(UTC).strftime("%H%M%S%f")
        write_json_gz(
            self.raw_dir / "leagues" / f"date={_today()}" / f"{name}-{stamp}.json.gz", payload
        )

    def append_snapshot(self, kind: str, record: dict) -> None:
        day = _today()
        key = (kind, day)
        stream = self._streams.get(key)
        if stream is None:
            path = self.raw_dir / kind / f"date={day}" / f"{self._run_id}.jsonl.gz"
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = gzip.open(path, "at", encoding="utf-8")  # noqa: SIM115
            self._streams[key] = stream
        stream.write(json.dumps(record, separators=(",", ":")) + "\n")
        stream.flush()

    def close(self) -> None:
        for stream in self._streams.values():
            stream.close()
        self._streams.clear()

    def iter_entities(self, kind: str) -> Iterator[Path]:
        yield from sorted((self.raw_dir / kind).glob("patch=*/*.json.gz"))

    def iter_snapshots(self, kind: str) -> Iterator[dict]:
        for path in sorted((self.raw_dir / kind).glob("date=*/*.jsonl.gz")):
            yield from iter_jsonl_gz(path)
