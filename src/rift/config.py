from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

APEX_TIERS = ("CHALLENGER", "GRANDMASTER", "MASTER")
DIAMOND_DIVISIONS = ("I", "II", "III", "IV")
TIER_ORDER = (
    "IRON",
    "BRONZE",
    "SILVER",
    "GOLD",
    "PLATINUM",
    "EMERALD",
    "DIAMOND",
    "MASTER",
    "GRANDMASTER",
    "CHALLENGER",
)
RANKED_SOLO_QUEUE = "RANKED_SOLO_5x5"
RANKED_SOLO_QUEUE_ID = 420


def parse_rate_limits(spec: str) -> list[tuple[int, float]]:
    """Parse Riot's "limit:seconds,limit:seconds" format (as used in X-App-Rate-Limit)."""
    windows = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        limit, seconds = part.split(":")
        windows.append((int(limit), float(seconds)))
    if not windows:
        raise ValueError(f"empty rate limit spec: {spec!r}")
    return windows


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass
class Settings:
    api_key: str | None = field(default=None, repr=False)
    data_dir: Path = Path("data")
    platform: str = "euw1"
    region: str = "europe"
    rate_limits: list[tuple[int, float]] = field(default_factory=lambda: [(20, 1.0), (100, 120.0)])
    max_retries: int = 5
    request_timeout: float = 30.0
    rank_ttl_hours: float = 24.0
    mastery_ttl_hours: float = 72.0
    player_recrawl_hours: float = 24.0
    matches_per_player: int = 20
    diamond_pages_per_seed: int = 1
    concurrency: int = 4
    match_start_time: int | None = None

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.sqlite"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "rift.duckdb"

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        start = os.environ.get("RIFT_MATCH_START_TIME")
        return cls(
            api_key=os.environ.get("RIOT_API_KEY", "").strip() or None,
            data_dir=Path(_env("RIFT_DATA_DIR", "data")),
            platform=_env("RIFT_PLATFORM", "euw1"),
            region=_env("RIFT_REGION", "europe"),
            rate_limits=parse_rate_limits(_env("RIFT_RATE_LIMITS", "20:1,100:120")),
            max_retries=int(_env("RIFT_MAX_RETRIES", "5")),
            rank_ttl_hours=float(_env("RIFT_RANK_TTL_HOURS", "24")),
            mastery_ttl_hours=float(_env("RIFT_MASTERY_TTL_HOURS", "72")),
            player_recrawl_hours=float(_env("RIFT_PLAYER_RECRAWL_HOURS", "24")),
            matches_per_player=int(_env("RIFT_MATCHES_PER_PLAYER", "20")),
            diamond_pages_per_seed=int(_env("RIFT_DIAMOND_PAGES_PER_SEED", "1")),
            concurrency=int(_env("RIFT_CONCURRENCY", "4")),
            match_start_time=int(start) if start else None,
        )
