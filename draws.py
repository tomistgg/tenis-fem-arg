"""Parse WTA draw PDFs and ITF draw JSON data."""

import json
import re
import time
import random
import urllib.parse
import requests
import fitz

from utils import normalize_player_name
from itf_drawsheet_cache import get_cached_drawsheet, save_drawsheet
from run_state import report_run_issue


_DRAW_TYPES = [
    ("MDS", "Main Draw"),
    ("QS", "Qualifying"),
]

_PDF_BASE = "https://wtafiles.wtatennis.com/pdf/draws/{year}/{tid}/{dtype}.pdf"

_WTA_API_MATCHES_URL = "https://api.wtatennis.com/tennis/tournaments/{tid}/{year}/matches?states=C"
_WTA_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://www.wtatennis.com/",
    "account": "wta",
}


def _extract_tournament_id(url):
    m = re.search(r'/tournaments/(\d+)/', url)
    return m.group(1) if m else None


def fetch_draw_pdf_bytes(tournament_id, year, draw_type="MDS"):
    url = _PDF_BASE.format(year=year, tid=tournament_id, dtype=draw_type)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:5] == b'%PDF-':
            return resp.content
        return None
    except Exception:
        return None


_WO_TOKENS = {'WO', 'W/O', 'W.O.'}


def _is_score(text):
    """Check if a line is a match score."""
    text = text.strip()
    if not text:
        return False
    if text.upper() in _WO_TOKENS:
        return True
    _S = r'[\d]+(?:\(\d+\))?'
    # Standard score: 2 or 3 set tokens (no RET/DEF required)
    standard = rf'^(?:{_S}\s+){{1,2}}{_S}$'
    # Retired/defaulted: 1–3 set tokens followed by RET or DEF
    retired  = rf'^{_S}(?:\s+{_S}){{0,2}}\s+(?:RET|DEF)$'
    return bool(re.match(standard, text) or re.match(retired, text))


def _is_completed_score(score_str):
    """Return True only if every set token in score_str represents a finished set.

    A finished set has at least one player on 6+ games (or 10+ for match tiebreaks).
    RET/DEF/WO tokens mark the match as complete regardless of the score.
    Rejects live/in-progress scores like '44 44' or '53 31'.
    """
    parts = score_str.strip().split()
    # Walkover/retirement/default marks the match as complete.
    if any(p.upper() in ('RET', 'DEF') or p.upper() in _WO_TOKENS for p in parts):
        return True

    has_set = False
    for p in parts:
        m = re.match(r'^(\d+)(?:\(\d+\))?$', p)
        if not m:
            continue
        digits = m.group(1)
        if len(digits) < 2:
            continue
        mid = len(digits) // 2
        w_games = int(digits[:mid])
        l_games = int(digits[mid:])
        if max(w_games, l_games) < 6:
            return False  # set is not finished (e.g. '44', '53')
        has_set = True
    return has_set


def _ret_def_token(text):
    """Normalize standalone retirement/default markers."""
    u = (text or "").strip().upper().rstrip(".")
    return u if u in ("RET", "DEF") else ""


def _is_winner_name(text):
    """Check if a line is a winner name like 'A. Sabalenka' or 'Xin. Wang'."""
    text = text.strip()
    if not text:
        return False
    return bool(re.match(r'^[A-Z][a-z]*\.\s+\S', text))




def _split_player_name(player_name):
    """Split 'LAST, First' style names into (last, first)."""
    player_name = (player_name or "").strip()
    if not player_name:
        return "", ""
    if "," in player_name:
        last, first = player_name.split(",", 1)
        return last.strip(), first.strip()
    parts = player_name.split()
    if len(parts) == 1:
        return parts[0], ""
    return parts[-1], " ".join(parts[:-1])


def _parse_winner_name_parts(winner_name):
    """Split abbreviated winner names like 'A. S. Sanchez' or 'Xin. Wang' into pieces.

    Returns (family_name, initials_string) where initials captures the FIRST character
    of each abbreviated token (e.g. 'Xin.' → 'X', not 'n').
    """
    clean = (winner_name or "").replace("...", "").strip()
    m = re.match(r'^((?:[^\W\d_]+\.\s*)+)(.+)$', clean, flags=re.UNICODE)
    if not m:
        return clean, ""
    # Capture first letter of each "Token." group, e.g. "Xin." → 'X', "A." → 'A'
    initials = "".join(re.findall(r'([^\W\d_])[^\W\d_]*\.', m.group(1), flags=re.UNICODE)).upper()
    family = m.group(2).strip()
    return family, initials


