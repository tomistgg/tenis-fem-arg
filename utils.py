import os
import json
import re
import unicodedata
import tempfile

import csv

from config import (
    TOURNAMENT_NAME_OVERRIDES, CITY_CASE_FIXES,
    COUNTRY_TO_CONTINENT, COUNTRY_OVERRIDES
)
from time_utils import utc_timestamp
from runtime_logging import get_logger
from runtime_paths import DATA_DIR

logger = get_logger("utils")
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_CACHE_STATE_FILE = os.path.join(DATA_DIR, "cache_state.json")
_DRAWS_STORE_CACHE_FILE = os.path.join(DATA_DIR, "draws_store_cache.json")
_CACHE_STATE_CACHE = None


def format_player_name(text):
    if not text:
        return ""
    return text.title()


# Common UTF-8-decoded-as-Latin-1 mojibake indicators
_MOJIBAKE_MARKERS = ('\u00c3', '\u00c3\u00a1', '\u00c3\u00a9', '\u00c3\u00ad', '\u00c3\u00b3', '\u00c3\u00ba')


def _repair_mojibake(text):
    """Re-decode text that was mistakenly decoded as Latin-1 instead of UTF-8."""
    if any(m in text for m in _MOJIBAKE_MARKERS):
        try:
            return text.encode('latin-1').decode('utf-8')
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    return text


def fix_encoding(text):
    """Fix encoding issues and normalize special characters (strips accents)."""
    if not text:
        return ""
    text = _repair_mojibake(text)
    try:
        nfkd_form = unicodedata.normalize('NFKD', text)
        return "".join(c for c in nfkd_form if not unicodedata.combining(c))
    except Exception:
        return text


def fix_encoding_keep_accents(text):
    """Fix encoding issues but preserve accents."""
    if not text:
        return ""
    return _repair_mojibake(text)


def normalize_player_name(text):
    """Normalize a player name for matching: repair encoding, strip accents,
    uppercase, and collapse internal whitespace. Used by tstrength and draws
    when matching player rows across data sources."""
    return re.sub(r"\s+", " ", fix_encoding(text or "").upper()).strip()


def write_text_if_changed(path, text, encoding="utf-8"):
    """Write text only when the file content actually changes."""
    try:
        with open(path, "r", encoding=encoding, newline="") as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass

    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    base_name = os.path.basename(path) or "cache"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=encoding,
            newline="\n",
            delete=False,
            dir=directory,
            prefix=f".{base_name}.",
            suffix=".tmp",
        ) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
            tmp_path = f.name
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise
    return True


def _cache_state_key(cache_file):
    return os.path.basename(str(cache_file or "").strip())


def _load_cache_state():
    global _CACHE_STATE_CACHE
    if isinstance(_CACHE_STATE_CACHE, dict):
        return _CACHE_STATE_CACHE

    state = {"files": {}, "entries": {}}
    try:
        with open(_CACHE_STATE_FILE, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except FileNotFoundError:
        raw = {}
    except Exception:
        raw = {}

    if isinstance(raw, dict):
        files = raw.get("files")
        entries = raw.get("entries")
        if isinstance(files, dict):
            state["files"] = files
        if isinstance(entries, dict):
            state["entries"] = entries
        # Backward compatibility: if the file was written as a flat mapping,
        # keep dict-valued keys usable under the new "files" section.
        if not state["files"] and not state["entries"]:
            state["files"] = {
                key: value for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, dict)
            }

    _CACHE_STATE_CACHE = state
    return state


