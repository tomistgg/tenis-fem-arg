"""Fetch a WTA draw PDF and write LATAM Round 1 pairings to a text file.

Outputs only Round 1 matches that include at least one Latin American player
(based on the 3-letter country codes in the PDF), formatted as "Player vs Player",
including:
- BYE positions (explicit "Bye" in the PDF)
- Qualifier placeholders (explicit "Qualifier" in the PDF, or missing player in a non-bye slot)
"""

import argparse
import hashlib
import re
import sys
from datetime import datetime
from typing import List, Optional
import os
import ssl
import smtplib
import subprocess
from email.message import EmailMessage
from email.utils import formataddr

import requests
import fitz

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import draws  # noqa: E402


_LATAM_COUNTRY_CODES = {
    # South America
    "ARG", "BOL", "BRA", "CHI", "COL", "ECU", "GUY", "PAR", "PER", "SUR", "URU", "VEN",
    # Central America + Mexico
    "MEX", "CRC", "GUA", "HON", "NCA", "PAN", "ESA",
    # Caribbean (Spanish/Latin presence in tennis)
    "CUB", "DOM", "PUR", "HAI",
}


# Tennis PDFs use 3-letter country codes; many are ISO-3166 alpha-3, but some follow
# IOC/ITF conventions (e.g. INA for Indonesia). Map to ISO-3166 alpha-2 for flag emojis.
_CODE3_TO_CODE2 = {
    "AFG": "AF",
    "ALB": "AL",
    "ALG": "DZ",  # IOC
    "DZA": "DZ",
    "AND": "AD",
    "ANG": "AO",  # IOC
    "AGO": "AO",
    "ANT": "AG",  # IOC often used for Antigua & Barbuda
    "ATG": "AG",
    "ARG": "AR",
    "ARM": "AM",
    "ARU": "AW",  # Aruba
    "ABW": "AW",
    "AUS": "AU",
    "AUT": "AT",
    "AZE": "AZ",
    "BAH": "BS",  # Bahamas
    "BHS": "BS",
    "BRN": "BH",  # Bahrain (IOC)
    "BHR": "BH",
    "BAN": "BD",  # Bangladesh (IOC)
    "BGD": "BD",
    "BAR": "BB",  # Barbados
    "BRB": "BB",
    "BLR": "BY",
    "BEL": "BE",
    "BIZ": "BZ",  # Belize (IOC)
    "BLZ": "BZ",
    "BEN": "BJ",
    "BER": "BM",  # Bermuda
    "BMU": "BM",
    "BHU": "BT",
    "BOL": "BO",
    "BIH": "BA",
    "BOT": "BW",  # Botswana
    "BWA": "BW",
    "BRA": "BR",
    "BRU": "BN",  # Brunei
    "BRN2": "BN",  # safeguard
    "BUL": "BG",  # Bulgaria (IOC)
    "BGR": "BG",
    "BUR": "BF",  # Burkina Faso (IOC)
    "BFA": "BF",
    "BDI": "BI",
    "CAM": "KH",  # Cambodia (IOC)
    "KHM": "KH",
    "CMR": "CM",
    "CAN": "CA",
    "CPV": "CV",
    "CAY": "KY",  # Cayman Islands
    "CYM": "KY",
    "CAF": "CF",
    "CHA": "TD",  # Chad (IOC)
    "TCD": "TD",
    "CHI": "CL",  # Chile (IOC)
    "CHL": "CL",
    "CHN": "CN",
    "TPE": "TW",  # Chinese Taipei (IOC)
    "COL": "CO",
    "COM": "KM",
    "CGO": "CG",  # Republic of the Congo (IOC)
    "COG": "CG",
    "COD": "CD",  # DR Congo
    "COK": "CK",
    "CRC": "CR",
    "CIV": "CI",
    "CRO": "HR",
    "CUB": "CU",
    "CYP": "CY",
    "CZE": "CZ",
    "DEN": "DK",
    "DJI": "DJ",
    "DMA": "DM",
    "DOM": "DO",
    "ECU": "EC",
    "EGY": "EG",
    "ESA": "SV",  # El Salvador (IOC)
    "SLV": "SV",
    "GEQ": "GQ",  # Equatorial Guinea
    "GNQ": "GQ",
    "ERI": "ER",
    "EST": "EE",
    "SWZ": "SZ",  # Eswatini (IOC keeps SWZ)
    "ETH": "ET",
    "FIJ": "FJ",
    "FIN": "FI",
    "FRA": "FR",
    "GAB": "GA",
    "GAM": "GM",  # Gambia (IOC)
    "GMB": "GM",
    "GEO": "GE",
    "GER": "DE",  # Germany (IOC)
    "DEU": "DE",
    "GHA": "GH",
    "GBR": "GB",
    "GRE": "GR",
    "GRL": "GL",
    "GUA": "GT",  # Guatemala (IOC)
    "GTM": "GT",
    "GUI": "GN",  # Guinea (IOC)
    "GIN": "GN",
    "GUY": "GY",
    "HAI": "HT",
    "HON": "HN",
    "HKG": "HK",
    "HUN": "HU",
    "ISL": "IS",
    "IND": "IN",
    "INA": "ID",  # Indonesia (IOC)
    "IDN": "ID",
    "IRI": "IR",  # Iran (IOC)
    "IRN": "IR",
    "IRQ": "IQ",
    "IRL": "IE",
    "ISR": "IL",
    "ITA": "IT",
    "JAM": "JM",
    "JPN": "JP",
    "JOR": "JO",
    "KAZ": "KZ",
    "KEN": "KE",
    "KIR": "KI",
    "KOS": "XK",  # Kosovo (not ISO; common sports code)
    "KUW": "KW",
    "KGZ": "KG",
    "LAO": "LA",
    "LAT": "LV",  # Latvia (IOC)
    "LVA": "LV",
    "LBN": "LB",
    "LES": "LS",  # Lesotho (IOC)
    "LSO": "LS",
    "LBR": "LR",
    "LBA": "LY",
    "LIE": "LI",
    "LTU": "LT",
    "LUX": "LU",
    "MAC": "MO",
    "MKD": "MK",
    "MAD": "MG",  # Madagascar (IOC)
    "MDG": "MG",
    "MAW": "MW",
    "MAS": "MY",  # Malaysia (IOC)
    "MYS": "MY",
    "MDV": "MV",
    "MLI": "ML",
    "MLT": "MT",
    "MHL": "MH",
    "MTN": "MR",  # Mauritania (IOC)
    "MRT": "MR",
    "MRI": "MU",  # Mauritius (IOC)
    "MUS": "MU",
    "MEX": "MX",
    "FSM": "FM",
    "MDA": "MD",
    "MON": "MC",  # Monaco (IOC)
    "MCO": "MC",
    "MGL": "MN",
    "MNE": "ME",
    "MAR": "MA",
    "MOZ": "MZ",
    "MYA": "MM",  # Myanmar (IOC)
    "MMR": "MM",
    "NAM": "NA",
    "NRU": "NR",
    "NEP": "NP",
    "NED": "NL",  # Netherlands (IOC)
    "NLD": "NL",
    "NZL": "NZ",
    "NCA": "NI",  # Nicaragua (IOC)
    "NIC": "NI",
    "NIG": "NE",  # Niger (IOC)
    "NER": "NE",
    "NGR": "NG",  # Nigeria (IOC)
    "NGA": "NG",
    "PRK": "KP",
    "KOR": "KR",
    "NOR": "NO",
    "OMA": "OM",
    "PAK": "PK",
    "PLE": "PS",  # Palestine (IOC)
    "PSE": "PS",
    "PAN": "PA",
    "PNG": "PG",
    "PAR": "PY",
    "PER": "PE",
    "PHI": "PH",  # Philippines (IOC)
    "PHL": "PH",
    "POL": "PL",
    "POR": "PT",
    "PUR": "PR",  # Puerto Rico (IOC)
    "PRI": "PR",
    "QAT": "QA",
    "ROU": "RO",
    "RSA": "ZA",  # South Africa (IOC)
    "ZAF": "ZA",
    "RUS": "RU",
    "RWA": "RW",
    "SKN": "KN",
    "LCA": "LC",
    "VIN": "VC",
    "SAM": "WS",
    "SMR": "SM",
    "STP": "ST",
    "KSA": "SA",  # Saudi Arabia (IOC)
    "SAU": "SA",
    "SEN": "SN",
    "SRB": "RS",
    "SEY": "SC",
    "SLE": "SL",
    "SGP": "SG",
    "SVK": "SK",
    "SLO": "SI",  # Slovenia (IOC)
    "SVN": "SI",
    "SOL": "SB",  # Solomon Islands (IOC)
    "SLB": "SB",
    "SOM": "SO",
    "ESP": "ES",
    "SRI": "LK",
    "SUD": "SD",
    "SUR": "SR",
    "SWE": "SE",
    "SUI": "CH",  # Switzerland (IOC)
    "CHE": "CH",
    "SYR": "SY",
    "TJK": "TJ",
    "TAN": "TZ",  # Tanzania (IOC)
    "TZA": "TZ",
    "THA": "TH",
    "TLS": "TL",
    "TOG": "TG",
    "TGA": "TO",
    "TRI": "TT",
    "TUN": "TN",
    "TUR": "TR",
    "TKM": "TM",
    "UGA": "UG",
    "UKR": "UA",
    "UAE": "AE",
    "URU": "UY",
    "USA": "US",
    "UZB": "UZ",
    "VAN": "VU",
    "VEN": "VE",
    "VIE": "VN",  # Vietnam (IOC)
    "VNM": "VN",
    "ISV": "VI",
    "YEM": "YE",
    "ZAM": "ZM",
    "ZIM": "ZW",
}


