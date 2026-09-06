from __future__ import annotations

import re
from datetime import date, datetime, timedelta

from config import repair_name_text
from time_utils import madrid_today

CALENDAR_CHANGE_RETENTION_DAYS = 3
CALENDAR_CHANGE_FIELDS = (
    ("name", "Name"),
    ("level", "Category"),
    ("surface", "Surface"),
    ("country", "Country"),
    ("startDate", "Start date"),
)

_ITF_CALENDAR_SEQUENCE_SUFFIX_RE = re.compile(r"\s+\d+$")
_TOURNAMENT_CATEGORY_PREFIX_RE = re.compile(
    r"^(?:WTA\s*\d+|W\s*\d+|GRAND\s+SLAM)\s+",
    flags=re.IGNORECASE,
)


def normalize_exact_name(value):
    return " ".join(repair_name_text(value).strip().upper().split())


def normalize_calendar_name(value, column=""):
    """Normalize calendar names while ignoring generated ITF sequence suffixes."""
    name = normalize_exact_name(value)
    if (column or "").strip().lower() == "itf":
        name = _ITF_CALENDAR_SEQUENCE_SUFFIX_RE.sub("", name)
    return name


def _calendar_identity_key(row):
    """Return a provider-backed identity that survives calendar field changes."""
    calendar_key = normalize_exact_name(row.get("calendarKey", ""))
    if calendar_key:
        return ("calendarKey", calendar_key)

    source = normalize_exact_name(row.get("source", ""))
    for field in ("tournamentId", "tournamentKey"):
        identifier = normalize_exact_name(row.get(field, ""))
        if identifier:
            return (field, source, identifier)

    return (
        "legacy",
        source,
        normalize_calendar_name(row.get("name", ""), row.get("column", "")),
        normalize_exact_name(row.get("level", "")),
        str(row.get("startDate") or row.get("week_label") or "")[:10],
    )


def _calendar_rows_by_identity(rows):
    grouped = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        grouped.setdefault(_calendar_identity_key(row), []).append(row)
    return grouped


def _calendar_representative(rows):
    return sorted(
        rows,
        key=lambda row: (
            str(row.get("startDate") or ""),
            str(row.get("week_label") or ""),
            str(row.get("name") or ""),
        ),
    )[0]


def _normalized_calendar_field(row, field):
    value = row.get(field, "")
    if field == "name":
        return normalize_calendar_name(value, row.get("column", ""))
    if field == "startDate":
        return str(value or "").strip()[:10]
    return normalize_exact_name(value)


def _calendar_week_start(row, today):
    start_date = str(row.get("startDate") or "").strip()[:10]
    if start_date:
        try:
            return datetime.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            pass

    week_label = str(row.get("week_label") or "").strip()
    if week_label.lower().startswith("week of "):
        month_day = week_label[8:].strip()
        try:
            return datetime.strptime(f"{month_day} {today.year}", "%B %d %Y").date()
        except ValueError:
            pass
    return None


def diff_calendar_tournaments(before_rows, after_rows, *, today=None):
    """Return additions, tracked field changes, and cancellations between snapshots."""
    today = today or madrid_today()
    before_by_id = _calendar_rows_by_identity(before_rows)
    after_by_id = _calendar_rows_by_identity(after_rows)

    added = [_calendar_representative(after_by_id[key]) for key in sorted(after_by_id.keys() - before_by_id.keys())]

    changed = []
    for key in sorted(before_by_id.keys() & after_by_id.keys()):
        before = _calendar_representative(before_by_id[key])
        after = _calendar_representative(after_by_id[key])
        changes = []
        for field, label in CALENDAR_CHANGE_FIELDS:
            # Do not alert once when an older snapshot gains newly tracked fields.
            if field not in before or field not in after:
                continue
            if _normalized_calendar_field(before, field) == _normalized_calendar_field(after, field):
                continue
            changes.append(
                {
                    "field": field,
                    "label": label,
                    "before": before.get(field, ""),
                    "after": after.get(field, ""),
                }
            )
        if changes:
            changed.append({"before": before, "after": after, "changes": changes})

    cancelled = []
    for key in sorted(before_by_id.keys() - after_by_id.keys()):
        before = _calendar_representative(before_by_id[key])
        start_date = _calendar_week_start(before, today)
        if start_date is not None and start_date <= today:
            continue
        cancelled.append(before)

    def sort_key(row):
        return (
            str(row.get("startDate") or row.get("week_label") or ""),
            normalize_exact_name(row.get("name", "")),
        )

    added.sort(key=sort_key)
    changed.sort(key=lambda item: sort_key(item["after"]))
    cancelled.sort(key=sort_key)
    return added, changed, cancelled