def _player_name_matches_winner(player_name, winner_name):
    """Return True when a full draw player name matches an abbreviated winner name."""
    if not player_name or not winner_name:
        return False

    # Exact normalized match first.
    p_norm = normalize_player_name(re.sub(r"\.\.\.$", "", player_name))
    w_raw = (winner_name or "").strip()
    w_norm = normalize_player_name(re.sub(r"\.\.\.$", "", w_raw))
    if p_norm == w_norm:
        return True

    truncated = w_raw.endswith("...")
    player_last, player_first = _split_player_name(player_name)
    winner_last, winner_initials = _parse_winner_name_parts(w_raw)
    p_last_norm = normalize_player_name(player_last)
    w_last_norm = normalize_player_name(winner_last)
    if not p_last_norm or not w_last_norm:
        return False
    if p_last_norm != w_last_norm:
        if not (truncated and len(w_last_norm) >= 5 and p_last_norm.startswith(w_last_norm)):
            return False

    # If we have winner initials, enforce first-initial agreement to avoid surname collisions.
    if winner_initials and player_first:
        first_tokens = re.findall(r"[A-Z]+", normalize_player_name(player_first))
        if first_tokens and first_tokens[0] and first_tokens[0][0] != winner_initials[0]:
            return False
    return True


def _infer_match_num_from_winner_name(winner_name, round_num, players, used_match_nums):
    """Infer the bracket match index from winner name + player positions."""
    if not winner_name or round_num < 1 or not players:
        return None

    positions = []
    for p in players:
        pos = p.get("pos")
        name = p.get("name", "")
        if isinstance(pos, int) and pos > 0 and _player_name_matches_winner(name, winner_name):
            positions.append(pos)

    if len(positions) != 1:
        return None

    inferred = ((positions[0] - 1) // 2) // (2 ** (round_num - 1))
    if used_match_nums is not None and inferred in used_match_nums:
        return None
    return inferred


# First code point must be any Unicode letter, not only ASCII, so names like
# "SÁNCHEZ, Ana Sofia" are parsed correctly.
_NAME_WITH_COMMA_RE = r'([^\W\d_][^,]*,\s*.+)'


def _parse_page(text):
    """Parse a single page's text into players, byes, qualifier placeholders, result entries, and round labels.

    The text from get_text() has the player entries split across multiple lines:
      position_line:  '1'  or  '3 Q'  or  '8'  or  '23'
      name_line:      '1 SABALENKA, Aryna'  or  'SAKATSUME, Himeno'  or  '29 JOINT, Maya'
      country_line:   'JPN'  (optional, sometimes missing)
    Or for byes:
      position_line:  '2'
      bye_line:       'Bye'

    After all player entries, result lines follow:
      winner_name:  'A. Sabalenka '  (abbreviated first name)
      score:        '64 63'          (optional, missing for bye advances and unplayed matches)
    """
    lines = text.split('\n')

    players = []
    byes = set()
    qualifiers = set()
    result_entries = []
    round_labels = []
    in_footer = False

    # Phase 1: Parse player entries and collect result lines
    i = 0
    current_pos = None
    current_seed = ""
    current_entry = ""
    players_done = False

    while i < len(lines):
        line = lines[i].strip()
        i += 1

        if not line or line == '\xa0':
            continue

        if in_footer:
            continue

        # Footer detection
        if line.startswith('WTA Supervisor') or line.startswith('Seeded players'):
            in_footer = True
            continue

        # Round labels at the bottom
        if re.match(r'^(Round of \d+|Quarterfinals|Semifinals|Final|Q\d)$', line):
            round_labels.append(line)
            continue
        # "Qualifier" is a round label only after the player section
        if line == 'Qualifier' and players_done:
            round_labels.append(line)
            continue

        # Skip known non-data
        if line in ('CHAMPION', 'TOP HALF', 'BOTTOM HALF', 'RELEASED', 'Winner',
                     'PLAYER', 'RANK'):
            continue
        if line.startswith('$') or re.match(r'^\d+\s*pt$', line):
            continue
        # Skip prize money lines like "$24,335" or "1,511,380" but NOT scores like "64 62"
        if re.match(r'^[\$\d,.\s]+$', line) and not re.match(r'^\d+$', line):
            if not (players_done and _is_score(line)):
                continue

        if not players_done:
            # Combined line: "POS ENTRY SEED NAME, First" e.g. "1 WC 1 NAVARRO, Emma"
            combo_match = re.match(
                rf'^(\d+)\s+(WC|LL|PR|SE|ALT|Alt|Q)(?:\s+(\d+))?\s+{_NAME_WITH_COMMA_RE}$',
                line
            )
            if combo_match:
                current_pos = int(combo_match.group(1))
                current_entry = combo_match.group(2)
                current_seed = combo_match.group(3) or ""
                name = combo_match.group(4).strip()
                country = ""
                inline_country = re.match(r'^(.+?)([A-Z]{3})$', name)
                if inline_country and re.match(r'.*[a-z]$', inline_country.group(1)):
                    name = inline_country.group(1).strip()
                    country = inline_country.group(2)
                if not country and i < len(lines):
                    next_line = lines[i].strip()
                    if re.match(r'^[A-Z]{3}$', next_line):
                        country = next_line
                        i += 1
                players.append({
                    "pos": current_pos,
                    "seed": current_seed,
                    "entry": current_entry,
                    "name": name,
                    "country": country,
                })
                current_pos = None
                continue

            # Try to parse position line: just a number, or "number entry" like "3 Q" or "28 Q"
            pos_match = re.match(r'^(\d+)(?:\s+(WC|LL|PR|SE|ALT|Alt|Q))?$', line)
            if pos_match:
                current_pos = int(pos_match.group(1))
                current_entry = pos_match.group(2) or ""
                current_seed = ""
                continue

            # Bye line
            if line == 'Bye' and current_pos is not None:
                byes.add(current_pos)
                current_pos = None
                continue

            # Qualifier placeholder (empty Q spot)
            if line == 'Qualifier' and current_pos is not None:
                qualifiers.add(current_pos)
                current_pos = None
                continue

            # Name line: "[seed] LASTNAME, Firstname" or just "LASTNAME, Firstname"
            name_match = re.match(rf'^(?:(\d+)\s+)?{_NAME_WITH_COMMA_RE}$', line)
            # Handle wrapped names: "LASTNAME," on one line, "Firstname" on next
            if not name_match and current_pos is not None:
                wrap_match = re.match(r'^(?:(\d+)\s+)?([^\W\d_][^,]*,)\s*$', line)
                if wrap_match and i < len(lines):
                    next_line = lines[i].strip()
                    if next_line and re.match(r'^[A-Z][a-z]', next_line):
                        combined = wrap_match.group(2) + ' ' + next_line
                        name_match = re.match(rf'^(?:(\d+)\s+)?{_NAME_WITH_COMMA_RE}$',
                                              (wrap_match.group(1) + ' ' if wrap_match.group(1) else '') + combined)
                        if name_match:
                            i += 1
            if name_match and current_pos is not None:
                current_seed = name_match.group(1) or ""
                name = name_match.group(2).strip()
                country = ""
                # Country code may be concatenated at end of name (e.g. "TiantsoaFRA")
                inline_country = re.match(r'^(.+?)([A-Z]{3})$', name)
                if inline_country and re.match(r'.*[a-z]$', inline_country.group(1)):
                    name = inline_country.group(1).strip()
                    country = inline_country.group(2)
                # Or country might be on the next line
                if not country and i < len(lines):
                    next_line = lines[i].strip()
                    if re.match(r'^[A-Z]{3}$', next_line):
                        country = next_line
                        i += 1
                players.append({
                    "pos": current_pos,
                    "seed": current_seed,
                    "entry": current_entry,
                    "name": name,
                    "country": country,
                })
                current_pos = None
                continue

            # If we hit a winner name (abbreviated like "A. Sabalenka"), players section is done
            if _is_winner_name(line):
                players_done = True
                # Fall through to result parsing below

        if players_done:
            # Result section: winner names and scores
            if _is_winner_name(line):
                name = re.sub(r'\s+\d+$', '', line).strip()
                result_entries.append({"name": name, "score": ""})
            elif _is_score(line):
                score_line = line
                # Some PDFs split retirement/default onto the next line:
                #   "46 76(2) 41"
                #   "RET"
                if not _is_completed_score(score_line) and i < len(lines):
                    token = _ret_def_token(lines[i].strip())
                    if token:
                        combined = f"{score_line} {token}"
                        if _is_completed_score(combined):
                            score_line = combined
                            i += 1
                # Only attach completed scores (guards against live/in-progress scores)
                if result_entries and not result_entries[-1]["score"] and _is_completed_score(score_line):
                    result_entries[-1]["score"] = score_line
            elif result_entries and not result_entries[-1]["score"]:
                # Handle standalone "RET"/"DEF" line.
                token = _ret_def_token(line)
                if token:
                    result_entries[-1]["score"] = token
            # Skip standalone numbers (seed annotations), country codes, etc.

    return players, byes, qualifiers, result_entries, round_labels


def parse_draw_pdf(pdf_bytes):
    """Parse a WTA draw PDF and return structured draw data."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    num_pages = doc.page_count

    # Parse header from first page
    page0_text = doc[0].get_text() or ""
    header_lines = page0_text.split('\n')
    tournament_name = header_lines[0].strip() if header_lines else ""
    location = header_lines[1].strip() if len(header_lines) > 1 else ""

    dates = prize = surface = ""
    if len(header_lines) > 2:
        parts = [p.strip() for p in header_lines[2].split('|')]
        if len(parts) >= 1: dates = parts[0]
        if len(parts) >= 2: prize = parts[1]
        if len(parts) >= 3: surface = parts[2]

    draw_type = ""
    for line in header_lines[3:8]:
        if 'DRAW' in line.upper():
            draw_type = line.strip()
            break

    all_players = []
    all_byes = set()
    all_qualifiers = set()
    page_results = []
    round_labels = []

    for page_idx in range(num_pages):
        text = doc[page_idx].get_text() or ""
        players, byes, qualifiers, result_entries, labels = _parse_page(text)
        all_players.extend(players)
        all_byes.update(byes)
        all_qualifiers.update(qualifiers)
        page_results.append(result_entries)
        if not round_labels and labels:
            round_labels = labels

    doc.close()

    # Deduplicate players by position
    all_players.sort(key=lambda p: p["pos"])
    seen = set()
    unique_players = []
    for p in all_players:
        if p["pos"] not in seen:
            seen.add(p["pos"])
            unique_players.append(p)

    # Compute draw size from max position
    max_pos = max(
        [p["pos"] for p in unique_players] + list(all_byes) + list(all_qualifiers),
        default=0
    )
    draw_size = max_pos

    # R1 matches per page
    r1_per_page = draw_size // (2 * num_pages) if num_pages > 0 else draw_size // 2

    # Group result entries into rounds for each page
    all_matches = []
    for page_idx, entries in enumerate(page_results):
        page_match_offset = page_idx * r1_per_page
        actual_r1 = _actual_r1_count(page_idx, r1_per_page, unique_players, all_byes)
        matches = _group_into_rounds(entries, actual_r1, page_match_offset, unique_players, ideal_r1=r1_per_page)
        all_matches.extend(matches)

    num_rounds = len(round_labels) if round_labels else None

    return {
        "tournament_name": tournament_name,
        "location": location,
        "dates": dates,
        "prize": prize,
        "surface": surface,
        "draw_type": draw_type,
        "draw_size": draw_size,
        "players": unique_players,
        "matches": all_matches,
        "byes": sorted(all_byes),
        "qualifiers": sorted(all_qualifiers),
        "round_labels": round_labels,
        "num_rounds": num_rounds,
    }


def _actual_r1_count(page_idx, r1_per_page, unique_players, all_byes):
    """Return the number of R1 result entries expected on a given page.

    Excludes match slots where neither position has a player — these occur when
    a draw position is completely absent (not a player and not a bye), which
    means no winner is listed in the PDF result section for that slot.
    """
    player_positions = {p["pos"] for p in unique_players}
    page_start = page_idx * 2 * r1_per_page + 1
    count = 0
    for i in range(r1_per_page):
        pos1 = page_start + i * 2
        pos2 = page_start + i * 2 + 1
        if pos1 in player_positions or pos2 in player_positions:
            count += 1
    return count


def _group_into_rounds(entries, r1_count, match_offset, players=None, ideal_r1=None):
    """Group result entries into rounds.

    R1 has r1_count entries (actual, excluding phantom slots).
    R2+ are sized from ideal_r1 (theoretical = r1_per_page) so a phantom R1
    slot doesn't shrink R2 — the phantom's R2 match still appears in the PDF.
    """
    if ideal_r1 is None:
        ideal_r1 = r1_count

    matches = []
    round_num = 1
    expected = r1_count
    ideal_expected = ideal_r1
    pos = 0

    while pos < len(entries) and expected >= 1:
        round_entries = entries[pos:pos + expected]
        used_match_nums = set()
        for match_num, entry in enumerate(round_entries):
            inferred_match_num = _infer_match_num_from_winner_name(
                entry.get("name", ""),
                round_num,
                players,
                used_match_nums,
            ) if players else None
            if inferred_match_num is not None:
                actual_match_num = inferred_match_num
                used_match_nums.add(actual_match_num)
            elif round_num == 1:
                actual_match_num = match_num + match_offset
                used_match_nums.add(actual_match_num)
            else:
                actual_match_num = match_num + match_offset // (2 ** (round_num - 1))
                used_match_nums.add(actual_match_num)
            matches.append({
                "round": round_num,
                "match_num": actual_match_num,
                "winner_name": entry["name"],
                "score": entry["score"],
            })
        pos += expected
        ideal_expected = ideal_expected // 2
        expected = ideal_expected
        round_num += 1

    return matches


def _wta_api_mid(match_id):
    """Extract numeric ID from MatchID string, e.g. 'LS016' -> 16."""
    try:
        return int(str(match_id)[2:])
    except (ValueError, TypeError):
        return None


def _wta_api_tree_depth(n):
    """Return the depth of node n in a complete binary tree (root=1 is depth 0)."""
    d = 0
    while n > 1:
        n >>= 1
        d += 1
    return d


def _wta_api_winner(m):
    """Return (first_name, last_name) of the match winner, or (None, None)."""
    w = m.get("Winner", "")
    if w == "2":
        return m.get("PlayerNameFirstA", ""), m.get("PlayerNameLastA", "")
    if w == "3":
        return m.get("PlayerNameFirstB", ""), m.get("PlayerNameLastB", "")
    if w == "4":
        # Retirement — Message field contains "{11|F. Lastname}"
        msg = m.get("Message", "") or ""
        try:
            inner = msg.split("{11|")[1].split("}")[0]
            last = inner.strip().split()[-1].lower()
            if last == (m.get("PlayerNameLastA") or "").lower():
                return m.get("PlayerNameFirstA", ""), m.get("PlayerNameLastA", "")
            if last == (m.get("PlayerNameLastB") or "").lower():
                return m.get("PlayerNameFirstB", ""), m.get("PlayerNameLastB", "")
        except (IndexError, AttributeError):
            pass
    return None, None


def _wta_api_score_compact(score_str):
    """Convert WTA API score '7-5,6-4(2)' to compact '75 64(2)' format."""
    if not score_str:
        return ""
    s = score_str.strip()
    ret_suffix = ""
    if re.search(r"ret'?d", s, re.IGNORECASE):
        s = re.sub(r"\s*ret'?d.*", "", s, flags=re.IGNORECASE).strip().rstrip(",")
        ret_suffix = " RET"
    parts = []
    for set_str in s.split(","):
        set_str = set_str.strip()
        tb = re.match(r'^(\d+)-(\d+)\((\d+)\)$', set_str)
        if tb:
            parts.append(f"{tb.group(1)}{tb.group(2)}({tb.group(3)})")
            continue
        plain = re.match(r'^(\d+)-(\d+)$', set_str)
        if plain:
            w, l = plain.group(1), plain.group(2)
            if int(w) >= 10 or int(l) >= 10:
                parts.append(f"{w}-{l}")  # super-tiebreak: keep hyphen
            else:
                parts.append(f"{w}{l}")
    return " ".join(parts) + ret_suffix


def _fetch_draw_from_wta_api(tournament_id, year):
    """Build a main-draw singles structure from the WTA matches API.

    Uses the binary-tree property of MatchIDs: match N's children are 2N and
    2N+1, which gives exact bracket positions without needing a position field.
    Returns a dict matching the parse_draw_pdf output format, or None on failure.
    """
    url = _WTA_API_MATCHES_URL.format(tid=tournament_id, year=year)
    try:
        resp = requests.get(url, headers=_WTA_API_HEADERS, timeout=15)
        if resp.status_code != 200:
            return None
        data = resp.json()
    except Exception:
        return None

    raw_matches = data.get("matches") if isinstance(data, dict) else data
    if not raw_matches:
        return None

    # Index main-draw singles by numeric MatchID
    md = {}
    for m in raw_matches:
        if m.get("DrawLevelType") != "M" or m.get("DrawMatchType") != "S":
            continue
        n = _wta_api_mid(m.get("MatchID", ""))
        if n:
            md[n] = m

    if not md:
        return None

    ids = sorted(md)
    max_depth = max(_wta_api_tree_depth(n) for n in ids)
    r1_ids = [n for n in ids if _wta_api_tree_depth(n) == max_depth]

    if not r1_ids:
        return None

    draw_size = len(r1_ids) * 2
    num_rounds = draw_size.bit_length() - 1  # log2(draw_size)

    # Build player list from R1 matches
    players = []
    for n in sorted(r1_ids):
        pos_base = (n - 2 ** max_depth) * 2 + 1
        for side, pos in (("A", pos_base), ("B", pos_base + 1)):
            first = md[n].get(f"PlayerNameFirst{side}", "")
            last = md[n].get(f"PlayerNameLast{side}", "")
            if not last:
                continue
            seed = str(md[n].get(f"Seed{side}") or "")
            entry = md[n].get(f"EntryType{side}") or ""
            if entry.lower() == "wc":
                entry = "WC"
            country = md[n].get(f"PlayerCountry{side}") or ""
            name = f"{last.upper()}, {first}" if first else last.upper()
            players.append({"pos": pos, "seed": seed, "entry": entry,
                            "name": name, "country": country})

    # Build match results from all completed matches
    matches_out = []
    for n in sorted(md):
        m = md[n]
        depth = _wta_api_tree_depth(n)
        round_num = max_depth - depth + 1   # R1=1, R2=2, QF=3, …
        match_num = n - 2 ** depth           # 0-indexed within the round

        w_first, w_last = _wta_api_winner(m)
        if not w_last:
            continue
        abbrev = f"{w_first[0]}." if w_first else ""
        winner_name = f"{abbrev} {w_last}".strip() if abbrev else w_last
        score = _wta_api_score_compact(m.get("ScoreString", ""))

        matches_out.append({"round": round_num, "match_num": match_num,
                             "winner_name": winner_name, "score": score})

    if not players:
        return None

    # Build round labels
    round_labels = []
    for r in range(1, num_rounds + 1):
        remaining = draw_size // (2 ** (r - 1))
        if remaining == 2:
            round_labels.append("Final")
        elif remaining == 4:
            round_labels.append("Semifinals")
        elif remaining == 8:
            round_labels.append("Quarterfinals")
        else:
            round_labels.append(f"Round of {remaining}")

    tournament_meta = data.get("tournament", {}) if isinstance(data, dict) else {}
    return {
        "tournament_name": tournament_meta.get("name", ""),
        "location": "",
        "dates": "",
        "prize": "",
        "surface": tournament_meta.get("surface", ""),
        "draw_type": "SINGLES MAIN DRAW",
        "draw_size": draw_size,
        "players": players,
        "matches": matches_out,
        "byes": [],
        "qualifiers": [],
        "round_labels": round_labels,
        "num_rounds": num_rounds,
    }


def fetch_tournament_draws(tournament_url, year):
    tid = _extract_tournament_id(tournament_url)
    if not tid:
        return {}

    draws = {}
    for dtype_code, dtype_label in _DRAW_TYPES:
        pdf_bytes = fetch_draw_pdf_bytes(tid, year, dtype_code)
        if pdf_bytes:
            try:
                draw_data = parse_draw_pdf(pdf_bytes)
                draws[dtype_code] = draw_data
            except Exception as e:
                print(f"Error parsing {dtype_label} draw for {tid}: {e}")

    # If no main draw players from PDF, try the WTA matches API as fallback
    if not draws.get("MDS", {}).get("players"):
        api_draw = _fetch_draw_from_wta_api(tid, year)
        if api_draw and api_draw.get("players"):
            print(f"  [WTA API] Built main draw for {tid} from matches API ({api_draw['draw_size']}-draw, {len(api_draw['matches'])} results)")
            draws["MDS"] = api_draw

    return draws


# ── ITF draw support ──────────────────────────────────────────────────────────

_ITF_DRAW_TYPES = [
    ("M", "MDS", "Main Draw"),
    ("Q", "QS", "Qualifying"),
]

_ITF_DRAWSHEET_URL = "https://www.itftennis.com/tennis/api/TournamentApi/GetDrawsheet"

_ITF_ENTRY_MAP = {
    "DA": "",
    "WC": "WC",
    "Q": "Q",
    "LL": "LL",
    "PR": "PR",
    "SE": "SE",
    "ALT": "ALT",
}


def _prime_itf_draw_session(driver, tournament_id):
    """Warm browser session on the tournament print page to reduce ITF API blocking."""
    if driver is None:
        return
    try:
        driver.get(
            "https://www.itftennis.com/en/tournament/draws-and-results/print/"
            f"?tournamentId={tournament_id}&circuitCode=WT"
        )
        time.sleep(random.uniform(1.8, 3.2))
    except Exception as exc:
        report_run_issue(
            "itf_draws", "prime browser session", exc, severity="degraded",
            context={"tournament_id": str(tournament_id)},
        )


_ITF_DRAW_BLOCKED = object()


def _is_itf_block_text(text):
    raw = str(text or "").strip()
    upper = raw.upper()
    return raw.startswith("<") and "NOINDEX" in upper and "NOFOLLOW" in upper


def _fetch_itf_drawsheet(tournament_id, classification, week_number=0):
    """Fetch an ITF drawsheet via GET API (no Selenium needed)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tournament_id}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Accept": "application/json, text/plain, */*",
    }
    params = {
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": str(tournament_id),
        "weekNumber": week_number,
    }
    try:
        resp = requests.get(_ITF_DRAWSHEET_URL, params=params, headers=headers, timeout=15)
        text = resp.text.strip()
        if _is_itf_block_text(text):
            return _ITF_DRAW_BLOCKED
        if not text.startswith("{"):
            return None
        return resp.json()
    except Exception:
        return None


def _fetch_itf_drawsheet_via_driver(tournament_id, classification, week_number, driver, timeout_ms=15000):
    """Fetch an ITF drawsheet via browser fetch() in Selenium context."""
    if driver is None:
        return None

    params = {
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "N",
        "tournamentId": str(tournament_id),
        "weekNumber": int(week_number),
    }
    full_url = f"{_ITF_DRAWSHEET_URL}?{urllib.parse.urlencode(params)}"

    script = """
const url = arguments[0];
const timeoutMs = arguments[1];
const done = arguments[arguments.length - 1];

let sent = false;
const finish = (obj) => {
  if (sent) return;
  sent = true;
  done(obj);
};

const controller = new AbortController();
const timer = setTimeout(() => {
  controller.abort();
  finish({ ok: false, error: "timeout" });
}, timeoutMs);

fetch(url, {
  method: "GET",
  credentials: "include",
  cache: "no-store",
  headers: {
    "Accept": "application/json, text/plain, */*"
  },
  signal: controller.signal,
})
  .then(async (resp) => {
    const text = await resp.text();
    finish({ ok: true, status: resp.status, text });
  })
  .catch((err) => finish({ ok: false, error: String(err) }))
  .finally(() => clearTimeout(timer));
