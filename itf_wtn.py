"""Cached ITF World Tennis Number lookups from official player profiles."""

import json
import math
import os
import re
import time
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import requests

from runtime_logging import get_logger
from time_utils import madrid_today
from utils import save_json_file

logger = get_logger("itf-wtn")
ITF_WTN_CACHE_FILENAME = "itf_wtn_cache.json"
PROFILE_URL = "https://www.itftennis.com/en/players/{slug}/{player_id}/{country}/wt/s/overview/"
REQUEST_INTERVAL_SECONDS = 2.0
PROFILE_BATCH_SIZE = 8
PROFILE_BATCH_COOLDOWN_SECONDS = 61.0


class ITFProfileBlocked(RuntimeError):
    pass


def _slug(value):
    ascii_text = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "player"


def player_profile_url(player):
    return PROFILE_URL.format(
        slug=_slug(player.get("name")),
        player_id=str(player.get("player_id") or "").strip(),
        country=str(player.get("country") or "").strip().lower() or "xx",
    )


def player_profile_urls(player, preferred_url=""):
    women_url = player_profile_url(player)
    junior_url = women_url.replace("/wt/s/overview/", "/jt/s/")
    api_url = str(player.get("profile_url") or "").strip().rstrip("/")
    if api_url.endswith("/wt"):
        api_url += "/s/overview/"
    elif api_url.endswith("/jt"):
        api_url += "/s/"
    urls = (preferred_url, api_url) if "/wt/" in preferred_url else (
        preferred_url, api_url, women_url, junior_url
    )
    return list(dict.fromkeys(url for url in urls if url))


def parse_wtn_singles(page_source):
    decoder = json.JSONDecoder()
    for marker in re.finditer(r"\bvar\s+props\s*=\s*", str(page_source or "")):
        try:
            props, _ = decoder.raw_decode(str(page_source)[marker.end():].lstrip())
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(props, dict) or "wtnSingles" not in props:
            continue
        value = props["wtnSingles"]
        if value in (None, ""):
            return None
        return float(value)
    raise ValueError("ITF profile did not contain wtnSingles props")


