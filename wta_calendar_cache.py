"""One shared WTA tournament calendar for every pipeline consumer."""

from __future__ import annotations

import json
import os
from datetime import date, timedelta

from http_client import get_with_retry
from pipeline_errors import PipelineError
from run_state import report_run_issue
from runtime_logging import get_logger
from runtime_paths import DATA_DIR
from time_utils import madrid_today, parse_utc_timestamp, utc_now, utc_timestamp
from utils import (
    dumps_wta_full_calendar_cache,
    expand_tstrength_cache,
    expand_wta_calendar_cache,
    get_cache_timestamp,
    save_json_file,
    set_cache_file_meta,
)

logger = get_logger("wta-calendar")

WTA_CALENDAR_URL = "https://api.wtatennis.com/tennis/tournaments/"
WTA_CALENDAR_CACHE_FILE = os.path.join(DATA_DIR, "wta_full_calendar_cache.json")
TSTRENGTH_CACHE_FILE = os.path.join(DATA_DIR, "tstrength_cache.json")
WTA_CALENDAR_TTL_SECONDS = 3 * 60 * 60
WTA_CALENDAR_LOOKBACK_DAYS = 28

WTA_CALENDAR_HEADERS = {
    "accept": "application/json",
    "account": "wta",
    "origin": "https://www.wtatennis.com",
    "referer": "https://www.wtatennis.com/",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
    ),
}

_attempted_without_run_id = False


def _tstrength_required_calendar_start(current):
    """Preserve tournament-strength catch-up after an unusually long run gap."""
    january_first = date(current.year, 1, 1)
    try:
        with open(TSTRENGTH_CACHE_FILE, encoding="utf-8") as handle:
            entries = expand_tstrength_cache(json.load(handle))
    except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        return january_first

    latest_start = None
    for entry in entries or []:
        if not isinstance(entry, dict) or str(entry.get("year") or "") != str(current.year):
            continue
        try:
            player_count = int(entry.get("playerCount") or 0)
        except (TypeError, ValueError):
            continue
        if player_count <= 0:
            continue
        try:
            start = date.fromisoformat(str(entry.get("startDate") or "")[:10])
        except (TypeError, ValueError):
            continue
        if latest_start is None or start > latest_start:
            latest_start = start

    if latest_start is None:
        return january_first
    return max(january_first, latest_start - timedelta(days=21))


def canonical_wta_calendar_range(today=None):
    """Return the normal shared-cache coverage: four weeks ago through year-end."""
    current = today or madrid_today()
    normal_start = current - timedelta(days=WTA_CALENDAR_LOOKBACK_DAYS)
    recovery_start = _tstrength_required_calendar_start(current)
    return (
        min(normal_start, recovery_start).isoformat(),
        date(current.year, 12, 31).isoformat(),
    )


def filter_wta_calendar(items, from_date, to_date, *, exclude_levels=()):
    excluded = {str(level or "").strip().casefold() for level in exclude_levels}
    result = []
    for tournament in items or []:
        if not isinstance(tournament, dict):
            continue
        start_date = str(tournament.get("startDate") or "")[:10]
        level = str(tournament.get("level") or "").strip().casefold()
        if from_date <= start_date <= to_date and level not in excluded:
            result.append(tournament)
    return result


def _load_calendar_payload():
    try:
        with open(WTA_CALENDAR_CACHE_FILE, encoding="utf-8") as handle:
            payload = expand_wta_calendar_cache(json.load(handle))
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        report_run_issue(
            "wta-calendar",
            "load shared calendar cache",
            exc,
            severity="degraded",
            context={"path": WTA_CALENDAR_CACHE_FILE},
        )
        return {}


def _payload_covers(payload, from_date, to_date):
    return bool(payload.get("from", "") <= from_date and payload.get("to", "") >= to_date)


def _payload_is_fresh(payload):
    fetched_at_text = get_cache_timestamp(WTA_CALENDAR_CACHE_FILE, payload=payload)
    if not fetched_at_text:
        return False
    try:
        fetched_at = parse_utc_timestamp(fetched_at_text)
    except (TypeError, ValueError):
        return False
    return (utc_now() - fetched_at).total_seconds() <= WTA_CALENDAR_TTL_SECONDS


def _calendar_was_attempted_this_run(payload):
    run_id = os.environ.get("WTARG_RUN_ID", "").strip()
    return bool(run_id and payload.get("lastAttemptRunId") == run_id)


def _save_calendar_payload(payload):
    save_json_file(
        WTA_CALENDAR_CACHE_FILE,
        payload,
        formatter=dumps_wta_full_calendar_cache,
    )