def _save_cache_state():
    state = _load_cache_state()
    write_text_if_changed(
        _CACHE_STATE_FILE,
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def get_cache_file_meta(cache_file, default=None):
    state = _load_cache_state()
    meta = state.get("files", {}).get(_cache_state_key(cache_file))
    if isinstance(meta, dict):
        return dict(meta)
    if isinstance(default, dict):
        return dict(default)
    return default if default is not None else {}


def set_cache_file_meta(cache_file, **meta):
    key = _cache_state_key(cache_file)
    if not key:
        return
    state = _load_cache_state()
    files = state.setdefault("files", {})
    current = files.get(key) if isinstance(files.get(key), dict) else {}
    updated = dict(current)
    changed = False
    for meta_key, meta_value in meta.items():
        if meta_value is None:
            continue
        if updated.get(meta_key) != meta_value:
            updated[meta_key] = meta_value
            changed = True
    if not changed and isinstance(current, dict) and updated == current:
        return
    files[key] = updated
    _save_cache_state()


def get_cache_entry_meta(cache_file, entry_key, default=None):
    state = _load_cache_state()
    file_key = _cache_state_key(cache_file)
    entry_map = state.get("entries", {}).get(file_key, {})
    meta = entry_map.get(str(entry_key or ""))
    if isinstance(meta, dict):
        return dict(meta)
    if isinstance(default, dict):
        return dict(default)
    return default if default is not None else {}


def set_cache_entry_meta(cache_file, entry_key, **meta):
    file_key = _cache_state_key(cache_file)
    entry_name = str(entry_key or "")
    if not file_key or not entry_name:
        return
    state = _load_cache_state()
    entries = state.setdefault("entries", {})
    entry_map = entries.get(file_key)
    if not isinstance(entry_map, dict):
        entry_map = {}
    current = entry_map.get(entry_name) if isinstance(entry_map.get(entry_name), dict) else {}
    updated = dict(current)
    changed = False
    for meta_key, meta_value in meta.items():
        if meta_value is None:
            continue
        if updated.get(meta_key) != meta_value:
            updated[meta_key] = meta_value
            changed = True
    if not changed and isinstance(current, dict) and updated == current:
        return
    entry_map[entry_name] = updated
    entries[file_key] = entry_map
    _save_cache_state()


def remove_cache_entry_meta(cache_file, entry_key):
    file_key = _cache_state_key(cache_file)
    entry_name = str(entry_key or "")
    if not file_key or not entry_name:
        return
    state = _load_cache_state()
    entry_map = state.get("entries", {}).get(file_key)
    if not isinstance(entry_map, dict) or entry_name not in entry_map:
        return
    del entry_map[entry_name]
    if entry_map:
        state["entries"][file_key] = entry_map
    else:
        state["entries"].pop(file_key, None)
    _save_cache_state()


def is_draw_completed(draw_key):
    """Return True when a draw/tournament has been marked complete."""
    meta = get_cache_entry_meta(_DRAWS_STORE_CACHE_FILE, draw_key)
    if not isinstance(meta, dict):
        return False
    return bool(str(meta.get("completedAt") or "").strip())


def mark_draw_completed(draw_key, completed_at=None):
    """Persist a completion marker for a draw/tournament cache key."""
    key = str(draw_key or "").strip()
    if not key:
        return
    completed_at = completed_at or utc_timestamp()
    set_cache_entry_meta(_DRAWS_STORE_CACHE_FILE, key, completedAt=completed_at)


_CACHE_TIMESTAMP_KEYS = ("fetchedAt", "updatedAt", "lastUpdated", "generatedAt")


def utc_now_iso():
    """Return a compact UTC timestamp for cache/status metadata."""
    return utc_timestamp()


def make_data_status(
    source,
    status,
    *,
    requested=None,
    effective=None,
    fetched_at=None,
    row_count=None,
    stale=False,
    reason=None,
):
    """Build user-safe data freshness metadata for generated pages."""
    payload = {
        "source": source,
        "status": status,
        "stale": bool(stale),
    }
    optional = {
        "requestedDate": requested,
        "effectiveDate": effective,
        "fetchedAt": fetched_at,
        "rowCount": row_count,
        "reason": reason,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload


def get_cache_timestamp(cache_file, entry_key=None, payload=None):
    """Return the freshest timestamp for a cache entry or file."""
    meta = get_cache_entry_meta(cache_file, entry_key) if entry_key is not None else get_cache_file_meta(cache_file)
    if isinstance(meta, dict):
        for key in _CACHE_TIMESTAMP_KEYS:
            value = meta.get(key)
            if value:
                return value

    if isinstance(payload, dict):
        for key in _CACHE_TIMESTAMP_KEYS:
            value = payload.get(key)
            if value:
                return value
        if entry_key is None:
            return None
        entry = payload.get(entry_key)
        if isinstance(entry, dict):
            for key in _CACHE_TIMESTAMP_KEYS:
                value = entry.get(key)
                if value:
                    return value
    return None


def load_cache(cache_file, *, strict=False):
    """Load cache from JSON file. Returns {} if the file does not exist."""
    try:
        with open(cache_file, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        if strict:
            raise
        logger.warning(f"Warning: ignoring unreadable cache {cache_file}: {e}")
        return {}


def save_cache(cache_file, cache_data, formatter=None):
    """Save rankings cache to JSON file"""
    save_json_file(cache_file, cache_data, formatter=formatter)


def merge_entry_list(cached_players, new_players):
    """Merge new scraped players with cached players, preserving sections that disappeared."""
    if isinstance(cached_players, dict):
        cached_players = [
            row for section in _ENTRY_LISTS_CACHE_SECTION_ORDER
            for row in (cached_players.get(section) or [])
            if isinstance(row, dict)
        ]
    if isinstance(new_players, dict):
        new_players = [
            row for section in _ENTRY_LISTS_CACHE_SECTION_ORDER
            for row in (new_players.get(section) or [])
            if isinstance(row, dict)
        ]

    cached_players = [p for p in (cached_players or []) if isinstance(p, dict)]
    new_players = [p for p in (new_players or []) if isinstance(p, dict)]

    new_main = [p for p in new_players if p.get("type") == "MAIN"]
    new_qual = [p for p in new_players if p.get("type") == "QUAL"]
    new_alt = [p for p in new_players if p.get("type") == "ALT"]
    cached_main = [p for p in cached_players if p.get("type") == "MAIN"]
    cached_qual = [p for p in cached_players if p.get("type") == "QUAL"]
    cached_alt = [p for p in cached_players if p.get("type") == "ALT"]
    final_main = new_main if new_main else cached_main
    final_qual = new_qual if new_qual else cached_qual
    final_alt = new_alt if new_alt else cached_alt
    return final_main + final_qual + final_alt


def fix_display_name(name):
    """Apply tournament name overrides and city casing fixes."""
    base = name.split(" Qualifying")[0]
    is_qual = name.endswith(" Qualifying")
    if base in TOURNAMENT_NAME_OVERRIDES:
        name = TOURNAMENT_NAME_OVERRIDES[base] + (" Qualifying" if is_qual else "")
    for wrong, right in CITY_CASE_FIXES.items():
        name = name.replace(wrong, right)
    return name


def get_tournament_sort_order(level):
    level_order = {
        "GrandSlam": 0, "Grand Slam": 0, "grandSlam": 0,
        "WTA1000": 1, "WTA 1000": 1,
        "WTA500": 2, "WTA 500": 2,
        "WTA250": 3, "WTA 250": 3,
        "WTA125": 4, "WTA 125": 4,
        "W100": 5, "W75": 6, "W60": 7,
        "W50": 8, "W35": 9, "W25": 10, "W15": 11
    }
    return level_order.get(level, 99)


def get_continent(country_code):
    """Map country code to continent key."""
    return COUNTRY_TO_CONTINENT.get((country_code or "").upper(), "europe")


def get_calendar_column(level):
    """Map tournament level to one of the 4 calendar columns."""
    lv = level.lower().replace(" ", "")
    if lv == "grandslam":
        return "gs"
    if lv in ("wta1000", "wta500", "wta250", "finals", "wtafinals"):
        return "wta_tour"
    if lv in ("wta125",):
        return "wta_125"
    return "itf"


def get_surface_class(surface):
    """Map surface string to CSS class."""
    s = (surface or "").lower()
    if "clay" in s:
        return "cal-clay"
    elif "carpet" in s:
        return "cal-carpet"
    elif "grass" in s:
        return "cal-grass"
    else:
        return "cal-hard"


def dumps_readable(payload, *, ensure_ascii=False, indent=2, list_item_indent=2, dict_item_indent=2):
    """Serialize JSON with top-level lists and dicts written one item per line."""
    if isinstance(payload, list):
        if not payload:
            return "[]"
        lines = ["["]
        for i, item in enumerate(payload):
            comma = "," if i < len(payload) - 1 else ""
            lines.append(
                " " * list_item_indent
                + json.dumps(item, ensure_ascii=ensure_ascii)
                + comma
            )
        lines.append("]")
        return "\n".join(lines)
    if isinstance(payload, dict):
        if not payload:
            return "{}"
        lines = ["{"]
        items = list(payload.items())
        for i, (key, value) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            lines.append(
                " " * dict_item_indent
                + json.dumps(key, ensure_ascii=ensure_ascii)
                + ": "
                + json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"))
                + comma
            )
        lines.append("}")
        return "\n".join(lines)
    return json.dumps(payload, ensure_ascii=ensure_ascii, indent=indent)


def compress_wta_rankings_bundle(payload):
    """Compress WTA rankings into shared player + per-date index rows."""
    if not isinstance(payload, dict):
        return payload
    if set(payload.keys()) <= {"p", "d"} and isinstance(payload.get("p"), list) and isinstance(payload.get("d"), dict):
        return payload

    players = []
    player_index = {}
    dates = {}

    for date_str, rows in payload.items():
        if not isinstance(rows, list):
            continue
        compressed_rows = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            player_key = (row.get("n", ""), row.get("c", ""), row.get("d", ""))
            idx = player_index.get(player_key)
            if idx is None:
                idx = len(players)
                player_index[player_key] = idx
                players.append([player_key[0], player_key[1], player_key[2]])
            compressed_rows.append([row.get("r"), row.get("pts", 0), idx])
        dates[date_str] = compressed_rows

    return {"p": players, "d": dates}


def dumps_wta_rankings_bundle(payload, *, ensure_ascii=False, indent=2):
    """Serialize WTA rankings as a readable self-decoding JS bundle."""
    compact = compress_wta_rankings_bundle(payload)
    if not isinstance(compact, dict):
        return json.dumps(compact, ensure_ascii=ensure_ascii)

    def _dump_row(row):
        return json.dumps(row, ensure_ascii=ensure_ascii, separators=(",", ":"))

    def _dump_rows(rows, level):
        pad = " " * level
        if not rows:
            return [pad + "[]"]
        lines = [pad + "["]
        item_pad = " " * (level + indent)
        for i, row in enumerate(rows):
            comma = "," if i < len(rows) - 1 else ""
            lines.append(item_pad + _dump_row(row) + comma)
        lines.append(pad + "]")
        return lines

    def _dump_players(players, level):
        return _dump_rows(players, level)

    def _dump_dates(dates, level):
        pad = " " * level
        if not dates:
            return [pad + "{}"]
        lines = [pad + "{"]
        items = list(dates.items())
        for i, (date_str, rows) in enumerate(items):
            comma = "," if i < len(items) - 1 else ""
            key_text = json.dumps(date_str, ensure_ascii=ensure_ascii)
            lines.append(pad + " " * indent + f"{key_text}: [")
            item_pad = " " * (level + indent * 2)
            for j, row in enumerate(rows or []):
                row_comma = "," if j < len(rows) - 1 else ""
                lines.append(item_pad + _dump_row(row) + row_comma)
            lines.append(pad + " " * indent + f"]{comma}")
        lines.append(pad + "}")
        return lines

    players = compact.get("p") or []
    dates = compact.get("d") or {}

    lines = [
        "(function() {",
        "  const b = {",
        '    "p": [',
    ]
    player_lines = _dump_players(players, 6)
    if len(player_lines) > 1:
        lines.extend(player_lines[1:-1])
    lines.extend([
        "    ],",
        '    "d": {',
    ])
    date_lines = _dump_dates(dates, 6)
    if len(date_lines) > 1:
        lines.extend(date_lines[1:-1])
    lines.extend([
        "    }",
        "  };",
        "  if (!b || typeof b !== 'object') return b;",
        "  const p = Array.isArray(b.p) ? b.p : null;",
        "  const d = b.d && typeof b.d === 'object' && !Array.isArray(b.d) ? b.d : null;",
        "  if (!p || !d) return b;",
        "  const o = {};",
        "  for (const [date, rows] of Object.entries(d)) {",
        "    if (!Array.isArray(rows)) {",
        "      o[date] = rows;",
        "      continue;",
        "    }",
        "    o[date] = rows.map(row => {",
        "      if (!Array.isArray(row)) return row;",
        "      const idx = Number.isInteger(row[2]) ? row[2] : -1;",
        "      const pl = idx >= 0 && idx < p.length ? p[idx] : [];",
        "      return { r: row[0] ?? null, pts: row[1] ?? 0, n: pl[0] || '', c: pl[1] || '', d: pl[2] || '' };",
        "    });",
        "  }",
        "  return o;",
        "})()",
    ])
    return "\n".join(lines)


def dumps_draws_store_cache(payload, *, ensure_ascii=False, indent=2):
    """Serialize draws_store_cache.json with readable nested object blocks.

    Lists stay compact per item so player and match rows remain one line each,
    while nested dicts are expanded enough to make tournament and draw sections
    easy to scan in diffs.
    """
    payload = compress_draws_store_cache(payload)
    def _is_primitive(value):
        return value is None or isinstance(value, (bool, int, float, str))

    def _inline_dict(value):
        if not isinstance(value, dict) or not value:
            return False
        if not all(_is_primitive(v) for v in value.values()):
            return False
        compact = json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"))
        return len(compact) <= 96

    def _render(value, level):
        pad = " " * level
        child_pad = " " * (level + indent)
        if isinstance(value, dict):
            if not value:
                return [pad + "{}"]
            lines = [pad + "{"]
            items = list(value.items())
            for i, (key, child) in enumerate(items):
                comma = "," if i < len(items) - 1 else ""
                key_text = json.dumps(key, ensure_ascii=ensure_ascii)
                if _inline_dict(child):
                    lines.append(
                        f"{child_pad}{key_text}: "
                        + json.dumps(child, ensure_ascii=ensure_ascii, separators=(",", ":"))
                        + comma
                    )
                elif isinstance(child, dict):
                    lines.append(f"{child_pad}{key_text}:")
                    child_lines = _render(child, level + indent)
                    if comma:
                        child_lines[-1] += comma
                    lines.extend(child_lines)
                elif isinstance(child, list):
                    if not child:
                        lines.append(f"{child_pad}{key_text}: []{comma}")
                    else:
                        lines.append(f"{child_pad}{key_text}: [")
                        item_pad = " " * (level + indent * 2)
                        for j, item in enumerate(child):
                            item_comma = "," if j < len(child) - 1 else ""
                            lines.append(
                                item_pad
                                + json.dumps(item, ensure_ascii=ensure_ascii)
                                + item_comma
                            )
                        lines.append(f"{child_pad}]{comma}")
                else:
                    lines.append(
                        f"{child_pad}{key_text}: "
                        + json.dumps(child, ensure_ascii=ensure_ascii)
                        + comma
                    )
            lines.append(pad + "}")
            return lines
        if isinstance(value, list):
            if not value:
                return [pad + "[]"]
            lines = [pad + "["]
            item_pad = " " * (level + indent)
            for i, item in enumerate(value):
                comma = "," if i < len(value) - 1 else ""
                lines.append(item_pad + json.dumps(item, ensure_ascii=ensure_ascii) + comma)
            lines.append(pad + "]")
            return lines
        return [pad + json.dumps(value, ensure_ascii=ensure_ascii)]

    return "\n".join(_render(payload, 0))


_ENTRY_LISTS_CACHE_PLAYER_FIELDS = (
    "pos",
    "name",
    "country",
    "rank",
    "priority",
    "pos_num",
    "entry",
    "seed_rank",
    "seed",
    # Appended for backwards-compatible expansion of older compact rows.
    "player_id",
)
_ENTRY_LISTS_CACHE_SECTION_ORDER = ("MAIN", "QUAL", "ALT")
_ENTRY_LISTS_CACHE_META_KEYS = {"_comment", "fields"}


def _compress_entry_list_player_row(row):
    values = [row.get(field, "") for field in _ENTRY_LISTS_CACHE_PLAYER_FIELDS]
    while values and _ENTRY_LISTS_CACHE_PLAYER_FIELDS[len(values) - 1] not in row:
        values.pop()
    return values


def dumps_entry_lists_cache(payload, *, ensure_ascii=False, indent=2):
    """Serialize entry_lists_cache.json in a compact grouped layout.

    Each tournament stores MAIN/QUAL/ALT sections separately and player rows are
    compact arrays, while the reader reconstructs the original player objects.
    """

    def _is_primitive(value):
        return value is None or isinstance(value, (bool, int, float, str))

    def _normalize_player_row(row):
        if not isinstance(row, dict):
            return row
        return {field: row.get(field, "") for field in _ENTRY_LISTS_CACHE_PLAYER_FIELDS}

    def _compress_player_row(row):
        normalized = _normalize_player_row(row)
        return [normalized[field] for field in _ENTRY_LISTS_CACHE_PLAYER_FIELDS]

    def _render_compact_value(value, level):
        pad = " " * level
        if isinstance(value, dict):
            if not value:
                return [pad + "{}"]
            lines = [pad + "{"]
            items = list(value.items())
            child_pad = " " * (level + indent)
            for i, (key, child) in enumerate(items):
                comma = "," if i < len(items) - 1 else ""
                key_text = json.dumps(key, ensure_ascii=ensure_ascii)
                if isinstance(child, list):
                    lines.append(f"{child_pad}{key_text}: [")
                    item_pad = " " * (level + indent * 2)
                    for j, item in enumerate(child):
                        item_comma = "," if j < len(child) - 1 else ""
                        if isinstance(item, list):
                            lines.append(
                                item_pad
                                + json.dumps(item, ensure_ascii=ensure_ascii, separators=(",", ":"))
                                + item_comma
                            )
                        else:
                            lines.append(
                                item_pad
                                + json.dumps(item, ensure_ascii=ensure_ascii)
                                + item_comma
                            )
                    lines.append(f"{child_pad}]{comma}")
                else:
                    lines.append(
                        f"{child_pad}{key_text}: "
                        + json.dumps(child, ensure_ascii=ensure_ascii, separators=(",", ":"))
                        + comma
                    )
            lines.append(pad + "}")
            return lines
        if isinstance(value, list):
            if not value:
                return [pad + "[]"]
            lines = [pad + "["]
            item_pad = " " * (level + indent)
            for i, item in enumerate(value):
                comma = "," if i < len(value) - 1 else ""
                if isinstance(item, list):
                    lines.append(item_pad + json.dumps(item, ensure_ascii=ensure_ascii, separators=(",", ":")) + comma)
                else:
                    lines.append(item_pad + json.dumps(item, ensure_ascii=ensure_ascii) + comma)
            lines.append(pad + "]")
            return lines
        return [pad + json.dumps(value, ensure_ascii=ensure_ascii)]

    compressed = compress_entry_lists_cache(payload)
    if not isinstance(compressed, dict):
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)

    lines = ["{"]
    lines.append(
        '  "fields": '
        + json.dumps(list(_ENTRY_LISTS_CACHE_PLAYER_FIELDS), ensure_ascii=ensure_ascii, separators=(",", ":"))
        + ("," if compressed else "")
    )
    items = [
        (tournament_key, tournament_value)
        for tournament_key, tournament_value in compressed.items()
        if tournament_key not in _ENTRY_LISTS_CACHE_META_KEYS
    ]
    for i, (tournament_key, tournament_value) in enumerate(items):
        comma = "," if i < len(items) - 1 else ""
        key_text = json.dumps(tournament_key, ensure_ascii=ensure_ascii)
        if not isinstance(tournament_value, dict):
            lines.append(
                "  "
                + key_text
                + ": "
                + json.dumps(tournament_value, ensure_ascii=ensure_ascii, separators=(",", ":"))
                + comma
            )
            continue
        lines.append(f"  {key_text}: {{")
        section_items = [
            (section, tournament_value.get(section, []))
            for section in _ENTRY_LISTS_CACHE_SECTION_ORDER
            if section in tournament_value
        ]
        for j, (section, players) in enumerate(section_items):
            section_comma = "," if j < len(section_items) - 1 else ""
            lines.append(f"    {json.dumps(section, ensure_ascii=ensure_ascii)}: [")
            for k, player in enumerate(players or []):
                player_comma = "," if k < len(players) - 1 else ""
                lines.append(
                    "      "
                    + json.dumps(player, ensure_ascii=ensure_ascii, separators=(",", ":"))
                    + player_comma
                )
            lines.append(f"    ]{section_comma}")
        lines.append(f"  }}{comma}")
    lines.append("}")
    return "\n".join(lines)


def compress_entry_lists_cache(payload):
    """Compress entry list player dicts into tournament->section->arrays."""
    if not isinstance(payload, dict):
        return payload

    if payload and all(isinstance(v, dict) for k, v in payload.items() if k not in _ENTRY_LISTS_CACHE_META_KEYS):
        sample = next((v for k, v in payload.items() if k not in _ENTRY_LISTS_CACHE_META_KEYS), None)
        if sample and all(isinstance(v, list) and (not v or isinstance(v[0], list)) for v in sample.values()):
            return payload

    compressed = {}
    for tournament_key, players in payload.items():
        if isinstance(tournament_key, str) and tournament_key in _ENTRY_LISTS_CACHE_META_KEYS:
            continue
        sections = {"MAIN": [], "QUAL": [], "ALT": []}
        if isinstance(players, dict):
            # Already grouped but still in object form.
            for section in _ENTRY_LISTS_CACHE_SECTION_ORDER:
                section_players = players.get(section) or []
                if not isinstance(section_players, list):
                    continue
                for row in section_players:
                    if not isinstance(row, dict):
                        continue
                    sections[section].append(_compress_entry_list_player_row(row))
        else:
            for row in players or []:
                if not isinstance(row, dict):
                    continue
                section = str(row.get("type") or "").upper()
                if section not in sections:
                    section = "MAIN"
                sections[section].append(_compress_entry_list_player_row(row))
        compressed[tournament_key] = {section: rows for section, rows in sections.items() if rows}
    return compressed


def expand_entry_lists_cache(payload):
    """Expand compact entry list caches back into the original player dict shape."""
    if not isinstance(payload, dict):
        return payload

    expanded = {}
    for tournament_key, tournament_value in payload.items():
        if isinstance(tournament_key, str) and tournament_key in _ENTRY_LISTS_CACHE_META_KEYS:
            continue
        if isinstance(tournament_value, dict) and any(section in tournament_value for section in _ENTRY_LISTS_CACHE_SECTION_ORDER):
            players = []
            for section in _ENTRY_LISTS_CACHE_SECTION_ORDER:
                rows = tournament_value.get(section) or []
                if not isinstance(rows, list):
                    continue
                for row in rows:
                    if isinstance(row, dict):
                        player = dict(row)
                    elif isinstance(row, list):
                        player = dict(zip(_ENTRY_LISTS_CACHE_PLAYER_FIELDS, row))
                    else:
                        continue
                    player["type"] = section
                    players.append(player)
            expanded[tournament_key] = players
        elif isinstance(tournament_value, list):
            # Backward-compatible flat list of player dicts.
            expanded[tournament_key] = [dict(row) for row in tournament_value if isinstance(row, dict)]
        else:
            # Normalize empty / malformed cache entries to the same list shape
            # the rest of the code expects.
            expanded[tournament_key] = []
    return expanded


def dumps_itf_calendar_cache(payload, *, ensure_ascii=False, indent=2):
    """Serialize itf_calendar_cache.json with one calendar item per line."""
    payload = compress_itf_calendar_cache(payload)
    def _render_item(item, level):
        pad = " " * level
        if isinstance(item, dict):
            return [pad + json.dumps(item, ensure_ascii=ensure_ascii, separators=(",", ":"))]
        return [pad + json.dumps(item, ensure_ascii=ensure_ascii)]

    if not isinstance(payload, dict):
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)

    lines = ["{"]
    meta_keys = [k for k in payload.keys() if k != "items"]
    for i, key in enumerate(meta_keys):
        comma = "," if i < len(meta_keys) - 1 or "items" in payload else ""
        lines.append(
            "  "
            + json.dumps(key, ensure_ascii=ensure_ascii)
            + ": "
            + json.dumps(payload[key], ensure_ascii=ensure_ascii, separators=(",", ":"))
            + comma
        )

    if "items" in payload:
        items = payload.get("items") or []
        lines.append('  "items": [')
        for i, item in enumerate(items):
            item_lines = _render_item(item, 4)
            if i < len(items) - 1:
                item_lines[-1] += ","
            lines.extend(item_lines)
        lines.append("  ]")
    lines.append("}")
    return "\n".join(lines)


def dumps_wta_full_calendar_cache(payload, *, ensure_ascii=False, indent=2):
    """Serialize WTA calendar caches with compact grouped item blocks."""
    payload = compress_wta_calendar_cache(payload)

    def _format_inline_dict(value, field_order=None):
        if not isinstance(value, dict):
            return json.dumps(value, ensure_ascii=ensure_ascii)
        parts = []
        seen = set()
        ordered_keys = list(field_order or ())
        for key in ordered_keys:
            if key in value:
                parts.append(
                    json.dumps(key, ensure_ascii=ensure_ascii)
                    + ": "
                    + json.dumps(value[key], ensure_ascii=ensure_ascii)
                )
                seen.add(key)
        for key, child in value.items():
            if key in seen:
                continue
            parts.append(
                json.dumps(key, ensure_ascii=ensure_ascii)
                + ": "
                + json.dumps(child, ensure_ascii=ensure_ascii)
            )
        if not parts:
            return "{}"
        return "{ " + ", ".join(parts) + " }"

    def _render_item(item, level):
        pad = " " * level
        item_pad = " " * (level + indent)
        if not isinstance(item, dict):
            return [pad + json.dumps(item, ensure_ascii=ensure_ascii)]

        lines = [pad + "{"]
        entries = []
        rendered = set()

        if "tournamentGroup" in item:
            entries.append(
                [
                    item_pad
                    + json.dumps("tournamentGroup", ensure_ascii=ensure_ascii)
                    + ": "
                    + _format_inline_dict(item.get("tournamentGroup"), ("id", "name", "level"))
                ]
            )
            rendered.add("tournamentGroup")

        grouped_keys = (
            ("year", "startDate", "endDate"),
            ("title", "level"),
            ("surface", "inOutdoor"),
            ("city", "country"),
            ("singlesDrawSize",),
        )
        for key_group in grouped_keys:
            parts = []
            for key in key_group:
                if key in item:
                    parts.append(
                        json.dumps(key, ensure_ascii=ensure_ascii)
                        + ": "
                        + json.dumps(item[key], ensure_ascii=ensure_ascii)
                    )
                    rendered.add(key)
            if parts:
                entries.append([item_pad + ", ".join(parts)])

        for key, value in item.items():
            if key in rendered:
                continue
            entries.append(
                [
                    item_pad
                    + json.dumps(key, ensure_ascii=ensure_ascii)
                    + ": "
                    + json.dumps(value, ensure_ascii=ensure_ascii)
                ]
            )
            rendered.add(key)

        for idx, entry_lines in enumerate(entries):
            if idx < len(entries) - 1:
                entry_lines[-1] += ","
            lines.extend(entry_lines)
        lines.append(pad + "}")
        return lines

    if not isinstance(payload, dict):
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)

    lines = ["{"]
    meta_keys = [k for k in payload.keys() if k != "items"]
    for i, key in enumerate(meta_keys):
        comma = "," if i < len(meta_keys) - 1 or "items" in payload else ""
        lines.append(
            "  "
            + json.dumps(key, ensure_ascii=ensure_ascii)
            + ": "
            + json.dumps(payload[key], ensure_ascii=ensure_ascii)
            + comma
        )

    if "items" in payload:
        items = payload.get("items") or []
        lines.append('  "items": [')
        for i, item in enumerate(items):
            item_lines = _render_item(item, 4)
            if i < len(items) - 1:
                item_lines[-1] += ","
            lines.extend(item_lines)
        lines.append("  ]")
    lines.append("}")
    return "\n".join(lines)