def _flag_emoji_from_country_code(code3: str) -> str:
    code3 = (code3 or "").strip().upper()
    if not code3:
        return ""
    code2 = _CODE3_TO_CODE2.get(code3)
    if not code2 and len(code3) == 2 and code3.isalpha():
        code2 = code3
    if not code2 or len(code2) != 2 or not code2.isalpha():
        return ""
    code2 = code2.upper()
    base = 0x1F1E6
    return chr(base + (ord(code2[0]) - ord("A"))) + chr(base + (ord(code2[1]) - ord("A")))


def _title_case_words(s: str) -> str:
    parts = [p for p in re.split(r"\s+", (s or "").strip()) if p]
    out = []
    for part in parts:
        # Keep internal apostrophes/hyphens reasonably.
        sub = re.split(r"([-'])", part)
        rebuilt = []
        for token in sub:
            if token in ("-", "'"):
                rebuilt.append(token)
            else:
                rebuilt.append(token[:1].upper() + token[1:].lower() if token else "")
        out.append("".join(rebuilt))
    return " ".join(out)


def _family_name_from_draw_name(name: str) -> str:
    raw = (name or "").strip()
    if not raw:
        return ""
    # Expected: "FAMILY, Given" or occasionally already "Family, Given".
    family = raw.split(",", 1)[0].strip()
    return _title_case_words(family)


