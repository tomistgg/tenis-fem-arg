"""Archive a profile-based singles WTN geometric mean for every main draw."""

import hashlib
import json
import math
import re
import time
from pathlib import Path

import requests

from draws import fetch_itf_tournament_draws, fetch_tournament_draws
from itf_wtn import (
    PROFILE_BATCH_COOLDOWN_SECONDS,
    PROFILE_BATCH_SIZE,
    REQUEST_INTERVAL_SECONDS,
    ITFProfileBlocked,
    _latest_profile_url,
    _load_cache,
    _normalize_cache,
    _profile_fetcher,
    _resolver_with_entry_cache_fallback,
    _store_observation,
    _week_start,
    _wta_player_with_itf_id,
    parse_wtn_singles,
    player_profile_urls,
)
from run_state import report_run_issue
from runtime_logging import get_logger
from time_utils import madrid_today
from utils import normalize_player_name, save_json_file

ARCHIVE_FILENAME = "main_draw_wtn_gm.json"
ERRORS_FILENAME = "main_draw_wtn_errors.json"
logger = get_logger("main-draw-wtn")


def _key(value):
    text = str(value or "")
    return text.lower() if text.lower().startswith("w-itf-") else text


def _player_name(player):
    name = str(player.get("name") or "").strip()
    if "," in name:
        family, given = name.split(",", 1)
        name = f"{given.strip()} {family.strip()}"
    return name