_WTA_CALENDAR_STRIPPED_FIELDS = {
    "announcements",
    "winners",
    "status",
    "liveScoringId",
    "prizeMoney",
    "prizeMoneyCurrency",
    "doublesDrawSize",
}


def compress_wta_calendar_cache(payload):
    """Drop default-only WTA calendar fields while keeping the same outer shape."""
    if not isinstance(payload, dict):
        return payload

    def _compress_item(item):
        if not isinstance(item, dict):
            return item
        compact = dict(item)
        for key in _WTA_CALENDAR_STRIPPED_FIELDS:
            compact.pop(key, None)
        if isinstance(compact.get("tournamentGroup"), dict) and "metadata" in compact["tournamentGroup"]:
            compact["tournamentGroup"] = dict(compact["tournamentGroup"])
            compact["tournamentGroup"].pop("metadata", None)
        return compact

    compact = dict(payload)
    items = compact.get("items")
    if isinstance(items, list):
        compact["items"] = [_compress_item(item) for item in items]
    return compact


def expand_wta_calendar_cache(payload):
    """Restore WTA calendar defaults removed during compression."""
    if not isinstance(payload, dict):
        return payload

    def _expand_item(item):
        if not isinstance(item, dict):
            return item
        expanded = dict(item)
        return expanded

    expanded = dict(payload)
    items = expanded.get("items")
    if isinstance(items, list):
        expanded["items"] = [_expand_item(item) for item in items]
    return expanded


