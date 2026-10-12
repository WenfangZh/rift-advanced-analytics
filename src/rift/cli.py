from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path

import duckdb

from rift.archive import Archive
from rift.client import RiotAuthError, RiotClient
from rift.config import Settings
from rift.crawler import Crawler
from rift.db import build_db, table_counts
from rift.state import CrawlState

EXIT_AUTH = 2


async def _crawl(settings: Settings, max_matches: int | None, seed_only: bool) -> dict:
    state = CrawlState(settings.state_path)
    archive = Archive(settings.raw_dir)
    task = asyncio.current_task()
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        async with RiotClient(settings) as client:
            crawler = Crawler(settings, client, state, archive)
            if seed_only:
                return {"players_seen": await crawler.seed(), "requests": client.request_count}
            result = await crawler.run(max_matches)
            return vars(result)
    finally:
        archive.close()
        state.close()


def cmd_crawl(settings: Settings, args: argparse.Namespace) -> int:
    max_matches = args.max_matches if args.max_matches and args.max_matches > 0 else None
    try:
        result = asyncio.run(_crawl(settings, max_matches, seed_only=False))
    except RiotAuthError as exc:
        logging.error("stopping: %s (is RIOT_API_KEY valid and unexpired?)", exc)
        return EXIT_AUTH
    except (KeyboardInterrupt, asyncio.CancelledError):
        logging.warning("interrupted; in-flight matches stay pending and resume next run")
        return 130
    print(json.dumps(result, indent=2))
    return 0


def cmd_seed(settings: Settings, args: argparse.Namespace) -> int:
    settings.diamond_pages_per_seed = args.pages
    try:
        result = asyncio.run(_crawl(settings, None, seed_only=True))
    except RiotAuthError as exc:
        logging.error("stopping: %s", exc)
        return EXIT_AUTH
    print(json.dumps(result, indent=2))
    return 0


def cmd_build_db(settings: Settings, args: argparse.Namespace) -> int:
    db_path = Path(args.db) if args.db else settings.db_path
    counts = build_db(settings.raw_dir, db_path)
    print(json.dumps({"db": str(db_path), "rows": counts}, indent=2))
    return 0


def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    out: dict = {"data_dir": str(settings.data_dir)}
    if settings.state_path.exists():
        state = CrawlState(settings.state_path)
        out["crawl"] = state.stats()
        state.close()
    raw = settings.raw_dir
    if raw.exists():
        out["raw_files"] = {
            d.name: sum(1 for p in d.rglob("*.gz")) for d in sorted(raw.iterdir()) if d.is_dir()
        }
        out["raw_bytes"] = sum(p.stat().st_size for p in raw.rglob("*.gz"))
    if settings.db_path.exists():
        con = duckdb.connect(str(settings.db_path), read_only=True)
        out["db_rows"] = table_counts(con)
        out["db_patches"] = dict(
            con.execute("SELECT patch, count(*) FROM matches GROUP BY 1 ORDER BY 1").fetchall()
        )
        con.close()
    print(json.dumps(out, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rift", description=__doc__)
    parser.add_argument("--data-dir", help="data directory (default: $RIFT_DATA_DIR or ./data)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    crawl = sub.add_parser("crawl", help="crawl matches (resumable)")
    crawl.add_argument(
        "--max-matches", type=int, default=None, help="stop after N new matches (0/omit = no limit)"
    )
    crawl.add_argument("--concurrency", type=int, help="matches processed in parallel")
    crawl.set_defaults(func=cmd_crawl)

    seed = sub.add_parser("seed", help="fetch apex ladders and more Diamond pages")
    seed.add_argument("--pages", type=int, default=1, help="Diamond pages per division")
    seed.set_defaults(func=cmd_seed)

    build = sub.add_parser("build-db", help="rebuild the DuckDB database from raw files")
    build.add_argument("--db", help="output path (default: <data-dir>/rift.duckdb)")
    build.set_defaults(func=cmd_build_db)

    stats = sub.add_parser("stats", help="show crawl and database statistics")
    stats.set_defaults(func=cmd_stats)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings.from_env()
    if args.data_dir:
        settings.data_dir = Path(args.data_dir)
    if getattr(args, "concurrency", None):
        settings.concurrency = args.concurrency
    return args.func(settings, args)


if __name__ == "__main__":
    sys.exit(main())
