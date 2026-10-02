"""Shared WTA weekly-ranking publication rules."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta
from pathlib import Path

PUBLICATION_CUTOFF = time(12, 0)
PUBLICATION_CUTOFF_LABEL = "Monday 12:00 America/New_York"
ACCEPTED_RANKING_STATUSES = {"confirmed_changed", "confirmed_frozen"}
MIN_CURRENT_WEEK_ROWS = 1000


def load_ranking_status(data_dir: str | Path) -> dict:
    try:
        with (Path(data_dir) / "wta_ranking_refresh_status.json").open(encoding="utf-8-sig") as source:
            status = json.load(source)
        return status if isinstance(status, dict) else {}
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}


def ranking_is_valid(rows) -> bool:
    """Apply the weekly refresh's completeness check to API and CSV rows."""

    if len(rows or []) < MIN_CURRENT_WEEK_ROWS:
        return False

    def field(row, lower, upper):
        value = row.get(lower, row.get(upper, ""))
        return str(value).strip() if value is not None else ""

    ids = [field(row, "id", "Id") for row in rows]
    return (
        any(field(row, "rank", "Rank") == "1" for row in rows)
        and all(ids)
        and len(ids) == len(set(ids))
        and all(field(row, "points", "Points") for row in rows)
        and all(field(row, "dob", "DOB") for row in rows)
    )


def publication_cutoff_at(eastern_now: datetime) -> datetime:
    """Return this week's Monday-noon WTA publication boundary in New York."""

    monday = eastern_now.date() - timedelta(days=eastern_now.weekday())
    return datetime.combine(monday, PUBLICATION_CUTOFF, tzinfo=eastern_now.tzinfo)


def publication_window_is_open(eastern_now: datetime) -> bool:
    """Allow the week's first ranking check at or after Monday noon Eastern."""

    return eastern_now >= publication_cutoff_at(eastern_now)


def ranking_date_is_accepted(date_str: str, eastern_now: datetime, status: Mapping[str, object]) -> bool:
    """Keep an unconfirmed or future ranking week out of shared caches and output."""

    try:
        ranking_date = date.fromisoformat(date_str)
    except (TypeError, ValueError):
        return False
    current_monday = eastern_now.date() - timedelta(days=eastern_now.weekday())
    if ranking_date > current_monday:
        return False
    if ranking_date == current_monday:
        return (
            publication_window_is_open(eastern_now)
            and status.get("requested_date") == date_str
            and status.get("status") in ACCEPTED_RANKING_STATUSES
        )
    return not (
        status.get("requested_date") == date_str
        and status.get("status") not in ACCEPTED_RANKING_STATUSES
    )


def accepted_ranking_dates(rankings_by_date: Mapping, eastern_now: datetime, status: Mapping[str, object]) -> list[str]:
    """Return publishable dates, checking the accepted week's stored rows too."""

    return [
        week
        for week in rankings_by_date
        if ranking_date_is_accepted(week, eastern_now, status)
        and (week != status.get("requested_date") or ranking_is_valid(rankings_by_date[week]))
    ]


def effective_wta_ranking_date(eastern_now: datetime, status: Mapping[str, object] | None = None) -> date:
    """Choose the newest WTA ranking date that the pipeline may publish.

    The current Monday becomes eligible only after the refresh accepts it.
    """

    requested = eastern_now.date() - timedelta(days=eastern_now.weekday())
    previous = requested - timedelta(days=7)
    return requested if ranking_date_is_accepted(requested.isoformat(), eastern_now, status or {}) else previous
