"""Parse WTA draw PDFs using plain text extraction with PyMuPDF."""

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
    """Parse a single page's text into players, byes, result entries, and round labels.

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

    return players, byes, result_entries, round_labels


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
    page_results = []
    round_labels = []

    for page_idx in range(num_pages):
        text = doc[page_idx].get_text() or ""
        players, byes, result_entries, labels = _parse_page(text)
        all_players.extend(players)
        all_byes.update(byes)
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
        [p["pos"] for p in unique_players] + list(all_byes),
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
