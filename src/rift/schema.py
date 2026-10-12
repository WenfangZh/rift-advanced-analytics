"""DuckDB schema. Column lists are the single source of truth for both the DDL
and the typed NDJSON load in rift.db."""

from __future__ import annotations

from rift.config import TIER_ORDER

TABLES: dict[str, dict[str, str]] = {
    "matches": {
        "match_id": "VARCHAR PRIMARY KEY",
        "platform_id": "VARCHAR",
        "queue_id": "INTEGER",
        "game_version": "VARCHAR",
        "patch": "VARCHAR",
        "game_creation": "TIMESTAMP",
        "game_start": "TIMESTAMP",
        "game_end": "TIMESTAMP",
        "duration_s": "INTEGER",
        "winning_team": "INTEGER",
        "is_remake": "BOOLEAN",
        "crawled_at": "TIMESTAMP",
        "has_timeline": "BOOLEAN",
    },
    "participants": {
        "match_id": "VARCHAR",
        "participant_id": "INTEGER",
        "puuid": "VARCHAR",
        "riot_id": "VARCHAR",
        "team_id": "INTEGER",
        "team_position": "VARCHAR",
        "individual_position": "VARCHAR",
        "champion_id": "INTEGER",
        "champion_name": "VARCHAR",
        "champ_level": "INTEGER",
        "win": "BOOLEAN",
        "kills": "INTEGER",
        "deaths": "INTEGER",
        "assists": "INTEGER",
        "gold_earned": "INTEGER",
        "total_minions_killed": "INTEGER",
        "neutral_minions_killed": "INTEGER",
        "damage_to_champions": "INTEGER",
        "damage_taken": "INTEGER",
        "damage_to_buildings": "INTEGER",
        "vision_score": "INTEGER",
        "wards_placed": "INTEGER",
        "wards_killed": "INTEGER",
        "control_wards_bought": "INTEGER",
        "items": "INTEGER[]",
        "summoner1_id": "INTEGER",
        "summoner2_id": "INTEGER",
        "primary_style": "INTEGER",
        "sub_style": "INTEGER",
        "perks": "INTEGER[]",
        "stat_perks": "INTEGER[]",
        "rank_fetched_at": "TIMESTAMP",
        "mastery_fetched_at": "TIMESTAMP",
    },
    "rank_snapshots": {
        "puuid": "VARCHAR",
        "fetched_at": "TIMESTAMP",
        "tier": "VARCHAR",
        "division": "VARCHAR",
        "league_points": "INTEGER",
        "wins": "INTEGER",
        "losses": "INTEGER",
        "hot_streak": "BOOLEAN",
        "veteran": "BOOLEAN",
        "fresh_blood": "BOOLEAN",
        "inactive": "BOOLEAN",
    },
    "mastery_snapshots": {
        "puuid": "VARCHAR",
        "fetched_at": "TIMESTAMP",
        "champion_id": "INTEGER",
        "champion_level": "INTEGER",
        "champion_points": "BIGINT",
        "last_play_time": "TIMESTAMP",
        "points_since_last_level": "BIGINT",
        "points_until_next_level": "BIGINT",
    },
    "timeline_frames": {
        "match_id": "VARCHAR",
        "frame_index": "INTEGER",
        "timestamp_ms": "INTEGER",
        "participant_id": "INTEGER",
        "total_gold": "INTEGER",
        "current_gold": "INTEGER",
        "xp": "INTEGER",
        "level": "INTEGER",
        "minions_killed": "INTEGER",
        "jungle_minions_killed": "INTEGER",
        "damage_to_champions": "INTEGER",
        "pos_x": "INTEGER",
        "pos_y": "INTEGER",
    },
    "timeline_events": {
        "match_id": "VARCHAR",
        "frame_index": "INTEGER",
        "event_index": "INTEGER",
        "timestamp_ms": "INTEGER",
        "event_type": "VARCHAR",
        "participant_id": "INTEGER",
        "killer_id": "INTEGER",
        "victim_id": "INTEGER",
        "team_id": "INTEGER",
        "killer_team_id": "INTEGER",
        "item_id": "INTEGER",
        "after_id": "INTEGER",
        "before_id": "INTEGER",
        "skill_slot": "INTEGER",
        "level": "INTEGER",
        "level_up_type": "VARCHAR",
        "ward_type": "VARCHAR",
        "creator_id": "INTEGER",
        "monster_type": "VARCHAR",
        "monster_sub_type": "VARCHAR",
        "building_type": "VARCHAR",
        "tower_type": "VARCHAR",
        "lane_type": "VARCHAR",
        "kill_type": "VARCHAR",
        "bounty": "INTEGER",
        "shutdown_bounty": "INTEGER",
        "multi_kill_length": "INTEGER",
        "kill_streak_length": "INTEGER",
        "gold_gain": "INTEGER",
        "assisting_ids": "INTEGER[]",
        "pos_x": "INTEGER",
        "pos_y": "INTEGER",
        "details": "JSON",
    },
}