def _fetch_pdf_bytes_from_url(pdf_url: str) -> Optional[bytes]:
    try:
        resp = requests.get(pdf_url, timeout=20)
        if resp.status_code == 200 and len(resp.content) > 500 and resp.content[:5] == b"%PDF-":
            return resp.content
        return None
    except Exception:
        return None


def _format_side(player, is_bye: bool, is_qual_placeholder: bool) -> str:
    if is_bye:
        return "BYE"
    if player:
        family = _family_name_from_draw_name(player.get("name") or "")
        flag = _flag_emoji_from_country_code(player.get("country") or "")
        return (family or "UNKNOWN") + flag
    if is_qual_placeholder:
        return "Qualifier"
    return "UNKNOWN"


def _is_latam_player(player) -> bool:
    if not player:
        return False
    code = (player.get("country") or "").strip().upper()
    return code in _LATAM_COUNTRY_CODES


def build_round1_match_lines(draw_data: dict) -> List[str]:
    draw_size = int(draw_data.get("draw_size") or 0)
    if draw_size <= 0 or draw_size % 2 != 0:
        raise ValueError(f"Unexpected draw_size={draw_size!r}")

    players = draw_data.get("players") or []
    players_by_pos = {int(p.get("pos")): p for p in players if p and p.get("pos")}
    byes = set(int(x) for x in (draw_data.get("byes") or []))
    qualifiers = set(int(x) for x in (draw_data.get("qualifiers") or []))

    lines: List[str] = []
    num_matches = draw_size // 2
    for match_idx in range(num_matches):
        pos1 = match_idx * 2 + 1
        pos2 = match_idx * 2 + 2

        p1 = players_by_pos.get(pos1)
        p2 = players_by_pos.get(pos2)

        is_bye1 = pos1 in byes
        is_bye2 = pos2 in byes

        # If the PDF explicitly marks the slot as "Qualifier", honor it.
        # Otherwise, mirror the site logic: missing player + not a bye => Qualifier.
        is_q1 = (pos1 in qualifiers) or (not p1 and not is_bye1)
        is_q2 = (pos2 in qualifiers) or (not p2 and not is_bye2)

        # Only include R1 pairings that feature at least one Latin American player.
        is_latam1 = _is_latam_player(p1)
        is_latam2 = _is_latam_player(p2)
        if not (is_latam1 or is_latam2):
            continue

        side1 = _format_side(p1, is_bye1, is_q1)
        side2 = _format_side(p2, is_bye2, is_q2)

        # Put the single LATAM player on the left (display-only).
        # If both are LATAM, preserve original drawsheet order.
        if (not is_latam1) and is_latam2:
            side1, side2 = side2, side1

        lines.append(f"{side1} vs {side2}")

    return lines