def _itf_level_value(level):
    match = re.fullmatch(r"W\s*(\d+)", str(level or "").strip(), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def _name_without_category(name):
    return _TOURNAMENT_CATEGORY_PREFIX_RE.sub("", normalize_exact_name(name)).strip()


def summarize_calendar_change(item):
    """Convert a raw snapshot diff into the concise website presentation model."""
    before = item.get("before") or {}
    after = item.get("after") or {}
    fields = {change.get("field") for change in (item.get("changes") or [])}
    actions = []

    if "startDate" in fields:
        new_date = str(after.get("startDate") or "").strip()[:10]
        actions.append(f"Moved to {new_date}" if new_date else "Start date removed")

    if "level" in fields:
        old_level = repair_name_text(str(before.get("level") or "")).strip()
        new_level = repair_name_text(str(after.get("level") or "")).strip()
        old_value = _itf_level_value(old_level)
        new_value = _itf_level_value(new_level)
        if old_value is not None and new_value is not None and new_value > old_value:
            actions.append(f"Upgraded to {new_level}")
        elif old_value is not None and new_value is not None and new_value < old_value:
            actions.append(f"Downgraded to {new_level}")
        else:
            actions.append(f"Category changed to {new_level}")

    if "name" in fields:
        old_subject = _name_without_category(before.get("name"))
        new_subject = _name_without_category(after.get("name"))
        if old_subject != new_subject or "level" not in fields:
            new_name = repair_name_text(str(after.get("name") or "")).strip()
            actions.append(f"Renamed to {new_name}")

    if "surface" in fields:
        surface = repair_name_text(str(after.get("surface") or "")).strip()
        actions.append(f"Surface changed to {surface}")

    if "country" in fields:
        country = repair_name_text(str(after.get("country") or "")).strip().upper()
        actions.append(f"Country changed to {country}")

    if not actions:
        return None
    return {
        "country": str(before.get("country") or after.get("country") or "").strip().upper(),
        "name": repair_name_text(str(before.get("name") or after.get("name") or "")).strip(),
        "startDate": str(before.get("startDate") or after.get("startDate") or "").strip()[:10],
        "actions": actions,
    }


def update_calendar_change_history(existing_history, before_rows, after_rows, *, detected_on=None):
    """Merge newly detected changes and retain the current day plus two prior days."""
    detected_on = detected_on or madrid_today()
    cutoff = detected_on - timedelta(days=CALENDAR_CHANGE_RETENTION_DAYS - 1)
    groups = {}

    for group in existing_history or []:
        if not isinstance(group, dict):
            continue
        date_text = str(group.get("date") or "")[:10]
        try:
            group_date = date.fromisoformat(date_text)
        except ValueError:
            continue
        if not cutoff <= group_date <= detected_on:
            continue
        changes = [change for change in (group.get("changes") or []) if isinstance(change, dict)]
        if changes:
            groups.setdefault(date_text, []).extend(changes)

    _, raw_changes, _ = diff_calendar_tournaments(before_rows, after_rows, today=detected_on)
    new_changes = [summary for item in raw_changes if (summary := summarize_calendar_change(item))]
    new_changes.sort(key=lambda change: (change.get("startDate", ""), normalize_exact_name(change.get("name", ""))))
    if new_changes:
        current = groups.setdefault(detected_on.isoformat(), [])
        for change in new_changes:
            if change not in current:
                current.append(change)

    return [{"date": date_text, "changes": groups[date_text]} for date_text in sorted(groups)]
