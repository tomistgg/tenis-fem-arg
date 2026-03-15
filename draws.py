"""Parse WTA draw PDFs and ITF draw JSON data."""

import math
import re
import requests
import fitz


_DRAW_TYPES = [
    ("MDS", "Main Draw"),
    ("QS", "Qualifying"),
]

_PDF_BASE = "https://wtafiles.wtatennis.com/pdf/draws/{year}/{tid}/{dtype}.pdf"


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


def _is_score(text):
    """Check if a line is a match score."""
    text = text.strip()
    if not text:
        return False
    return bool(re.match(
        r'^[\d]+(?:\(\d+\))?\s+[\d]+(?:\(\d+\))?(?:\s+[\d]+(?:\(\d+\))?)?(?:\s+RET)?(?:\s+DEF)?$',
        text
    ))


def _is_winner_name(text):
    """Check if a line is a winner name like 'A. Sabalenka' or 'Xin. Wang'."""
    text = text.strip()
    if not text:
        return False
    return bool(re.match(r'^[A-Z][a-z]*\.\s+\S', text))


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
                r'^(\d+)\s+(WC|LL|PR|SE|ALT|Alt|Q)\s+(\d+)\s+([A-Z][A-Z\s]+,\s*.+)$', line)
            if combo_match:
                current_pos = int(combo_match.group(1))
                current_entry = combo_match.group(2)
                current_seed = combo_match.group(3)
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
            name_match = re.match(r'^(?:(\d+)\s+)?([A-Z][A-Z\s]+,\s*.+)$', line)
            # Handle wrapped names: "LASTNAME," on one line, "Firstname" on next
            if not name_match and current_pos is not None:
                wrap_match = re.match(r'^(?:(\d+)\s+)?([A-Z][A-Z\s]+,)\s*$', line)
                if wrap_match and i < len(lines):
                    next_line = lines[i].strip()
                    if next_line and re.match(r'^[A-Z][a-z]', next_line):
                        combined = wrap_match.group(2) + ' ' + next_line
                        name_match = re.match(r'^(?:(\d+)\s+)?([A-Z][A-Z\s]+,\s*.+)$',
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
                # Attach score to the last name entry
                if result_entries and not result_entries[-1]["score"]:
                    result_entries[-1]["score"] = line
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
        matches = _group_into_rounds(entries, r1_per_page, page_match_offset)
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


def _group_into_rounds(entries, r1_count, match_offset):
    """Group result entries into rounds.

    R1 has r1_count entries, R2 has r1_count/2, R3 has r1_count/4, etc.
    """
    matches = []
    round_num = 1
    expected = r1_count
    pos = 0

    while pos < len(entries) and expected >= 1:
        round_entries = entries[pos:pos + expected]
        for match_num, entry in enumerate(round_entries):
            if round_num == 1:
                actual_match_num = match_num + match_offset
            else:
                actual_match_num = match_num + match_offset // (2 ** (round_num - 1))
            matches.append({
                "round": round_num,
                "match_num": actual_match_num,
                "winner_name": entry["name"],
                "score": entry["score"],
            })
        pos += expected
        expected = expected // 2
        round_num += 1

    return matches


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


def _fetch_itf_drawsheet(tournament_id, classification, week_number=0):
    """Fetch an ITF drawsheet via POST API (no Selenium needed)."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Referer": f"https://www.itftennis.com/en/tournament/draws-and-results/print/?tournamentId={tournament_id}&circuitCode=WT",
        "Origin": "https://www.itftennis.com",
        "Content-Type": "application/json",
    }
    payload = {
        "circuitCode": "WT",
        "eventClassificationCode": classification,
        "matchTypeCode": "S",
        "tourType": "WT",
        "tournamentId": str(tournament_id),
        "weekNumber": week_number,
    }
    try:
        resp = requests.post(_ITF_DRAWSHEET_URL, json=payload, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception:
        return None


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
    given = wp.get("givenName", "")
    abbrev = given[0] + "." if given else ""
    winner_name = f"{abbrev} {wp.get('familyName', '')}"

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


def _parse_itf_draw(data):
    """Convert ITF drawsheet JSON to the same format as parse_draw_pdf output."""
    if not data or not isinstance(data, dict):
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
                family = player.get("familyName", "")
                given = player.get("givenName", "")
                name = f"{family.upper()}, {given}"
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


def fetch_itf_tournament_draws(tournament_id, is_multiweek=False):
    """Fetch and parse ITF draws for a tournament. Returns dict like WTA draws."""
    draws = {}
    for classification, dtype_code, dtype_label in _ITF_DRAW_TYPES:
        week_number = 1 if is_multiweek else 0
        raw = _fetch_itf_drawsheet(tournament_id, classification, week_number)
        if raw and raw.get("koGroups"):
            try:
                parsed = _parse_itf_draw(raw)
                if parsed and parsed["players"]:
                    draws[dtype_code] = parsed
            except Exception as e:
                print(f"Error parsing ITF {dtype_label} for {tournament_id}: {e}")
    return draws


def get_itf_tournament_id(tournament_key, driver):
    """Get ITF tournamentId from tournamentKey via Selenium."""
    api_url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetEventFilters?tournamentKey={tournament_key}"
    try:
        driver.get(api_url)
        import time
        time.sleep(1)
        raw = driver.find_element("tag name", "body").text.strip()
        import json
        data = json.loads(raw)
        return data.get("tournamentId")
    except Exception:
        return None
