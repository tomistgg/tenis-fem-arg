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

from pipeline_errors import DataValidationError
from runtime_paths import DATA_DIR
from time_utils import parse_utc_timestamp, utc_now, utc_timestamp
from utils import (
    dumps_itf_drawsheets_cache,
    expand_itf_drawsheet_cache,
    get_cache_timestamp,
    remove_cache_entry_meta,
    save_json_file,
    set_cache_entry_meta,
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
        with open(_CACHE_FILE, encoding="utf-8") as f:
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
    except (TypeError, ValueError):
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


def _drawsheet_nationality_status(data, nationality):
    """Return ``(published, present)`` for a nationality in round one.

    Once a draw's opening round is published, every direct acceptance,
    qualifier, lucky loser, and wildcard has a position in that round.  The
    opening round is therefore the authoritative roster for that draw.
    """
    if not isinstance(data, dict):
        return False, False

    target = str(nationality or "").strip().upper()
    published = False
    present = False
    for group in data.get("koGroups") or []:
        if not isinstance(group, dict):
            continue
        rounds = group.get("rounds") or []
        if not rounds or not isinstance(rounds[0], dict):
            continue
        for match in rounds[0].get("matches") or []:
            if not isinstance(match, dict):
                continue
            for team in match.get("teams") or []:
                if not isinstance(team, dict):
                    continue
                players = team.get("players") or []
                singular_player = team.get("player")
                if isinstance(singular_player, dict):
                    players = [*players, singular_player]
                for player in players:
                    if not isinstance(player, dict):
                        continue
                    # ITF may publish empty match/team placeholders before the
                    # draw itself. Actual player data is the publication boundary.
                    published = True
                    if (
                        str(player.get("nationality") or "").strip().upper() == target
                    ):
                        present = True
    return published, present


def tournament_draw_codes_with_definitive_no_nationality(
    tournament_ids,
    nationality="ARG",
    *,
    week_number=0,
):
    """Return draw codes that can no longer add a player from ``nationality``.

    A published qualifying opening round is final for qualifying.  A published
    main draw without the nationality becomes final only when qualifying is
    also published without that nationality; otherwise an unresolved qualifier
    could still enter the main draw.  Stale cache entries are intentional here
    because a published opening round remains authoritative after the short
    same-run request cache expires.
    """
    wanted_ids = {
        str(tournament_id).strip()
        for tournament_id in tournament_ids or []
        if str(tournament_id or "").strip()
    }
    if not wanted_ids:
        return set()

    cache = _load_raw_cache()
    result = {}
    for tournament_id in wanted_ids:
        statuses = {}
        for classification in ("Q", "M"):
            cache_key = _cache_key(tournament_id, classification, week_number)
            entry = cache.get(cache_key)
            data = entry.get("data") if isinstance(entry, dict) else None
            statuses[classification] = _drawsheet_nationality_status(data, nationality)

        q_published, q_present = statuses["Q"]
        m_published, m_present = statuses["M"]
        excluded_codes = set()
        if q_published and not q_present:
            excluded_codes.add("Q")
            if m_published and not m_present:
                excluded_codes.add("M")
        if excluded_codes:
            result[tournament_id] = excluded_codes
    return result


def tournament_ids_with_published_main_draw(tournament_ids):
    """Return tournament IDs whose main-draw opening round is published."""
    return tournament_ids_with_published_draw(tournament_ids, "M")


def tournament_ids_with_published_qualifying_draw(tournament_ids):
    """Return tournament IDs whose qualifying opening round is published."""
    return tournament_ids_with_published_draw(tournament_ids, "Q")


def tournament_ids_with_published_draw(tournament_ids, classification):
    """Return tournament IDs whose requested opening round is published."""
    wanted_ids = {
        str(tournament_id).strip()
        for tournament_id in tournament_ids or []
        if str(tournament_id or "").strip()
    }
    if not wanted_ids:
        return set()

    draw_code = str(classification or "").strip().upper()
    if draw_code not in {"M", "Q"}:
        raise ValueError(f"Unsupported ITF draw classification: {classification!r}")

    cache = _load_raw_cache()
    published_ids = set()
    for tournament_id in wanted_ids:
        key_prefix = f"{tournament_id}_{draw_code}_"
        for cache_key, entry in cache.items():
            if not str(cache_key).startswith(key_prefix):
                continue
            data = entry.get("data") if isinstance(entry, dict) else None
            published, _ = _drawsheet_nationality_status(data, "")
            if published:
                published_ids.add(tournament_id)
                break
    return published_ids


def tournament_ids_with_definitive_no_nationality(
    tournament_ids,
    nationality="ARG",
    *,
    week_number=0,
):
    """Return regular-event IDs whose published Q and M draws exclude a nation."""
    draw_codes = tournament_draw_codes_with_definitive_no_nationality(
        tournament_ids,
        nationality,
        week_number=week_number,
    )
    return {
        tournament_id
        for tournament_id, excluded_codes in draw_codes.items()
        if excluded_codes == {"Q", "M"}
    }


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
