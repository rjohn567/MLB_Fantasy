#!/usr/bin/env python3
"""
MLB Fantasy League - Best Available Team by HR Leaders
Pulls HR leaders from MLB, removes fantasy team players, and builds best available team
"""

import requests
import pandas as pd
from collections import defaultdict

def get_hr_leaders():
    """Get current season HR leaders with positions"""
    print("Fetching HR leaders from MLB statsapi...")
    
    try:
        # Fetch player stats data from MLB API
        url = "https://statsapi.mlb.com/api/v1/stats?stats=season&group=hitting&limit=500"
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        players_data = []
        
        # Parse the response - stats are in splits array
        stats_group = data.get("stats", [{}])[0]
        splits = stats_group.get("splits", [])
        
        for entry in splits:
            player_info = entry.get("player", {})
            position_info = entry.get("position", {})
            stat_info = entry.get("stat", {})
            
            name = player_info.get("fullName", "")
            home_runs = int(stat_info.get("homeRuns", 0))
            position = position_info.get("abbreviation", "Unknown")
            
            if name and home_runs > 0:
                players_data.append({
                    "Name": name,
                    "HR": home_runs,
                    "Position": position
                })
        
        # Sort by HR descending
        return sorted(players_data, key=lambda x: x["HR"], reverse=True)
    
    except Exception as e:
        print(f"Error fetching HR leaders: {e}")
        import traceback
        traceback.print_exc()
        return []


def load_fantasy_teams():
    """Load players already on fantasy teams"""
    print("Loading fantasy teams...")
    fantasy_df = pd.read_csv("fantasy_teams.csv")
    # Strip whitespace from player names
    taken_players = set(fantasy_df["PlayerName"].str.strip().unique())
    return taken_players


def filter_available_players(all_players, taken_players):
    """Remove players that are already taken"""
    available = [p for p in all_players if p["Name"] not in taken_players]
    print(f"Found {len(available)} available players after filtering\n")
    return available


def build_best_team(available_players):
    """Build the best team by selecting top player at each position"""
    
    # Group players by position, consolidating OF positions
    position_groups = defaultdict(list)
    for player in available_players:
        pos = player["Position"]
        # Consolidate all outfield positions into "OF"
        if pos in ["LF", "CF", "RF"]:
            pos = "OF"
        position_groups[pos].append(player)
    
    # Sort each position by HR count
    for pos in position_groups:
        position_groups[pos].sort(key=lambda x: x["HR"], reverse=True)
    
    # Required positions
    required_positions = ["C", "1B", "2B", "SS", "3B", "OF", "OF", "OF", "DH"]
    
    team = []
    total_hrs = 0
    
    print("=" * 50)
    print(f"{'Position':<12} {'Player':<30} {'HR':<5}")
    print("=" * 50)
    
    # Track which OF players we've already selected
    of_count = 0
    
    for pos_slot in required_positions:
        if pos_slot == "OF":
            # Find an available OF
            if "OF" in position_groups and position_groups["OF"]:
                player = position_groups["OF"].pop(0)
                team.append(player)
                total_hrs += player["HR"]
                print(f"{pos_slot:<12} {player['Name']:<30} {player['HR']:<5}")
                of_count += 1
            else:
                print(f"{pos_slot:<12} {'NO PLAYER AVAILABLE':<30} {'0':<5}")
        else:
            # Find specific position player
            if pos_slot in position_groups and position_groups[pos_slot]:
                player = position_groups[pos_slot].pop(0)
                team.append(player)
                total_hrs += player["HR"]
                print(f"{pos_slot:<12} {player['Name']:<30} {player['HR']:<5}")
            else:
                print(f"{pos_slot:<12} {'NO PLAYER AVAILABLE':<30} {'0':<5}")
    
    print("=" * 50)
    print(f"TOTAL HRs: {total_hrs}")
    print(f"Team roster: {len(team)}/9 positions filled")
    print("=" * 50)
    
    return team, total_hrs


def main():
    # Get HR leaders
    hr_leaders = get_hr_leaders()
    if not hr_leaders:
        print("Failed to fetch HR leaders")
        return
    
    print(f"Total HR leaders found: {len(hr_leaders)}\n")
    
    # Load taken players
    taken_players = load_fantasy_teams()
    print(f"Players already on teams: {len(taken_players)}")
    print(f"Sample taken players: {list(taken_players)[:5]}\n")
    
    # Filter available players
    available = filter_available_players(hr_leaders, taken_players)
    
    # Build best team
    team, total_hrs = build_best_team(available)
    
    print("\nTop available HR leaders (not on fantasy teams):")
    print(f"{'Rank':<6} {'Player':<30} {'Position':<10} {'HR':<5}")
    print("-" * 52)
    for i, player in enumerate(available[:20], 1):
        print(f"{i:<6} {player['Name']:<30} {player['Position']:<10} {player['HR']:<5}")


if __name__ == "__main__":
    main()
