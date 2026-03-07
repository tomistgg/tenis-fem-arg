"""Parse WTA draw PDFs and fetch draws for active tournaments."""

import io
import re
import requests
import pdfplumber


_DRAW_TYPES = [
    ("MS", "Main Draw"),
    ("QS", "Qualifying"),
]

_PDF_BASE = "https://wtafiles.wtatennis.com/pdf/draws/{year}/{tid}/{dtype}.pdf"


def _extract_tournament_id(url):
    """Extract numeric tournament ID from a WTA tournament URL."""
    m = re.search(r'/tournaments/(\d+)/', url)
    return m.group(1) if m else None


def fetch_draw_pdf_bytes(tournament_id, year, draw_type="MS"):
    """Download a WTA draw PDF. Returns bytes or None if not found."""
    url = _PDF_BASE.format(year=year, tid=tournament_id, dtype=draw_type)
    try:
        resp = requests.get(url, timeout=15)
        if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:5] == b'%PDF-':
            return resp.content
        return None
    except Exception:
        return None


def parse_draw_pdf(pdf_bytes):
    """
    Parse a WTA draw PDF and return structured draw data.

    Returns dict with:
      - tournament_name, location, dates, prize, surface, draw_type
      - draw_size (number of players)
      - players: list of {pos, seed, entry, name, country}
      - matches: list of {round, match_num, player1_pos, player2_pos, winner_name, score}
    """
    pdf = pdfplumber.open(io.BytesIO(pdf_bytes))
    page = pdf.pages[0]
    words = page.extract_words(keep_blank_chars=True, x_tolerance=2, y_tolerance=2)

    # Parse header from text
    text = page.extract_text() or ""
    lines = text.split('\n')

    tournament_name = lines[0].strip() if lines else ""
    location = lines[1].strip() if len(lines) > 1 else ""

    dates = prize = surface = ""
    if len(lines) > 2:
        header_line = lines[2]
        parts = [p.strip() for p in header_line.split('|')]
        if len(parts) >= 1:
            dates = parts[0].strip()
        if len(parts) >= 2:
            prize = parts[1].strip()
        if len(parts) >= 3:
            surface = parts[2].strip()

    draw_type = ""
    if len(lines) > 3:
        for line in lines[3:6]:
            if 'DRAW' in line.upper():
                draw_type = line.strip()
                break

    # Separate words into columns by x-coordinate
    # Player entries: x0 < 175 (pos, seed, entry, name, country)
    # Results: x0 >= 175
    player_words = [w for w in words if w['x0'] < 175 and w['top'] > 90]
    result_words = [w for w in words if w['x0'] >= 175 and w['top'] > 90]

    # Find the y-boundary where the footer begins (round prize row, seeded players, etc.)
    footer_y = page.height
    for w in words:
        if w['text'] == 'Q1' and w['top'] > 200:
            footer_y = min(footer_y, w['top'])
            break
    for w in words:
        if w['text'] in ('Seeded', 'WTA', 'Supervisor') and w['top'] > 200:
            footer_y = min(footer_y, w['top'])
    # Also check for round labels like "R16", "QF", "SF", "Final" at bottom
    for w in words:
        if w['text'] in ('R16', 'QF', 'SF', 'Final', 'R32', 'R64', 'R128') and w['top'] > 200 and w['x0'] < 120:
            footer_y = min(footer_y, w['top'])

    player_words = [w for w in player_words if w['top'] < footer_y]
    result_words = [w for w in result_words if w['top'] < footer_y]

    # Parse player entries
    # Group by y-coordinate (rows)
    players = _parse_players(player_words)
    draw_size = len(players)

    # Parse match results from result columns
    matches = _parse_results(result_words, players, draw_size)

    pdf.close()

    return {
        "tournament_name": tournament_name,
        "location": location,
        "dates": dates,
        "prize": prize,
        "surface": surface,
        "draw_type": draw_type,
        "draw_size": draw_size,
        "players": players,
        "matches": matches,
    }


