"""
main.py
Fetches 2026 HR totals for fantasy roster players and updates Google Sheets.

Relies on player_teams_cache.json built by cache_teams.py.
HR lookups run in parallel via ThreadPoolExecutor for speed.
If a player's stats come back empty (e.g. on the IL), their last known
2026 HR total is preserved from hr_cache.json rather than zeroing out.

Usage:
    python main.py
    python main.py --rebuild-cache   # rebuild team cache before running
"""

import json
import os
import sys
import pandas as pd
import statsapi
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from gspread_formatting import CellFormat, Color, TextFormat, format_cell_ranges
import time
from zoneinfo import ZoneInfo


TEAM_CACHE_FILE = "player_teams_cache.json"
HR_CACHE_FILE = "hr_cache.json"
MAX_WORKERS = 8  # Parallel HR fetch threads - tune to taste

# ---------------------------------------------------------------------------
# MLB Team brand colors
# ---------------------------------------------------------------------------
TEAM_COLORS = {
    "Arizona Diamondbacks":  {"bg": "#A71930", "text": "#E3D4AD"},
    "Atlanta Braves":        {"bg": "#13274F", "text": "#CE1141"},
    "Baltimore Orioles":     {"bg": "#DF4601", "text": "#000000"},
    "Boston Red Sox":        {"bg": "#BD3039", "text": "#0C2340"},
    "Chicago Cubs":          {"bg": "#0E3386", "text": "#CC3433"},
    "Chicago White Sox":     {"bg": "#27251F", "text": "#C4CED4"},
    "Cincinnati Reds":       {"bg": "#C6011F", "text": "#000000"},
    "Cleveland Guardians":   {"bg": "#0C2340", "text": "#E31937"},
    "Colorado Rockies":      {"bg": "#33006F", "text": "#C4CED4"},
    "Detroit Tigers":        {"bg": "#0C2340", "text": "#FA4616"},
    "Houston Astros":        {"bg": "#002D62", "text": "#EB6E1F"},
    "Kansas City Royals":    {"bg": "#004687", "text": "#BD9B60"},
    "Los Angeles Angels":    {"bg": "#BA0021", "text": "#003263"},
    "Los Angeles Dodgers":   {"bg": "#005A9C", "text": "#A5ACAF"},
    "Miami Marlins":         {"bg": "#00A3E0", "text": "#EF3340"},
    "Milwaukee Brewers":     {"bg": "#12284B", "text": "#FFC52F"},
    "Minnesota Twins":       {"bg": "#002B5C", "text": "#D31145"},
    "New York Mets":         {"bg": "#002D72", "text": "#FF5910"},
    "New York Yankees":      {"bg": "#003087", "text": "#E6E6E6"},
    "Athletics":             {"bg": "#003831", "text": "#EFB21E"},
    "Philadelphia Phillies": {"bg": "#E81828", "text": "#002D72"},
    "Pittsburgh Pirates":    {"bg": "#27251F", "text": "#FDB827"},
    "San Diego Padres":      {"bg": "#2F241D", "text": "#FFC425"},
    "San Francisco Giants":  {"bg": "#FD5A1E", "text": "#27251F"},
    "Seattle Mariners":      {"bg": "#0C2340", "text": "#005C5C"},
    "St. Louis Cardinals":   {"bg": "#C41E3A", "text": "#0C2340"},
    "Tampa Bay Rays":        {"bg": "#092C5C", "text": "#8FBCE6"},
    "Texas Rangers":         {"bg": "#003278", "text": "#C0111F"},
    "Toronto Blue Jays":     {"bg": "#134A8E", "text": "#E8291C"},
    "Washington Nationals":  {"bg": "#AB0003", "text": "#14225A"},
}


# ---------------------------------------------------------------------------
# Team cache
# ---------------------------------------------------------------------------

def load_team_cache() -> dict[str, str]:
    """Load the player -> MLB team cache. Returns empty dict if missing."""
    cache_path = Path(TEAM_CACHE_FILE)
    if not cache_path.exists():
        print(f"⚠️  Team cache '{TEAM_CACHE_FILE}' not found.")
        print("   Run  python cache_teams.py  to build it, or pass --rebuild-cache.")
        return {}
    data = json.loads(cache_path.read_text())
    built_at = data.get("built_at", "unknown")
    players = data.get("players", {})
    print(f"✅ Loaded team cache ({len(players)} players, built {built_at})")
    return players