_ITF_CALENDAR_STRIPPED_FIELDS = {
    "hospitality",
    "liveStreamingUrl",
    "liveLink",
    "tourStatusCode",
    "tourStatusDesc",
    "promotionalName",
    "recognisedTournamentLink",
    "isRecognised",
    "prizeMoney",
    "id",
    "year",
    "surfaceCode",
    "dates",
    "venue",
    "tennisCategoryCode",
}


def compress_itf_calendar_cache(payload):
    """Drop default-only ITF calendar fields while keeping the same outer shape."""
    if not isinstance(payload, dict):
        return payload

    def _compress_item(item):
        if not isinstance(item, dict):
            return item
        compact = dict(item)
        for key in _ITF_CALENDAR_STRIPPED_FIELDS:
            compact.pop(key, None)
        return compact

    compact = dict(payload)
    items = compact.get("items")
    if isinstance(items, list):
        compact["items"] = [_compress_item(item) for item in items]
    return compact


def expand_itf_calendar_cache(payload):
    """Restore ITF calendar defaults removed during compression."""
    if not isinstance(payload, dict):
        return payload

    def _expand_item(item):
        if not isinstance(item, dict):
            return item
        expanded = dict(item)
        return expanded

    expanded = dict(payload)
    items = expanded.get("items")
    if isinstance(items, list):
        expanded["items"] = [_expand_item(item) for item in items]
    return expanded