def write_lines_to_file(lines: List[str], out_path: str) -> None:
    # Use UTF-8 with BOM for Windows-friendly emoji display in common editors.
    with open(out_path, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(lines).rstrip() + "\n")


def _load_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _save_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_secret_file(path: str) -> str:
    """Read a one-line secret from disk, tolerating common Windows encodings.

    Notepad often saves as UTF-16; reading as UTF-8 would raise and look "empty".
    """
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except Exception:
        return ""

    if not raw:
        return ""

    # Heuristic: lots of NUL bytes usually means UTF-16 text.
    if b"\x00" in raw:
        try:
            return raw.decode("utf-16").strip()
        except Exception:
            pass

    for enc in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(enc).strip()
        except Exception:
            continue
    return ""


def send_email(
    *,
    smtp_host: str,
    smtp_port: int,
    smtp_user: str,
    smtp_password: str,
    mail_from: str,
    mail_to: List[str],
    subject: str,
    body: str,
    attachments: Optional[List[tuple]] = None,
    starttls: bool = True,
) -> None:
    msg = EmailMessage()
    msg["From"] = mail_from
    msg["To"] = ", ".join([t.strip() for t in (mail_to or []) if t and t.strip()])
    msg["Subject"] = subject
    # Explicit UTF-8 so flag emojis survive transport.
    msg.set_content(body, charset="utf-8")

    for att in attachments or []:
        filename, content_bytes, maintype, subtype = att
        msg.add_attachment(content_bytes, maintype=maintype, subtype=subtype, filename=filename)

    if int(smtp_port) == 465 and not starttls:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(smtp_host, int(smtp_port), context=context, timeout=30) as server:
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return

    with smtplib.SMTP(smtp_host, int(smtp_port), timeout=30) as server:
        if starttls:
            server.starttls(context=ssl.create_default_context())
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def write_heartbeat(path: str, *, tid: str, draw_letter: str, year: int, matches: int, changed: bool, emailed: bool) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"last_run={ts}\n"
        f"id={tid}\n"
        f"draw={draw_letter}\n"
        f"year={year}\n"
        f"matches={matches}\n"
        f"changed={str(bool(changed)).lower()}\n"
        f"emailed={str(bool(emailed)).lower()}\n"
    )
    _save_text(path, text)