# ---------------------------------------------------------------------------
# HR cache (persists 2026 totals across runs so IL players don't lose count)
# ---------------------------------------------------------------------------

def load_hr_cache() -> dict[str, int]:
    """Load last known 2026 HR totals from the previous run."""
    path = Path(HR_CACHE_FILE)
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data.get("players", {})


def save_hr_cache(hr_map: dict[str, int]) -> None:
    """Persist HR totals so IL players don't lose their 2026 count."""
    output = {
        "season": 2026,
        "updated_at": datetime.now().isoformat(),
        "players": hr_map,
    }
    Path(HR_CACHE_FILE).write_text(json.dumps(output, indent=2))


# ---------------------------------------------------------------------------
# HR fetching (parallel, IL-safe)
# ---------------------------------------------------------------------------

def fetch_hr(player_name: str, last_known: int = 0) -> tuple[str, int]:
    """
    Return (player_name, hr_count) for the 2026 season.
    If the API returns no stats (IL, not yet played, etc.), returns
    last_known instead of 0 so the player's count is preserved.
    """
    if not player_name or pd.isna(player_name):
        return (player_name, 0)
    try:
        players = statsapi.lookup_player(player_name)
        if not players:
            print(f"  ⚠️  Could not find player: {player_name} — holding at {last_known} HRs")
            return (player_name, last_known)

        person_id = players[0]["id"]
        stats_data = statsapi.player_stat_data(
            person_id, group="hitting", type="season"
        )
        stats_list = stats_data.get("stats", [])

        if stats_list:
            hr = int(stats_list[0].get("stats", {}).get("homeRuns", 0))
            return (player_name, hr)
        else:
            # Empty stats = likely on IL or hasn't played yet this season.
            # Hold the last known 2026 total - do NOT fall back to old seasons.
            print(f"  ⚠️  No 2026 stats for {player_name} (IL?) — holding at {last_known} HRs")
            return (player_name, last_known)

    except Exception as e:
        print(f"  ❌ Error fetching HRs for {player_name}: {e}")
        return (player_name, last_known)


def fetch_all_hrs(players: list[str], hr_cache: dict[str, int]) -> dict[str, int]:
    """Fetch 2026 HR counts for all players in parallel."""
    results: dict[str, int] = {}
    total = len(players)
    completed = 0

    print(f"\n📊 Fetching 2026 HRs for {total} players ({MAX_WORKERS} threads)...")
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(fetch_hr, p, hr_cache.get(p, 0)): p
            for p in players
        }
        for future in as_completed(futures):
            name, hrs = future.result()
            results[name] = hrs
            completed += 1
            cached = hr_cache.get(name, 0)
            tag = " (held from cache — on IL?)" if hrs == cached and cached > 0 and hrs == hr_cache.get(name) else ""
            print(f"  [{completed}/{total}] {name}: {hrs} HRs{tag}")

    return results


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_roster(csv_path: str = "fantasy_teams.csv") -> pd.DataFrame:
    df = pd.read_csv(csv_path)
    df["PlayerName"] = df["PlayerName"].str.strip()
    required = {"TeamName", "PlayerName", "Position"}
    if not required.issubset(df.columns):
        raise ValueError(f"CSV must contain columns: {required}")
    return df


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