def _positive_wtn(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 < number <= 40 else None


def _roster(draw):
    players = [p for p in draw.get("players", []) if _player_name(p)]
    # Qualifier placeholders and byes are positions, not players with profiles.
    players = [p for p in players if _player_name(p).casefold() not in {"qualifier", "bye", "q", "tbd"}]
    positions = {int(p["pos"]) for p in players}
    byes = {int(pos) for pos in draw.get("byes", [])}
    size = int(draw.get("draw_size") or len(players) + len(byes))
    unfilled = len(set(range(1, size + 1)) - positions - byes)
    fingerprint = hashlib.sha256(
        json.dumps(
            [
                sorted(
                    (
                        int(p["pos"]),
                        normalize_player_name(_player_name(p)),
                        p.get("country", ""),
                        str(p.get("itf_id") or ""),
                    )
                    for p in players
                ),
                sorted(byes),
                size,
            ],
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    return players, unfilled, fingerprint


def collect_main_draws(
    driver,
    wta_groups,
    itf_groups,
    draws_store,
    prefetched=None,
    *,
    today=None,
    archive_path=None,
    itf_tournament_ids=None,
):
    """Yield all published main draws, independently of website ARG filters."""
    today = today or madrid_today()
    seen = set()
    prefetched = prefetched or {}
    itf_tournament_ids = itf_tournament_ids or {}
    wta_groups = {week: dict(events) for week, events in (wta_groups or {}).items()}
    itf_groups = {week: dict(events) for week, events in (itf_groups or {}).items()}
    if archive_path and Path(archive_path).exists():
        archive = json.loads(Path(archive_path).read_text(encoding="utf-8"))
        active = {_key(key) for groups in (wta_groups, itf_groups) for events in groups.values() for key in events}
        for key, record in archive.items():
            if record.get("status") == "pending" and key not in active:
                groups = itf_groups if key.startswith("w-itf-") else wta_groups
                groups.setdefault("Pending WTN", {})[key] = record
    for source, groups in (("WTA", wta_groups), ("ITF", itf_groups)):
        for week, tournaments in (groups or {}).items():
            for raw_key, info in tournaments.items():
                key = _key(raw_key)
                seen.add(key)
                cached = draws_store.get(key) or {}
                draw = (cached.get("draws") or {}).get("MDS")
                if source == "ITF":
                    # The website parser can discard a main draw without ARG
                    # players. Parse the raw ITF cache with that filter disabled.
                    tid = info.get("tournamentId") or itf_tournament_ids.get(key)
                    if tid:
                        info = {**info, "tournamentId": tid}
                        try:
                            all_draws = fetch_itf_tournament_draws(
                                tid,
                                is_multiweek=info.get("is_multiweek", False),
                                driver=driver,
                                tournament_name=info.get("name", key),
                                draw_types=["MDS"],
                                include_all_players=True,
                            )
                            draw = (all_draws or {}).get("MDS") or draw
                        except Exception as exc:
                            report_run_issue("main-draw-wtn", "fetch main draw", exc, context={"key": key})
                    draw = draw or (prefetched.get(key) or {}).get("MDS")
                elif not draw:
                    try:
                        year = re.search(r"/tournaments/\d+/[^/]+/(\d{4})/", key)
                        fetched = fetch_tournament_draws(
                            key,
                            year.group(1) if year else str(today.year),
                            start_date=info.get("startDate"),
                            draw_types=["MDS"],
                            poll_early=True,
                        )
                        draw = (fetched or {}).get("MDS")
                    except Exception as exc:
                        report_run_issue("main-draw-wtn", "fetch main draw", exc, context={"key": key})
                if draw and draw.get("players"):
                    if (cached.get("draws") or {}).get("MDS"):
                        cached["draws"]["MDS"] = draw
                    yield key, {**info, "week": week, "source": source, "draws": {"MDS": draw}}
    # Capture existing draws before the website prunes finished events.
    for raw_key, tournament in (draws_store or {}).items():
        key = _key(raw_key)
        if key not in seen and (tournament.get("draws") or {}).get("MDS", {}).get("players"):
            yield key, {**tournament, "tournamentId": tournament.get("tournamentId") or itf_tournament_ids.get(key)}


def _resolve_player(player, tournament_key, resolver):
    name = _player_name(player)
    player_id = str(player.get("itf_id") or "").strip()
    if not player_id and tournament_key.startswith("w-itf-"):
        player_id = str(player.get("player_id") or "").strip()
    if player_id:
        return {**player, "player_id": player_id, "name": name}
    resolved = resolver({**player, "name": name})
    if not resolved and str(player.get("country") or "").strip() in {"", "-"}:
        # Some WTA PDF rows omit the country. A unique canonical identity can
        # supply it before the existing name/country ITF-cache lookup.
        from config import PLAYER_IDENTITY_INDEX

        identity = PLAYER_IDENTITY_INDEX.resolve("wta", name=name)
        if identity and identity.country:
            resolved = resolver({**player, "name": name, "country": identity.country})
    return resolved


def _restore_profile_wtn(players, key, profiles, resolver):
    """Restore display WTNs when a refreshed draw has replaced its player rows."""
    for player in players:
        resolved = _resolve_player(player, key, resolver)
        if not resolved:
            continue
        observations = profiles.get(str(resolved["player_id"]), {}).get("weeks", {})
        for _, observation in sorted(observations.items(), reverse=True):
            if observation.get("source") == "profile" and _positive_wtn(observation.get("wtn")) is not None:
                player["wtn"] = str(observation["wtn"])
                break


def refresh_main_draw_wtn(
    driver,
    tournaments,
    archive_path,
    profile_cache_path,
    errors_path,
    *,
    entry_cache=None,
    today=None,
    fetch_source=None,
    resolve_itf_player=None,
    profile_batch_size=PROFILE_BATCH_SIZE,
    profile_batch_cooldown_seconds=PROFILE_BATCH_COOLDOWN_SECONDS,
):
    """Freeze complete roster means; retry missing profiles and changed rosters.

    Individual observations remain in the existing operational profile cache.
    The permanent tournament archive contains aggregates and metadata only.
    No entry-list WTN or stale profile WTN can substitute for a live lookup.
    """
    today = today or madrid_today()
    archive_file = Path(archive_path)
    # A damaged archive must fail visibly instead of discarding historical GMs.
    archive = json.loads(archive_file.read_text(encoding="utf-8")) if archive_file.exists() else {}
    if not isinstance(archive, dict):
        raise ValueError("Main-draw WTN archive must be an object")
    profiles = _normalize_cache(_load_cache(profile_cache_path))
    resolver = _resolver_with_entry_cache_fallback(
        entry_cache or {},
        profiles,
        resolve_itf_player or _wta_player_with_itf_id,
    )
    source_fetcher = fetch_source
    fetched_players = {}
    attempts = 0
    blocked_reason = ""
    errors = []

    for raw_key, tournament in tournaments:
        key = _key(raw_key)
        draw = (tournament.get("draws") or {}).get("MDS") or {}
        players, unfilled, fingerprint = _roster(draw)
        if not players:
            continue
        previous = archive.get(key) or {}
        if previous.get("status") == "complete" and previous.get("rosterHash") == fingerprint:
            _restore_profile_wtn(players, key, profiles, resolver)
            draw["wtn_gm"] = previous["wtn_gm"]
            continue
        values = []
        missing = []
        for player in players:
            name = _player_name(player)
            resolved = _resolve_player(player, key, resolver)
            if not resolved:
                missing.append({"name": name, "reason": "No unambiguous ITF player ID"})
                continue
            player_id = str(resolved["player_id"])
            if player_id not in fetched_players:
                wtn, url, reason = None, "", blocked_reason
                if not blocked_reason:
                    if attempts and profile_batch_size > 0 and attempts % profile_batch_size == 0:
                        save_json_file(profile_cache_path, profiles)
                        if profile_batch_cooldown_seconds > 0:
                            time.sleep(profile_batch_cooldown_seconds)
                    attempts += 1
                    if source_fetcher is None:
                        source_fetcher = _profile_fetcher(driver, 0.5, REQUEST_INTERVAL_SECONDS)
                    try:
                        for candidate in player_profile_urls(
                            resolved, _latest_profile_url(profiles.get(player_id, {}))
                        ):
                            url = candidate
                            try:
                                wtn = _positive_wtn(parse_wtn_singles(source_fetcher(candidate)))
                                if wtn is not None:
                                    break
                            except requests.HTTPError as exc:
                                if exc.response is None or exc.response.status_code != 404:
                                    raise
                            except ValueError:
                                continue
                        if wtn is None:
                            reason = "ITF profile has no valid singles WTN"
                        else:
                            _store_observation(
                                profiles,
                                player_id,
                                resolved,
                                _week_start(today),
                                {
                                    "wtn": wtn,
                                    "source": "profile",
                                    "profile_url": url,
                                    "retrieved_at": today.isoformat(),
                                },
                            )
                    except ITFProfileBlocked as exc:
                        blocked_reason = f"ITF profiles blocked: {exc}"
                        reason = blocked_reason
                    except Exception as exc:
                        reason = str(exc) or type(exc).__name__
                fetched_players[player_id] = (wtn, url, reason)
            wtn, url, reason = fetched_players[player_id]
            if wtn is None:
                missing.append({"name": name, "itf_id": player_id, "profile_url": url, "reason": reason})
            else:
                values.append(wtn)
                player["wtn"] = str(wtn)

        complete = not missing and not unfilled
        archive[key] = {
            "name": tournament.get("name", key),
            "level": tournament.get("level", ""),
            "startDate": tournament.get("startDate", ""),
            "endDate": tournament.get("endDate", ""),
            "tournamentId": tournament.get("tournamentId"),
            "is_multiweek": tournament.get("is_multiweek", False),
            "draw": "MDS",
            "wtn_gm": round(math.exp(sum(math.log(v) for v in values) / len(values)), 4) if complete else None,
            "playerCount": len(players),
            "wtnPlayerCount": len(values),
            "unfilledPositions": unfilled,
            "status": "complete" if complete else "pending",
            "observedOn": today.isoformat(),
            "source": "ITF player profiles",
            "rosterHash": fingerprint,
        }
        draw["wtn_gm"] = archive[key]["wtn_gm"]
        if missing:
            errors.append({"tournament_key": key, "tournament_name": tournament.get("name", key), "players": missing})
            logger.warning(
                "Main-draw WTN missing for %s: %s", tournament.get("name", key), ", ".join(p["name"] for p in missing)
            )
        save_json_file(archive_path, archive)
    save_json_file(profile_cache_path, profiles)
    save_json_file(archive_path, archive)
    save_json_file(errors_path, errors)
    return archive