PRIMARY_KEYS = {
    "participants": ("match_id", "participant_id"),
    "rank_snapshots": ("puuid", "fetched_at"),
}

_tier_values = ", ".join(f"('{t}', {i})" for i, t in enumerate(TIER_ORDER))

VIEWS = {
    "tiers": f"SELECT * FROM (VALUES {_tier_values}) AS t(tier, tier_rank)",
    "mastery_ranked": """
        SELECT *,
            champion_points / nullif(sum(champion_points) OVER w, 0) AS points_share,
            row_number() OVER (PARTITION BY puuid, fetched_at ORDER BY champion_points DESC) AS champion_rank
        FROM mastery_snapshots
        WINDOW w AS (PARTITION BY puuid, fetched_at)
    """,
    "participant_context": """
        SELECT
            p.*,
            m.patch,
            m.game_start,
            m.is_remake,
            r.tier,
            r.division,
            r.league_points,
            r.wins AS season_wins,
            r.losses AS season_losses,
            t.tier_rank,
            CASE
                WHEN t.tier_rank >= 7 THEN 2800 + r.league_points
                WHEN t.tier_rank IS NOT NULL THEN
                    t.tier_rank * 400
                    + (4 - CASE r.division WHEN 'I' THEN 1 WHEN 'II' THEN 2 WHEN 'III' THEN 3 ELSE 4 END) * 100
                    + r.league_points
            END AS rank_score,
            CASE WHEN p.mastery_fetched_at IS NOT NULL THEN coalesce(ms.champion_level, 0) END AS mastery_level,
            CASE WHEN p.mastery_fetched_at IS NOT NULL THEN coalesce(ms.champion_points, 0) END AS mastery_points,
            CASE WHEN p.mastery_fetched_at IS NOT NULL THEN coalesce(ms.points_share, 0) END AS mastery_share,
            ms.champion_rank AS mastery_rank
        FROM participants p
        JOIN matches m USING (match_id)
        LEFT JOIN rank_snapshots r ON r.puuid = p.puuid AND r.fetched_at = p.rank_fetched_at
        LEFT JOIN tiers t ON t.tier = r.tier
        LEFT JOIN mastery_ranked ms
            ON ms.puuid = p.puuid AND ms.fetched_at = p.mastery_fetched_at AND ms.champion_id = p.champion_id
    """,
    "lane_matchups": """
        SELECT
            a.match_id, a.patch, a.game_start, a.is_remake, a.team_position,
            a.puuid, a.team_id, a.champion_id, a.champion_name, a.win,
            a.tier, a.division, a.league_points, a.tier_rank, a.rank_score,
            a.mastery_level, a.mastery_points, a.mastery_share, a.mastery_rank,
            b.puuid AS opp_puuid, b.champion_id AS opp_champion_id, b.champion_name AS opp_champion_name,
            b.tier AS opp_tier, b.division AS opp_division, b.league_points AS opp_league_points,
            b.tier_rank AS opp_tier_rank, b.rank_score AS opp_rank_score,
            b.mastery_level AS opp_mastery_level, b.mastery_points AS opp_mastery_points,
            b.mastery_share AS opp_mastery_share, b.mastery_rank AS opp_mastery_rank,
            a.tier = b.tier AS same_tier,
            a.tier = b.tier AND a.division = b.division AS same_division
        FROM participant_context a
        JOIN participant_context b
            ON a.match_id = b.match_id AND a.team_position = b.team_position AND a.team_id <> b.team_id
        WHERE a.team_position <> ''
    """,
}


def create_table_sql(name: str) -> str:
    columns = [f"{col} {typ}" for col, typ in TABLES[name].items()]
    if name in PRIMARY_KEYS:
        columns.append(f"PRIMARY KEY ({', '.join(PRIMARY_KEYS[name])})")
    return f"CREATE TABLE {name} ({', '.join(columns)})"


def load_columns(name: str) -> dict[str, str]:
    return {col: typ.replace(" PRIMARY KEY", "") for col, typ in TABLES[name].items()}
