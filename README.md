# rift-advanced-analytics

A historical dataset of **EUW Diamond+ ranked solo/duo** games, built to compute better
measures than the usual stats sites:

- lane matchup win rates restricted to games where both laners are in the same tier,
- win rates adjusted for champion mastery and one-trick-ness,
- later, win probability added (WPA) from a win-probability model trained on timeline frames.

Phase 1 (this repo today) is the **crawler** and the **database**. There is no website yet.

## Setup

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                      # creates .venv with runtime + dev dependencies
cp .env.example .env         # then paste your key into .env
uv run rift --help
```

The key is read from the `RIOT_API_KEY` environment variable (or `.env`, which is
git-ignored). It is sent only in the `X-Riot-Token` header and never logged.

### Getting a Riot API key

1. Sign in at <https://developer.riotgames.com> with your Riot account.
2. The dashboard shows a **development key** (`RGAPI-…`). It expires **24 hours** after it is
   generated; click "Regenerate" to get a new one each day.
3. Dev key limits: **20 requests / 1 s** and **100 requests / 2 min**, per routing value
   (`euw1` and `europe` are counted separately).
4. For an always-on crawler, register a **personal API key** (Dashboard → "Register Product" →
   Personal API Key). It has the same rate limits but doesn't expire daily. A production key
   (much higher limits) requires a product that Riot approves.

If the limits change, set `RIFT_RATE_LIMITS` (e.g. `500:10,30000:600`). The client also
adopts whatever the `X-App-Rate-Limit` response header advertises.

## Usage

```bash
uv run rift crawl --max-matches 200   # crawl 200 new matches, then stop
uv run rift crawl                     # crawl until the frontier is exhausted (or Ctrl-C)
uv run rift seed --pages 5            # optional: pull 5 more Diamond pages per division
uv run rift build-db                  # (re)build data/rift.duckdb from the raw archive
uv run rift stats                     # crawl queue, caches, raw files, DB row counts
```

Global options: `--data-dir PATH` (default `$RIFT_DATA_DIR` or `./data`) and `-v`.
All other settings are environment variables; see `.env.example`.

### How the crawler works

1. **Seed**: the Challenger, Grandmaster and Master ladders (`league-v4`, refreshed at most
   daily) plus pages of Diamond I–IV (`league-v4 entries`, `RIFT_DIAMOND_PAGES_PER_SEED` pages
   per division at a time; more pages are fetched whenever the player frontier runs dry).
2. **Expand**: pick the least recently crawled player and fetch their recent ranked match ids
   (`match-v5 by-puuid`, `queue=420`, `RIFT_MATCHES_PER_PLAYER` ids, only games newer than the
   previous visit). New ids go into the work queue.
3. **Fetch**: for each queued match, download the match and its timeline, then for all 10
   participants their current solo-queue rank (`league-v4 entries/by-puuid`) and champion
   masteries (`champion-mastery-v4 by-puuid`). Both are cached with a TTL (24 h / 72 h by default).
   Participants who turn out to be Diamond+ are added to the player frontier, so the crawl
   snowballs beyond the seeded pages.
4. A per-match **manifest** records which rank and mastery snapshot each participant got, so
   the database can join each game to the exact snapshot taken when it was crawled.

Rate limiting is a sliding log per routing value that holds a slot from send until the response
arrives, adopts `X-App-Rate-Limit` / `X-App-Rate-Limit-Count`, honours `Retry-After` on 429 and
retries 5xx/transport errors with exponential backoff. A 401/403 stops the crawl with exit code 2
(usually an expired dev key).

### Resumability and idempotency

Crawl state lives in `data/state.sqlite` (players, `match_queue`, rank/mastery caches, seed
cursors). A match is marked `done` only after its match, timeline and manifest files are on disk,
and each step is skipped if already done, so killing the process at any point (Ctrl-C,
`SIGTERM`, a reboot) loses nothing: just run `rift crawl` again. A match that fails 3 times is
marked `failed` and skipped.

### Request budget

| What                                   | Calls                    | Host     |
|----------------------------------------|--------------------------|----------|
| Match + timeline                       | 2 per match              | europe   |
| Match ids per player visit             | 1 per player (~20 ids)   | europe   |
| Rank lookup (cached 24 h)              | ≤ 10 per match           | euw1     |
| Mastery lookup (cached 72 h)           | ≤ 10 per match           | euw1     |

With a dev/personal key, each host allows 100 calls per 2 min, i.e. **~3,000 per hour per host**.

- The `europe` host is the hard ceiling: ~2.1 calls per match (match, timeline, plus a share of
  the match-id lookups) means **~1,400 matches/hour, ~33,000/day** at best.
- At the start every participant is new, so the `euw1` host needs ~20 calls per match and limits
  the crawl to **~150 matches/hour**. Diamond+ EUW is a small pool (tens of thousands of
  players) who play each other constantly, so the cache hit rate climbs quickly and throughput
  moves toward the `europe` ceiling. Raising the TTLs trades rank freshness for speed.
- Disk (measured on the first live crawl): ~12 KB match + ~65 KB timeline + ~4 KB manifest gzipped per game, plus ~3 KB per mastery snapshot, so roughly **2.5–3 GB per 30k matches**.

### Running on an always-on machine

Use a personal key (dev keys expire daily). A simple systemd unit:

```ini
# /etc/systemd/system/rift-crawl.service
[Unit]
Description=rift crawler
After=network-online.target

