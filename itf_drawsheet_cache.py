"""Per-run cache for ITF Drawsheet API responses.

Both populate_data/itf_load_new.py (match-history extraction) and main.py via
draws.py (website bracket view) hit GET /tennis/api/TournamentApi/GetDrawsheet
with the same payload shape. This module lets the first caller populate a
short-lived on-disk cache so the second caller can skip the redundant API hit.

TTL is deliberately short: the cache should survive one workflow run
(~30-40 min between itf_load_new.py and main.py) but expire before the next
cron fires two hours later.
"""
import json
import os

from time_utils import parse_utc_timestamp, utc_now, utc_timestamp
from runtime_paths import DATA_DIR
from pipeline_errors import DataValidationError
from utils import (
    save_json_file, dumps_itf_drawsheets_cache,
    expand_itf_drawsheet_cache,
    get_cache_timestamp, set_cache_entry_meta, remove_cache_entry_meta,
)

_CACHE_FILE = os.path.join(DATA_DIR, "itf_drawsheets_cache.json")
_CACHE_TTL_SECONDS = 60 * 60  # 1 hour
_STALE_RETENTION_SECONDS = 14 * 24 * 60 * 60  # 14 days


def _cache_key(tournament_id, classification, week_number):
    return f"{tournament_id}_{classification}_{int(week_number or 0)}"


def _load_raw_cache():
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise DataValidationError(
            component="itf_drawsheet_cache",
            operation="load cache",
            message=f"cannot read existing drawsheet cache: {_CACHE_FILE}",
            context={"path": _CACHE_FILE, "cause": str(exc)},
        ) from exc
    if not isinstance(raw, dict):
        raise DataValidationError(
            component="itf_drawsheet_cache",
            operation="validate cache",
            message=f"drawsheet cache must contain a JSON object: {_CACHE_FILE}",
            context={"path": _CACHE_FILE},
        )
    return expand_itf_drawsheet_cache(raw)


def _save_raw_cache(cache_obj):
    os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
    cleaned = {}
    if isinstance(cache_obj, dict):
        for key, value in cache_obj.items():
            if isinstance(value, dict):
                item = dict(value)
                item.pop("fetchedAt", None)
                cleaned[key] = item
            else:
                cleaned[key] = value
    else:
        cleaned = cache_obj
    save_json_file(_CACHE_FILE, cleaned, formatter=dumps_itf_drawsheets_cache)


def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return parse_utc_timestamp(ts_str)
    except Exception:
        return None


def get_cached_drawsheet(tournament_id, classification, week_number, allow_stale=False):
    """Return cached drawsheet JSON if fresh, else None.

    When ``allow_stale`` is True, return any structurally valid cached drawsheet
    regardless of age. This is used only as a fallback when live ITF requests
    are blocked by Incapsula but we already captured the raw draw earlier.
    """
    if not tournament_id:
        return None
    cache = _load_raw_cache()
    cache_key = _cache_key(tournament_id, classification, week_number)
    entry = cache.get(cache_key)
    if not isinstance(entry, dict):
        return None
    ts = _parse_ts(get_cache_timestamp(_CACHE_FILE, cache_key, entry))
    if ts is None:
        return None
    age = (utc_now() - ts).total_seconds()
    if not allow_stale and age > _CACHE_TTL_SECONDS:
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    # Only treat real drawsheet payloads as cache hits. ITF sometimes returns
    # JSON error objects such as {"Message": "..."}; those should force a
    # refetch instead of short-circuiting the loader.
    if "koGroups" not in data:
        return None
    return data


def save_drawsheet(tournament_id, classification, week_number, data):
    """Persist a successful drawsheet response for same-run reuse.

    Evicts only very old entries on save so blocked runs can still fall back to
    recently captured raw drawsheets.
    """
    if not (tournament_id and isinstance(data, dict) and "koGroups" in data):
        return
    cache = _load_raw_cache()
    now_utc = utc_now()
    now_ts = utc_timestamp(now_utc)
    fresh = {}
    for k, v in cache.items():
        if not isinstance(v, dict):
            continue
        ts = _parse_ts(get_cache_timestamp(_CACHE_FILE, k, v))
        if ts is None:
            continue
        if (now_utc - ts).total_seconds() <= _STALE_RETENTION_SECONDS:
            fresh[k] = v
    cache_key = _cache_key(tournament_id, classification, week_number)
    fresh[cache_key] = {
        "fetchedAt": now_ts,
        "data": data,
    }
    for k, v in fresh.items():
        if not isinstance(v, dict):
            continue
        ts_str = get_cache_timestamp(_CACHE_FILE, k, v)
        if ts_str:
            set_cache_entry_meta(_CACHE_FILE, k, fetchedAt=ts_str)
    for k in set(cache.keys()) - set(fresh.keys()):
        remove_cache_entry_meta(_CACHE_FILE, k)
    _save_raw_cache(fresh)