_HISTORY_ALWAYS_EMPTY_FIELDS = (
    "PLAYER", "ENTRY", "SEED", "RESULT",
    "RIVAL_ENTRY", "RIVAL_SEED", "RIVAL", "RIVAL_COUNTRY",
)
_HISTORY_GROUP_FIELD_ORDER = (
    "TOURNAMENT_ID", "DRAW", "TOURNAMENT", "CATEGORY", "SURFACE", "MATCH_TYPE",
)


def compress_history_data(rows):
    """Compress flat history rows into tournament/draw groups.

    Shared fields are hoisted to the group level. Fields that are always empty
    in the current dataset are omitted and restored by the decoder.
    """
    if not rows:
        return []
    groups = []
    index = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = (row.get("TOURNAMENT_ID", ""), row.get("DRAW", ""))
        bucket = index.get(key)
        if bucket is None:
            bucket = []
            index[key] = bucket
            groups.append((key, bucket))
        bucket.append(row)

    compressed = []
    for (tournament_id, draw), bucket in groups:
        if not bucket:
            continue
        shared_keys = []
        key_set = set(bucket[0].keys())
        for row in bucket[1:]:
            key_set &= set(row.keys())
        for key in bucket[0].keys():
            if key in _HISTORY_ALWAYS_EMPTY_FIELDS:
                continue
            if key not in key_set:
                continue
            first_value = bucket[0].get(key)
            if all(row.get(key) == first_value for row in bucket[1:]):
                shared_keys.append(key)

        group = {"TOURNAMENT_ID": tournament_id, "DRAW": draw}
        for key in _HISTORY_GROUP_FIELD_ORDER:
            if key in ("TOURNAMENT_ID", "DRAW"):
                continue
            if key in shared_keys:
                group[key] = bucket[0].get(key)
        for key in shared_keys:
            if key in ("TOURNAMENT_ID", "DRAW") or key in group:
                continue
            group[key] = bucket[0].get(key)

        reduced_rows = []
        for row in bucket:
            reduced = {}
            for key, value in row.items():
                if key in _HISTORY_ALWAYS_EMPTY_FIELDS:
                    continue
                if key in group and group[key] == value:
                    continue
                reduced[key] = value
            reduced_rows.append(reduced)
        group["rows"] = reduced_rows
        compressed.append(group)
    return compressed


def dumps_history_data(payload, *, ensure_ascii=False, indent=2):
    """Serialize compressed history groups with one row per line."""
    if not isinstance(payload, list) or not payload:
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)
    first = payload[0]
    if not isinstance(first, dict) or "rows" not in first:
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)

    group_order = list(_HISTORY_GROUP_FIELD_ORDER)
    lines = ["["]
    for i, group in enumerate(payload):
        if not isinstance(group, dict):
            continue
        comma = "," if i < len(payload) - 1 else ""
        lines.append("  {")
        seen = set()
        for key in group_order:
            if key in group and key != "rows":
                seen.add(key)
                lines.append(
                    "    "
                    + json.dumps(key, ensure_ascii=ensure_ascii)
                    + ": "
                    + json.dumps(group[key], ensure_ascii=ensure_ascii)
                    + ","
                )
        for key, value in group.items():
            if key == "rows" or key in seen:
                continue
            lines.append(
                "    "
                + json.dumps(key, ensure_ascii=ensure_ascii)
                + ": "
                + json.dumps(value, ensure_ascii=ensure_ascii)
                + ","
            )
        rows = group.get("rows") or []
        if rows:
            lines.append('    "rows": [')
            for j, row in enumerate(rows):
                row_comma = "," if j < len(rows) - 1 else ""
                lines.append(
                    "      "
                    + json.dumps(row, ensure_ascii=ensure_ascii, separators=(",", ":"))
                    + row_comma
                )
            lines.append("    ]")
        else:
            lines.append('    "rows": []')
        lines.append("  }" + comma)
    lines.append("]")
    return "\n".join(lines)


