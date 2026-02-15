import requests
import pandas as pd
from datetime import datetime, timedelta

from config import CONTINENT_KEYS
from utils import get_continent, get_calendar_column, get_tournament_sort_order


def get_next_monday():
    today = datetime.now()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    next_mon = today + timedelta(days=days_until_monday)
    return next_mon.replace(hour=0, minute=0, second=0, microsecond=0)


def get_monday_from_date(date_str):
    date = datetime.strptime(date_str, "%Y-%m-%d")
    weekday = date.weekday()
    if weekday >= 5:
        days_until_monday = 7 - weekday
        monday = date + timedelta(days=days_until_monday)
    else:
        days_since_monday = weekday
        monday = date - timedelta(days=days_since_monday)
    return monday


def format_week_label(monday_date):
    months_en = {
        1: "January", 2: "February", 3: "March", 4: "April",
        5: "May", 6: "June", 7: "July", 8: "August",
        9: "September", 10: "October", 11: "November", 12: "December"
    }
    return f"Week of {months_en[monday_date.month]} {monday_date.day}"


def get_monday_offset(date_str, weeks_back):
    dt = pd.to_datetime(date_str)
    monday = dt - timedelta(days=dt.weekday())
    return (monday - timedelta(weeks=weeks_back)).strftime('%Y-%m-%d')


def generate_dynamic_monday_map(num_weeks=4):
    next_monday = get_next_monday()
    monday_map = {}
    for week_offset in range(num_weeks):
        monday = next_monday + timedelta(weeks=week_offset)
        monday_str = monday.strftime("%Y-%m-%d")
        week_label = format_week_label(monday)
        monday_map[monday_str] = week_label
    return monday_map


def build_calendar_data(tournaments):
    """Build week-based calendar data with 3 columns. A tournament appears in a week if it has >= 4 days in that week."""
    next_monday = get_next_monday()

    parsed = []
    seen = set()
    for t in tournaments:
        if t["name"] in seen:
            continue
        seen.add(t["name"])
        start = pd.to_datetime(t.get("startDate"))
        end_str = t.get("endDate")
        end = pd.to_datetime(end_str) if end_str else start + timedelta(days=6)
        continent = get_continent(t.get("country", ""))
        parsed.append({"name": t["name"], "level": t["level"], "surface": t.get("surface", ""), "country": t.get("country", ""), "continent": continent, "start": start, "end": end})

    grand_slams = [t for t in parsed if t["level"].lower().replace(" ", "") == "grandslam"]
    for gs in grand_slams:
        qual_end = gs["start"] - timedelta(days=1)
        qual_start = qual_end - timedelta(days=6)
        parsed.append({
            "name": f'{gs["name"]} Qualifying',
            "level": gs["level"],
            "surface": gs.get("surface", ""),
            "country": gs.get("country", ""),
            "continent": gs.get("continent", "europe"),
            "start": qual_start,
            "end": qual_end
        })

    end_of_year = datetime(next_monday.year, 12, 31)
    total_weeks = ((end_of_year - next_monday).days // 7) + 1

    column_keys = ["wta_tour", "wta_125", "itf"]
    calendar_weeks = []
    for week_offset in range(total_weeks):
        monday = next_monday + timedelta(weeks=week_offset)
        sunday = monday + timedelta(days=6)
        week_label = format_week_label(monday)

        columns = {k: {c: [] for c in CONTINENT_KEYS} for k in column_keys}
        for t in parsed:
            overlap_start = max(t["start"].date(), monday.date())
            overlap_end = min(t["end"].date(), sunday.date())
            if overlap_start <= overlap_end:
                days_in_week = (overlap_end - overlap_start).days + 1
                if days_in_week >= 4:
                    col = get_calendar_column(t["level"])
                    cont = t.get("continent", "europe")
                    columns[col][cont].append({"name": t["name"], "level": t["level"], "surface": t.get("surface", "")})

        for col in column_keys:
            for cont in CONTINENT_KEYS:
                columns[col][cont].sort(key=lambda x: get_tournament_sort_order(x["level"]))

        has_any = any(columns[k][c] for k in column_keys for c in CONTINENT_KEYS)
        calendar_weeks.append({"week_label": week_label, "columns": columns, "has_any": has_any})

    while calendar_weeks and not calendar_weeks[-1]["has_any"]:
        calendar_weeks.pop()

    return calendar_weeks


def get_sheety_matches():
    """Fetch match history from Sheety API"""
    url = "https://api.sheety.co/6db57031b06f3dea3029e25e8bc924b9/wtaMatches/matches"
    try:
        r = requests.get(url, timeout=10)
        data = r.json()
        if 'matches' in data:
            return data['matches']
        return []
    except Exception as e:
        print(f"Error fetching matches from Sheety: {e}")
        return []
