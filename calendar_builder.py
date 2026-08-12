from datetime import date, timedelta

import pandas as pd

from config import CONTINENT_KEYS
from time_utils import madrid_today
from utils import get_calendar_column, get_continent, get_tournament_sort_order


def _collapse_calendar_text(value):
    return " ".join(str(value or "").split()).strip()


def get_calendar_tournament_key(tournament):
    """Return a stable key for calendar dedupe and snapshot comparisons."""
    if not isinstance(tournament, dict):
        return ""

    for field in ("calendarKey", "sourceKey"):
        key = _collapse_calendar_text(tournament.get(field))
        if key:
            return key

    source = _collapse_calendar_text(tournament.get("source")).lower()
    for field in ("tournamentKey", "tournamentId", "id", "key"):
        identifier = _collapse_calendar_text(tournament.get(field))
        if identifier:
            return f"{source}:{identifier.lower()}" if source else identifier.lower()

    return "|".join(
        [
            _collapse_calendar_text(tournament.get("name")).lower(),
            _collapse_calendar_text(tournament.get("level")).lower(),
            _collapse_calendar_text(tournament.get("country")).upper(),
            _collapse_calendar_text(tournament.get("surface")).lower(),
            str(tournament.get("startDate") or "")[:10],
            str(tournament.get("endDate") or "")[:10],
        ]
    )


def get_next_monday():
    today = madrid_today()
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0:
        days_until_monday = 7
    return today + timedelta(days=days_until_monday)


def get_monday_from_date(date_str):
    parsed_date = date.fromisoformat(str(date_str)[:10])
    weekday = parsed_date.weekday()
    if weekday >= 5:
        days_until_monday = 7 - weekday
        monday = parsed_date + timedelta(days=days_until_monday)
    else:
        days_since_monday = weekday
        monday = parsed_date - timedelta(days=days_since_monday)
    return monday


def get_previous_monday(date_str):
    """Return the Monday of the week containing date_str (or earlier if the
    date is already a Monday) as a YYYY-MM-DD string. Returns None for missing
    or unparseable input. Tolerant: accepts None, leading/trailing whitespace,
    and timestamp strings (only the leading 10 chars are inspected)."""
    if not date_str:
        return None
    base = str(date_str).strip()
    if len(base) >= 10:
        base = base[:10]
    try:
        d = date.fromisoformat(base)
    except ValueError:
        return None
    monday = d - timedelta(days=d.weekday())
    return monday.strftime("%Y-%m-%d")


def format_week_label(monday_date):
    months_en = {
        1: "January",
        2: "February",
        3: "March",
        4: "April",
        5: "May",
        6: "June",
        7: "July",
        8: "August",
        9: "September",
        10: "October",
        11: "November",
        12: "December",
    }
    return f"Week of {months_en[monday_date.month]} {monday_date.day}"


def get_monday_offset(date_str, weeks_back):
    dt = pd.to_datetime(date_str)
    monday = dt - timedelta(days=dt.weekday())
    return (monday - timedelta(weeks=weeks_back)).strftime("%Y-%m-%d")


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
    """Build week-based calendar data with three columns.

    A tournament appears in a week when it has at least four days in that week.
    """
    next_monday = get_next_monday()

    parsed = []
    seen = set()
    for t in tournaments:
        calendar_key = get_calendar_tournament_key(t)
        if calendar_key in seen:
            continue
        seen.add(calendar_key)
        start = pd.to_datetime(t.get("startDate"))
        end_str = t.get("endDate")
        end = pd.to_datetime(end_str) if end_str else start + timedelta(days=6)
        continent = get_continent(t.get("country", ""))
        parsed.append(
            {
                "name": t["name"],
                "level": t["level"],
                "surface": t.get("surface", ""),
                "country": t.get("country", ""),
                "continent": continent,
                "start": start,
                "end": end,
                "calendarKey": calendar_key,
                "tournamentKey": t.get("tournamentKey", ""),
                "tournamentId": t.get("tournamentId", ""),
                "source": t.get("source", ""),
            }
        )

    grand_slams = [t for t in parsed if t["level"].lower().replace(" ", "") == "grandslam"]
    for gs in grand_slams:
        qual_end = gs["start"] - timedelta(days=1)
        qual_start = qual_end - timedelta(days=6)
        parsed.append(
            {
                "name": f"{gs['name']} Qualifying",
                "level": gs["level"],
                "surface": gs.get("surface", ""),
                "country": gs.get("country", ""),
                "continent": gs.get("continent", "europe"),
                "start": qual_start,
                "end": qual_end,
                "calendarKey": f"{gs.get('calendarKey', '')}|qualifying",
                "tournamentKey": gs.get("tournamentKey", ""),
                "tournamentId": gs.get("tournamentId", ""),
                "source": gs.get("source", ""),
            }
        )

    end_of_year = date(next_monday.year, 12, 31)
    total_weeks = ((end_of_year - next_monday).days // 7) + 1

    column_keys = ["gs", "wta_tour", "wta_125", "itf"]
    calendar_weeks = []
    for week_offset in range(total_weeks):
        monday = next_monday + timedelta(weeks=week_offset)
        sunday = monday + timedelta(days=6)
        week_label = format_week_label(monday)

        columns = {k: {c: [] for c in CONTINENT_KEYS} for k in column_keys}
        for t in parsed:
            overlap_start = max(t["start"].date(), monday)
            overlap_end = min(t["end"].date(), sunday)
            if overlap_start <= overlap_end:
                days_in_week = (overlap_end - overlap_start).days + 1
                if days_in_week >= 4:
                    col = get_calendar_column(t["level"])
                    cont = t.get("continent", "europe")
                    columns[col][cont].append(
                        {
                            "name": t["name"],
                            "level": t["level"],
                            "surface": t.get("surface", ""),
                            "country": t.get("country", ""),
                            "startDate": t["start"].strftime("%Y-%m-%d"),
                            "endDate": t["end"].strftime("%Y-%m-%d"),
                            "calendarKey": t.get("calendarKey", ""),
                            "tournamentKey": t.get("tournamentKey", ""),
                            "tournamentId": t.get("tournamentId", ""),
                            "source": t.get("source", ""),
                        }
                    )

        for col in column_keys:
            for cont in CONTINENT_KEYS:
                columns[col][cont].sort(key=lambda x: get_tournament_sort_order(x["level"]))

        has_any = any(columns[k][c] for k in column_keys for c in CONTINENT_KEYS)
        calendar_weeks.append(
            {
                "week_label": week_label,
                "monday_date": monday.strftime("%Y-%m-%d"),
                "columns": columns,
                "has_any": has_any,
            }
        )

    while calendar_weeks and not calendar_weeks[-1]["has_any"]:
        calendar_weeks.pop()

    return calendar_weeks
