"""Per-run cache for ITF Drawsheet API responses.

Both populate_data/itf_load_new.py (match-history extraction) and main.py via
draws.py (website bracket view) hit POST /tennis/api/TournamentApi/GetDrawsheet
with the same payload shape. This module lets the first caller populate a
short-lived on-disk cache so the second caller can skip the redundant API hit.

TTL is deliberately short: the cache should survive one workflow run
(~30-40 min between itf_load_new.py and main.py) but expire before the next
cron fires two hours later.
"""
import json
import os
from datetime import datetime, timezone

_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "itf_drawsheets_cache.json"
)
_CACHE_TTL_SECONDS = 60 * 60  # 1 hour


def _cache_key(tournament_id, classification, week_number):
    return f"{tournament_id}_{classification}_{int(week_number or 0)}"


def _load_raw_cache():
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_raw_cache(cache_obj):
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache_obj, f, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        pass


def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
    except Exception:
        return None


def get_cached_drawsheet(tournament_id, classification, week_number):
    """Return cached drawsheet JSON if fresh, else None."""
    if not tournament_id:
        return None
    cache = _load_raw_cache()
    entry = cache.get(_cache_key(tournament_id, classification, week_number))
    if not isinstance(entry, dict):
        return None
    ts = _parse_ts(entry.get("fetchedAt"))
    if ts is None:
        return None
    age = (datetime.now(timezone.utc) - ts).total_seconds()
    if age > _CACHE_TTL_SECONDS:
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def save_drawsheet(tournament_id, classification, week_number, data):
    """Persist a successful drawsheet response for same-run reuse.

    Evicts stale entries on every save so the file does not grow unbounded.
    """
    if not (tournament_id and isinstance(data, dict)):
        return
    cache = _load_raw_cache()
    now_utc = datetime.now(timezone.utc)
    fresh = {}
    for k, v in cache.items():
        if not isinstance(v, dict):
            continue
        ts = _parse_ts(v.get("fetchedAt"))
        if ts is None:
            continue
        if (now_utc - ts).total_seconds() <= _CACHE_TTL_SECONDS:
            fresh[k] = v
    fresh[_cache_key(tournament_id, classification, week_number)] = {
        "fetchedAt": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": data,
    }
    _save_raw_cache(fresh)