def _load_cache(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _week_start(value):
    try:
        day = value if isinstance(value, date) else date.fromisoformat(str(value or "")[:10])
    except (TypeError, ValueError):
        return ""
    return (day - timedelta(days=day.weekday())).isoformat()


def _normalize_cache(cache):
    """Migrate weekly history to one latest observation, preserving retry metadata."""
    normalized = {}
    for player_id, record in cache.items():
        if not isinstance(record, dict):
            continue
        target = {k: v for k, v in record.items() if k != "weeks"}
        candidates = []
        for stored_week, value in (record.get("weeks") or {}).items():
            if isinstance(value, dict) and _valid_wtn(value.get("wtn")):
                observation = dict(value)
                observation.setdefault("retrieved_at", stored_week)
                if not observation.get("source") and observation.get("profile_url"):
                    observation["source"] = "profile"
                candidates.append(observation)
        if _valid_wtn(record.get("wtn")):
            observation = {k: record[k] for k in _OBSERVATION_FIELDS if k in record}
            if not observation.get("source") and observation.get("profile_url"):
                observation["source"] = "profile"
            candidates.append(observation)
        if record.get("no_wtn_checked_at"):
            candidates = []
        if candidates:
            latest = max(candidates, key=_observation_priority)
            for field in _OBSERVATION_FIELDS:
                target.pop(field, None)
            target.update(latest)
            profiles = [v for v in candidates if v.get("source") == "profile" and v.get("profile_url")]
            if profiles:
                target["last_profile_url"] = max(profiles, key=_observation_priority)["profile_url"]
        normalized[player_id] = target
    return normalized


_OBSERVATION_FIELDS = {"wtn", "source", "source_key", "profile_url", "retrieved_at", "checked_at", "observed_on"}


def _valid_wtn(value):
    try:
        return math.isfinite(float(value)) and 0 < float(value) <= 40
    except (TypeError, ValueError):
        return False


def _observation_priority(observation):
    return (str(observation.get("observed_on") or observation.get("retrieved_at")
                or observation.get("checked_at") or ""), observation.get("source") == "profile")


def _store_observation(cache, player_id, player, observation):
    if not _valid_wtn(observation.get("wtn")):
        return
    record = cache.setdefault(player_id, {"name": "", "country": ""})
    record["name"] = player.get("name") or record.get("name", "")
    record["country"] = player.get("country") or record.get("country", "")
    if _valid_wtn(record.get("wtn")) and _observation_priority(observation) < _observation_priority(record):
        return
    for field in _OBSERVATION_FIELDS:
        record.pop(field, None)
    record.update(observation)
    record.pop("no_wtn_checked_at", None)
    if observation.get("source") == "profile" and observation.get("profile_url"):
        record["last_profile_url"] = observation["profile_url"]


def _select_recent_observation(record, today, max_age_days=5, *, source=None):
    """Return the newest WTN observation saved in the rolling freshness window."""
    cutoff = today - timedelta(days=max_age_days)
    if not _valid_wtn(record.get("wtn")) or (source and record.get("source") != source):
        return {}
    try:
        observed_date = date.fromisoformat(_observation_priority(record)[0][:10])
    except (TypeError, ValueError):
        return {}
    return record if cutoff <= observed_date <= today else {}


def _recent_no_wtn_check(record, today):
    try:
        checked = date.fromisoformat(record.get("no_wtn_checked_at", ""))
    except (TypeError, ValueError):
        return False
    return today - timedelta(days=5) <= checked <= today


def _store_no_wtn_check(cache, player_id, player, profile_url, today):
    record = cache.setdefault(player_id, {})
    record["name"] = player.get("name") or record.get("name", "")
    record["country"] = player.get("country") or record.get("country", "")
    for field in _OBSERVATION_FIELDS:
        record.pop(field, None)
    record.update(no_wtn_checked_at=today.isoformat(), last_profile_url=profile_url)


def checkpoint_profile_wtn(cache_path, cache, player_id):
    """Persist verified profiles independently of wider dataset acceptance."""
    journal_path = os.environ.get("WTARG_WTN_CHECKPOINT_PATH")
    if journal_path:
        record = cache[player_id]
        journal = _load_cache(journal_path)
        journal[player_id] = {
            key: record[key] for key in (
                "name", "country", "wtn", "source", "profile_url", "retrieved_at",
                "no_wtn_checked_at", "last_profile_url",
            )
            if key in record
        }
        save_json_file(journal_path, journal)
    save_json_file(cache_path, cache)


def recover_profile_wtn_checkpoints(journal_path, cache_path):
    """Merge only validated profile observations; preserve all other data."""
    if not Path(journal_path).exists():
        return 0
    journal = json.loads(Path(journal_path).read_text(encoding="utf-8-sig"))
    if not isinstance(journal, dict):
        raise ValueError("WTN checkpoint must be an object")
    today = madrid_today()
    for pid, record in journal.items():
        url = str(record.get("profile_url") or record.get("last_profile_url") or "") if isinstance(record, dict) else ""
        valid_value = (
            _select_recent_observation(record, today, source="profile")
            if isinstance(record, dict) and not record.get("no_wtn_checked_at") else {}
        )
        valid_absence = (
            _recent_no_wtn_check(record, today) and not _valid_wtn(record.get("wtn"))
            if isinstance(record, dict) else False
        )
        if (not str(pid).isdigit() or not (valid_value or valid_absence)
                or not url.startswith("https://www.itftennis.com/en/players/") or f"/{pid}/" not in url):
            raise ValueError(f"Invalid verified WTN checkpoint for {pid}")
    # A damaged production cache must never be replaced with an empty cache.
    path = Path(cache_path)
    cache = json.loads(path.read_text(encoding="utf-8-sig")) if path.exists() else {}
    if not isinstance(cache, dict):
        raise ValueError("WTN cache must be an object")
    saved = 0
    for pid, record in journal.items():
        previous = dict(cache.get(pid, {}))
        if record.get("no_wtn_checked_at"):
            checked = date.fromisoformat(record["no_wtn_checked_at"])
            latest = _observation_priority(previous)[0][:10]
            if not (_valid_wtn(previous.get("wtn")) and latest >= checked.isoformat()):
                _store_no_wtn_check(cache, pid, record, record["last_profile_url"], checked)
        else:
            _store_observation(cache, pid, record, {
                key: record[key] for key in ("wtn", "source", "profile_url", "retrieved_at")
            })
        saved += cache[pid] != previous
    if saved:
        save_json_file(cache_path, cache)
    return saved


def _latest_profile_url(record):
    return record.get("last_profile_url") or record.get("profile_url", "")


def _current_entry_list(key, tournament_weeks, today):
    if not tournament_weeks:
        return True
    week = tournament_weeks.get(str(key).removesuffix("#qual"))
    return bool(week and str(week)[:10] >= (today - timedelta(days=today.weekday())).isoformat())


def _identity_key(player):
    name = str(player.get("name") or "").strip()
    country = str(player.get("country") or "").strip().upper()
    if not name or not country or country == "-":
        return None
    return _slug(name), country


def _resolver_with_entry_cache_fallback(entry_cache, cache, base_resolver):
    """Resolve WTA players from unique ITF name/country observations when needed."""
    candidates = {}

    def add_candidate(player_id, player):
        player_id = str(player_id or "").strip()
        key = _identity_key(player)
        if player_id.startswith("800") and key:
            candidates.setdefault(key, set()).add(player_id)

    for player_id, record in cache.items():
        if isinstance(record, dict):
            add_candidate(player_id, record)
    for key, players in (entry_cache or {}).items():
        if str(key).startswith("http"):
            continue
        for player in players or []:
            add_candidate(player.get("player_id"), player)

    def resolve(player):
        resolved = base_resolver(player)
        if resolved and str(resolved.get("player_id") or "").strip():
            return resolved
        player_ids = candidates.get(_identity_key(player), set())
        if len(player_ids) != 1:
            return None
        return {
            "player_id": next(iter(player_ids)),
            "name": player.get("name", ""),
            "country": player.get("country", ""),
            "type": player.get("type", ""),
        }

    return resolve


def entry_list_wtn_status(entry_cache, cache_path, *, tournament_weeks=None, resolve_itf_player=None):
    """Count WTA entry rows by the age of the WTN value they would display."""
    cache = _normalize_cache(_load_cache(cache_path))
    tournament_weeks = tournament_weeks or {}
    resolve_itf_player = _resolver_with_entry_cache_fallback(
        entry_cache,
        cache,
        resolve_itf_player or _wta_player_with_itf_id,
    )
    counts = {"total": 0, "current_week": 0, "previous_week": 0, "other_week": 0, "missing": 0, "unmapped": 0}
    for key, players in (entry_cache or {}).items():
        if not str(key).startswith("http"):
            continue
        target_week = _week_start(tournament_weeks.get(str(key).removesuffix("#qual")))
        for player in players or []:
            if player.get("type") == "ALT":
                continue
            counts["total"] += 1
            itf_player = resolve_itf_player(player)
            if not itf_player:
                counts["unmapped"] += 1
                counts["missing"] += 1
                continue
            observation = cache.get(str(itf_player["player_id"]), {})
            observed_week = _week_start(_observation_priority(observation)[0])
            previous_week = (
                (date.fromisoformat(target_week) - timedelta(days=7)).isoformat() if target_week else ""
            )
            if _valid_wtn(observation.get("wtn")) and observed_week == target_week:
                counts["current_week"] += 1
            elif _valid_wtn(observation.get("wtn")) and observed_week == previous_week:
                counts["previous_week"] += 1
            elif _valid_wtn(observation.get("wtn")):
                counts["other_week"] += 1
            else:
                counts["missing"] += 1
    return counts


def propagate_wtn(
    entry_cache, cache, draws_store=None, *, resolve_itf_player=None, fresh_singles_only=False, today=None
):
    """Use the player's latest value everywhere, independently of tournament dates."""
    from main_draw_wtn import _resolve_player

    resolver = _resolver_with_entry_cache_fallback(entry_cache, cache, resolve_itf_player or _wta_player_with_itf_id)

    today = today or madrid_today()

    def update(player, key, require_fresh=False):
        # ITF entry-list IDs are authoritative; WTA IDs need resolution.
        pid = str(player.get("player_id") or "") if not str(key).startswith("http") else ""
        resolved = None if pid else _resolve_player(player, key, resolver)
        pid = str(resolved["player_id"]) if resolved else pid
        record = cache.get(pid, {})
        if record.get("no_wtn_checked_at"):
            player["wtn"] = "-"
            return
        if require_fresh and not _select_recent_observation(record, today):
            player["wtn"] = "-"
            return
        if _valid_wtn(record.get("wtn")):
            player["wtn"] = str(record["wtn"])
        else:
            player.setdefault("wtn", "-")

    for key, players in (entry_cache or {}).items():
        for player in players or []:
            update(player, key)
    for key, tournament in (draws_store or {}).items():
        for kind, draw in (tournament.get("draws") or {}).items():
            for player in draw.get("players", []):
                if player.get("members"):
                    for member in player["members"]:
                        update(member, key)
                else:
                    update(player, key, require_fresh=fresh_singles_only and kind in {"MDS", "QS"})


def _wta_player_with_itf_id(player):
    from config import PLAYER_IDENTITY_INDEX

    record = PLAYER_IDENTITY_INDEX.resolve(
        "wta",
        player_id=player.get("player_id"),
        name=player.get("name"),
    )
    if not record or not record.itf_id:
        return None
    return {
        "player_id": record.itf_id,
        "name": record.itf_name or player.get("name") or record.display_name,
        "country": player.get("country") or record.country,
        "type": player.get("type", ""),
    }


def _profile_source(driver, url, settle_seconds):
    driver.get(url)
    if settle_seconds:
        time.sleep(settle_seconds)
    source = driver.page_source or ""
    if len(source) < 5000 and re.search(r"noindex\s*,\s*nofollow", source, re.IGNORECASE):
        raise ITFProfileBlocked("ITF profile request was challenged")
    return source


def _profile_fetcher(driver, settle_seconds, request_interval_seconds):
    """Reuse the browser's verified ITF session for inexpensive profile requests."""
    bootstrapped = False
    browser_cookies = []
    user_agent = ""
    last_request_at = 0.0

    def copy_browser_session():
        nonlocal browser_cookies, user_agent
        browser_cookies = driver.get_cookies()
        user_agent = driver.execute_script("return navigator.userAgent")

    def request_source(url):
        nonlocal last_request_at
        wait = request_interval_seconds - (time.monotonic() - last_request_at)
        if wait > 0:
            time.sleep(wait)
        # ITF's protection rejects repeated requests on one HTTP connection.
        # A short-lived session still reuses the browser-verified cookies.
        with requests.Session() as session:
            for cookie in browser_cookies:
                options = {"path": cookie.get("path") or "/"}
                if cookie.get("domain"):
                    options["domain"] = cookie["domain"]
                session.cookies.set(cookie["name"], cookie["value"], **options)
            response = session.get(url, headers={"User-Agent": user_agent}, timeout=30)
            last_request_at = time.monotonic()
            if len(response.text) < 5000 and re.search(r"noindex\s*,\s*nofollow", response.text, re.IGNORECASE):
                raise ITFProfileBlocked("ITF profile request was challenged")
            if response.status_code in (403, 429):
                raise ITFProfileBlocked(f"ITF profile request returned HTTP {response.status_code}")
            response.raise_for_status()
            return response.text

    def fetch(url):
        nonlocal bootstrapped
        if not bootstrapped:
            source = _profile_source(driver, url, settle_seconds)
            copy_browser_session()
            bootstrapped = True
            return source

        source = request_source(url)
        if "wtnSingles" in source:
            return source

        source = _profile_source(driver, url, settle_seconds)
        copy_browser_session()
        return source

    return fetch


def refresh_entry_list_wtn(
    driver,
    entry_cache,
    cache_path,
    *,
    today=None,
    fetch_source=None,
    resolve_itf_player=None,
    fetch_profiles=True,
    check_new_entry_lists=True,
    fresh_itf_entry_lists=None,
    tournament_weeks=None,
    max_profile_fetches=None,
    profile_batch_size=PROFILE_BATCH_SIZE,
    profile_batch_cooldown_seconds=PROFILE_BATCH_COOLDOWN_SECONDS,
    settle_seconds=0.5,
    request_interval_seconds=REQUEST_INTERVAL_SECONDS,
    include_entry_players=True,
    draw_players=None,
    profile_failures=None,
    include_itf_entry_players=False,
):
    """Refresh and propagate WTNs for WTA and ITF entry-list players."""
    today = today or madrid_today()
    today_text = today.isoformat()
    tournament_weeks = tournament_weeks or {}
    cache = _normalize_cache(_load_cache(cache_path))
    base_resolver = resolve_itf_player or _wta_player_with_itf_id

    seen_path = Path(cache_path).with_name(Path(cache_path).stem.removesuffix("_cache") + "_entry_lists_seen.json")
    seen_lists = _load_cache(seen_path)
    # A live re-read still contains the WTN from the list's publication day.
    # Consume each list only once, including players added on later re-reads.
    for key, players in (fresh_itf_entry_lists or {}).items():
        if str(key).startswith("http") or key in seen_lists or not players:
            continue
        published = tournament_weeks.get(str(key).removesuffix("#qual"))
        observed_on = (
            min(today, date.fromisoformat(str(published)[:10]) - timedelta(days=17)).isoformat()
            if published else today_text
        )
        seen_lists[key] = {"first_seen": today_text, "observed_on": observed_on}
        for player in players or []:
            player_id = str(player.get("player_id") or "").strip()
            value = player.get("wtn")
            if player_id and value not in (None, "", "-"):
                _store_observation(
                    cache,
                    player_id,
                    player,
                    {
                        "wtn": str(value),
                        "source": "entry_list",
                        "source_key": str(key),
                        "retrieved_at": today_text,
                        "observed_on": observed_on,
                    },
                )
    save_json_file(seen_path, seen_lists)

    resolve_itf_player = _resolver_with_entry_cache_fallback(entry_cache, cache, base_resolver)

    players_by_id = {}
    player_entry_lists = {}
    for key, players in (entry_cache or {}).items():
        if not include_entry_players or (not str(key).startswith("http") and not include_itf_entry_players):
            continue
        if not _current_entry_list(key, tournament_weeks, today):
            continue
        for player in players or []:
            if player.get("type") == "ALT":
                continue
            itf_player = ({**player, "player_id": str(player["player_id"])}
                          if not str(key).startswith("http") and player.get("player_id")
                          else resolve_itf_player(player))
            if itf_player:
                player_id = str(itf_player["player_id"])
                player_entry_lists.setdefault(player_id, set()).add(str(key))
                previous = players_by_id.get(player_id)
                if previous is None or (previous.get("type") != "MAIN" and itf_player.get("type") == "MAIN"):
                    players_by_id[player_id] = itf_player

    draw_player_ids = set()
    for player in draw_players or []:
        player_id = str(player["player_id"])
        draw_player_ids.add(player_id)
        players_by_id.setdefault(player_id, player)

    pending_entry_lists = {
        player_id: keys - set(cache.get(player_id, {}).get("entry_lists_checked", {}))
        for player_id, keys in player_entry_lists.items()
    } if check_new_entry_lists else {}
    targets = [
        (player_id, player)
        for player_id, player in players_by_id.items()
        if not _select_recent_observation(cache.get(player_id, {}), today)
        and not _recent_no_wtn_check(cache.get(player_id, {}), today)
    ] if fetch_profiles else []
    targets.sort(key=lambda item: (
        item[0] not in draw_player_ids,
        not bool(pending_entry_lists.get(item[0])),
        item[0] in cache,
        {"MAIN": 0, "QUAL": 1, "ALT": 2}.get(item[1].get("type"), 1),
        item[0],
    ))
    queue_size = len(targets)
    failures = profile_failures if profile_failures is not None else {}
    if max_profile_fetches is not None:
        for player_id, _ in targets[max_profile_fetches:]:
            failures[player_id] = f"Deferred by {max_profile_fetches}-profile run limit"
        targets = targets[:max_profile_fetches]
    succeeded = 0
    if fetch_profiles:
        logger.info("Singles WTN queue: %s unique players, %s profiles need refreshing; attempting %s.",
                    len(players_by_id), queue_size, len(targets))
    source_fetcher = fetch_source or (
        _profile_fetcher(driver, settle_seconds, request_interval_seconds) if targets else None
    )
    if targets and fetch_source is None and profile_batch_cooldown_seconds > 0:
        logger.info(
            f"Pausing ITF WTN profile requests for {profile_batch_cooldown_seconds:g}s before the first request."
        )
        time.sleep(profile_batch_cooldown_seconds)
    for index, (player_id, player) in enumerate(targets, start=1):
        record = cache.get(player_id, {})
        try:
            profile_url = ""
            for candidate_url in player_profile_urls(player, _latest_profile_url(record)):
                try:
                    wtn = parse_wtn_singles(source_fetcher(candidate_url))
                    profile_url = candidate_url
                    break
                except requests.HTTPError as exc:
                    if exc.response is not None and exc.response.status_code == 404:
                        continue
                    raise
                except ValueError:
                    continue
            if not profile_url:
                raise ValueError("ITF profile did not contain a valid singles WTN")
            if not _valid_wtn(wtn):
                _store_no_wtn_check(cache, player_id, player, profile_url, today)
                checkpoint_profile_wtn(cache_path, cache, player_id)
                failures.pop(player_id, None)
                continue
            _store_observation(
                cache,
                player_id,
                player,
                {
                    "wtn": wtn,
                    "source": "profile",
                    "profile_url": profile_url,
                    "retrieved_at": today_text,
                },
            )
            if check_new_entry_lists:
                cache[player_id].setdefault("entry_lists_checked", {}).update(
                    dict.fromkeys(player_entry_lists.get(player_id, ()), today_text)
                )
            checkpoint_profile_wtn(cache_path, cache, player_id)
            failures.pop(player_id, None)
            succeeded += 1
        except ITFProfileBlocked as exc:
            for pending_id, _ in targets[index - 1:]:
                failures[pending_id] = f"ITF profiles blocked: {exc}"
            logger.warning(f"ITF WTN refresh paused after {index - 1} profiles: {exc}")
            break
        except Exception as exc:
            failures[player_id] = str(exc) or type(exc).__name__
            logger.warning(f"ITF WTN profile failed for {player.get('name', player_id)}: {exc}")
        if profile_batch_size > 0 and index % profile_batch_size == 0:
            save_json_file(cache_path, cache)
            logger.info(f"Refreshing ITF WTN profiles ({index}/{len(targets)}).")
            if index < len(targets) and profile_batch_cooldown_seconds > 0:
                logger.info(
                    f"Pausing ITF WTN profile requests for {profile_batch_cooldown_seconds:g}s."
                )
                time.sleep(profile_batch_cooldown_seconds)

    save_json_file(cache_path, cache)
    if fetch_profiles:
        logger.info("Singles WTN refresh: %s updated, %s unavailable or unattempted.",
                    succeeded, queue_size - succeeded)
    propagate_wtn(entry_cache, cache, resolve_itf_player=resolve_itf_player)
    return cache


def refresh_draw_wtn(
    driver, draws_store, cache_path, *, entry_cache=None, today=None, resolve_itf_player=None,
    main_draws=None, archive_path=None, include_entry_players=False, profile_failures=None, **profile_options
):
    """Refresh stale or missing WTNs for active singles draws, never doubles."""
    from main_draw_wtn import _draw_observation, _player_name, _resolve_player, _roster
    from run_state import report_run_issue

    today = today or madrid_today()
    entry_cache = entry_cache or {}
    cache = _normalize_cache(_load_cache(cache_path))
    resolver = _resolver_with_entry_cache_fallback(
        entry_cache, cache, resolve_itf_player or _wta_player_with_itf_id
    )
    targets = {}
    locations = {}
    unresolved = []
    archive = (
        json.loads(Path(archive_path).read_text(encoding="utf-8-sig"))
        if archive_path and Path(archive_path).exists() else {}
    )
    # Pending GM draws can be hidden on the website or outside the active week.
    pending = []
    for key, tournament in main_draws or []:
        _, _, fingerprint = _roster((tournament.get("draws") or {}).get("MDS") or {})
        previous = archive.get(key, {})
        if previous.get("status") != "complete" or previous.get("rosterHash") != fingerprint:
            pending.append((key, tournament))
    for key, tournament, for_gm in [
        *((key, tournament, False) for key, tournament in (draws_store or {}).items()),
        *((key, tournament, True) for key, tournament in pending),
    ]:
        meta = tournament.get("meta") or tournament
        end = str(meta.get("endDate") or "")[:10]
        if not for_gm and end and end < today.isoformat():
            continue
        for kind, draw in (tournament.get("draws") or {}).items():
            if kind not in {"MDS", "QS"} or (for_gm and kind != "MDS"):
                continue
            for player in draw.get("players", []):
                name = _player_name(player)
                if not name or name.casefold() in {
                    "qualifier", "bye", "q", "tbd", "(available slot)", "(special exempt)"
                }:
                    continue
                resolved = _resolve_player(player, key, resolver)
                if not resolved:
                    unresolved.append({
                        "name": name, "tournament": key, "tournament_name": meta.get("name", key),
                        "draw": kind, "reason": "ITF profile not identified: no unambiguous ITF player ID",
                    })
                    continue
                pid = str(resolved["player_id"])
                record = cache.get(pid, {})
                needs_refresh = (not _draw_observation(record, key, today=today) if for_gm
                                 else not _select_recent_observation(record, today))
                if needs_refresh and not _recent_no_wtn_check(record, today):
                    targets.setdefault(pid, {**resolved, "type": "MAIN" if kind == "MDS" else "QUAL"})
                    locations.setdefault(pid, []).append({
                        "tournament": key, "tournament_name": meta.get("name", key), "draw": kind,
                    })
    failures = profile_failures if profile_failures is not None else {}
    cache = refresh_entry_list_wtn(
        driver, entry_cache, cache_path, today=today, resolve_itf_player=resolver,
        include_entry_players=include_entry_players, draw_players=list(targets.values()), check_new_entry_lists=False,
        profile_failures=failures,
        include_itf_entry_players=include_entry_players,
        **profile_options,
    )
    if include_entry_players:
        for key, players in entry_cache.items():
            if not _current_entry_list(key, profile_options.get("tournament_weeks"), today):
                continue
            for player in players or []:
                if player.get("type") == "ALT":
                    continue
                name = _player_name(player)
                if not name or name.casefold() in {
                    "qualifier", "bye", "q", "tbd", "(available slot)", "(special exempt)"
                }:
                    continue
                resolved = _resolve_player(player, str(key), resolver)
                pid = str(resolved["player_id"]) if resolved else ""
                if not pid:
                    unresolved.append({"name": name, "tournament": key, "draw": "entry",
                                       "reason": "ITF profile not identified: no unambiguous ITF player ID"})
                if pid in failures and pid not in targets and not _recent_no_wtn_check(cache.get(pid, {}), today):
                    unresolved.append({"name": _player_name(player), "itf_id": pid,
                                       "tournament": key, "draw": "entry",
                                       "profile_url": (
                                           _latest_profile_url(cache.get(pid, {})) or player_profile_url(resolved)
                                       ),
                                       "reason": failures[pid]})
    for pid, player in targets.items():
        if not _select_recent_observation(cache.get(pid, {}), today) and not _recent_no_wtn_check(
            cache.get(pid, {}), today
        ):
            unresolved.extend(
                {**location, "name": player.get("name", pid), "itf_id": pid,
                 "profile_url": _latest_profile_url(cache.get(pid, {})) or player_profile_url(player),
                 "reason": failures.get(pid, "Fresh singles WTN unavailable")}
                for location in locations[pid]
            )
    unresolved = list({
        (player.get("itf_id") or player["name"].casefold(), player["tournament"], player["draw"]): player
        for player in unresolved
    }.values())
    if unresolved:
        report_run_issue(
            "itf-wtn", "refresh active singles draw WTNs",
            ValueError(f"Fresh WTN unavailable for {len(unresolved)} singles draw players"),
            severity="degraded", context={"players": unresolved},
        )
    propagate_wtn(
        entry_cache, cache, draws_store, resolve_itf_player=resolver, fresh_singles_only=True, today=today
    )
    return cache