[Service]
WorkingDirectory=/opt/rift-advanced-analytics
EnvironmentFile=/opt/rift-advanced-analytics/.env
ExecStart=/usr/local/bin/uv run rift crawl
Restart=always
RestartSec=300
# exit code 2 = invalid/expired key: don't hammer the API, wait for a human
RestartPreventExitStatus=2

[Install]
WantedBy=multi-user.target
```

`sudo systemctl enable --now rift-crawl` then `journalctl -u rift-crawl -f`. When the frontier is
exhausted the process exits cleanly and systemd restarts it after `RestartSec`, at which point
players older than `RIFT_PLAYER_RECRAWL_HOURS` are revisited. Rebuild the DB whenever you want
fresh tables (e.g. a nightly `rift build-db` cron job); it doesn't interfere with the crawler.
Without systemd, `tmux` + `while true; do uv run rift crawl; sleep 300; done` works too.

## Storage

```
data/
  state.sqlite                                   crawl state (frontier, queue, caches)
  rift.duckdb                                    analytics DB, rebuilt by `rift build-db`
  raw/                                           permanent archive, source of truth
    matches/patch=16.19/EUW1_123.json.gz
    timelines/patch=16.19/EUW1_123.json.gz
    manifests/patch=16.19/EUW1_123.json.gz
    ranks/date=2026-10-12/<run>.jsonl.gz         rank snapshots (append-only)
    masteries/date=2026-10-12/<run>.jsonl.gz     mastery snapshots (append-only)
    leagues/date=2026-10-12/*.json.gz            seed ladder pages
```

`rift build-db` parses the whole archive into a fresh DuckDB file and atomically swaps it in, so
the DB can always be deleted and rebuilt; nothing in it is authoritative.

## Schema

Timestamps are UTC `TIMESTAMP`s.

| Table / view          | Grain                                  | Main columns |
|-----------------------|----------------------------------------|--------------|
| `matches`             | match                                  | `match_id`, `patch` (from `gameVersion`, e.g. `16.19`), `game_version`, `game_start`, `game_end`, `duration_s`, `winning_team`, `is_remake`, `crawled_at`, `has_timeline` |
| `participants`        | match × participant                    | `puuid`, `riot_id`, `team_id`, `team_position`, `champion_id/name`, `win`, `kills/deaths/assists`, `gold_earned`, CS, damage, `vision_score`, wards, `items[7]`, `summoner1/2_id`, `primary_style`, `sub_style`, `perks[6]`, `stat_perks[3]`, `rank_fetched_at`, `mastery_fetched_at` |
| `rank_snapshots`      | puuid × fetch time                     | `tier`, `division`, `league_points`, `wins`, `losses`, flags (NULL tier = unranked in solo queue) |
| `mastery_snapshots`   | puuid × fetch time × champion          | `champion_level`, `champion_points`, `last_play_time` |
| `timeline_frames`     | match × minute × participant           | `total_gold`, `current_gold`, `xp`, `level`, `minions_killed`, `jungle_minions_killed`, `damage_to_champions`, `pos_x/y` |
| `timeline_events`     | match × event                          | `event_type`, `timestamp_ms`, `participant_id` (actor), `killer_id`, `victim_id`, `assisting_ids`, `item_id`, `before_id/after_id` (undo), `ward_type`, `monster_type/sub_type`, `building_type`, `tower_type`, `lane_type`, `level`, `pos_x/y`, `details` (full event JSON). All event types are kept. |
| `tiers` (view)        | tier                                   | `tier`, `tier_rank` (IRON=0 … CHALLENGER=9) |
| `mastery_ranked` (view)| mastery row                           | adds `points_share` (share of the player's total mastery points; an OTP-ness measure) and `champion_rank` (1 = most played) |
| `participant_context` (view) | match × participant             | participant + `patch`, `tier`, `division`, `league_points`, `rank_score` (linear LP scale), `mastery_points/level/share/rank` for the champion played |
| `lane_matchups` (view)| match × laner, opponent's perspective too | both laners' champion, `win`, tier/division/LP/`rank_score`, mastery; `same_tier`, `same_division` |

`lane_matchups` has one row per laner per lane (each pairing appears twice, once from each side),
so a matchup win rate is simply:

```sql
SELECT champion_name, opp_champion_name, count(*) AS games, avg(win::INT) AS win_rate
FROM lane_matchups
WHERE team_position = 'MIDDLE' AND same_tier AND NOT is_remake
GROUP BY ALL HAVING games >= 50
ORDER BY win_rate DESC;
```

Caveat: ranks and masteries are snapshots **at crawl time**, not at game time. The crawler
fetches a player's most recent games, so the gap is usually hours or days; `crawled_at - game_start`
lets you filter for it.

## Development

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest
```

Tests use synthetic Riot-shaped fixtures (`tests/factory.py`) and an in-memory fake API
(`tests/conftest.py`); they never touch the network.
