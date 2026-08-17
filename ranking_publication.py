"""Shared WTA weekly-ranking publication rules."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta

PUBLICATION_CUTOFF = time(12, 0)
PUBLICATION_CUTOFF_LABEL = "Monday 12:00 America/New_York"
ACCEPTED_RANKING_STATUSES = {"confirmed_changed", "confirmed_frozen"}


def publication_cutoff_at(eastern_now: datetime) -> datetime:
    """Return this week's Monday-noon WTA publication boundary in New York."""

    monday = eastern_now.date() - timedelta(days=eastern_now.weekday())
    return datetime.combine(monday, PUBLICATION_CUTOFF, tzinfo=eastern_now.tzinfo)


def publication_window_is_open(eastern_now: datetime) -> bool:
    """Allow the week's first ranking check at or after Monday noon Eastern."""

    return eastern_now >= publication_cutoff_at(eastern_now)


def effective_wta_ranking_date(eastern_now: datetime, status: Mapping[str, object] | None = None) -> date:
    """Choose the newest WTA ranking date that the pipeline may publish.

    Before Monday noon in New York, the previous Monday remains authoritative.
    After the cutoff, retain the previous week only when the weekly refresh has
    explicitly reported that the current ranking is still unavailable.
    """

    requested = eastern_now.date() - timedelta(days=eastern_now.weekday())
    previous = requested - timedelta(days=7)
    if not publication_window_is_open(eastern_now):
        return previous

    current_status = status or {}
    if str(current_status.get("requested_date") or "") != requested.isoformat():
        return requested
    if str(current_status.get("status") or "") in ACCEPTED_RANKING_STATUSES:
        return requested

    previous_date = str(current_status.get("previous_date") or "")
    try:
        parsed_previous = date.fromisoformat(previous_date)
    except ValueError:
        return previous
    return parsed_previous if parsed_previous == previous else previous