def dumps_itf_drawsheets_cache(payload, *, ensure_ascii=False, indent=2):
    """Serialize itf_drawsheets_cache.json in a readable nested JSON layout."""
    payload = compress_itf_drawsheet_cache(payload)
    inline_groups = [
        ("eventId", "drawsheetStructure", "tourType"),
        ("roundDesc", "roundNumber"),
        ("matchId", "playStatusCode", "resultStatusDesc", "resultStatusCode"),
        ("playerId", "nationality", "givenName", "familyName", "hiddenPlayer"),
        ("score", "losingScore"),
        ("scores", "isWinner", "seeding", "tieNationCode", "entryStatus"),
    ]

    def _is_primitive(value):
        return value is None or isinstance(value, (bool, int, float, str))

    def _can_inline_dict(value):
        if not isinstance(value, dict) or not value:
            return False
        if not all(_is_primitive(v) for v in value.values()):
            return False
        keys = set(value.keys())
        return any(keys.issubset(set(group)) for group in inline_groups)

    def _append_comma(lines):
        if lines:
            lines[-1] += ","

    def _render_list_item(item, level):
        pad = " " * level
        if isinstance(item, dict):
            if _can_inline_dict(item):
                return [pad + json.dumps(item, ensure_ascii=ensure_ascii, separators=(",", ":"))]
            return _render_dict(item, level)
        if isinstance(item, list):
            return _render_list(item, level)
        return [pad + json.dumps(item, ensure_ascii=ensure_ascii)]

    def _render_list(value, level):
        pad = " " * level
        if not value:
            return [pad + "[]"]
        lines = [pad + "["]
        for idx, item in enumerate(value):
            item_lines = _render_list_item(item, level + indent)
            if idx < len(value) - 1:
                _append_comma(item_lines)
            lines.extend(item_lines)
        lines.append(pad + "]")
        return lines

    def _render_group_line(value, keys, level):
        pad = " " * level
        parts = []
        for key in keys:
            if key in value:
                parts.append(
                    json.dumps(key, ensure_ascii=ensure_ascii)
                    + ": "
                    + json.dumps(
                        value[key],
                        ensure_ascii=ensure_ascii,
                        separators=(",", ":"),
                    )
                )
        return [pad + ", ".join(parts)] if parts else []

    def _match_group(value, key, rendered):
        for group in inline_groups:
            if key not in group:
                continue
            present = [gk for gk in group if gk in value and gk not in rendered]
            if present and present[0] == key:
                return present
        return None

    def _render_property(key, value, level):
        pad = " " * level
        key_text = json.dumps(key, ensure_ascii=ensure_ascii)
        if _is_primitive(value):
            return [pad + key_text + ": " + json.dumps(value, ensure_ascii=ensure_ascii)]
        if isinstance(value, list):
            if not value:
                return [pad + key_text + ": []"]
            if key == "scores":
                return [
                    pad
                    + key_text
                    + ": "
                    + json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"))
                ]
            lines = [pad + key_text + ": ["]
            for idx, item in enumerate(value):
                item_lines = _render_list_item(item, level + indent * 2)
                if idx < len(value) - 1:
                    _append_comma(item_lines)
                lines.extend(item_lines)
            lines.append(pad + "]")
            return lines
        if isinstance(value, dict):
            if key == "player" and _can_inline_dict(value):
                return [
                    pad
                    + key_text
                    + ": "
                    + json.dumps(value, ensure_ascii=ensure_ascii, separators=(",", ":"))
                ]
            lines = [pad + key_text + ":"]
            lines.extend(_render_dict(value, level + indent))
            return lines
        return [pad + key_text + ": " + json.dumps(value, ensure_ascii=ensure_ascii)]

    def _render_dict(value, level):
        pad = " " * level
        if not value:
            return [pad + "{}"]
        lines = [pad + "{"]
        keys = list(value.keys())
        rendered = set()
        entries = []
        for key in keys:
            if key in rendered:
                continue
            group = _match_group(value, key, rendered)
            if group:
                entries.append(_render_group_line(value, group, level + indent))
                rendered.update(group)
            else:
                entries.append(_render_property(key, value[key], level + indent))
                rendered.add(key)
        for idx, entry_lines in enumerate(entries):
            if idx < len(entries) - 1:
                _append_comma(entry_lines)
            lines.extend(entry_lines)
        lines.append(pad + "}")
        return lines

    def _render(payload_value, level):
        if isinstance(payload_value, dict):
            return _render_dict(payload_value, level)
        if isinstance(payload_value, list):
            return _render_list(payload_value, level)
        return [" " * level + json.dumps(payload_value, ensure_ascii=ensure_ascii)]

    return "\n".join(_render(payload, 0))


_DRAWS_STORE_META_FIELDS = (
    "name", "level", "week", "startDate", "endDate", "fetchedAt", "arg_visibility"
)
_DRAWS_STORE_DRAW_DEFAULT_KEYS = {
    "tournament_name", "location", "dates", "prize", "surface", "draw_type",
    "players", "matches", "byes", "qualifiers", "round_labels",
}
_DRAWS_STORE_PLAYER_DEFAULTS = {
    "pos": "",
    "seed": "",
    "entry": "",
    "name": "",
    "country": "",
}


def compress_draws_store_cache(payload):
    """Compress draw cache entries into meta + draw payloads with empty fields omitted."""
    if not isinstance(payload, dict):
        return payload

    def _compress_value(value):
        if isinstance(value, dict):
            compact = {}
            for key, child in value.items():
                child_compact = _compress_value(child)
                if child_compact in (None, "", [], {}):
                    continue
                compact[key] = child_compact
            return compact
        if isinstance(value, list):
            return [_compress_value(item) for item in value]
        return value

    compressed = {}
    for t_key, entry in payload.items():
        if not isinstance(entry, dict):
            compressed[t_key] = entry
            continue
        meta = {}
        draws = {}
        if isinstance(entry.get("meta"), dict) and isinstance(entry.get("draws"), dict):
            meta.update(_compress_value(entry.get("meta") or {}))
            draws.update(_compress_value(entry.get("draws") or {}))
        else:
            for field in _DRAWS_STORE_META_FIELDS:
                if field in entry:
                    value = _compress_value(entry.get(field))
                    if value not in (None, "", [], {}):
                        meta[field] = value
            for key, value in entry.items():
                if key in _DRAWS_STORE_META_FIELDS or key == "draws":
                    continue
                meta[key] = _compress_value(value)
            draws = _compress_value(entry.get("draws") or {})
        compressed[t_key] = {"meta": meta, "draws": draws}
    return compressed


def expand_draws_store_cache(payload):
    """Expand compact draw cache entries back into the original in-memory shape."""
    if not isinstance(payload, dict):
        return payload

    def _expand_value(value):
        if isinstance(value, dict):
            expanded = {key: _expand_value(child) for key, child in value.items()}
            # Fill common draw defaults so the rest of the code sees the same shape.
            if "players" in expanded or "matches" in expanded or "byes" in expanded or "round_labels" in expanded:
                for key in ("tournament_name", "location", "dates", "prize", "surface", "draw_type"):
                    expanded.setdefault(key, "")
                for key in ("players", "matches", "byes", "qualifiers", "round_labels"):
                    expanded.setdefault(key, [])
            if "players" in expanded and isinstance(expanded.get("players"), list):
                expanded["players"] = [_expand_player(item) for item in expanded["players"]]
            if "qualifiers" in expanded and isinstance(expanded.get("qualifiers"), list):
                expanded["qualifiers"] = [_expand_value(item) for item in expanded["qualifiers"]]
            if "matches" in expanded and isinstance(expanded.get("matches"), list):
                expanded["matches"] = [_expand_value(item) for item in expanded["matches"]]
            return expanded
        if isinstance(value, list):
            return [_expand_value(item) for item in value]
        return value

    def _expand_player(value):
        if not isinstance(value, dict):
            return value
        expanded = dict(value)
        for key, default in _DRAWS_STORE_PLAYER_DEFAULTS.items():
            expanded.setdefault(key, default)
        return expanded

    expanded = {}
    for t_key, entry in payload.items():
        if not isinstance(entry, dict):
            expanded[t_key] = entry
            continue
        if "meta" in entry and "draws" in entry:
            meta = entry.get("meta") if isinstance(entry.get("meta"), dict) else {}
            draws = entry.get("draws") if isinstance(entry.get("draws"), dict) else {}
            flat = {}
            flat.update(_expand_value(meta))
            flat["draws"] = {dtype: _expand_value(draw) for dtype, draw in draws.items()}
            expanded[t_key] = flat
        else:
            flat = {}
            for field in _DRAWS_STORE_META_FIELDS:
                if field in entry:
                    flat[field] = _expand_value(entry.get(field))
            for key, value in entry.items():
                if key in _DRAWS_STORE_META_FIELDS or key == "draws":
                    continue
                flat[key] = _expand_value(value)
            flat.setdefault("draws", {})
            flat["draws"] = {dtype: _expand_value(draw) for dtype, draw in (flat.get("draws") or {}).items()}
            expanded[t_key] = flat
    return expanded