def _sha256_bytes_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def render_pdf_pages_as_images(pdf_bytes: bytes, *, dpi: int = 150, image_format: str = "jpg") -> List[tuple]:
    """Render each PDF page to an image attachment tuple: (filename, bytes, maintype, subtype)."""
    fmt = (image_format or "jpg").strip().lower()
    if fmt in ("jpg", "jpeg"):
        fmt = "jpg"
        subtype = "jpeg"
        ext = "jpg"
        tobytes_arg = "jpeg"
    elif fmt == "png":
        subtype = "png"
        ext = "png"
        tobytes_arg = "png"
    else:
        raise ValueError("Unsupported --render-format. Use jpg or png.")

    dpi = int(dpi) if dpi else 150
    if dpi < 72:
        dpi = 72
    if dpi > 300:
        dpi = 300

    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)

    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        out: List[tuple] = []
        for i in range(doc.page_count):
            page = doc.load_page(i)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img_bytes = pix.tobytes(tobytes_arg)
            filename = f"page_{i + 1:02d}.{ext}"
            out.append((filename, img_bytes, "image", subtype))
        return out
    finally:
        doc.close()


def extract_pdf_subject(pdf_bytes: bytes) -> str:
    """Best-effort extraction of the human title shown on WTA PDFs.

    Prefer the PDF metadata title if present; otherwise scan the first page for a line
    containing 'AVAILABLE' (e.g. 'MAIN DRAW AVAILABLE: Miami Open presented by Itau').
    """
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return ""

    try:
        meta = doc.metadata or {}
        title = (meta.get("title") or "").strip()
        if title and re.search(r"\bAVAILABLE\b", title, flags=re.IGNORECASE):
            return title

        if doc.page_count <= 0:
            return ""
        text = doc.load_page(0).get_text() or ""
        for raw_line in text.split("\n"):
            line = (raw_line or "").strip()
            if not line:
                continue
            if re.search(r"\bAVAILABLE\b", line, flags=re.IGNORECASE):
                # Keep the PDF's original casing/punctuation.
                return line
        return ""
    finally:
        doc.close()


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="Tournament id (e.g. 609)")
    ap.add_argument("--draw", help="M for Main Draw (MDS), Q for Qualifying (QS)")
    ap.add_argument("--tournament-url", help="WTA tournament page URL containing /tournaments/<id>/...")
    ap.add_argument("--year", type=int, help="Draw year (used with --tournament-url)")
    ap.add_argument("--draw-type", default="MDS", help="MDS or QS (default: MDS)")
    ap.add_argument("--pdf-url", help="Direct PDF URL (overrides --tournament-url/--year/--draw-type)")
    ap.add_argument("--out", help="Output text file path (default depends on args)")
    ap.add_argument("--email-to", help="Send results to this email (comma-separated for multiple recipients)")
    ap.add_argument("--email-from", help="From address (default: smtp user)")
    ap.add_argument("--email-from-name", help="Display name for the From header (e.g. Tommy)")
    ap.add_argument("--email-subject", help="Email subject (default: derived from id/draw)")
    ap.add_argument("--smtp-host", help="SMTP host (e.g. smtp.gmail.com)")
    ap.add_argument("--smtp-port", type=int, help="SMTP port (e.g. 587 for STARTTLS, 465 for SSL)")
    ap.add_argument("--smtp-user", help="SMTP username (often your email address)")
    ap.add_argument("--smtp-pass", help="SMTP password (not recommended; prefer --smtp-pass-env)")
    ap.add_argument("--smtp-pass-env", help="Environment variable name containing SMTP password/app password")
    ap.add_argument("--smtp-pass-file", help="Path to a file containing SMTP password/app password")
    ap.add_argument("--smtp-no-starttls", action="store_true", help="Disable STARTTLS (use for SMTP_SSL:465)")
    ap.add_argument("--email-always", action="store_true", help="Email every run even if unchanged")
    ap.add_argument("--email-attach-pages", action="store_true", help="Attach an image of each PDF page to the email")
    ap.add_argument("--render-dpi", type=int, default=150, help="PDF render DPI for page images (default: 150)")
    ap.add_argument("--render-format", default="jpg", help="Page image format: jpg or png (default: jpg)")
    ap.add_argument("--state-dir", default=".email_state", help="Directory to store last-sent state (default: .email_state)")
    ap.add_argument("--stop-task-on-email", action="store_true", help="Disable the Windows scheduled task after sending an email")
    ap.add_argument("--task-name", default="WTA Draw Watcher", help="Windows Task Scheduler task name to disable (default: WTA Draw Watcher)")
    ap.add_argument("--no-pdf-ok", action="store_true", help="If the draw PDF is not available yet, exit 0 without emailing (recommended for scheduled polling)")
    ap.add_argument("--sentinel-file", help="Path to a file created after a successful email send (prevents re-sending without using .email_state)")
    ap.add_argument(
        "--heartbeat-file",
        help="Write/update this file every run with a timestamp and summary (useful for Task Scheduler verification)",
    )
    args = ap.parse_args(argv)

    # Task Scheduler may run with a different working directory (often System32).
    # Resolve state/heartbeat paths relative to this script so files land in the repo consistently.
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if args.state_dir and not os.path.isabs(str(args.state_dir)):
        args.state_dir = os.path.join(script_dir, str(args.state_dir))
    if args.heartbeat_file and not os.path.isabs(str(args.heartbeat_file)):
        args.heartbeat_file = os.path.join(script_dir, str(args.heartbeat_file))

    def disable_windows_task(task_name: str) -> bool:
        """Disable a scheduled task by name. Returns True if it appears to succeed."""
        try:
            proc = subprocess.run(
                ["schtasks.exe", "/Change", "/TN", str(task_name), "/Disable"],
                capture_output=True,
                text=True,
                timeout=20,
            )
            return proc.returncode == 0
        except Exception:
            return False

    def maybe_email(
        lines: List[str],
        *,
        tid: str,
        draw_letter: str,
        year: int,
        pdf_bytes: Optional[bytes],
        pdf_subject: str,
        tournament_name: str,
        sentinel_path: Optional[str],
    ) -> tuple[bool, bool]:
        if not args.email_to:
            return False, False
        if not args.smtp_host or not args.smtp_port or not args.smtp_user:
            raise ValueError("Missing SMTP config: --smtp-host, --smtp-port, --smtp-user are required for email.")

        smtp_password = ""
        if args.smtp_pass_file:
            smtp_password = _read_secret_file(str(args.smtp_pass_file))
        if args.smtp_pass_env:
            smtp_password = os.environ.get(str(args.smtp_pass_env), "") or ""
        if not smtp_password and args.smtp_pass:
            smtp_password = str(args.smtp_pass)
        if not smtp_password:
            raise ValueError("Missing SMTP password: use --smtp-pass-file (recommended), --smtp-pass-env, or --smtp-pass.")

        fallback_subject = ""
        tname = (tournament_name or "").strip()
        if tname and draw_letter in ("M", "Q"):
            if draw_letter == "M":
                fallback_subject = f"MAIN DRAW AVAILABLE: {tname}"
            else:
                fallback_subject = f"QUALIFYING MAIN DRAW AVAILABLE: {tname}"
        subject = args.email_subject or (pdf_subject or fallback_subject or f"WTA {tid} {draw_letter} LATAM R1 ({year})")
        mail_from_addr = args.email_from or args.smtp_user
        if args.email_from_name:
            mail_from = formataddr((str(args.email_from_name), str(mail_from_addr)))
        else:
            mail_from = str(mail_from_addr)
        recipients = [t.strip() for t in re.split(r"[,\s;]+", str(args.email_to)) if t and t.strip()]
        body = "\n".join(lines).rstrip() + "\n"

        # If we're going to disable the task after sending, a simple sentinel file is enough
        # and avoids keeping a .email_state folder around.
        if args.stop_task_on_email and sentinel_path:
            if os.path.exists(sentinel_path) and not args.email_always:
                return False, False
            changed = True
        else:
            state_key = f"{tid}_{draw_letter}_{year}.sha256"
            state_path = os.path.join(args.state_dir, state_key)
            pdf_hash = ""
            if args.email_attach_pages:
                if not pdf_bytes:
                    raise ValueError("--email-attach-pages requires the PDF bytes (unexpected missing PDF).")
                pdf_hash = _sha256_bytes_hex(pdf_bytes)
            new_hash = _sha256_hex(body + ("\nPDF_SHA256=" + pdf_hash if pdf_hash else ""))
            changed = True

            if not args.email_always:
                old_hash = _load_text(state_path).strip()
                if old_hash == new_hash:
                    changed = False
                    return changed, False

        attachments: List[tuple] = []
        if args.email_attach_pages:
            attachments = render_pdf_pages_as_images(
                pdf_bytes,
                dpi=int(args.render_dpi),
                image_format=str(args.render_format),
            )

        send_email(
            smtp_host=str(args.smtp_host),
            smtp_port=int(args.smtp_port),
            smtp_user=str(args.smtp_user),
            smtp_password=smtp_password,
            mail_from=str(mail_from),
            mail_to=recipients,
            subject=str(subject),
            body=body,
            attachments=attachments,
            starttls=not bool(args.smtp_no_starttls),
        )
        if args.stop_task_on_email and sentinel_path:
            _save_text(sentinel_path, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        else:
            _save_text(state_path, new_hash)
        return changed, True

    if args.id and args.draw:
        tid = str(args.id).strip()
        draw_letter = str(args.draw).strip().upper()
        if draw_letter not in ("M", "Q"):
            print("Unsupported --draw. Use M or Q.", file=sys.stderr)
            return 2
        dtype = "MDS" if draw_letter == "M" else "QS"
        year = datetime.now().year
        out_path = args.out
        if not out_path and not args.email_to:
            out_path = f"{tid}_{draw_letter}.txt"
        heartbeat_path = args.heartbeat_file
        if not heartbeat_path and args.email_to and not args.stop_task_on_email:
            heartbeat_path = os.path.join(args.state_dir, f"last_run_{tid}_{draw_letter}_{year}.txt")
        sentinel_path = args.sentinel_file
        if not sentinel_path and args.stop_task_on_email and args.email_to:
            sentinel_path = os.path.join(script_dir, f".sent_{tid}_{draw_letter}_{year}.txt")

        pdf_bytes = draws.fetch_draw_pdf_bytes(tid, year, dtype)
        if not pdf_bytes:
            if args.no_pdf_ok or args.email_to:
                # Draw not released yet: keep scheduled polling without marking task as failed.
                return 0
            print("Failed to download a valid draw PDF for that id/year/draw.", file=sys.stderr)
            return 2

        draw_data = draws.parse_draw_pdf(pdf_bytes)
        pdf_subject = extract_pdf_subject(pdf_bytes)
        tournament_name = draw_data.get("tournament_name") or ""
        lines = build_round1_match_lines(draw_data)
        if out_path:
            write_lines_to_file(lines, out_path)
        changed, emailed = maybe_email(
            lines,
            tid=tid,
            draw_letter=draw_letter,
            year=year,
            pdf_bytes=pdf_bytes,
            pdf_subject=pdf_subject,
            tournament_name=tournament_name,
            sentinel_path=sentinel_path,
        )
        if heartbeat_path:
            write_heartbeat(
                heartbeat_path,
                tid=tid,
                draw_letter=draw_letter,
                year=year,
                matches=len(lines),
                changed=changed,
                emailed=emailed,
            )
        if emailed and args.stop_task_on_email:
            disable_windows_task(args.task_name)
        return 0

    if args.pdf_url:
        pdf_bytes = _fetch_pdf_bytes_from_url(args.pdf_url)
        if not pdf_bytes:
            print("Failed to download a valid PDF from --pdf-url.", file=sys.stderr)
            return 2
        draw_data = draws.parse_draw_pdf(pdf_bytes)
        pdf_subject = extract_pdf_subject(pdf_bytes)
        tournament_name = draw_data.get("tournament_name") or ""
        lines = build_round1_match_lines(draw_data)
        out_path = args.out or ("matches.txt" if not args.email_to else None)
        if out_path:
            write_lines_to_file(lines, out_path)
        year = datetime.now().year
        detected_draw_letter = "Q" if "QUAL" in str(draw_data.get("draw_type") or "").upper() else "M"
        sentinel_path = args.sentinel_file
        if not sentinel_path and args.stop_task_on_email and args.email_to:
            sentinel_path = os.path.join(script_dir, f".sent_PDF_{detected_draw_letter}_{year}.txt")
        changed, emailed = maybe_email(
            lines,
            tid="PDF",
            draw_letter=detected_draw_letter,
            year=year,
            pdf_bytes=pdf_bytes,
            pdf_subject=pdf_subject,
            tournament_name=tournament_name,
            sentinel_path=sentinel_path,
        )
        heartbeat_path = args.heartbeat_file
        if not heartbeat_path and args.email_to and not args.stop_task_on_email:
            heartbeat_path = os.path.join(args.state_dir, f"last_run_PDF_?_{year}.txt")
        if heartbeat_path:
            write_heartbeat(
                heartbeat_path,
                tid="PDF",
                draw_letter="?",
                year=year,
                matches=len(lines),
                changed=changed,
                emailed=emailed,
            )
        return 0

    if not args.tournament_url or not args.year:
        print("Provide either --id and --draw, or --pdf-url, or (--tournament-url and --year).", file=sys.stderr)
        return 2

    tid = draws._extract_tournament_id(args.tournament_url)
    if not tid:
        print("Could not extract tournament id from --tournament-url.", file=sys.stderr)
        return 2

    dtype = (args.draw_type or "MDS").strip().upper()
    if not re.match(r"^(MDS|QS)$", dtype):
        print("Unsupported --draw-type. Use MDS or QS.", file=sys.stderr)
        return 2

    pdf_bytes = draws.fetch_draw_pdf_bytes(tid, args.year, dtype)
    if not pdf_bytes:
        print("Failed to download a valid draw PDF for that tournament/year/type.", file=sys.stderr)
        return 2

    draw_data = draws.parse_draw_pdf(pdf_bytes)
    pdf_subject = extract_pdf_subject(pdf_bytes)
    tournament_name = draw_data.get("tournament_name") or ""
    lines = build_round1_match_lines(draw_data)
    out_path = args.out or ("matches.txt" if not args.email_to else None)
    if out_path:
        write_lines_to_file(lines, out_path)
    draw_letter = "M" if dtype == "MDS" else "Q"
    year = int(args.year)
    sentinel_path = args.sentinel_file
    if not sentinel_path and args.stop_task_on_email and args.email_to:
        sentinel_path = os.path.join(script_dir, f".sent_{tid}_{draw_letter}_{year}.txt")
    changed, emailed = maybe_email(
        lines,
        tid=str(tid),
        draw_letter=draw_letter,
        year=year,
        pdf_bytes=pdf_bytes,
        pdf_subject=pdf_subject,
        tournament_name=tournament_name,
        sentinel_path=sentinel_path,
    )
    heartbeat_path = args.heartbeat_file
    if not heartbeat_path and args.email_to and not args.stop_task_on_email:
        heartbeat_path = os.path.join(args.state_dir, f"last_run_{tid}_{draw_letter}_{year}.txt")
    if heartbeat_path:
        write_heartbeat(
            heartbeat_path,
            tid=str(tid),
            draw_letter=draw_letter,
            year=year,
            matches=len(lines),
            changed=changed,
            emailed=emailed,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
