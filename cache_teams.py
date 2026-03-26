"""
cache_teams.py
Run this occasionally (start of season, after trades, etc.) to rebuild
the player -> MLB team cache. Teams change rarely so no need to fetch
them every time the main script runs.

Usage:
    python cache_teams.py
"""

import json
import pandas as pd
import statsapi
from datetime import datetime
from pathlib import Path

CACHE_FILE = "player_teams_cache.json"


def fetch_mlb_team(player_name: str) -> str:
    """Look up a player's current MLB team, IL-resilient."""
    if not player_name or pd.isna(player_name):
        return "Unknown"
    try:
        players = statsapi.lookup_player(player_name)
        if not players:
            return "Unknown"
        p = players[0]

        # Primary: currentTeam id -> full team name via API
        team_id = p.get("currentTeam", {}).get("id")
        if team_id:
            team_data = statsapi.get("team", {"teamId": team_id})
            teams = team_data.get("teams", [])
            if teams:
                name = teams[0].get("name", "")
                # Reject IL/generic entries - real teams have a proper city+name
                if name and "IL" not in name and "Injured" not in name:
                    return name

        # Fallback: pull team from 2026 season stats where team name is embedded
        stats = statsapi.player_stat_data(
            p["id"], group="hitting", type="season"
        ).get("stats", [])
        if stats:
            team_name = stats[0].get("team", {}).get("name", "")
            if team_name:
                return team_name

        # Last resort: most recent yearByYear entry just for the team name
        # (only used for team color lookup, not HR data)
        stats = statsapi.player_stat_data(
            p["id"], group="hitting", type="yearByYear"
        ).get("stats", [])
        if stats:
            team_name = stats[-1].get("team", {}).get("name", "")
            if team_name:
                return team_name

    except Exception as e:
        print(f"  ❌ Error fetching team for {player_name}: {e}")
    return "Unknown"


def build_cache(csv_path: str = "fantasy_teams.csv") -> None:
    """Read the roster CSV and write a fresh team cache JSON."""
    df = pd.read_csv(csv_path)
    df["PlayerName"] = df["PlayerName"].str.strip()

    players = [p for p in df["PlayerName"].unique() if p and pd.notna(p)]
    total = len(players)

    print(f"Building team cache for {total} players...")
    print("=" * 50)

    cache: dict[str, str] = {}

    for i, player_name in enumerate(players, 1):
        print(f"  [{i}/{total}] {player_name}...", end=" ", flush=True)
        team = fetch_mlb_team(player_name)
        cache[player_name] = team
        print(team)

    output = {
        "built_at": datetime.now().isoformat(),
        "players": cache,
    }

    Path(CACHE_FILE).write_text(json.dumps(output, indent=2))
    print(f"\n✅ Cache saved to {CACHE_FILE} ({len(cache)} players)")
    print(f"   Built at: {output['built_at']}")


if __name__ == "__main__":
    build_cache()
