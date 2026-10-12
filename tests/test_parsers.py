from __future__ import annotations

from rift.archive import patch_from_version
from rift.parsers import parse_mastery_snapshot, parse_match, parse_rank_snapshot, parse_timeline
from tests import factory
from tests.conftest import POOL


def test_patch_from_version():
    assert patch_from_version("15.20.712.3456") == "15.20"
    assert patch_from_version("14.3.555.1") == "14.3"
    assert patch_from_version(None) == "unknown"


def test_parse_match_and_participants():
    match = factory.make_match("EUW1_1", POOL[:10], blue_wins=False)
    manifest = {
        "crawled_at": 1_760_003_600_000,
        "has_timeline": True,
        "participants": [{"puuid": POOL[0], "rank_fetched_at": 1_000, "mastery_fetched_at": 2_000}],
    }
    row, participants = parse_match(match, manifest)
    assert row["match_id"] == "EUW1_1"
    assert row["patch"] == "15.20"
    assert row["winning_team"] == 200
    assert row["duration_s"] == 1800
    assert row["is_remake"] is False
    assert row["game_start"].startswith("2025-10-09")
    assert row["has_timeline"] is True

    assert len(participants) == 10
    p0 = participants[0]
    assert p0["team_position"] == "TOP"
    assert p0["champion_name"] == "Aatrox"
    assert p0["win"] is False
    assert p0["items"] == [1000, 1001, 1002, 1003, 1004, 1005, 1006]
    assert p0["perks"] == [8010, 9111, 9104, 8299, 8444, 8453]
    assert p0["stat_perks"] == [5008, 5008, 5011]
    assert (p0["primary_style"], p0["sub_style"]) == (8000, 8400)
    assert p0["riot_id"] == "player0#EUW"
    assert p0["rank_fetched_at"] == "1970-01-01 00:00:01.000000"
    assert participants[1]["rank_fetched_at"] is None


def test_parse_match_legacy_duration_in_ms():
    match = factory.make_match("EUW1_1", POOL[:10])
    del match["info"]["gameEndTimestamp"]
    match["info"]["gameDuration"] = 1_800_000
    row, _ = parse_match(match)
    assert row["duration_s"] == 1800
    assert row["crawled_at"] is None


def test_parse_timeline():
    frames, events = parse_timeline(factory.make_timeline("EUW1_1", n_frames=3))
    assert len(frames) == 30
    f = next(r for r in frames if r["frame_index"] == 2 and r["participant_id"] == 4)
    assert (f["total_gold"], f["level"], f["minions_killed"], f["pos_x"]) == (1304, 3, 16, 4000)

    by_type = {e["event_type"]: e for e in events}
    assert set(by_type) == {
        "ITEM_PURCHASED",
        "ITEM_UNDO",
        "WARD_PLACED",
        "LEVEL_UP",
        "CHAMPION_KILL",
        "ELITE_MONSTER_KILL",
        "BUILDING_KILL",
        "ITEM_SOLD",
        "GAME_END",
    }
    kill = by_type["CHAMPION_KILL"]
    assert (kill["participant_id"], kill["victim_id"], kill["assisting_ids"]) == (3, 8, [2])
    assert "victimDamageDealt" not in kill["details"]
    assert by_type["WARD_PLACED"]["participant_id"] == 5
    assert by_type["ITEM_UNDO"]["before_id"] == 1055
    assert by_type["BUILDING_KILL"]["tower_type"] == "OUTER_TURRET"
    assert by_type["ELITE_MONSTER_KILL"]["monster_sub_type"] == "FIRE_DRAGON"
    assert by_type["GAME_END"]["details"]["winningTeam"] == 100


def test_parse_rank_snapshot_picks_solo_queue():
    flex = dict(factory.solo_entry("GOLD"), queueType="RANKED_FLEX_SR")
    row = parse_rank_snapshot(
        {"puuid": "x", "fetched_at": 0, "entries": [flex, factory.solo_entry("MASTER", "I", 120)]}
    )
    assert (row["tier"], row["division"], row["league_points"]) == ("MASTER", "I", 120)
    unranked = parse_rank_snapshot({"puuid": "x", "fetched_at": 0, "entries": [flex]})
    assert unranked["tier"] is None


def test_parse_mastery_snapshot():
    rows = list(
        parse_mastery_snapshot(
            {"puuid": "x", "fetched_at": 0, "masteries": factory.masteries({1: 5, 2: 7})}
        )
    )
    assert [(r["champion_id"], r["champion_points"]) for r in rows] == [(1, 5), (2, 7)]
