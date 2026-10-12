from __future__ import annotations

import duckdb
import pytest

from rift.cli import main
from rift.db import build_db


@pytest.fixture
def built(populated_raw, tmp_path):
    db_path = tmp_path / "rift.duckdb"
    counts = build_db(populated_raw, db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    yield counts, con
    con.close()


def test_row_counts(built):
    counts, _ = built
    assert counts == {
        "matches": 2,
        "participants": 20,
        "rank_snapshots": 10,
        "mastery_snapshots": 19,
        "timeline_frames": 60,
        "timeline_events": 18,
    }


def test_build_is_idempotent(populated_raw, tmp_path):
    db_path = tmp_path / "rift.duckdb"
    first = build_db(populated_raw, db_path)
    second = build_db(populated_raw, db_path)
    assert first == second
    con = duckdb.connect(str(db_path), read_only=True)
    dupes = con.execute(
        "SELECT count(*) FROM (SELECT match_id, participant_id FROM participants GROUP BY ALL HAVING count(*) > 1)"
    ).fetchone()[0]
    con.close()
    assert dupes == 0


def test_matches_table(built):
    _, con = built
    rows = con.execute(
        "SELECT match_id, patch, winning_team, duration_s FROM matches ORDER BY 1"
    ).fetchall()
    assert rows == [("EUW1_1", "15.20", 100, 1800), ("EUW1_2", "15.20", 200, 1800)]


def test_lane_matchups_view(built):
    _, con = built
    total = con.execute("SELECT count(*) FROM lane_matchups").fetchone()[0]
    assert total == 2 * 5 * 2

    top = con.execute(
        """
        SELECT champion_name, opp_champion_name, win, tier, opp_tier, same_tier, same_division,
               mastery_points, opp_mastery_points
        FROM lane_matchups WHERE match_id = 'EUW1_1' AND team_position = 'TOP'
        ORDER BY team_id
        """
    ).fetchall()
    assert top == [
        ("Aatrox", "Darius", True, "DIAMOND", "DIAMOND", True, False, 100_000, 600_000),
        ("Darius", "Aatrox", False, "DIAMOND", "DIAMOND", True, False, 600_000, 100_000),
    ]

    jungle = con.execute(
        "SELECT tier, opp_tier, same_tier FROM lane_matchups "
        "WHERE match_id = 'EUW1_1' AND team_position = 'JUNGLE' AND team_id = 100"
    ).fetchone()
    assert jungle == ("DIAMOND", "MASTER", False)

    utility = con.execute(
        "SELECT tier, same_tier, mastery_points, mastery_share, opp_tier FROM lane_matchups "
        "WHERE match_id = 'EUW1_1' AND team_position = 'UTILITY' AND team_id = 100"
    ).fetchone()
    assert utility == (None, None, 0, 0.0, "EMERALD")

    same_tier_wins = con.execute(
        "SELECT count(*) FROM lane_matchups WHERE same_tier AND win"
    ).fetchone()[0]
    assert same_tier_wins == 2 * 3


def test_participant_context_rank_score_and_share(built):
    _, con = built
    row = con.execute(
        "SELECT tier, division, league_points, rank_score, mastery_rank, round(mastery_share, 3) "
        "FROM participant_context WHERE match_id = 'EUW1_1' AND participant_id = 2"
    ).fetchone()
    assert row == ("DIAMOND", "II", 50, 6 * 400 + 2 * 100 + 50, 1, round(200_000 / 250_000, 3))


def test_cli_build_db(populated_raw, capsys):
    data_dir = populated_raw.parent
    assert main(["--data-dir", str(data_dir), "build-db"]) == 0
    assert '"matches": 2' in capsys.readouterr().out
    assert main(["--data-dir", str(data_dir), "stats"]) == 0
    assert '"lane_matchups"' not in capsys.readouterr().out