def _record_failed_attempt(payload, from_date, to_date):
    global _attempted_without_run_id

    attempted_at = utc_timestamp()
    run_id = os.environ.get("WTARG_RUN_ID", "").strip()
    updated = dict(payload)
    updated.setdefault("from", from_date)
    updated.setdefault("to", to_date)
    updated.setdefault("items", [])
    updated["lastAttemptAt"] = attempted_at
    if run_id:
        updated["lastAttemptRunId"] = run_id
    else:
        _attempted_without_run_id = True
    _save_calendar_payload(updated)
    set_cache_file_meta(
        WTA_CALENDAR_CACHE_FILE,
        lastAttemptAt=attempted_at,
        lastAttemptRunId=run_id or None,
    )
    return updated


def _fetch_calendar_once(from_date, to_date, component):
    response = get_with_retry(
        WTA_CALENDAR_URL,
        component=component,
        attempts=4,
        headers=WTA_CALENDAR_HEADERS,
        params={
            "page": 0,
            "pageSize": 500,
            "excludeLevels": "ITF",
            "from": from_date,
            "to": to_date,
        },
        timeout=(10, 20),
        failure_status="degraded",
    )
    data = response.json()
    items = data.get("content", []) if isinstance(data, dict) else []
    if not isinstance(items, list) or not items:
        raise ValueError("WTA calendar returned no tournaments")
    if data.get("last") is False:
        raise ValueError("WTA calendar exceeded the shared 500-tournament request")
    return items


def get_shared_wta_calendar(
    from_date=None,
    to_date=None,
    *,
    exclude_levels=(),
    component="wta-calendar",
):
    """Return one cached calendar, filtered locally for a consumer's window.

    The first caller in a run may refresh the canonical broad range. All later
    callers—including child processes—reuse the resulting file. A failed first
    attempt is also recorded by run ID so the pipeline does not hammer the same
    endpoint repeatedly during that execution.
    """
    default_from, default_to = canonical_wta_calendar_range()
    requested_from = str(from_date or default_from)[:10]
    requested_to = str(to_date or default_to)[:10]
    fetch_from = min(default_from, requested_from)
    fetch_to = max(default_to, requested_to)

    payload = _load_calendar_payload()
    cached_items = payload.get("items") if isinstance(payload.get("items"), list) else []
    if cached_items and _payload_covers(payload, fetch_from, fetch_to) and _payload_is_fresh(payload):
        return filter_wta_calendar(
            cached_items,
            requested_from,
            requested_to,
            exclude_levels=exclude_levels,
        )

    if _calendar_was_attempted_this_run(payload) or (
        not os.environ.get("WTARG_RUN_ID", "").strip() and _attempted_without_run_id
    ):
        logger.warning("Reusing cached WTA calendar after this run's single refresh attempt.")
        return filter_wta_calendar(
            cached_items,
            requested_from,
            requested_to,
            exclude_levels=exclude_levels,
        )

    logger.info(f"Refreshing shared WTA calendar ({fetch_from} to {fetch_to})...")
    try:
        items = _fetch_calendar_once(fetch_from, fetch_to, component)
    except Exception as exc:
        payload = _record_failed_attempt(payload, fetch_from, fetch_to)
        cached_items = payload.get("items") if isinstance(payload.get("items"), list) else []
        if cached_items:
            if not isinstance(exc, PipelineError):
                report_run_issue(
                    component,
                    "fetch shared WTA calendar",
                    exc,
                    severity="degraded",
                    context={"fallback": "cached calendar"},
                )
            logger.warning("Using cached WTA calendar after the shared refresh failed.")
        else:
            report_run_issue(
                component,
                "fetch shared WTA calendar",
                exc,
                severity="partial",
                context={"fallback": None},
            )
        return filter_wta_calendar(
            cached_items,
            requested_from,
            requested_to,
            exclude_levels=exclude_levels,
        )

    fetched_at = utc_timestamp()
    run_id = os.environ.get("WTARG_RUN_ID", "").strip()
    payload = {
        "from": fetch_from,
        "to": fetch_to,
        "fetchedAt": fetched_at,
        "lastAttemptAt": fetched_at,
        "items": items,
    }
    if run_id:
        payload["fetchedRunId"] = run_id
        payload["lastAttemptRunId"] = run_id
    _save_calendar_payload(payload)
    set_cache_file_meta(
        WTA_CALENDAR_CACHE_FILE,
        fetchedAt=fetched_at,
        lastAttemptAt=fetched_at,
        fetchedRunId=run_id or None,
        lastAttemptRunId=run_id or None,
        **{"from": fetch_from, "to": fetch_to},
    )
    return filter_wta_calendar(
        items,
        requested_from,
        requested_to,
        exclude_levels=exclude_levels,
    )
