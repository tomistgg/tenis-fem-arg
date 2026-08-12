"""Shared timezone-aware clocks and timestamp conversion helpers.

UTC is the internal representation for instants.  Madrid and New York are
used only where a business rule depends on the civil calendar in that zone.
"""

from datetime import UTC, date, datetime, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

UTC = UTC
MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    """Return the current instant as an aware UTC datetime."""
    return datetime.now(UTC)


def as_utc(value: object) -> datetime:
    """Convert an aware datetime to UTC, rejecting ambiguous naive values."""
    if not isinstance(value, datetime):
        raise TypeError("value must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetimes are not valid instants; attach a source timezone first")
    return value.astimezone(UTC)


def now_in(zone: tzinfo, now_utc: datetime | None = None) -> datetime:
    """Return an instant in ``zone``; ``now_utc`` supports deterministic tests."""
    instant = utc_now() if now_utc is None else as_utc(now_utc)
    return instant.astimezone(zone)


def madrid_now(now_utc: datetime | None = None) -> datetime:
    return now_in(MADRID, now_utc)


def new_york_now(now_utc: datetime | None = None) -> datetime:
    return now_in(NEW_YORK, now_utc)


def madrid_today(now_utc: datetime | None = None) -> date:
    return madrid_now(now_utc).date()


def new_york_today(now_utc: datetime | None = None) -> date:
    return new_york_now(now_utc).date()


def parse_utc_timestamp(value: Any) -> datetime:
    """Parse an ISO-8601 timestamp and normalize it to aware UTC."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("missing UTC timestamp")
    parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    return as_utc(parsed)


def utc_timestamp(value: datetime | None = None) -> str:
    """Format an aware instant using the project's compact UTC wire format."""
    instant = utc_now() if value is None else as_utc(value)
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")