_ITF_DRAWSHEET_STRIPPED_FIELDS = {
    "h2hLink",
    "liveScoresLink",
    "playStatusDesc",
    "profileLink",
    "MatchTypeCode",
    "CircuitCode",
}


_ITF_DRAWSHEET_DEFAULTS = {
    "tourType": None,
    "groupName": None,
    "tieNationCode": None,
    "seeding": None,
    "losingScore": None,
    "resultStatusDesc": None,
    "resultStatusCode": None,
    "hiddenPlayer": False,
}


def compress_itf_drawsheet_cache(payload):
    """Compact ITF singles players, set scores, and default/null fields."""
    if not isinstance(payload, dict):
        return payload

    def _compress(value):
        if isinstance(value, dict):
            compact = {}
            for key, child in value.items():
                if key in _ITF_DRAWSHEET_STRIPPED_FIELDS:
                    continue
                child_compact = _compress(child)
                if key == "scores" and isinstance(child_compact, list):
                    while child_compact and child_compact[-1] is None:
                        child_compact.pop()
                    compact_scores = []
                    for score in child_compact:
                        if isinstance(score, dict) and set(score) == {"score"}:
                            compact_scores.append(score["score"])
                        elif (
                            isinstance(score, dict)
                            and set(score) == {"score", "losingScore"}
                            and score.get("score") is not None
                            and score.get("losingScore") is not None
                        ):
                            compact_scores.append(
                                f'{score["score"]}({score["losingScore"]})'
                            )
                        else:
                            compact_scores.append(score)
                    child_compact = compact_scores
                elif key == "players" and isinstance(child_compact, list):
                    while child_compact and child_compact[-1] is None:
                        child_compact.pop()
                    if not child_compact:
                        continue
                    if len(child_compact) == 1 and isinstance(child_compact[0], dict):
                        compact["player"] = child_compact[0]
                        continue
                if key in _ITF_DRAWSHEET_DEFAULTS and child_compact == _ITF_DRAWSHEET_DEFAULTS[key]:
                    continue
                compact[key] = child_compact
            return compact
        if isinstance(value, list):
            return [_compress(item) for item in value]
        return value

    return _compress(payload)


def expand_itf_drawsheet_cache(payload):
    """Restore omitted ITF drawsheet defaults so downstream code sees the full shape."""
    if not isinstance(payload, dict):
        return payload

    def _expand(value):
        if isinstance(value, dict):
            expanded = {key: _expand(child) for key, child in value.items()}
            if "scores" in expanded and "isWinner" in expanded:
                scores = expanded.get("scores")
                if isinstance(scores, list):
                    expanded_scores = []
                    for score in scores:
                        if isinstance(score, (int, float)) and not isinstance(score, bool):
                            expanded_scores.append({"score": score, "losingScore": None})
                            continue
                        if isinstance(score, str):
                            match = re.fullmatch(r"(-?\d+)\((-?\d+)\)", score)
                            if match:
                                expanded_scores.append({
                                    "score": int(match.group(1)),
                                    "losingScore": int(match.group(2)),
                                })
                                continue
                        expanded_scores.append(score)
                    if expanded_scores and len(expanded_scores) < 5:
                        expanded_scores.extend([None] * (5 - len(expanded_scores)))
                    expanded["scores"] = expanded_scores
                player = expanded.pop("player", None)
                expanded.setdefault("players", [player] if player is not None else [])
            if "koGroups" in expanded:
                expanded.setdefault("tourType", None)
            if "rounds" in expanded and "roundDesc" not in expanded:
                expanded.setdefault("groupName", None)
            if "players" in expanded and "scores" in expanded:
                expanded.setdefault("seeding", None)
                expanded.setdefault("tieNationCode", None)
            if "score" in expanded:
                expanded.setdefault("losingScore", None)
            if "matchId" in expanded and "teams" in expanded:
                expanded.setdefault("resultStatusDesc", None)
                expanded.setdefault("resultStatusCode", None)
            if "playerId" in expanded:
                expanded.setdefault("hiddenPlayer", False)
            return expanded
        if isinstance(value, list):
            return [_expand(item) for item in value]
        return value

    return _expand(payload)


def compress_row_list_cache(payload, field_order):
    """Convert a list of row dicts into compact positional rows."""
    if not isinstance(payload, list):
        return payload
    if not payload:
        return payload
    if isinstance(payload[0], list):
        return payload
    compressed = []
    for item in payload:
        if isinstance(item, dict):
            compressed.append([item.get(field, "") for field in field_order])
        else:
            compressed.append(item)
    return compressed


def expand_row_list_cache(payload, field_order):
    """Convert compact positional rows back into row dicts."""
    if not isinstance(payload, list):
        return payload
    if not payload:
        return payload
    if isinstance(payload[0], dict):
        return payload
    expanded = []
    for item in payload:
        if isinstance(item, dict):
            expanded.append(dict(item))
        elif isinstance(item, list):
            expanded.append({
                field: (item[idx] if idx < len(item) else "")
                for idx, field in enumerate(field_order)
            })
        else:
            expanded.append(item)
    return expanded


def compress_row_mapping_cache(payload, field_order):
    """Convert dict-of-list caches into compact positional rows."""
    if not isinstance(payload, dict):
        return payload
    return {
        key: compress_row_list_cache(value, field_order) if isinstance(value, list) else value
        for key, value in payload.items()
    }


def expand_row_mapping_cache(payload, field_order):
    """Convert dict-of-list caches back into row dicts."""
    if not isinstance(payload, dict):
        return payload
    return {
        key: expand_row_list_cache(value, field_order) if isinstance(value, list) else value
        for key, value in payload.items()
    }


_TOURNAMENT_SNAPSHOT_FIELDS = (
    "name",
    "level",
    "surface",
    "country",
    "startDate",
    "endDate",
    "week",
)


def compress_tournament_snapshot(payload):
    """Convert tournament snapshot entries into compact positional rows."""
    if not isinstance(payload, dict):
        return payload
    compressed = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            compressed[key] = [value.get(field, "") for field in _TOURNAMENT_SNAPSHOT_FIELDS]
        else:
            compressed[key] = value
    return compressed


def expand_tournament_snapshot(payload):
    """Expand compact tournament snapshot rows back into dict entries."""
    if not isinstance(payload, dict):
        return payload
    expanded = {}
    for key, value in payload.items():
        if isinstance(value, list):
            expanded[key] = {
                field: (value[idx] if idx < len(value) else "")
                for idx, field in enumerate(_TOURNAMENT_SNAPSHOT_FIELDS)
            }
        else:
            expanded[key] = value
    return expanded


_CALENDAR_SNAPSHOT_FIELDS = (
    "column",
    "continent",
    "name",
    "level",
    "surface",
    "source",
    "tournamentKey",
    "tournamentId",
    "calendarKey",
)


def compress_calendar_snapshot(payload):
    """Group calendar snapshot rows by week label and store compact positional rows."""
    if not isinstance(payload, list):
        return payload
    compressed = {"fields": list(_CALENDAR_SNAPSHOT_FIELDS), "weeks": {}}
    weeks = compressed["weeks"]
    for row in payload:
        if not isinstance(row, dict):
            continue
        week_label = row.get("week_label", "")
        weeks.setdefault(week_label, []).append(
            [row.get(field, "") for field in _CALENDAR_SNAPSHOT_FIELDS]
        )
    return compressed