"""
    try:
        result = driver.execute_async_script(script, full_url, int(timeout_ms))
    except Exception:
        return None
    if not isinstance(result, dict) or not result.get("ok") or int(result.get("status", 0)) != 200:
        return None

    text = str(result.get("text") or "").strip()
    if _is_itf_block_text(text):
        return _ITF_DRAW_BLOCKED
    if not text.startswith("{"):
        return None
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _parse_itf_score(teams, winner_idx):
    """Build a WTA-style score string from ITF score data.

    WTA format combines winner+loser games per set: "64 75(3) 62"
    means winner won 6-4, 7-5(3), 6-2.

    The losingScore field on the LOSER's side contains the tiebreak points
    they scored (e.g., loser has score=6, losingScore=4 means they lost the
    tiebreak 4, so the set was 7-6(4) from the winner's perspective).
    """
    if winner_idx is None:
        return ""
    loser_idx = 1 - winner_idx
    w_scores = teams[winner_idx].get("scores") or []
    l_scores = teams[loser_idx].get("scores") or []
    parts = []
    for i in range(len(w_scores)):
        ws = w_scores[i] if i < len(w_scores) else None
        ls = l_scores[i] if i < len(l_scores) else None
        if ws is None or ls is None:
            continue
        w_val = ws.get("score")
        l_val = ls.get("score")
        if w_val is None or l_val is None:
            continue
        # Combine winner+loser games like WTA format: "64" means 6-4
        #
        # Special case: match-tiebreaks (super tiebreaks) can be 10+ points
        # (e.g. 11-9). Those must not be encoded as compact digits because
        # downstream rendering would interpret "119" as "1-1".
        if w_val >= 10 or l_val >= 10:
            parts.append(f"{w_val}-{l_val}")
            continue
        tb = ls.get("losingScore")
        if tb is not None and tb > 0:
            parts.append(f"{w_val}{l_val}({tb})")
        else:
            parts.append(f"{w_val}{l_val}")
    return " ".join(parts)


def _build_itf_match_entry(match, teams, round_num, match_idx):
    """Build a match entry dict from an ITF match, handling PC, WO, and RET."""
    result_code = match.get("resultStatusCode")
    play_code = match.get("playStatusCode")

    # A match has a result if it was played (PC) or decided by walkover/retirement
    has_result = play_code == "PC" or result_code in ("WO", "RET", "DEF")
    if not has_result:
        return None

    winner_idx = None
    for t_idx, team in enumerate(teams):
        if team.get("isWinner"):
            winner_idx = t_idx
            break
    if winner_idx is None:
        return None

    winner_team = teams[winner_idx]
    wp = (winner_team.get("players") or [None])[0]
    if not wp:
        return None

    # Abbreviate: use only first letter of first given name, like WTA "J. Riera"
    given = wp.get("givenName") or ""
    abbrev = given[0] + "." if given else ""
    family = wp.get("familyName") or ""
    winner_name = f"{abbrev} {family}".strip()

    score = _parse_itf_score(teams, winner_idx)
    if result_code == "RET":
        score += " RET" if score else "RET"
    elif result_code == "WO":
        score = "W/O"
    elif result_code == "DEF":
        score += " DEF" if score else "DEF"

    return {
        "round": round_num,
        "match_num": match_idx,
        "winner_name": winner_name,
        "score": score,
    }


def _drawsheet_has_arg_in_round1(data):
    """Return True when round 1 contains at least one ARG player."""
    if not isinstance(data, dict):
        return False
    ko_groups = data.get("koGroups") or []
    if not ko_groups:
        return False
    rounds_data = ko_groups[0].get("rounds") or []
    if not rounds_data:
        return False
    for match in (rounds_data[0].get("matches") or []):
        for team in (match.get("teams") or []):
            for player in (team.get("players") or []):
                if isinstance(player, dict) and str(player.get("nationality") or "").upper() == "ARG":
                    return True
    return False


def _parse_itf_draw(data):
    """Convert ITF drawsheet JSON to the same format as parse_draw_pdf output."""
    if not data or not isinstance(data, dict):
        return None

    if not _drawsheet_has_arg_in_round1(data):
        return None

    ko_groups = data.get("koGroups") or []
    if not ko_groups:
        return None

    rounds_data = ko_groups[0].get("rounds") or []
    if not rounds_data:
        return None

    # Round 1 defines the draw positions
    r1 = rounds_data[0]
    r1_matches = r1.get("matches") or []
    draw_size = len(r1_matches) * 2

    players = []
    byes = []
    all_matches = []
    round_labels = []

    # Parse R1 to build player list and byes
    for m_idx, match in enumerate(r1_matches):
        teams = match.get("teams") or []
        if len(teams) < 2:
            continue

        is_bye = match.get("resultStatusCode") == "BYE"
        pos1 = m_idx * 2 + 1
        pos2 = m_idx * 2 + 2

        for t_idx, team in enumerate(teams):
            pos = pos1 if t_idx == 0 else pos2
            team_players = team.get("players") or []
            player = team_players[0] if team_players and team_players[0] else None

            if player:
                family = (player.get("familyName") or "").strip()
                given = (player.get("givenName") or "").strip()
                if family and given:
                    name = f"{family.upper()}, {given}"
                elif family:
                    name = family.upper()
                else:
                    name = given
                country = player.get("nationality", "")
                seed = str(team.get("seeding")) if team.get("seeding") else ""
                entry_raw = team.get("entryStatus") or ""
                entry = _ITF_ENTRY_MAP.get(entry_raw, entry_raw)
                players.append({
                    "pos": pos,
                    "seed": seed,
                    "entry": entry,
                    "name": name,
                    "country": country,
                })
            elif is_bye:
                byes.append(pos)

        # Build match result for R1
        match_entry = _build_itf_match_entry(match, teams, 1, m_idx)
        if match_entry:
            all_matches.append(match_entry)

    # Parse subsequent rounds
    for r_idx in range(1, len(rounds_data)):
        rnd = rounds_data[r_idx]
        rnd_matches = rnd.get("matches") or []
        round_num = r_idx + 1

        for m_idx, match in enumerate(rnd_matches):
            teams = match.get("teams") or []
            if len(teams) < 2:
                continue
            match_entry = _build_itf_match_entry(match, teams, round_num, m_idx)
            if match_entry:
                all_matches.append(match_entry)

    # Build round labels
    round_label_map = {
        "1st Round": "Round of " + str(draw_size),
        "2nd Round": "Round of " + str(draw_size // 2),
        "3rd Round": "Round of " + str(draw_size // 4),
        "Quarter-finals": "Quarterfinals",
        "Semi-finals": "Semifinals",
        "Final": "Final",
    }
    for rnd in rounds_data:
        desc = rnd.get("roundDesc", "")
        label = round_label_map.get(desc, desc)
        round_labels.append(label)

    num_rounds = len(rounds_data)

    return {
        "tournament_name": "",
        "location": "",
        "dates": "",
        "prize": "",
        "surface": "",
        "draw_type": "",
        "draw_size": draw_size,
        "players": players,
        "matches": all_matches,
        "byes": sorted(byes),
        "round_labels": round_labels,
        "num_rounds": num_rounds,
    }


def _draw_is_complete(draw_data, is_qualifying=False):
    """Return True if the draw is fully finished.

    For main draws: the final round having at least one result is sufficient,
    since a final cannot be played without all prior rounds being done.

    For qualifying: ALL expected matches in the final qualifying round must be
    present, because multiple simultaneous final-round matches exist and any one
    could finish before the others.  The expected count is inferred from the
    number of first-round matches (draw_size / 2^(num_rounds-1)).
    """
    if not isinstance(draw_data, dict):
        return False
    matches = draw_data.get("matches") or []
    num_rounds = draw_data.get("num_rounds") or 0
    if not num_rounds:
        return False
    final_round_matches = [m for m in matches if m.get("round") == num_rounds]
    if not final_round_matches:
        return False
    if is_qualifying and num_rounds > 1:
        r1_count = sum(1 for m in matches if m.get("round") == 1)
        expected_final = r1_count // (2 ** (num_rounds - 1))
        if expected_final > 0 and len(final_round_matches) < expected_final:
            return False
    return True


def fetch_itf_tournament_draws(
    tournament_id,
    is_multiweek=False,
    driver=None,
    cached_draws=None,
    tournament_name="",
    return_meta=False,
):
    """Fetch and parse ITF draws for a tournament. Returns dict like WTA draws.

    Pass cached_draws (dict keyed by dtype_code like "MDS"/"QS") to skip
    re-fetching draw types that are already complete.

    When return_meta=True, returns (draws, meta) where meta includes any
    blocked-response records observed during the fetch.
    """
    draws = {}
    blocked_responses = []
    blocked_response_keys = set()
    cached_draws = cached_draws or {}
    # Keep week probing conservative.
    # Regular events should normally live at week=0, while multi-week circuits
    # use week=1 and, if needed, week=2. We do not jump from week=0 straight to
    # week=2 because that creates extra blocked requests with very little payoff.
    week_candidates = [1, 2] if is_multiweek else [0]

    if driver is not None:
        _prime_itf_draw_session(driver, tournament_id)

    for classification, dtype_code, dtype_label in _ITF_DRAW_TYPES:
        # Skip fetching if the cached draw for this type is already complete.
        if _draw_is_complete(cached_draws.get(dtype_code), is_qualifying=(dtype_code == "QS")):
            draws[dtype_code] = cached_draws[dtype_code]
            continue

        for week_number in week_candidates:
            # Reuse any drawsheet already fetched earlier in this workflow run by
            # populate_data/itf_load_new.py - avoids duplicate calls to the same
            # GetDrawsheet endpoint.
            raw = get_cached_drawsheet(tournament_id, classification, week_number)
            if not (raw and raw.get("koGroups")):
                # ITF API is highly sensitive to bot-rate patterns. Try once,
                # and if we detect a block page, re-prime the session and retry
                # exactly once before moving on.
                raw = None
                blocked = False
                for attempt in range(2):
                    if attempt == 1 and blocked and driver is not None:
                        _prime_itf_draw_session(driver, tournament_id)
                    if driver is not None:
                        candidate = _fetch_itf_drawsheet_via_driver(
                            tournament_id, classification, week_number, driver
                        )
                    else:
                        candidate = _fetch_itf_drawsheet(tournament_id, classification, week_number)
                    if candidate is _ITF_DRAW_BLOCKED:
                        blocked = True
                        raw = None
                        blocked_key = (dtype_code, str(week_number))
                        if blocked_key not in blocked_response_keys:
                            blocked_response_keys.add(blocked_key)
                            blocked_responses.append({
                                "endpoint": "itf",
                                "tournament_id": str(tournament_id),
                                "tournament_name": tournament_name,
                                "code": dtype_code,
                                "week_number": str(week_number),
                            })
                    else:
                        blocked = False
                        raw = candidate
                    if raw and raw.get("koGroups"):
                        break
                    if not blocked:
                        break
                    time.sleep(random.uniform(0.5, 1.4))
                if not (raw and raw.get("koGroups")):
                    raw = get_cached_drawsheet(
                        tournament_id,
                        classification,
                        week_number,
                        allow_stale=True,
                    )
                if raw and raw.get("koGroups"):
                    save_drawsheet(tournament_id, classification, week_number, raw)
            if not (raw and raw.get("koGroups")):
                continue
            try:
                parsed = _parse_itf_draw(raw)
                if parsed and parsed["players"]:
                    draws[dtype_code] = parsed
                    break
            except Exception as e:
                print(f"Error parsing ITF {dtype_label} for {tournament_id}: {e}")
    if return_meta:
        return draws, {"blocked_responses": blocked_responses}
    return draws
