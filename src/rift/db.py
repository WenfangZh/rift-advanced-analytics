"""Build the normalized DuckDB database from the raw archive.

The build is always from scratch into a temporary file that atomically replaces
the previous database, so it is idempotent and the raw archive stays the only
source of truth."""

from __future__ import annotations

import gzip
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import IO

import duckdb

from rift.archive import Archive, read_json_gz
from rift.parsers import (
    parse_mastery_snapshot,
    parse_match,
    parse_rank_snapshot,
    parse_timeline,
)
from rift.schema import TABLES, VIEWS, create_table_sql, load_columns

log = logging.getLogger(__name__)


class _Staging:
    """Typed NDJSON staging files, one per table, loaded with read_json."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._files: dict[str, IO[str]] = {}
        self.counts: dict[str, int] = dict.fromkeys(TABLES, 0)

    def path(self, table: str) -> Path:
        return self.directory / f"{table}.ndjson.gz"

    def write(self, table: str, row: dict) -> None:
        fh = self._files.get(table)
        if fh is None:
            fh = gzip.open(self.path(table), "wt", encoding="utf-8", compresslevel=1)  # noqa: SIM115
            self._files[table] = fh
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")
        self.counts[table] += 1

    def close(self) -> None:
        for fh in self._files.values():
            fh.close()


def build_db(raw_dir: Path, db_path: Path) -> dict[str, int]:
    archive = Archive(raw_dir)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=db_path.parent, prefix=".build-") as tmp:
        staging = _Staging(Path(tmp))

        manifests = {
            m["match_id"]: m for m in (read_json_gz(p) for p in archive.iter_entities("manifests"))
        }
        match_ids: set[str] = set()
        for path in archive.iter_entities("matches"):
            match = read_json_gz(path)
            match_row, participant_rows = parse_match(match, manifests.get(path.name[:-8]))
            if match_row["match_id"] in match_ids:
                continue
            match_ids.add(match_row["match_id"])
            staging.write("matches", match_row)
            for row in participant_rows:
                staging.write("participants", row)

        seen_timelines: set[str] = set()
        for path in archive.iter_entities("timelines"):
            timeline = read_json_gz(path)
            match_id = timeline["metadata"]["matchId"]
            if match_id not in match_ids or match_id in seen_timelines:
                continue
            seen_timelines.add(match_id)
            frames, events = parse_timeline(timeline)
            for row in frames:
                staging.write("timeline_frames", row)
            for row in events:
                staging.write("timeline_events", row)

        seen_ranks: set[tuple[str, int]] = set()
        for record in archive.iter_snapshots("ranks"):
            key = (record["puuid"], record["fetched_at"])
            if key not in seen_ranks:
                seen_ranks.add(key)
                staging.write("rank_snapshots", parse_rank_snapshot(record))

        seen_masteries: set[tuple[str, int]] = set()
        for record in archive.iter_snapshots("masteries"):
            key = (record["puuid"], record["fetched_at"])
            if key not in seen_masteries:
                seen_masteries.add(key)
                for row in parse_mastery_snapshot(record):
                    staging.write("mastery_snapshots", row)
        staging.close()

        tmp_db = Path(tmp) / "rift.duckdb"
        con = duckdb.connect(str(tmp_db))
        try:
            for table in TABLES:
                con.execute(create_table_sql(table))
                if staging.counts[table]:
                    columns = load_columns(table)
                    con.execute(
                        f"INSERT INTO {table} SELECT {', '.join(columns)} FROM read_json(?, "
                        "format = 'newline_delimited', columns = ?)",
                        [str(staging.path(table)), columns],
                    )
            for name, sql in VIEWS.items():
                con.execute(f"CREATE VIEW {name} AS {sql}")
            counts = table_counts(con)
        finally:
            con.close()
        os.replace(tmp_db, db_path)
    log.info("built %s: %s", db_path, counts)
    return counts


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}