def print_roster_stats(df: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("FANTASY MLB STATS - 2026 SEASON TRACKER")
    print("=" * 70)

    starters = df[df["Position"] != "Bench"]
    bench = df[df["Position"] == "Bench"]

    print("\n⭐ STARTERS BY TEAM:")
    print("-" * 70)
    for team in starters["TeamName"].unique():
        rows = starters[starters["TeamName"] == team].sort_values("HRs", ascending=False)
        print(f"\n  {team}:")
        for _, row in rows.iterrows():
            name = row["PlayerName"] if pd.notna(row["PlayerName"]) else "[EMPTY]"
            print(f"    {row['Position']:3} | {name:25} | {int(row['HRs']):3} HRs")

    print("\n🪑 BENCH PLAYERS:")
    print("-" * 70)
    for team in bench["TeamName"].unique():
        for _, row in bench[bench["TeamName"] == team].iterrows():
            name = row["PlayerName"] if pd.notna(row["PlayerName"]) else "[EMPTY]"
            print(f"  {team:10} | {name:25} | {int(row['HRs']):3} HRs")

    print("\n📈 TEAM TOTALS (Starters Only):")
    print("-" * 70)
    team_totals = starters.groupby("TeamName")["HRs"].sum().sort_values(ascending=False)
    for team, total in team_totals.items():
        print(f"  {team:15} | {total:3} HRs")


# ---------------------------------------------------------------------------
# Google Sheets formatting helpers
# ---------------------------------------------------------------------------

def _hex_color(hex_str: str) -> Color:
    r = int(hex_str[1:3], 16) / 255
    g = int(hex_str[3:5], 16) / 255
    b = int(hex_str[5:7], 16) / 255
    return Color(red=r, green=g, blue=b)


def apply_team_formatting(ws, df: pd.DataFrame, sheet2_data: list, teams: list) -> None:
    """Apply MLB team colors to player cells in Sheet 2."""
    player_team_map = dict(zip(df["PlayerName"], df["MLBTeam"]))
    formatting_requests = []

    for row_idx, row_data in enumerate(sheet2_data[1:], start=2):
        if not row_data or not any(row_data):
            continue
        for team_idx, _ in enumerate(teams):
            player_col = (team_idx * 2) + 2
            hr_col = player_col + 1
            if player_col - 1 < len(row_data):
                player_name = row_data[player_col - 1]
                if player_name and player_name in player_team_map:
                    mlb_team = player_team_map[player_name]
                    if mlb_team in TEAM_COLORS:
                        colors = TEAM_COLORS[mlb_team]
                        fmt = CellFormat(
                            backgroundColor=_hex_color(colors["bg"]),
                            textFormat=TextFormat(
                                foregroundColor=_hex_color(colors["text"]),
                                bold=True,
                            ),
                        )
                        player_cell = f"{chr(64 + player_col)}{row_idx}"
                        hr_cell = f"{chr(64 + hr_col)}{row_idx}"
                        formatting_requests.extend([(player_cell, fmt), (hr_cell, fmt)])

    if formatting_requests:
        try:
            format_cell_ranges(ws, formatting_requests)
            print("✅ MLB team colors applied")
        except Exception as e:
            print(f"⚠️  Could not apply team formatting: {e}")


# ---------------------------------------------------------------------------
# Google Sheets update
# ---------------------------------------------------------------------------

def update_google_sheet(df: pd.DataFrame) -> None:
    print("\n" + "=" * 70)
    print("GOOGLE SHEETS UPDATE")
    print("=" * 70)

    try:
        # Try to pull the secret from GitHub environment variables
        creds_json = os.environ.get("GOOGLE_CREDS")
        
        scopes = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive",
        ]

        if creds_json:
            # Running on GitHub Actions
            info = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
            print("🔐 Authenticated via GitHub Secrets")
        else:
            # Running locally on your Mac
            creds = Credentials.from_service_account_file("service_account.json", scopes=scopes)
            print("📂 Authenticated via local service_account.json")
        client = gspread.authorize(creds)
        sheet = client.open("Fantasy Tracker")

        # --- Sheet 1: Team Rankings ---
        try:
            ws1 = sheet.get_worksheet(0)
        except Exception:
            ws1 = sheet.add_worksheet(title="Team Rankings", rows=100, cols=10)
        ws1.clear()

        starters = df[df["Position"] != "Bench"]
        team_totals = starters.groupby("TeamName")["HRs"].sum().sort_values(ascending=False)
        sheet1_data = [["Team Name", "Total HRs"]] + [
            [team, int(hr)] for team, hr in team_totals.items()
        ]
        ws1.update(sheet1_data, f"A1:B{len(sheet1_data)}")
        print("✅ Sheet 1 (Team Rankings) updated")

        timestamp = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d %I:%M %p ET")
        ws1.update_cell(18, 1, f"Last updated at: {timestamp}")

        # --- Sheet 2: Roster pivot ---
        try:
            ws2 = sheet.get_worksheet(1)
        except Exception:
            ws2 = sheet.add_worksheet(title="Roster Details", rows=200, cols=50)
        ws2.clear()

        teams = sorted(df["TeamName"].unique())
        positions = ["C", "1B", "2B", "SS", "3B", "OF", "OF", "OF", "DH"]

        headers = ["Position"]
        for team in teams:
            headers += [f"{team}-Player", f"{team}-HR"]

        sheet2_data = [headers]
        position_count: dict[str, int] = {}

        for position in positions:
            position_count[position] = position_count.get(position, 0) + 1
            occ = position_count[position] - 1
            row = [position]
            for team in teams:
                players_at_pos = df[
                    (df["TeamName"] == team)
                    & (df["Position"] == position)
                    & (df["Position"] != "Bench")
                ]
                if not players_at_pos.empty and occ < len(players_at_pos):
                    p = players_at_pos.iloc[occ]
                    row += [p["PlayerName"] if pd.notna(p["PlayerName"]) else "", int(p["HRs"])]
                else:
                    row += ["", 0]
            sheet2_data.append(row)

        # Totals row
        total_row = ["TOTAL"]
        for team in teams:
            hr_sum = int(df[(df["TeamName"] == team) & (df["Position"] != "Bench")]["HRs"].sum())
            total_row += ["", hr_sum]
        sheet2_data.append(total_row)
        sheet2_data.append([])  # spacing

        # Bench section
        bench_header = ["BENCH"] + ["", ""] * len(teams)
        sheet2_data.append(bench_header)

        bench_row = [""]
        for team in teams:
            bench_players = df[(df["TeamName"] == team) & (df["Position"] == "Bench")]
            if not bench_players.empty:
                p = bench_players.iloc[0]
                bench_row += [p["PlayerName"] if pd.notna(p["PlayerName"]) else "", int(p["HRs"])]
            else:
                bench_row += ["", 0]
        sheet2_data.append(bench_row)

        num_cols = len(headers)
        col_letter = chr(64 + num_cols) if num_cols < 27 else f"A{chr(64 + num_cols - 26)}"
        ws2.update(sheet2_data, f"A1:{col_letter}{len(sheet2_data)}")
        print("✅ Sheet 2 (Roster Details) updated")

        apply_team_formatting(ws2, df, sheet2_data, teams)

        timestamp = datetime.now(ZoneInfo("America/New_York")).strftime("%m/%d %I:%M %p ET")
        ws2.update_cell(18, 1, f"Last updated at: {timestamp}")
        print("✅ Timestamp written")

    except FileNotFoundError:
        print("❌ service_account.json not found")
    except gspread.exceptions.SpreadsheetNotFound:
        print("❌ 'Fantasy Tracker' spreadsheet not found")
    except gspread.exceptions.APIError as e:
        print(f"❌ Google API Error: {e}")
        # Only try to read the file if it actually exists locally
        if os.path.exists("service_account.json"):
            try:
                sa = json.loads(Path("service_account.json").read_text())
                print(f"   Share the sheet with: {sa.get('client_email', '?')}")
            except Exception:
                pass
    except Exception as e:
        print(f"❌ Unexpected error: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    start_time = time.perf_counter()
    rebuild_cache = "--rebuild-cache" in sys.argv

    if rebuild_cache:
        import subprocess
        subprocess.run([sys.executable, "cache_teams.py"], check=True)

    try:
        df = load_roster(csv_path="fantasy_teams.csv")
    except FileNotFoundError:
        print("❌ fantasy_teams.csv not found")
        sys.exit(1)

    # Load team cache (built by cache_teams.py)
    team_cache = load_team_cache()
    df["MLBTeam"] = df["PlayerName"].map(lambda n: team_cache.get(n, "Unknown"))

    uncached = df[df["MLBTeam"] == "Unknown"]["PlayerName"].dropna().unique()
    if len(uncached):
        print(f"⚠️  {len(uncached)} player(s) not in team cache: {list(uncached)}")
        print("   Run  python cache_teams.py  to refresh.")

    # Load last known 2026 HR totals (preserves counts for IL players)
    hr_cache = load_hr_cache()

    # Fetch live HR counts in parallel
    players = df["PlayerName"].dropna().unique().tolist()
    hr_map = fetch_all_hrs(players, hr_cache)

    # Save updated totals back to HR cache
    save_hr_cache(hr_map)

    df["HRs"] = df["PlayerName"].map(lambda n: hr_map.get(n, 0))

    print_roster_stats(df)
    update_google_sheet(df)

    print("\n" + "=" * 70)
    end_time = time.perf_counter()

    elapsed_time = end_time - start_time
    print(f"Execution took: {elapsed_time:.4f} seconds")
    print("✅ Done!")
    print("=" * 70)


if __name__ == "__main__":
    main()
