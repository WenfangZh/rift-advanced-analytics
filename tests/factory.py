"""Synthetic payloads shaped like Riot API responses (trimmed to the fields we use)."""

from __future__ import annotations

POSITIONS = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
CHAMPIONS = [
    (266, "Aatrox"),
    (64, "LeeSin"),
    (103, "Ahri"),
    (222, "Jinx"),
    (412, "Thresh"),
    (122, "Darius"),
    (121, "Khazix"),
    (238, "Zed"),
    (51, "Caitlyn"),
    (111, "Nautilus"),
]
GAME_START_MS = 1_760_000_000_000


def make_match(
    match_id: str,
    puuids: list[str],
    *,
    version: str = "15.20.712.3456",
    blue_wins: bool = True,
    queue_id: int = 420,
) -> dict:
    participants = []
    for i, puuid in enumerate(puuids):
        team_id = 100 if i < 5 else 200
        champ_id, champ_name = CHAMPIONS[i]
        participants.append(
            {
                "participantId": i + 1,
                "puuid": puuid,
                "riotIdGameName": f"player{i}",
                "riotIdTagline": "EUW",
                "teamId": team_id,
                "teamPosition": POSITIONS[i % 5],
                "individualPosition": POSITIONS[i % 5],
                "championId": champ_id,
                "championName": champ_name,
                "champLevel": 15,
                "win": (team_id == 100) == blue_wins,
                "kills": i,
                "deaths": 10 - i,
                "assists": 2 * i,
                "goldEarned": 10_000 + 100 * i,
                "totalMinionsKilled": 150 + i,
                "neutralMinionsKilled": 10,
                "totalDamageDealtToChampions": 20_000 + i,
                "totalDamageTaken": 18_000,
                "damageDealtToBuildings": 3_000,
                "visionScore": 25,
                "wardsPlaced": 10,
                "wardsKilled": 3,
                "visionWardsBoughtInGame": 2,
                **{f"item{k}": 1000 + k for k in range(7)},
                "summoner1Id": 4,
                "summoner2Id": 14,
                "gameEndedInEarlySurrender": False,
                "perks": {
                    "statPerks": {"offense": 5008, "flex": 5008, "defense": 5011},
                    "styles": [
                        {
                            "description": "primaryStyle",
                            "style": 8000,
                            "selections": [
                                {"perk": 8010},
                                {"perk": 9111},
                                {"perk": 9104},
                                {"perk": 8299},
                            ],
                        },
                        {
                            "description": "subStyle",
                            "style": 8400,
                            "selections": [{"perk": 8444}, {"perk": 8453}],
                        },
                    ],
                },
            }
        )
    return {
        "metadata": {"matchId": match_id, "participants": puuids},
        "info": {
            "platformId": "EUW1",
            "queueId": queue_id,
            "gameVersion": version,
            "gameCreation": GAME_START_MS - 60_000,
            "gameStartTimestamp": GAME_START_MS,
            "gameEndTimestamp": GAME_START_MS + 1_800_000,
            "gameDuration": 1800,
            "participants": participants,
            "teams": [{"teamId": 100, "win": blue_wins}, {"teamId": 200, "win": not blue_wins}],
        },
    }


def make_timeline(match_id: str, n_frames: int = 3) -> dict:
    frames = []
    for f in range(n_frames):
        frames.append(
            {
                "timestamp": f * 60_000,
                "participantFrames": {
                    str(pid): {
                        "participantId": pid,
                        "totalGold": 500 + f * 400 + pid,
                        "currentGold": 100,
                        "xp": f * 300,
                        "level": 1 + f,
                        "minionsKilled": f * 8,
                        "jungleMinionsKilled": 0,
                        "position": {"x": 1000 * pid, "y": 500 * f},
                        "damageStats": {"totalDamageDoneToChampions": 50 * f},
                    }
                    for pid in range(1, 11)
                },
                "events": [],
            }
        )
    frames[0]["events"] = [
        {"type": "ITEM_PURCHASED", "timestamp": 1500, "participantId": 1, "itemId": 1055},
        {
            "type": "ITEM_UNDO",
            "timestamp": 1800,
            "participantId": 1,
            "beforeId": 1055,
            "afterId": 0,
            "goldGain": 450,
        },
        {"type": "WARD_PLACED", "timestamp": 2000, "creatorId": 5, "wardType": "YELLOW_TRINKET"},
    ]
    if n_frames > 1:
        frames[1]["events"] = [
            {
                "type": "LEVEL_UP",
                "timestamp": 61_000,
                "participantId": 2,
                "level": 2,
                "levelUpType": "NORMAL",
            },
            {
                "type": "CHAMPION_KILL",
                "timestamp": 70_000,
                "killerId": 3,
                "victimId": 8,
                "assistingParticipantIds": [2],
                "bounty": 300,
                "shutdownBounty": 0,
                "killStreakLength": 1,
                "position": {"x": 7000, "y": 7000},
                "victimDamageDealt": [{"basic": True}],
                "victimDamageReceived": [{"basic": True}],
            },
            {
                "type": "ELITE_MONSTER_KILL",
                "timestamp": 90_000,
                "killerId": 2,
                "killerTeamId": 100,
                "monsterType": "DRAGON",
                "monsterSubType": "FIRE_DRAGON",
                "position": {"x": 9800, "y": 4400},
            },
            {
                "type": "BUILDING_KILL",
                "timestamp": 100_000,
                "killerId": 1,
                "teamId": 200,
                "buildingType": "TOWER_BUILDING",
                "towerType": "OUTER_TURRET",
                "laneType": "TOP_LANE",
                "bounty": 250,
                "position": {"x": 4300, "y": 13800},
            },
            {"type": "ITEM_SOLD", "timestamp": 110_000, "participantId": 4, "itemId": 1001},
        ]
    frames[-1]["events"].append(
        {"type": "GAME_END", "timestamp": (n_frames - 1) * 60_000 + 10, "winningTeam": 100}
    )
    return {"metadata": {"matchId": match_id}, "info": {"frameInterval": 60_000, "frames": frames}}


def solo_entry(tier: str, division: str = "I", lp: int = 50) -> dict:
    return {
        "queueType": "RANKED_SOLO_5x5",
        "tier": tier,
        "rank": division,
        "leaguePoints": lp,
        "wins": 100,
        "losses": 90,
        "hotStreak": False,
        "veteran": False,
        "freshBlood": False,
        "inactive": False,
    }


def masteries(champion_points: dict[int, int]) -> list[dict]:
    return [
        {
            "championId": cid,
            "championLevel": min(10, pts // 10_000 + 1),
            "championPoints": pts,
            "lastPlayTime": GAME_START_MS - 86_400_000,
            "championPointsSinceLastLevel": 0,
            "championPointsUntilNextLevel": 0,
        }
        for cid, pts in champion_points.items()
    ]