def _parse_players(player_words):
    """Parse player entries from left-column words."""
    # Group words into rows by y-coordinate using position numbers as anchors
    # First find all position-number words (at x < 40)
    pos_ys = []
    for w in player_words:
        if w['x0'] < 40 and re.match(r'^\d+', w['text']):
            pos_ys.append(w['top'])
    pos_ys.sort()

    # Assign each word to nearest position row
    rows = {y: [] for y in pos_ys}
    for w in player_words:
        best_y = min(pos_ys, key=lambda y: abs(y - w['top'])) if pos_ys else None
        if best_y is not None and abs(w['top'] - best_y) < 8:
            rows[best_y].append(w)

    # Sort rows by y
    sorted_rows = sorted(rows.items(), key=lambda x: x[0])

    players = []
    for _, row_words in sorted_rows:
        row_words.sort(key=lambda w: w['x0'])
        # Concatenate text
        full_text = ' '.join(w['text'] for w in row_words)

        # Try to match a player line: starts with position number
        m = re.match(
            r'^(\d+)\s*'              # position
            r'(WC|Q|LL|SE|PR|Alt)?\s*'  # optional entry joined to pos like "10WC"
            r'(?:(\d+)\s*)?'          # optional seed
            r'(WC|Q|LL|SE|PR|Alt)?\s*'  # optional entry after seed
            r'([A-Z][A-Z\' -]+(?:,\s*[A-Za-z -]+)?)'  # name: LASTNAME, First
            r'(?:\s+([A-Z]{3}))?',    # optional country
            full_text
        )
        if not m:
            # Try alternate pattern: pos + WC concatenated, like "7WC"
            m = re.match(
                r'^(\d+)(WC|Q|LL|SE|PR|Alt)\s*'
                r'(?:(\d+)\s*)?'
                r'(WC|Q|LL|SE|PR|Alt)?\s*'
                r'([A-Z][A-Z\' -]+(?:,\s*[A-Za-z -]+)?)'
                r'(?:\s+([A-Z]{3}))?',
                full_text
            )
        if not m:
            continue

        pos_str = m.group(1)
        entry1 = m.group(2) or ""
        seed_str = m.group(3) or ""
        entry2 = m.group(4) or ""
        name = m.group(5).strip()
        country = m.group(6) or ""

        entry = entry1 or entry2

        # Disambiguate: if there's a number right after pos that looks like a seed
        # Re-parse from raw words for accuracy
        pos = int(pos_str)

        # Determine seed from the word at x ~46-53
        seed = ""
        for w in row_words:
            if 46 <= w['x0'] <= 53 and w['text'].isdigit():
                seed = w['text']
                break

        # Get name from words at x >= 55 and < 163
        name_parts = []
        for w in row_words:
            if 54 <= w['x0'] < 160:
                name_parts.append(w['text'])
        if name_parts:
            name = ' '.join(name_parts)

        # Get country from words at x ~162-173
        country = ""
        for w in row_words:
            if 160 <= w['x0'] <= 175 and re.match(r'^[A-Z]{2,3}$', w['text']):
                country = w['text']
                break

        # Get entry from position word (e.g. "7WC", "10WC")
        entry = ""
        for w in row_words:
            if w['x0'] < 55:
                em = re.search(r'\d(WC|LL|PR|SE|Alt)$', w['text'])
                if em:
                    entry = em.group(1)

        players.append({
            "pos": pos,
            "seed": seed,
            "entry": entry,
            "name": name,
            "country": country,
        })

    # Sort by position and deduplicate
    players.sort(key=lambda p: p['pos'])
    seen = set()
    unique = []
    for p in players:
        if p['pos'] not in seen:
            seen.add(p['pos'])
            unique.append(p)

    return unique


def _parse_results(result_words, players, draw_size):
    """Parse match results from result-column words."""
    if not result_words:
        return []

    # Identify columns by x-coordinate clusters
    x_values = [w['x0'] for w in result_words]
    if not x_values:
        return []

    # Cluster x values into columns (R1, R2, R3, etc.)
    x_sorted = sorted(set(round(x) for x in x_values))
    columns = []
    if x_sorted:
        col_start = x_sorted[0]
        for i in range(1, len(x_sorted)):
            if x_sorted[i] - x_sorted[i - 1] > 50:
                columns.append(col_start)
                col_start = x_sorted[i]
        columns.append(col_start)

    num_rounds = len(columns)
    if num_rounds == 0:
        return []

    # Group result words by column and y-coordinate
    matches = []
    for round_idx, col_x in enumerate(columns):
        col_words = [w for w in result_words if abs(round(w['x0']) - col_x) < 50
                     and (round_idx + 1 >= len(columns) or w['x0'] < columns[round_idx + 1] - 25)]

        # Group into pairs: winner name line + score line
        # Sort by y
        col_words.sort(key=lambda w: w['top'])

        # Group into rows
        rows = []
        current_row = []
        last_y = -100
        for w in col_words:
            if w['top'] - last_y > 5:
                if current_row:
                    rows.append(current_row)
                current_row = [w]
            else:
                current_row.append(w)
            last_y = w['top']
        if current_row:
            rows.append(current_row)

        # Pair rows: name + score
        row_texts = []
        for row in rows:
            row.sort(key=lambda w: w['x0'])
            row_texts.append(' '.join(w['text'] for w in row).strip())

        # Filter out empty/whitespace rows
        row_texts = [t for t in row_texts if t.strip()]

        i = 0
        match_num = 0
        while i < len(row_texts):
            winner_text = row_texts[i]
            score = ""
            # Check if next line is a score
            if i + 1 < len(row_texts) and _is_score(row_texts[i + 1]):
                score = row_texts[i + 1]
                i += 2
            else:
                i += 1

            # Skip if this looks like a score itself
            if _is_score(winner_text):
                continue

            # Clean winner name: remove trailing seed number
            winner_name = re.sub(r'\s+\d+$', '', winner_text).strip()

            matches.append({
                "round": round_idx + 1,
                "match_num": match_num,
                "winner_name": winner_name,
                "score": score,
            })
            match_num += 1

    return matches


def _is_score(text):
    """Check if text looks like a tennis score."""
    text = text.strip()
    # Tennis scores: "64 64", "76(5) 60", "57 75 61", "06 63 50 RET"
    return bool(re.match(
        r'^[\d]+(?:\(\d+\))?\s+[\d]+(?:\(\d+\))?(?:\s+[\d]+(?:\(\d+\))?)?(?:\s+RET)?(?:\s+DEF)?$',
        text
    ))


def fetch_tournament_draws(tournament_url, year):
    """
    Fetch all available draws for a WTA tournament.
    Returns dict: {draw_type_code: parsed_draw_data, ...}
    """
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