def expand_calendar_snapshot(payload):
    """Expand compact week-grouped calendar snapshot rows back into dict entries."""
    if isinstance(payload, list) or not isinstance(payload, dict):
        return payload
    weeks = payload.get("weeks")
    if not isinstance(weeks, dict):
        return payload
    fields = payload.get("fields") or _CALENDAR_SNAPSHOT_FIELDS
    expanded = []
    for week_label, rows in weeks.items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                item = dict(row)
                item.setdefault("week_label", week_label)
                expanded.append(item)
                continue
            if not isinstance(row, list):
                continue
            item = {"week_label": week_label}
            for idx, field in enumerate(fields):
                item[field] = row[idx] if idx < len(row) else ""
            expanded.append(item)
    return expanded


def dumps_calendar_snapshot(payload, *, ensure_ascii=False, indent=2):
    """Serialize the calendar snapshot with one row per line inside each week."""
    payload = compress_calendar_snapshot(payload)
    if not isinstance(payload, dict):
        return dumps_readable(payload, ensure_ascii=ensure_ascii, indent=indent)

    lines = ["{"]
    fields = payload.get("fields", [])
    lines.append(
        '  "fields": '
        + json.dumps(fields, ensure_ascii=ensure_ascii, separators=(",", ":"))
        + ","
    )
    lines.append('  "weeks": {')
    weeks = payload.get("weeks") or {}
    week_items = list(weeks.items())
    for i, (week_label, rows) in enumerate(week_items):
        week_comma = "," if i < len(week_items) - 1 else ""
        lines.append(
            "    "
            + json.dumps(week_label, ensure_ascii=ensure_ascii)
            + ": ["
        )
        rows = rows or []
        for j, row in enumerate(rows):
            row_comma = "," if j < len(rows) - 1 else ""
            lines.append(
                "      "
                + json.dumps(row, ensure_ascii=ensure_ascii, separators=(",", ":"))
                + row_comma
            )
        lines.append("    ]" + week_comma)
    lines.append("  }")
    lines.append("}")
    return "\n".join(lines)


_DRAWS_SNAPSHOT_FIELDS = (
    "name",
    "types",
)


def compress_draws_snapshot(payload):
    """Convert draws snapshot entries into compact positional rows."""
    if not isinstance(payload, dict):
        return payload
    compressed = {}
    for key, value in payload.items():
        if isinstance(value, dict):
            compressed[key] = [
                value.get("name", ""),
                value.get("types", []),
            ]
        else:
            compressed[key] = value
    return compressed


def expand_draws_snapshot(payload):
    """Expand compact draws snapshot rows back into dict entries."""
    if not isinstance(payload, dict):
        return payload
    expanded = {}
    for key, value in payload.items():
        if isinstance(value, list):
            expanded[key] = {
                "name": value[0] if len(value) > 0 else "",
                "types": value[1] if len(value) > 1 else [],
            }
        else:
            expanded[key] = value
    return expanded


def expand_gs_calendar_cache(payload):
    """Expand compact GS/OG calendar data back into a list of row dicts."""
    if not isinstance(payload, dict):
        return payload
    if not all(key in payload for key in ("meta", "fields", "rows")):
        return [dict(payload)] if payload else payload

    meta = payload.get("meta") or {}
    fields = payload.get("fields") or []
    rows = payload.get("rows") or []
    expanded = []

    for row in rows:
        if isinstance(row, dict):
            item = dict(meta)
            item.update(row)
            expanded.append(item)
        elif isinstance(row, list):
            item = dict(meta)
            item.update(
                {
                    field: (row[idx] if idx < len(row) else "")
                    for idx, field in enumerate(fields)
                }
            )
            expanded.append(item)
        else:
            expanded.append(row)

    return expanded


_POINTS_DISTRIBUTION_FIELDS = (
    "Description",
    "W",
    "F",
    "SF",
    "QF",
    "R16",
    "R32",
    "R64",
    "R128",
    "QLFR",
    "QR3",
    "QR2",
    "QR1",
    "5W",
    "4W",
    "3W",
    "2W_KO",
    "2W_RR",
    "1W_KO",
    "1W_RR",
    "0W",
)


def compress_points_distribution(payload):
    """Convert fixed-shape points rows into compact positional rows."""
    if not isinstance(payload, list):
        return payload
    if not payload:
        return payload
    if isinstance(payload[0], list):
        return payload
    compressed = []
    for item in payload:
        if isinstance(item, dict):
            compressed.append([item.get(field, None) for field in _POINTS_DISTRIBUTION_FIELDS])
        else:
            compressed.append(item)
    return compressed


def expand_points_distribution(payload):
    """Expand compact points rows back into dict entries."""
    if not isinstance(payload, list):
        return payload
    if not payload:
        return payload
    if isinstance(payload[0], dict):
        return payload
    expanded = []
    for item in payload:
        if isinstance(item, dict):
            expanded.append(dict(item))
        elif isinstance(item, list):
            expanded.append({
                field: (item[idx] if idx < len(item) else None)
                for idx, field in enumerate(_POINTS_DISTRIBUTION_FIELDS)
            })
        else:
            expanded.append(item)
    return expanded


_TOURNAMENT_DRAW_SIZE_FIELDS = (
    "source",
    "date",
    "tournamentName",
    "tournamentId",
    "category",
    "mainDrawSize",
    "qualifyingSize",
    "description",
)


def compress_tournament_draw_sizes(payload):
    """Convert draw-size rows into compact positional rows."""
    return compress_row_list_cache(payload, _TOURNAMENT_DRAW_SIZE_FIELDS)


def expand_tournament_draw_sizes(payload):
    """Expand compact draw-size rows back into dict entries."""
    return expand_row_list_cache(payload, _TOURNAMENT_DRAW_SIZE_FIELDS)


_WTA_MISSING_TOURNAMENT_FIELDS = (
    "tournamentName",
    "tournamentLink",
    "tourCode",
    "dates",
    "location",
    "surfaceDesc",
    "surfaceCode",
)


_TSTRENGTH_CACHE_FIELDS = (
    "id",
    "name",
    "city",
    "level",
    "startDate",
    "surface",
    "country",
    "region",
    "year",
    "draw",
    "participantsLocked",
    "rankings",
    "hm",
    "gm",
    "playerCount",
)


def compress_tstrength_cache(payload):
    return compress_row_list_cache(payload, _TSTRENGTH_CACHE_FIELDS)


def expand_tstrength_cache(payload):
    return expand_row_list_cache(payload, _TSTRENGTH_CACHE_FIELDS)


_ITF_RANKING_FIELDS = ("Player", "Rank", "Country", "Key")


def compress_itf_rankings_cache(payload):
    return compress_row_mapping_cache(payload, _ITF_RANKING_FIELDS)


def expand_itf_rankings_cache(payload):
    return expand_row_mapping_cache(payload, _ITF_RANKING_FIELDS)


def save_json_file(path, payload, formatter=None):
    dump_func = formatter or dumps_readable
    text = dump_func(payload, ensure_ascii=False) + "\n"
    write_text_if_changed(path, text, encoding="utf-8")


def save_json_array_one_line_per_item(path, items, transform=None):
    """Write a JSON array with one compact object per line (easy to diff/edit).

    Optional transform(item) callable is applied to each item before serialization,
    e.g. to repair encoding on all nested strings before writing.
    """
    items = list(items or [])
    lines = ["["]
    for i, item in enumerate(items):
        # Every item except the last needs a trailing comma.
        comma = "," if i < len(items) - 1 else ""
        lines.append("  " + json.dumps(transform(item) if transform else item, ensure_ascii=False) + comma)
    lines.append("]")
    write_text_if_changed(path, "\n".join(lines) + "\n", encoding="utf-8")


def override_country_for_player(player_name, country_code):
    key = (player_name or "").strip().upper()
    if key in COUNTRY_OVERRIDES:
        return COUNTRY_OVERRIDES[key]
    return country_code


def normalize_country_overrides(rows, name_key, country_key):
    for row in rows or []:
        row[country_key] = override_country_for_player(row.get(name_key, ""), row.get(country_key, ""))
    return rows


def load_csv_rows(file_path, delimiter=','):
    rows = []
    try:
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            for row in reader:
                rows.append(row)
    except Exception as e:
        logger.error(f"Error reading {file_path}: {e}")
    return rows
