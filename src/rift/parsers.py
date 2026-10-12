"""Turn raw Riot payloads into flat rows for the DuckDB tables (see rift.schema)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

from rift.archive import patch_from_version
from rift.config import RANKED_SOLO_QUEUE

Row = dict[str, Any]

EVENT_COLUMNS_FROM_JSON = {
    "killer_id": "killerId",
    "victim_id": "victimId",
    "team_id": "teamId",
    "killer_team_id": "killerTeamId",
    "item_id": "itemId",
    "after_id": "afterId",
    "before_id": "beforeId",
    "skill_slot": "skillSlot",
    "level": "level",
    "level_up_type": "levelUpType",
    "ward_type": "wardType",
    "creator_id": "creatorId",
    "monster_type": "monsterType",
    "monster_sub_type": "monsterSubType",
    "building_type": "buildingType",
    "tower_type": "towerType",
    "lane_type": "laneType",
    "kill_type": "killType",
    "bounty": "bounty",
    "shutdown_bounty": "shutdownBounty",
    "multi_kill_length": "multiKillLength",
    "kill_streak_length": "killStreakLength",
    "gold_gain": "goldGain",
}
BULKY_EVENT_KEYS = {"victimDamageDealt", "victimDamageReceived"}


def ms_to_ts(ms: int | None) -> str | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")


def parse_match(match: dict, manifest: dict | None = None) -> tuple[Row, list[Row]]:
    info = match["info"]
    match_id = match["metadata"]["matchId"]
    winner = next((t["teamId"] for t in info.get("teams", []) if t.get("win")), None)
    duration = info.get("gameDuration")
    if duration is not None and "gameEndTimestamp" not in info:
        duration = duration // 1000
    participants = info.get("participants", [])
    match_row = {
        "match_id": match_id,
        "platform_id": info.get("platformId"),
        "queue_id": info.get("queueId"),
        "game_version": info.get("gameVersion"),
        "patch": patch_from_version(info.get("gameVersion")),
        "game_creation": ms_to_ts(info.get("gameCreation")),
        "game_start": ms_to_ts(info.get("gameStartTimestamp")),
        "game_end": ms_to_ts(info.get("gameEndTimestamp")),
        "duration_s": duration,
        "winning_team": winner,
        "is_remake": any(p.get("gameEndedInEarlySurrender") for p in participants),
        "crawled_at": ms_to_ts(manifest["crawled_at"]) if manifest else None,
        "has_timeline": bool(manifest and manifest.get("has_timeline")),
    }
    context = {p["puuid"]: p for p in (manifest or {}).get("participants", []) if p.get("puuid")}
    return match_row, [
        _participant_row(match_id, p, context.get(p.get("puuid"))) for p in participants
    ]


def _participant_row(match_id: str, p: dict, context: dict | None) -> Row:
    perks = p.get("perks", {})
    styles = {s.get("description"): s for s in perks.get("styles", [])}
    primary = styles.get("primaryStyle", {})
    sub = styles.get("subStyle", {})
    stat = perks.get("statPerks", {})
    game_name, tag = p.get("riotIdGameName"), p.get("riotIdTagline")
    context = context or {}
    return {
        "match_id": match_id,
        "participant_id": p.get("participantId"),
        "puuid": p.get("puuid"),
        "riot_id": f"{game_name}#{tag}" if game_name else None,
        "team_id": p.get("teamId"),
        "team_position": p.get("teamPosition") or "",
        "individual_position": p.get("individualPosition"),
        "champion_id": p.get("championId"),
        "champion_name": p.get("championName"),
        "champ_level": p.get("champLevel"),
        "win": p.get("win"),
        "kills": p.get("kills"),
        "deaths": p.get("deaths"),
        "assists": p.get("assists"),
        "gold_earned": p.get("goldEarned"),
        "total_minions_killed": p.get("totalMinionsKilled"),
        "neutral_minions_killed": p.get("neutralMinionsKilled"),
        "damage_to_champions": p.get("totalDamageDealtToChampions"),
        "damage_taken": p.get("totalDamageTaken"),
        "damage_to_buildings": p.get("damageDealtToBuildings"),
        "vision_score": p.get("visionScore"),
        "wards_placed": p.get("wardsPlaced"),
        "wards_killed": p.get("wardsKilled"),
        "control_wards_bought": p.get("visionWardsBoughtInGame"),
        "items": [p.get(f"item{i}", 0) for i in range(7)],
        "summoner1_id": p.get("summoner1Id"),
        "summoner2_id": p.get("summoner2Id"),
        "primary_style": primary.get("style"),
        "sub_style": sub.get("style"),
        "perks": [s.get("perk") for s in primary.get("selections", []) + sub.get("selections", [])],
        "stat_perks": [stat.get("offense"), stat.get("flex"), stat.get("defense")],
        "rank_fetched_at": ms_to_ts(context.get("rank_fetched_at")),
        "mastery_fetched_at": ms_to_ts(context.get("mastery_fetched_at")),
    }


def parse_timeline(timeline: dict) -> tuple[list[Row], list[Row]]:
    match_id = timeline["metadata"]["matchId"]
    frames: list[Row] = []
    events: list[Row] = []
    for frame_index, frame in enumerate(timeline["info"].get("frames", [])):
        for key, pf in frame.get("participantFrames", {}).items():
            position = pf.get("position") or {}
            frames.append(
                {
                    "match_id": match_id,
                    "frame_index": frame_index,
                    "timestamp_ms": frame.get("timestamp"),
                    "participant_id": int(pf.get("participantId", key)),
                    "total_gold": pf.get("totalGold"),
                    "current_gold": pf.get("currentGold"),
                    "xp": pf.get("xp"),
                    "level": pf.get("level"),
                    "minions_killed": pf.get("minionsKilled"),
                    "jungle_minions_killed": pf.get("jungleMinionsKilled"),
                    "damage_to_champions": (pf.get("damageStats") or {}).get(
                        "totalDamageDoneToChampions"
                    ),
                    "pos_x": position.get("x"),
                    "pos_y": position.get("y"),
                }
            )
        for event_index, event in enumerate(frame.get("events", [])):
            position = event.get("position") or {}
            row = {
                "match_id": match_id,
                "frame_index": frame_index,
                "event_index": event_index,
                "timestamp_ms": event.get("timestamp"),
                "event_type": event.get("type"),
                "participant_id": event.get("participantId")
                or event.get("killerId")
                or event.get("creatorId"),
            }
            row.update({col: event.get(key) for col, key in EVENT_COLUMNS_FROM_JSON.items()})
            row["assisting_ids"] = event.get("assistingParticipantIds")
            row["pos_x"] = position.get("x")
            row["pos_y"] = position.get("y")
            row["details"] = {k: v for k, v in event.items() if k not in BULKY_EVENT_KEYS}
            events.append(row)
    return frames, events


def parse_rank_snapshot(record: dict) -> Row:
    entry = next(
        (e for e in record.get("entries") or [] if e.get("queueType") == RANKED_SOLO_QUEUE), {}
    )
    return {
        "puuid": record["puuid"],
        "fetched_at": ms_to_ts(record["fetched_at"]),
        "tier": entry.get("tier"),
        "division": entry.get("rank"),
        "league_points": entry.get("leaguePoints"),
        "wins": entry.get("wins"),
        "losses": entry.get("losses"),
        "hot_streak": entry.get("hotStreak"),
        "veteran": entry.get("veteran"),
        "fresh_blood": entry.get("freshBlood"),
        "inactive": entry.get("inactive"),
    }


def parse_mastery_snapshot(record: dict) -> Iterator[Row]:
    fetched_at = ms_to_ts(record["fetched_at"])
    for m in record.get("masteries") or []:
        yield {
            "puuid": record["puuid"],
            "fetched_at": fetched_at,
            "champion_id": m.get("championId"),
            "champion_level": m.get("championLevel"),
            "champion_points": m.get("championPoints"),
            "last_play_time": ms_to_ts(m.get("lastPlayTime")),
            "points_since_last_level": m.get("championPointsSinceLastLevel"),
            "points_until_next_level": m.get("championPointsUntilNextLevel"),
        }
