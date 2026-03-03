import json
from html import escape
import os
from datetime import datetime, timedelta
from config import PLAYER_MAPPING, CONTINENT_KEYS, CONTINENT_LABELS, NAME_LOOKUP
from utils import format_player_name, get_tournament_sort_order, get_surface_class
from wta import _load_wta_csv

def generate_html(tournament_groups, tournament_store, players_data, schedule_map,
                  cleaned_history, calendar_data, match_history_data, wta_rankings=None,
                  national_team_data=None, captains_data=None):
    """Generate the complete HTML page and write it to index.html."""

    # Load points distribution
    points_dist_path = os.path.join(os.path.dirname(__file__), 'data', 'points_distribution.json')
    with open(points_dist_path, 'r', encoding='utf-8') as f:
        points_distribution = json.load(f)

    # Load tournament draw sizes (combined WTA + ITF)
    draw_sizes_path = os.path.join(os.path.dirname(__file__), 'data', 'tournament_draw_sizes.json')
    try:
        with open(draw_sizes_path, 'r', encoding='utf-8') as f:
            all_draw_sizes = json.load(f)
    except Exception:
        all_draw_sizes = []
    itf_draw_sizes = [t for t in all_draw_sizes if t.get('source') == 'ITF']
    wta_draw_sizes = [t for t in all_draw_sizes if t.get('source') == 'WTA']

    # Build tournament side menu HTML for Entry Lists
    entry_menu_html = ""
    first_key = None
    for week, tourneys in tournament_groups.items():
        week_has_data = False
        for t_key in tourneys.keys():
            if t_key in tournament_store and tournament_store[t_key]:
                week_has_data = True
                break
        if not week_has_data: continue

        entry_menu_html += f'<div class="entry-menu-week">{week.upper()}</div>'
        sorted_tourneys = sorted(tourneys.items(), key=lambda x: get_tournament_sort_order(x[1]["level"]))

        for t_key, t_info in sorted_tourneys:
            if t_key in tournament_store and tournament_store[t_key]:
                t_name = t_info["name"]
                active = " active" if first_key is None else ""
                if first_key is None: first_key = t_key
                entry_menu_html += f'<div class="entry-menu-item{active}" data-key="{t_key}" onclick="selectEntryTournament(this)">{t_name}</div>'

    # Build table rows
    table_rows = ""
    week_keys = list(tournament_groups.keys())

    def get_sort_key(player_name):
        p = next(item for item in players_data if item["Player"] == player_name)
        rank = p['Rank']
        if isinstance(rank, int): return (0, rank)
        else:
            itf_rank = int(rank.replace("ITF ", "")) if isinstance(rank, str) and "ITF" in rank else 999999
            return (1, itf_rank)

    for p_name in sorted([p['Player'] for p in players_data], key=get_sort_key):
        p = next(item for item in players_data if item["Player"] == p_name)
        player_display = format_player_name(p['Player'])
        row = f'<tr data-name="{player_display.lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        mobile_name = "<br>".join(player_display.split())
        row += f'<td class="sticky-col col-name"><span class="desktop-only">{player_display}</span><span class="mobile-only">{mobile_name}</span></td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "\u2014")
            val = val.replace("Sharm ElSheikh", "Sharm ES")
            is_main = "(Q)" not in val and val != "\u2014"
            row += f'<td class="col-week">{"<b>" if is_main else ""}{val}{"</b>" if is_main else ""}</td>'
        table_rows += row + "</tr>"

    # Build history players list
    history_arg_players = set()
    for m in match_history_data:
        if m.get('winnerCountry') == 'ARG' or m.get('winner_country') == 'ARG':
            name = m.get('winnerName') or m.get('winner_name')
            if name:
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))
        if m.get('loserCountry') == 'ARG' or m.get('loser_country') == 'ARG':
            name = m.get('loserName') or m.get('loser_name')
            if name:
                name_upper = name.strip().upper()
                display_name = NAME_LOOKUP.get(name_upper, name_upper)
                history_arg_players.add(format_player_name(display_name))

    history_players_sorted = sorted(list(history_arg_players))

    # Build roadtogs player list: only players present in the WTA rankings
    wta_ranking_names = {format_player_name(p.get("Player", "")).upper() for p in (wta_rankings or [])}
    roadtogs_players_sorted = [name for name in history_players_sorted if name.upper() in wta_ranking_names]

    # Compute GS cutoff dates
    current_year = str(datetime.now().year)
    gs_list_raw = [
        ("Australian Open", "#0066B3", "AO"),
        ("Roland Garros",   "#C8602A", "RG"),
        ("Wimbledon",        "#3D7A3D", "WIM"),
        ("US Open",          "#003087", "USO"),
    ]
    gs_data = []
    for gs_name, gs_color, gs_id in gs_list_raw:
        monday_date = None
        for week in calendar_data:
            for col_key in ["wta_tour", "wta_125", "itf"]:
                for tournaments in week.get("columns", {}).get(col_key, {}).values():
                    if any(t["name"] == gs_name for t in tournaments):
                        monday_date = week.get("monday_date")
                        break
                if monday_date:
                    break
            if monday_date:
                break
        if monday_date:
            gs_dt = datetime.strptime(monday_date, "%Y-%m-%d")
            md_cutoff = (gs_dt - timedelta(weeks=6)).strftime("%Y-%m-%d")
            q_cutoff  = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
        else:
            gs_upper = gs_name.upper()
            dates = [
                r["DATE"] for r in cleaned_history
                if gs_upper in (r.get("TOURNAMENT") or "").upper()
                and (r.get("DATE") or "").startswith(current_year)
            ]
            if dates:
                earliest = min(dates)
                gs_dt = datetime.strptime(earliest, "%Y-%m-%d")
                gs_dt -= timedelta(days=gs_dt.weekday())
                gs_dt += timedelta(weeks=52)
                md_cutoff = (gs_dt - timedelta(weeks=6)).strftime("%Y-%m-%d")
                q_cutoff  = (gs_dt - timedelta(weeks=4)).strftime("%Y-%m-%d")
            else:
                md_cutoff = "N/A"
                q_cutoff  = "N/A"
        gs_data.append({"id": gs_id, "name": gs_name, "color": gs_color,
                         "qCutoff": q_cutoff, "mdCutoff": md_cutoff})

    # Sort: soonest upcoming GS first (by qCutoff ascending); N/A last
    gs_data.sort(key=lambda g: g["qCutoff"] if g["qCutoff"] != "N/A" else "9999-99-99")

    gs_tables_html = ""
    for gs in gs_data:
        gs_id    = gs["id"]
        gs_name  = gs["name"]
        gs_color = gs["color"]
        q_cutoff = gs["qCutoff"]
        md_cutoff = gs["mdCutoff"]
        gs_tables_html += (
            f'<table class="gs-cutoff-table">'
            f'<thead>'
            f'<tr><th colspan="4" style="background:{gs_color} !important;color:white !important;">{gs_name.upper()}</th></tr>'
            f'<tr><th>D</th><th>Cut Off</th><th>Acc. Pts</th><th>Est. Need</th></tr>'
            f'</thead>'
            f'<tbody>'
            f'<tr><td>Q</td><td>{q_cutoff}</td><td id="gs-acc-q-{gs_id}">-</td><td id="gs-est-q-{gs_id}">-</td></tr>'
            f'<tr><td>MD</td><td>{md_cutoff}</td><td id="gs-acc-md-{gs_id}">-</td><td id="gs-est-md-{gs_id}">-</td></tr>'
            f'</tbody>'
            f'</table>'
        )
    gs_cutoffs_json = json.dumps(gs_data)

    # Build calendar HTML
    col_keys = ["wta_tour", "wta_125", "itf"]
    col_labels = {"wta_tour": "WTA TOUR", "wta_125": "WTA 125", "itf": "ITF"}
    cont_labels = CONTINENT_LABELS

    calendar_html = '<table class="calendar-table"><thead><tr>'
    calendar_html += '<th class="cal-cat-header"></th><th class="cal-cont-header"></th>'
    for week in calendar_data:
        calendar_html += f'<th class="cal-week-header">{week["week_label"]}</th>'
    calendar_html += '</tr></thead><tbody>'

    for ck in col_keys:
        for ci, cont in enumerate(CONTINENT_KEYS):
            row_cls = "cal-group-first" if ci == 0 else ("cal-group-last" if ci == len(CONTINENT_KEYS) - 1 else "")
            calendar_html += f'<tr class="{row_cls}">' if row_cls else '<tr>'
            if ci == 0:
                calendar_html += f'<td class="cal-cat-label" rowspan="{len(CONTINENT_KEYS)}">{col_labels[ck]}</td>'
            calendar_html += f'<td class="cal-cont-label">{cont_labels[cont]}</td>'
            for week in calendar_data:
                calendar_html += '<td class="cal-cell">'
                if week["columns"][ck][cont]:
                    for t in week["columns"][ck][cont]:
                        sc = get_surface_class(t.get("surface", ""))
                        calendar_html += f'<span class="calendar-tournament {sc}">{t["name"]}</span>'
                calendar_html += '</td>'
            calendar_html += '</tr>'

    calendar_html += '</tbody></table>'

    # Build cascading year/month/day selects for ranking week picker
    _all_csv = _load_wta_csv()
    _all_dates = sorted(_all_csv.keys())
    _latest_date = _all_dates[-1] if _all_dates else ""

    # Build nested date index: year(str) -> month(int) -> [day(int), ...]
    _date_index = {}
    for _d in _all_dates:
        try:
            _dt = datetime.strptime(_d, "%Y-%m-%d")
            _y = str(_dt.year)
            if _y not in _date_index:
                _date_index[_y] = {}
            _m = _dt.month
            if _m not in _date_index[_y]:
                _date_index[_y][_m] = []
            _date_index[_y][_m].append(_dt.day)
        except Exception:
            pass

    _latest_year = _latest_month_int = _latest_day_int = 0
    _latest_year_str = ""
    if _latest_date:
        try:
            _ldt = datetime.strptime(_latest_date, "%Y-%m-%d")
            _latest_year_str = str(_ldt.year)
            _latest_year = _ldt.year
            _latest_month_int = _ldt.month
            _latest_day_int = _ldt.day
        except Exception:
            pass

    _all_years = sorted(_date_index.keys(), reverse=True)
    rankings_year_options = ""
    for _y in _all_years:
        _sel = ' selected' if _y == _latest_year_str else ''
        rankings_year_options += f'<option value="{_y}"{_sel}>{_y}</option>'

    rankings_dates_index_json = json.dumps(_date_index)
    rankings_latest_year_str = _latest_year_str
    rankings_latest_month = _latest_month_int
    rankings_latest_day = _latest_day_int

    # Build rankings table rows (initial: latest week)
    rankings_rows = ""
    for p in (wta_rankings or []):
        dob = p.get("DOB", "")
        if dob and "T" in dob:
            dob = dob.split("T")[0]
        name = format_player_name(p.get("Player", ""))
        row_class = "arg-player-row" if (p.get("Country") or "").upper() == "ARG" else ""
        rankings_rows += f'<tr class="{row_class}"><td>{p.get("Rank", "")}</td><td style="text-align:left;font-weight:bold;">{name}</td><td>{p.get("Country", "")}</td><td>{p.get("Points", "")}</td><td>{dob}</td></tr>'

    default_national_columns = ["N", "Player", "Date", "Event", "Round", "Tie", "Partner", "Opponent", "Result", "Score"]
    national_columns = list(national_team_data[0].keys()) if national_team_data else default_national_columns

    header_label_map = {"N": "#", "Result": "RES.", "Round": "RND"}
    header_style_map = {
        "N": ' style="width:30px"',
        "Player": ' style="width:140px"',
        "Date": ' style="width:90px"',
        "Event": ' style="width:110px"',
        "Round": ' style="width:80px"',
        "Tie": ' style="width:110px"',
        "Partner": ' style="width:160px"',
        "Opponent": "",
        "Result": ' style="width:50px"',
        "Score": ' style="width:110px"'
    }
    national_header_html = "".join(
        f'<th{header_style_map.get(col, "")}>{escape(header_label_map.get(col, col.upper()))}</th>'
        for col in national_columns
    )

    national_rows = ""
    for row in (national_team_data or []):
        national_rows += '<tr>'
        for col in national_columns:
            value = str(row.get(col, "") or "")
            cell_style = ""

            if col == "Player":
                value = format_player_name(value)
                cell_style = ' style="font-weight:bold;"'
            elif col == "Result":
                if value.upper() == "W":
                    cell_style = ' style="color: #166534; font-weight: bold;"'
                elif value.upper() == "L":
                    cell_style = ' style="color: #991b1b; font-weight: bold;"'

            if col in ("Player", "Partner", "Score"):
                desktop_value = escape(value)
                mobile_value = "<br>".join(escape(value).split())
                display_value = f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
            elif col == "Opponent":
                desktop_value = escape(value)
                parts = value.split("/")
                display_parts = []
                for part in parts:
                    display_parts.append("<br>".join(escape(part.strip()).split()))
                mobile_value = "<br>/<br>".join(display_parts) if len(parts) > 1 else display_parts[0]
                display_value = f'<span class="desktop-only">{desktop_value}</span><span class="mobile-only">{mobile_value}</span>'
            else:
                display_value = escape(value)
            national_rows += f'<td{cell_style}>{display_value}</td>'
        national_rows += '</tr>'

    default_captains_columns = ["N", "Captain", "Year"]
    captains_columns = list(captains_data[0].keys()) if captains_data else default_captains_columns

    captains_header_html = "".join(
        f'<th{header_style_map.get(col, "")}>{escape(header_label_map.get(col, col.upper()))}</th>'
        for col in captains_columns
    )

    captains_rows = ""
    for row in (captains_data or []):
        captains_rows += '<tr>'
        for col in captains_columns:
            value = str(row.get(col, "") or "")
            cell_style = ""

            if col == "Captain":
                value = format_player_name(value)
                cell_style = ' style="font-weight:bold;"'

            captains_rows += f'<td{cell_style}>{escape(value)}</td>'
        captains_rows += '</tr>'

    # Generate the full HTML template
    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, minimum-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
        <title>WT Argentina</title>
        <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
        <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            html {{ -webkit-text-size-adjust: 100%; text-size-adjust: 100%; overflow-x: hidden; max-width: 100vw; }}
            .mobile-only {{ display: none; }}
            .desktop-only {{ display: inline; }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; min-height: 100vh; overflow-y: auto; overflow-x: auto; -webkit-text-size-adjust: 100%; text-size-adjust: 100%; }}
            .app-container {{ display: flex; width: 100%; min-height: 100vh; }}
            .sidebar {{ width: 180px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; min-height: 100vh; }}
            .sidebar-header {{ padding: 25px 15px; font-size: 15px; font-weight: 800; color: #75AADB; border-bottom: 1px solid #475569; }}
            .menu-item {{ padding: 15px 20px; cursor: pointer; color: #cbd5e1; font-size: 14px; border-bottom: 1px solid #334155; transition: 0.2s; }}
            .menu-item:hover {{ background: #334155; color: white; }}
            .menu-item.active {{ background: #75AADB; color: white; font-weight: bold; }}
            .main-content {{ flex: 1; overflow-y: visible; background: #f8fafc; padding: 20px; display: flex; flex-direction: column; }}
            .single-layout {{ width: 100%; min-width: 0; display: flex; flex-direction: column; }}
            #view-upcoming {{ max-width: 1200px; margin: 0 auto; }}
            #view-entrylists {{ width: 100%; max-width: 1100px; margin: 0; }}
            #view-rankings {{ max-width: 700px; margin: 0 auto; }}
            #view-national {{ max-width: 1400px; margin: 0 auto; }}
            #view-captains {{ max-width: 640px; margin: 0 auto; }}
            #view-roadtogs {{ max-width: 800px; margin: 0 auto; }}
            .roadtogs-controls {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }}
            #roadtogs-table {{ width: 100%; table-layout: fixed; }}
            #roadtogs-table th, #roadtogs-table td {{ padding: 8px 12px; text-align: left; overflow: hidden; text-overflow: ellipsis; }}
            #roadtogs-table th:nth-child(1), #roadtogs-table td:nth-child(1) {{ width: 95px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(2) {{ text-align: center; }}
            #roadtogs-table td:nth-child(2) {{ white-space: normal; word-break: break-word; }}
            #roadtogs-table th:nth-child(3), #roadtogs-table td:nth-child(3) {{ width: 85px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(4), #roadtogs-table td:nth-child(4) {{ width: 40px; white-space: nowrap; text-align: center; }}
            #roadtogs-table th:nth-child(5), #roadtogs-table td:nth-child(5) {{ width: 95px; white-space: nowrap; text-align: center; }}
            .roadtogs-separator td {{ background: #334155; color: white; text-align: center !important; font-weight: bold; font-size: 12px; letter-spacing: 1px; padding: 6px 12px !important; }}
            .roadtogs-cutoffs {{ margin-bottom: 8px; display: flex; flex-wrap: nowrap; gap: 10px; align-items: flex-start; }}
            .roadtogs-legend {{ margin-bottom: 12px; font-size: 11px; color: #64748b; line-height: 1.5; }}
            .gs-cutoff-table {{ border-collapse: collapse !important; font-size: 10px; width: auto !important; table-layout: auto !important; }}
            .gs-cutoff-table th, .gs-cutoff-table td {{ border: 1px solid #cbd5e1; padding: 2px 6px; text-align: center; }}
            .gs-cutoff-table thead tr:last-child th {{ background: #f1f5f9 !important; font-weight: bold; color: #475569 !important; }}
            .header-row {{ width: 100%; margin-bottom: 20px; display: flex; flex-direction: column; align-items: center; position: relative; gap: 10px; }}
            h1 {{ margin: 0; font-size: 22px; color: #1e293b; }}
            .search-container {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-filter-container {{ position: absolute; right: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-toggle-btn {{ padding: 8px 12px; border-radius: 8px; border: 2px solid #94a3b8; background: white; font-family: inherit; font-size: 12px; font-weight: bold; color: #1e293b; cursor: pointer; white-space: nowrap; }}
            .rankings-toggle-btn:hover {{ background: #f1f5f9; }}
            .rankings-filter-container {{ display: flex; align-items: center; }}
            .rankings-date-picker {{ display: flex; align-items: stretch; border: 2px solid #94a3b8; border-radius: 8px; overflow: hidden; background: white; }}
            .rankings-date-select {{ width: auto; font-size: 12px; font-weight: bold; padding: 8px 22px 8px 8px; border: none !important; border-radius: 0 !important; background-color: transparent !important; }}
            #rankings-year-select {{ min-width: 82px; }}
            #rankings-month-select {{ min-width: 74px; border-left: 1px solid #cbd5e1 !important; }}
            #rankings-day-select {{ min-width: 62px; border-left: 1px solid #cbd5e1 !important; }}
            .rankings-load-btn {{ border: none; border-left: 2px solid #94a3b8; border-radius: 0; background: #75AADB; font-family: inherit; font-size: 13px; font-weight: bold; color: white; cursor: pointer; padding: 0 10px; line-height: 1; }}
            .rankings-load-btn:hover {{ background: #5a8fb8; }}
            .rankings-controls {{ display: flex; align-items: center; width: 100%; gap: 8px; }}
            .rankings-controls .search-container {{ position: static; transform: none; flex: 1; display: flex; justify-content: flex-start; }}
            .rankings-controls .rankings-filter-container {{ position: static; transform: none; flex: 0 0 auto; }}
            .rankings-btn-end {{ flex: 1; display: flex; justify-content: flex-end; }}
            #rankings-search {{ width: 190px; }}
            input, select {{ padding: 8px 12px; border-radius: 8px; border: 2px solid #94a3b8; font-family: inherit; font-size: 13px; width: 250px; box-sizing: border-box; }}
            select {{ background: white; font-weight: bold; cursor: pointer; appearance: none; background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }}
            .content-card {{ background: white; box-shadow: 0 4px 20px rgba(0,0,0,0.05); width: 100%; border: 1px solid black; }}
            .table-wrapper {{ overflow-x: auto; width: 100%; }}
            table {{ border-collapse: separate; border-spacing: 0; width: 100%; table-layout: fixed; border: 1px solid black; }}
            #view-upcoming table {{ width: max-content; min-width: 100%; }}
            th {{ position: sticky; top: 0; background: #75AADB !important; color: white; padding: 10px 15px; font-size: 11px; font-weight: bold; border-bottom: 2px solid #1e293b; border-right: 1px solid #1e293b; z-index: 10; text-transform: uppercase; text-align: center; }}
            td {{ padding: 8px 12px; border-bottom: 1px solid #94a3b8; text-align: center; font-size: 13px; border-right: 1px solid #94a3b8; }}
            #view-entrylists td {{ font-size: 12px; padding: 6px 10px; }}
            #view-entrylists table {{ table-layout: auto; }}
            #view-entrylists .entry-content {{ align-items: flex-start; }}
            #view-entrylists .content-card {{ width: 100%; max-width: 760px; margin: 0; }}

            /* Entry Lists layout */
            .entry-layout {{ display: flex; flex-direction: row; gap: 25px; width: 100%; }}
            .entry-menu {{ width: 480px; flex-shrink: 0; display: flex; flex-wrap: wrap; align-items: stretch; background: white; border: 1px solid black; align-self: flex-start; }}
            .entry-menu-header {{ width: 100%; background: #75AADB; color: white; font-size: 14px; font-weight: bold; text-align: center; padding: 12px; }}
            .entry-menu-week {{ width: 100%; background: #e2e8f0; font-size: 11px; font-weight: bold; text-align: center; padding: 8px; color: #475569; border-bottom: 1px solid #cbd5e1; }}
            .entry-menu-item {{ flex: 1 1 calc(33.333% - 1px); min-width: 0; padding: 10px 8px; font-size: 12px; cursor: pointer; border-bottom: 1px solid #e2e8f0; border-right: 1px solid #e2e8f0; color: #334155; transition: background 0.15s; text-align: center; box-sizing: border-box; }}
            .entry-menu-item:hover {{ background: #f1f5f9; }}
            .entry-menu-item.active {{ background: #dbeafe; color: #1e40af; font-weight: bold; }}
            .entry-content {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            #view-rankings table {{ table-layout: auto; }}
            #view-rankings td {{ font-size: 12px; padding: 6px 10px; }}
            #view-rankings.rankings-show-all tr.arg-player-row td {{ background-color: #e0f2fe !important; }}
            .sticky-col {{ position: sticky; background: white !important; z-index: 2; }}
            .row-arg {{ background-color: #e0f2fe !important; }}
            td.col-week {{ width: 170px; font-size: 11px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            th.sticky-col {{ z-index: 11; background: #75AADB !important; color: white; }}
            .col-rank {{ left: 0; width: 32px; min-width: 45px; max-width: 45px; }}
            .col-name {{ left: 45px; width: 112px; min-width: 112px; max-width: 112px; text-align: left; font-weight: bold; }}
            .col-week {{ width: 150px; font-size: 11px; font-weight: bold; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            .divider-row td {{ background: #e2e8f0; font-weight: bold; text-align: center; padding: 5px 15px; font-size: 11px; border-right: none; }}
            tr.hidden {{ display: none; }}
            table:not(.calendar-table) tr:hover td {{ background: #f1f5f9; }}
            table:not(.calendar-table) tr:hover td.sticky-col {{ background: #f1f5f9 !important; }}
            .dropdown-header {{ background-color: #e2e8f0 !important; font-weight: bold !important; text-align: center !important; padding: 12px 0 !important; font-size: 11px; display: block; }}
            .dropdown-item {{ padding: 8px 15px; text-align: left; background-color: #ffffff; }}

            .select2-container--default .select2-selection--single {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                height: 38px;
                padding: 4px 12px;
                font-family: inherit;
                font-size: 13px;
            }}
            .select2-container--default .select2-selection--single .select2-selection__rendered {{
                color: #1e293b;
                line-height: 28px;
                padding-left: 0;
            }}
            .select2-container--default .select2-selection--single .select2-selection__arrow {{
                height: 36px;
            }}
            .select2-container--default.select2-container--open .select2-selection--single {{
                border-color: #75AADB;
            }}
            .select2-dropdown {{
                border: 2px solid #94a3b8;
                border-radius: 8px;
                font-family: inherit;
            }}
            .select2-search--dropdown .select2-search__field {{
                border: 1px solid #94a3b8;
                border-radius: 4px;
                padding: 4px 8px;
                font-family: inherit;
            }}
            .select2-results__option {{
                padding: 8px 12px;
                font-size: 13px;
            }}
            .select2-results__option--highlighted {{
                background-color: #75AADB !important;
                color: white !important;
            }}
            .select2-container {{
                width: 250px !important;
            }}

            #view-entrylists .content-card {{ overflow-y: visible; max-height: none; }}

            /* National Team table: allow horizontal expansion instead of squeezing columns */
            #view-national .table-wrapper {{ overflow-x: auto; }}
            #national-table {{ table-layout: auto; width: max-content; min-width: 100%; }}
            #national-table th, #national-table td {{
                font-size: 11px;
                padding: 5px 6px;
                white-space: normal;
                overflow-wrap: anywhere;
                line-height: 1.2;
            }}
            #national-table th:nth-child(2), #national-table td:nth-child(2) {{
                text-align: center;
                min-width: 185px;
            }}
            #national-table th:nth-child(7), #national-table td:nth-child(7) {{
                min-width: 160px;
                white-space: nowrap;
            }}
            #national-table th:nth-child(8), #national-table td:nth-child(8) {{
                text-align: center;
                width: 270px;
                min-width: 270px;
                white-space: nowrap;
            }}
            #national-table th:nth-child(9), #national-table td:nth-child(9) {{
                min-width: 55px;
                white-space: nowrap;
                text-align: center;
            }}
            #national-table th:nth-child(6), #national-table td:nth-child(6) {{
                min-width: 110px;
                white-space: nowrap;
            }}
            #national-table th:nth-child(10), #national-table td:nth-child(10) {{
                min-width: 110px;
                white-space: nowrap;
            }}

            /* Captains table: keep compact width and avoid stretched columns */
            #view-captains .content-card {{ width: fit-content; max-width: 100%; margin: 0 auto; }}
            #view-captains .table-wrapper {{ width: fit-content; max-width: 100%; overflow-x: auto; }}
            #captains-table {{ width: max-content; min-width: 0; table-layout: auto; margin: 0; }}
            #captains-table th, #captains-table td {{
                font-size: 11px;
                padding: 5px 6px;
                white-space: nowrap;
                line-height: 1.2;
            }}
            #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 42px; }}
            #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: auto; }}
            #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 64px; }}

            #history-table th {{ background: #75AADB !important; position: sticky; top: 0; z-index: 10; }}
            #history-table {{ table-layout: fixed; width: 100%; }}
            #history-table th:nth-child(1) {{ width: 80px; }} /* DATE */
            #history-table th:nth-child(2) {{ width: auto; }} /* TOURNAMENT */
            #history-table th:nth-child(3) {{ width: 70px; }} /* SURFACE */
            #history-table th:nth-child(4) {{ width: 100px; }} /* ROUND */
            #history-table th:nth-child(5) {{ width: auto; }} /* PLAYER */
            #history-table th:nth-child(6) {{ width: 50px; }} /* RESULT */
            #history-table th:nth-child(7) {{ width: 120px; }} /* SCORE */
            #history-table th:nth-child(8) {{ width: auto; min-width: 200px; }} /* OPPONENT */
            #history-table td {{ font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
            #history-table td:nth-child(2) {{ white-space: normal; overflow: visible; text-overflow: clip; }} /* Allow TOURNAMENT to wrap */
            #history-table td:nth-child(8) {{ white-space: normal; overflow: visible; text-overflow: clip; }} /* Allow OPPONENT to wrap */

            /* Filter Panel Styles */
            .history-layout {{ display: flex; gap: 20px; width: 100%; align-items: flex-start; }}
            .filter-panel {{ width: 250px; padding: 15px; flex-shrink: 0; border: 2px solid black; background: white; align-self: flex-start; }}
            .filter-panel h3 {{ margin: -15px -15px 15px -15px; font-size: 16px; color: white; text-align: center; font-weight: bold; background: #75AADB; border: none; padding: 12px; border-radius: 0; }}
            .filter-group {{ margin-bottom: 20px; text-align: left; }}
            .filter-group-title {{ font-size: 13px; font-weight: bold; color: #475569; margin-bottom: 8px; cursor: pointer; user-select: none; display: flex; justify-content: center; align-items: center; text-align: center; position: relative; }}
            .filter-group-title:hover {{ color: #75AADB; }}
            .filter-options {{ border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px; background: #f8fafc; text-align: left; }}
            .filter-options.scrollable {{ max-height: 180px; overflow-y: auto; }}
            .filter-option {{ padding: 6px 10px; margin-bottom: 4px; font-size: 12px; text-align: left; cursor: pointer; user-select: none; border-radius: 3px; transition: background 0.15s; }}
            .filter-option:hover {{ background: #e2e8f0; }}
            .filter-option.selected {{ font-weight: bold; background: #dbeafe; color: #1e40af; }}
            .filter-actions {{ margin-top: 20px; display: flex; justify-content: space-between; align-items: center; gap: 10px; }}
            .filter-instructions {{ font-size: 10px; color: #64748b; flex: 1; line-height: 1.3; padding-left: 15px; }}
            .filter-btn {{ padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-family: inherit; font-size: 12px; font-weight: bold; white-space: nowrap; }}
            .filter-btn-clear {{ background: #e2e8f0; color: #475569; }}
            .filter-btn-clear:hover {{ background: #cbd5e1; }}
            #filter-opponent-select {{ font-size: 11px; }}
            .history-content {{ flex: 1; display: flex; flex-direction: column; min-width: 0; }}
            .collapse-icon {{ font-size: 14px; position: absolute; right: 0; }}
            .filter-group.collapsed .filter-options {{ display: none; }}
            .filter-group.collapsed .opponent-select-container {{ display: none; }}
            .filter-group.collapsed .collapse-icon::before {{ content: '\u25bc'; }}
            .filter-group:not(.collapsed) .collapse-icon::before {{ content: '\u25b2'; }}
            .filter-search {{ width: 100%; padding: 6px 8px; border: 1px solid #cbd5e1; border-radius: 4px; font-family: inherit; font-size: 11px; margin-bottom: 8px; box-sizing: border-box; }}
            .filter-search:focus {{ outline: none; border-color: #75AADB; }}
            .table-header-section {{ margin-bottom: 15px; display: flex; align-items: center; justify-content: space-between; }}
            .table-title {{ margin: 0; font-size: 22px; color: #1e293b; flex: 1; text-align: center; }}
            .player-select-container {{ width: 250px; }}
            .history-summary-container {{ width: 250px; text-align: right; }}
            .history-wl-counter {{ font-size: 14px; font-weight: 700; color: #1e293b; }}

            /* Calendar Styles */
            #view-calendar {{ width: 100%; min-height: 0; }}
            .calendar-container {{ width: 100%; min-width: 100%; min-height: 0; margin-bottom: 0; display: block; }}
            .calendar-container .table-wrapper {{ display: block; overflow-x: auto; overflow-y: hidden; -webkit-overflow-scrolling: touch; width: 100%; max-width: 100%; border-right: 1px solid #1e293b; }}
            .calendar-table {{ border-collapse: separate; border-spacing: 0; width: max-content; min-width: max-content; table-layout: auto; border: 1px solid black; }}
            .calendar-table th {{ padding: 4px 4px; vertical-align: top; border-bottom: 2px solid #1e293b; border-right: 1px solid #1e293b; }}
            .calendar-table td {{ padding: 4px 4px; vertical-align: top; border-bottom: 1px solid #94a3b8; border-right: 1px solid #94a3b8; }}
            .cal-week-header {{ background: #75AADB; color: white; font-size: 10px; font-weight: bold; text-align: center; white-space: nowrap; padding: 6px 6px; position: sticky; top: 0; z-index: 10; min-width: 90px; }}
            .cal-cat-header {{ background: #75AADB; color: white; position: sticky; top: 0; left: 0; z-index: 15; width: 24px; min-width: 24px; max-width: 24px; box-sizing: border-box; }}
            .cal-cont-header {{ background: #75AADB; color: white; position: sticky; top: 0; left: 24px; z-index: 15; min-width: 58px; }}
            .cal-cat-label {{ background: #1e293b; color: white; font-size: 11px; font-weight: bold; text-align: center; vertical-align: middle !important; text-transform: uppercase; writing-mode: vertical-lr; text-orientation: mixed; transform: rotate(180deg); padding: 0; width: 24px; min-width: 24px; max-width: 24px; position: sticky; left: 0; z-index: 14; border-color: #1e293b !important; box-shadow: inset 0 0 0 50px #1e293b; box-sizing: border-box; flex: 0 0 24px; }}
            .cal-cont-label {{ background: #f1f5f9; font-size: 10px; font-weight: 600; color: #475569; text-align: center; vertical-align: middle !important; white-space: nowrap; position: sticky; left: 24px; z-index: 14; min-width: 58px; }}
            .cal-cell {{ font-size: 10px; min-height: 24px; vertical-align: middle !important; }}
            .cal-group-first td {{ border-top: 1px solid #1e293b; }}
            .cal-group-last td {{ border-bottom: 1px solid #1e293b; }}
            .calendar-tournament {{ display: block; font-size: 10px; padding: 2px 6px; border-radius: 3px; line-height: 1.3; font-weight: 600; white-space: nowrap; margin: 1px 0; }}
            .cal-clay {{ background: #e8a882; color: #5c2e0e; }}
            .cal-hard {{ background: #88b4e8; color: #1a3a5c; }}
            .cal-grass {{ background: #7cc89a; color: #1a4a2e; }}

            /* Mobile Menu Toggle */
            .mobile-menu-toggle {{ display: none; position: fixed; top: 15px; left: 15px; z-index: 1000; background: #1e293b; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; font-size: 18px; }}
            .sidebar.mobile-hidden {{ transform: translateX(-100%); }}

            /* Responsive Styles */
            @media (max-width: 1024px) {{
                /* Tablet adjustments */
                input, select {{ width: 200px; }}
                .select2-container {{ width: 200px !important; }}
            }}

            @media (max-width: 768px) {{
                /* Mobile styles */
                body {{ overflow-x: hidden; max-width: 100vw; }}
                .mobile-only {{ display: inline; }}
                .desktop-only {{ display: none; }}
                .mobile-menu-toggle {{ display: none; }}

                .app-container {{ flex-direction: column; }}

                .sidebar {{
                    position: fixed;
                    top: 0;
                    left: 0;
                    right: 0;
                    width: 100% !important;
                    max-width: 100% !important;
                    padding-left: env(safe-area-inset-left);
                    padding-right: env(safe-area-inset-right);
                    box-sizing: border-box;
                    height: auto;
                    min-height: 0;
                    z-index: 999;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.2);
                    flex-direction: row;
                    overflow-x: hidden;
                    overflow-y: hidden;
                    white-space: normal;
                }}
                .sidebar.mobile-hidden {{ transform: none; }}
                .sidebar-header {{ display: none; }}

                .main-content {{
                    padding: 56px 1px 8px 1px;
                    width: 100%;
                    box-sizing: border-box;
                }}

                .menu-item {{
                    flex: 1 1 0;
                    border-bottom: none;
                    border-right: 1px solid #334155;
                    white-space: normal;
                    min-height: 40px;
                    padding: 4px 3px;
                    font-size: 8px;
                    line-height: 1.1;
                    text-align: center;
                    display: flex;
                    align-items: center;
                    justify-content: center;
                }}
                .menu-item:last-child {{ border-right: none; }}

                #view-upcoming, #view-rankings, #view-national, #view-captains, #view-roadtogs {{ max-width: 100%; }}

                .entry-layout {{ flex-direction: column; gap: 15px; }}
                .entry-menu {{
                    width: 100%;
                    display: flex;
                    flex-wrap: wrap;
                    align-items: stretch;
                    border: 1px solid black;
                }}
                .entry-menu-header {{
                    width: 100%;
                    font-size: 11px;
                    padding: 8px;
                }}
                .entry-menu-week {{
                    width: 100%;
                    font-size: 9px;
                    padding: 5px 6px;
                }}
                .entry-menu-item {{
                    width: auto;
                    flex: 1 1 calc(33.333% - 1px);
                    min-width: 0;
                    border-bottom: 1px solid #cbd5e1;
                    border-right: 1px solid #cbd5e1;
                    padding: 5px 6px;
                    font-size: 9px;
                    line-height: 1.1;
                    text-align: center;
                    box-sizing: border-box;
                }}

                /* Adjust header rows */
                .header-row {{
                    flex-direction: column;
                    gap: 10px;
                    align-items: stretch;
                }}

                .search-container {{
                    position: static;
                    transform: none;
                    width: 100%;
                    order: 2;
                }}
                .rankings-filter-container {{
                    position: static;
                    transform: none;
                    width: 100%;
                    order: 3;
                    display: flex;
                    justify-content: center;
                }}

                /* Rankings mobile: two-row controls layout */
                #view-rankings .rankings-controls {{
                    flex-wrap: wrap;
                    gap: 6px;
                    align-items: center;
                    order: 2;
                }}
                #view-rankings .search-container {{
                    flex: 0 0 100% !important;
                    width: 100% !important;
                    position: static;
                    transform: none;
                    order: 1;
                }}
                #view-rankings #rankings-search {{
                    width: 100% !important;
                    height: 32px;
                    padding: 4px 10px;
                    font-size: 11px;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-rankings #rankings-search::placeholder {{ font-size: 10px; }}
                #view-rankings .rankings-filter-container {{
                    flex: 1 1 auto !important;
                    width: auto !important;
                    position: static;
                    transform: none;
                    order: 2;
                    justify-content: flex-start;
                }}
                #view-rankings .rankings-btn-end {{
                    flex: 0 0 auto !important;
                    order: 3;
                }}
                #view-rankings .rankings-toggle-btn {{
                    height: 32px;
                    padding: 0 10px;
                    font-size: 11px;
                    line-height: 1;
                    box-sizing: border-box;
                    margin: 0;
                    white-space: nowrap;
                }}
                #view-rankings .rankings-date-picker {{
                    height: 32px;
                }}
                #view-rankings .rankings-date-select {{
                    height: 32px;
                    padding: 0 20px 0 6px;
                    font-size: 10px;
                    width: auto !important;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-rankings #rankings-year-select {{ min-width: 70px !important; }}
                #view-rankings #rankings-month-select {{ min-width: 62px !important; }}
                #view-rankings #rankings-day-select {{ min-width: 52px !important; }}
                #view-rankings .rankings-load-btn {{
                    height: 32px;
                    padding: 0 9px;
                    font-size: 12px;
                    box-sizing: border-box;
                    margin: 0;
                }}
                #view-entrylists .rankings-filter-container {{
                    width: auto !important;
                    display: flex;
                    justify-content: flex-end;
                    align-items: stretch;
                    margin: 0;
                }}
                #view-entrylists #btn-prio1 {{
                    height: 28px;
                    padding: 0 10px;
                    font-size: 10px;
                    line-height: 1;
                    box-sizing: border-box;
                    margin: 0;
                }}

                h1 {{
                    font-size: 18px;
                    text-align: center;
                    order: 1;
                }}

                input, select {{
                    width: 100%;
                    max-width: 100%;
                }}

                .select2-container {{
                    width: 100% !important;
                }}

                /* Table adjustments */
                .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}

                table {{
                    font-size: 10px;
                    min-width: 560px;
                }}

                th, td {{
                    padding: 4px 6px;
                    font-size: 9px;
                }}

                /* Upcoming: mobile layout */
                #view-upcoming table {{ width: 100%; min-width: 100%; table-layout: fixed; }}
                #view-upcoming th, #view-upcoming td {{ font-size: 7px; padding: 2px 2px; }}
                #view-upcoming th {{ font-size: 6px; }}
                #view-upcoming th.col-week {{ font-size: 6px !important; }}
                #view-upcoming td.col-week, #view-upcoming .col-week {{ font-size: 5px; line-height: 1.6; }}
                #view-upcoming .col-rank {{
                    width: 20px !important;
                    min-width: 20px !important;
                    max-width: 20px !important;
                    left: 0;
                }}
                #view-upcoming th.col-rank, #view-upcoming td.col-rank {{
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    line-height: 1.05;
                }}
                #view-upcoming .col-name {{
                    width: 62px !important;
                    min-width: 62px !important;
                    max-width: 62px !important;
                }}
                #view-upcoming .col-name {{ left: 20px; }}
                #view-upcoming th.col-name, #view-upcoming td.col-name {{
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    text-overflow: clip;
                    text-align: center;
                }}
                #view-upcoming .col-week, #view-upcoming td.col-week {{ width: auto !important; min-width: 0 !important; max-width: none !important; }}

                /* Entry Lists: compact mode */
                #view-entrylists table {{ min-width: 0; table-layout: auto; }}
                #view-entrylists th {{ font-size: 10px; padding: 3px 4px; }}
                #view-entrylists td {{ font-size: 10px; padding: 4px 4px; }}
                #view-entrylists .entry-content .header-row {{
                    margin-bottom: 8px;
                    flex-direction: row !important;
                    align-items: center !important;
                    position: relative;
                }}
                #view-entrylists #entry-title {{ font-size: 14px; margin: 0; text-align: center; width: 100%; }}
                #view-entrylists .rankings-filter-container {{ position: absolute; right: 0; top: 50%; transform: translateY(-50%); flex-shrink: 0; }}

                /* Rankings table: compact mode */
                #view-rankings .content-card {{
                    width: 100%;
                    margin: 0 auto;
                }}
                #view-rankings .content-card .table-wrapper {{
                    width: 100%;
                    overflow-x: auto;
                }}
                #view-rankings table {{ min-width: 100%; width: 100%; margin: 0; table-layout: fixed; }}
                #view-rankings th, #view-rankings td {{
                    font-size: 7px;
                    padding: 2px 2px;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                /* Entry-list style: fixed side columns, wide PLAYER */
                #view-rankings th:nth-child(1), #view-rankings td:nth-child(1) {{ width: 20px !important; }}
                #view-rankings th:nth-child(2), #view-rankings td:nth-child(2) {{
                    width: 120px !important;
                    max-width: 120px !important;
                    text-align: left;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                #view-rankings th:nth-child(3), #view-rankings td:nth-child(3) {{ width: 26px !important; }}
                #view-rankings th:nth-child(4), #view-rankings td:nth-child(4) {{ width: 42px !important; }}
                #view-rankings th:nth-child(5), #view-rankings td:nth-child(5) {{ width: 34px !important; }}
                #view-rankings th:nth-child(6), #view-rankings td:nth-child(6) {{ width: 58px !important; }}

                .col-name {{
                    min-width: 98px;
                    max-width: 98px;
                }}

                .col-week {{
                    font-size: 9px;
                }}

                /* History layout - stack vertically */
                .history-layout {{
                    flex-direction: column;
                    gap: 12px;
                }}

                /* Mobile-only history flow:
                   1) title + player search
                   2) filters in wrapped rows
                   3) full-width table */
                #view-history {{
                    width: 100%;
                    max-width: 100%;
                }}

                .history-content {{
                    width: 100%;
                    display: flex;
                    flex-direction: column;
                    gap: 12px;
                }}

                .filter-panel {{
                    width: min(100%, 420px);
                    max-width: 420px;
                    margin-left: auto;
                    margin-right: auto;
                    padding: 4px;
                    margin-bottom: 0;
                    border: 2px solid black;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 4px;
                    align-items: flex-start;
                    box-sizing: border-box;
                }}

                .filter-panel h3 {{
                    font-size: 9px;
                    padding: 5px;
                    width: 100%;
                    margin: -4px -4px 4px -4px;
                }}

                .filter-group {{
                    margin-bottom: 0;
                    flex: 1 1 100px;
                    min-width: 95px;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                    padding: 2px;
                    background: #f8fafc;
                }}
                .filter-group-title {{ font-size: 8px; margin-bottom: 2px; }}
                .filter-option {{ font-size: 7px; padding: 2px 3px; margin-bottom: 1px; }}
                .filter-options.scrollable {{ max-height: 120px; }}

                .table-header-section {{
                    flex-direction: column;
                    gap: 10px;
                    margin-bottom: 0;
                    align-items: stretch;
                }}

                .player-select-container {{
                    width: min(100%, 420px);
                    max-width: 420px;
                    margin-left: auto;
                    margin-right: auto;
                    box-sizing: border-box;
                }}

                .table-title {{ font-size: 14px; text-align: center; }}

                .history-summary-container {{
                    width: 100%;
                    text-align: center;
                }}

                .history-wl-counter {{
                    font-size: 12px;
                }}

                .filter-actions {{
                    width: 100%;
                    margin-top: 4px;
                    justify-content: space-between;
                    order: 99;
                }}

                .filter-instructions {{
                    padding-left: 8px;
                    font-size: 9px;
                }}
                .filter-btn-clear {{ margin-right: 8px; }}

                /* Opponent search (Select2): shorter height and smaller text */
                .opponent-select-container .select2-container--default .select2-selection--single {{
                    height: 24px;
                    min-height: 24px;
                    padding: 0 6px;
                }}
                .opponent-select-container .select2-container--default .select2-selection--single .select2-selection__rendered {{
                    line-height: 22px;
                    font-size: 8px;
                }}
                .opponent-select-container .select2-container--default .select2-selection--single .select2-selection__arrow {{
                    height: 22px;
                }}
                #select2-filter-opponent-select-results .select2-results__option {{
                    font-size: 8px;
                }}

                .content-card {{
                    width: 100%;
                }}

                .history-content .content-card {{
                    width: 100%;
                }}

                /* History table */
                #view-history .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #history-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #history-table th,
                #history-table td {{
                    font-size: 5px;
                    padding: 2px 3px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    line-height: 1.15;
                }}

                #history-table th:nth-child(1), #history-table td:nth-child(1) {{ width: 10%; }}
                #history-table th:nth-child(2), #history-table td:nth-child(2) {{ width: 18%; }}
                #history-table th:nth-child(3), #history-table td:nth-child(3) {{ width: 9%; }}
                #history-table th:nth-child(4), #history-table td:nth-child(4) {{ width: 9%; }}
                #history-table th:nth-child(5), #history-table td:nth-child(5) {{ width: 19%; }}
                #history-table th:nth-child(6), #history-table td:nth-child(6) {{ width: 8%; }}
                #history-table th:nth-child(7), #history-table td:nth-child(7) {{ width: 11%; }}
                #history-table th:nth-child(8), #history-table td:nth-child(8) {{ width: 16%; }}

                /* National Team table */
                #view-national .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #national-table {{
                    width: 100%;
                    table-layout: fixed;
                }}
                #national-table th,
                #national-table td {{
                    font-size: 7px;
                    padding: 1px 1px;
                    white-space: normal;
                    word-break: break-word;
                    line-height: 1.1;
                    overflow: hidden;
                }}
                #national-table th:nth-child(1), #national-table td:nth-child(1) {{ width: 12px !important; }}
                #national-table th:nth-child(2), #national-table td:nth-child(2) {{ width: 48px !important; }}
                #national-table th:nth-child(3), #national-table td:nth-child(3) {{ width: 34px !important; }}
                #national-table th:nth-child(4), #national-table td:nth-child(4) {{ width: 28px !important; }}
                #national-table th:nth-child(5), #national-table td:nth-child(5) {{ width: 16px !important; }}
                #national-table th:nth-child(6), #national-table td:nth-child(6) {{ width: 38px !important; }}
                #national-table th:nth-child(7), #national-table td:nth-child(7) {{ width: 38px !important; }}
                #national-table th:nth-child(8), #national-table td:nth-child(8) {{ width: 50px !important; }}
                #national-table th:nth-child(9), #national-table td:nth-child(9) {{ width: 14px !important; text-align: center; }}
                #national-table th:nth-child(10), #national-table td:nth-child(10) {{ width: 28px !important; }}

                /* Captains table mobile */
                #view-captains .content-card {{ max-width: 100%; }}
                #view-captains .table-wrapper {{ overflow-x: hidden; }}
                #captains-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #captains-table th,
                #captains-table td {{
                    font-size: 9px;
                    padding: 3px 4px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    line-height: 1.15;
                }}
                #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 12%; }}
                #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: 58%; }}
                #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 30%; }}

                /* Calendar mobile */
                .calendar-container .table-wrapper {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
                .cal-week-header {{ font-size: 7px; padding: 3px 3px; min-width: 80px; }}
                .cal-cat-header, .cal-cont-header {{ font-size: 7px; }}
                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}
                #view-calendar .content-card {{
                    border: none;
                    box-shadow: none;
                    background: transparent;
                }}
                .cal-cat-header {{ position: sticky !important; position: -webkit-sticky !important; left: 0; z-index: 14; background: #75AADB; }}
                .cal-cont-header {{ position: sticky !important; position: -webkit-sticky !important; left: 24px; z-index: 14; background: #75AADB; }}
                .cal-cat-label {{ position: sticky !important; position: -webkit-sticky !important; left: 0; z-index: 14; }}
                .cal-cont-label {{ position: sticky !important; position: -webkit-sticky !important; left: 24px; z-index: 14; background: #f1f5f9; }}
                .calendar-container .table-wrapper {{ position: relative; }}

                /* Points Breakdown mobile */
                #view-roadtogs .roadtogs-controls {{
                    flex-wrap: nowrap;
                    align-items: center;
                    justify-content: flex-start;
                    gap: 8px;
                }}
                #view-roadtogs .player-select-container {{
                    width: 55%;
                    max-width: none;
                    margin-left: 0;
                    margin-right: 0;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single {{
                    height: 26px;
                    min-height: 26px;
                    padding-top: 0;
                    padding-bottom: 0;
                    display: flex;
                    align-items: center;
                    position: relative;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single .select2-selection__rendered {{
                    line-height: 1;
                    font-size: 9px;
                    padding-left: 6px;
                    flex: 1;
                    min-width: 0;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }}
                #view-roadtogs .player-select-container .select2-container--default .select2-selection--single .select2-selection__arrow {{
                    height: 100%;
                    position: absolute;
                    top: 0;
                    right: 1px;
                }}
                #select2-roadtogsPlayerSelect-results .select2-results__option {{
                    font-size: 9px;
                    padding: 3px 6px;
                }}
                #roadtogs-points-total {{
                    font-size: 11px !important;
                    white-space: nowrap;
                    padding-right: 0 !important;
                    padding-left: 0 !important;
                    margin-left: auto;
                    margin-right: 20px;
                }}
                #view-roadtogs .content-card {{
                    width: 100%;
                }}
                #view-roadtogs .table-wrapper {{
                    overflow-x: hidden;
                }}
                #roadtogs-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #roadtogs-table th,
                #roadtogs-table td {{
                    font-size: 8px;
                    padding: 3px 3px;
                    white-space: nowrap;
                    overflow: hidden;
                    text-overflow: ellipsis;
                }}
                #roadtogs-table th:nth-child(1), #roadtogs-table td:nth-child(1) {{ width: 18% !important; }}
                #roadtogs-table th:nth-child(2) {{ text-align: center !important; }}
                #roadtogs-table th:nth-child(3), #roadtogs-table td:nth-child(3) {{ width: 18% !important; }}
                #roadtogs-table th:nth-child(4), #roadtogs-table td:nth-child(4) {{ width: 9% !important; }}
                #roadtogs-table th:nth-child(5), #roadtogs-table td:nth-child(5) {{ width: 18% !important; }}
            }}

            @media (max-width: 480px) {{
                /* Extra small mobile */
                h1 {{
                    font-size: 16px;
                }}

                .sidebar-header {{
                    font-size: 14px;
                    padding: 20px 10px;
                }}

                .menu-item {{
                    font-size: 7px;
                    min-height: 36px;
                    padding: 3px 2px;
                }}

                th, td {{
                    padding: 2px 3px;
                    font-size: 7px;
                }}

                #view-upcoming .col-name {{
                    width: 56px !important;
                    min-width: 56px !important;
                    max-width: 56px !important;
                }}

                .filter-panel h3 {{
                    font-size: 11px;
                }}

                .filter-group-title {{
                    font-size: 10px;
                }}

                .filter-option {{
                    font-size: 9px;
                }}

                #history-table th, #history-table td {{
                    font-size: 5px;
                    padding: 2px 2px;
                }}

                #view-national .table-wrapper {{
                    overflow-x: auto;
                    -webkit-overflow-scrolling: touch;
                }}
                #national-table {{
                    width: 100%;
                    table-layout: fixed;
                }}
                #national-table th,
                #national-table td {{
                    font-size: 6px;
                    padding: 1px 1px;
                    white-space: normal;
                    word-break: break-word;
                    line-height: 1.05;
                    overflow: hidden;
                }}
                #national-table th:nth-child(1), #national-table td:nth-child(1) {{ width: 10px !important; }}
                #national-table th:nth-child(2), #national-table td:nth-child(2) {{ width: 40px !important; }}
                #national-table th:nth-child(3), #national-table td:nth-child(3) {{ width: 28px !important; }}
                #national-table th:nth-child(4), #national-table td:nth-child(4) {{ width: 24px !important; }}
                #national-table th:nth-child(5), #national-table td:nth-child(5) {{ width: 14px !important; }}
                #national-table th:nth-child(6), #national-table td:nth-child(6) {{ width: 32px !important; }}
                #national-table th:nth-child(7), #national-table td:nth-child(7) {{ width: 32px !important; }}
                #national-table th:nth-child(8), #national-table td:nth-child(8) {{ width: 42px !important; }}
                #national-table th:nth-child(9), #national-table td:nth-child(9) {{ width: 12px !important; text-align: center; }}
                #national-table th:nth-child(10), #national-table td:nth-child(10) {{ width: 24px !important; }}

                #view-captains .table-wrapper {{
                    overflow-x: hidden;
                }}
                #captains-table {{
                    width: 100%;
                    min-width: 0;
                    table-layout: fixed;
                }}
                #captains-table th,
                #captains-table td {{
                    font-size: 8px;
                    padding: 2px 3px;
                    white-space: normal;
                    overflow-wrap: anywhere;
                    word-break: break-word;
                    line-height: 1.15;
                }}
                #captains-table th:nth-child(1), #captains-table td:nth-child(1) {{ width: 12%; }}
                #captains-table th:nth-child(2), #captains-table td:nth-child(2) {{ width: 58%; }}
                #captains-table th:nth-child(3), #captains-table td:nth-child(3) {{ width: 30%; }}

                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}

                /* Points Breakdown extra-small */
                #roadtogs-table th,
                #roadtogs-table td {{
                    font-size: 7px;
                    padding: 2px 2px;
                }}
                #roadtogs-points-total {{
                    font-size: 11px !important;
                }}
            }}

            /* iOS WebKit-specific: keep Upcoming columns tight and consistent with desktop emulation */
            @supports (-webkit-touch-callout: none) {{
                @media (max-width: 768px) {{
                    #view-upcoming th,
                    #view-upcoming td {{
                        -webkit-text-size-adjust: 100%;
                        text-size-adjust: 100%;
                    }}
                }}
            }}
        </style>
    </head>
    <body onload="renderHistoryTable();">
        <button class="mobile-menu-toggle" onclick="toggleMobileMenu()">\\u2630</button>
        <div class="app-container">
            <div class="sidebar" id="sidebar">
                <div class="sidebar-header">WT Argentina</div>
                <div class="menu-item active" id="btn-upcoming" onclick="switchTab('upcoming')">Upcoming Tournaments</div>
                <div class="menu-item" id="btn-entrylists" onclick="switchTab('entrylists')">Entry Lists</div>
                <div class="menu-item" id="btn-calendar" onclick="switchTab('calendar')">Calendar</div>
                <div class="menu-item" id="btn-rankings" onclick="switchTab('rankings')">WTA Rankings</div>
                <div class="menu-item" id="btn-history" onclick="switchTab('history')">Match History</div>
                <div class="menu-item" id="btn-national" onclick="switchTab('national')">Argentina NT Players</div>
                <div class="menu-item" id="btn-captains" onclick="switchTab('captains')">Argentina NT Captains</div>
                <div class="menu-item" id="btn-roadtogs" onclick="switchTab('roadtogs')">Points Breakdown</div>
            </div>

            <div class="main-content">
                <div id="view-upcoming" class="single-layout">
                    <div class="header-row">
                        <div class="search-container">
                            <input type="text" id="s" placeholder="Search player..." oninput="filter()">
                        </div>
                        <h1>Upcoming Tournaments</h1>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table>
                                <thead>
                                    <tr>
                                        <th class="sticky-col col-rank">Rank</th>
                                        <th class="sticky-col col-name">Player</th>
                                        {"".join([f'<th class="col-week">{w}</th>' for w in week_keys])}
                                    </tr>
                                </thead>
                                <tbody id="tb">{table_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-entrylists" style="display: none;">
                    <div class="entry-layout">
                        <div class="entry-menu">
                            <div class="entry-menu-header">Tournaments</div>
                            {entry_menu_html}
                        </div>
                        <div class="entry-content">
                            <div class="header-row">
                                <h1 id="entry-title">Entry List</h1>
                                <div class="rankings-filter-container">
                                    <button id="btn-prio1" class="rankings-toggle-btn" style="display:none;" onclick="togglePrio1()">Show Prio 1</button>
                                </div>
                            </div>
                            <div class="content-card">
                                <table>
                                    <thead>
                                        <tr>
                                            <th style="width:15px">#</th>
                                            <th>PLAYER</th>
                                            <th style="width:35px">NAT</th>
                                            <th style="width:70px">E-Rank</th>
                                            <th id="entry-prio-header" style="width:35px;display:none">PRIO</th>
                                        </tr>
                                    </thead>
                                    <tbody id="entry-body"></tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="view-rankings" class="single-layout rankings-show-all" style="display: none;">
                    <div class="header-row">
                        <h1>WTA Rankings</h1>
                        <div class="rankings-controls">
                            <div class="search-container">
                                <input type="text" id="rankings-search" placeholder="Search player..." oninput="filterRankings()">
                            </div>
                            <div class="rankings-filter-container">
                                <div class="rankings-date-picker">
                                    <select id="rankings-year-select" class="rankings-date-select" onchange="onRankingYearChange(this.value)">{rankings_year_options}</select>
                                    <select id="rankings-month-select" class="rankings-date-select" onchange="onRankingMonthChange()"></select>
                                    <select id="rankings-day-select" class="rankings-date-select"></select>
                                    <button id="rankings-load-btn" class="rankings-load-btn" onclick="applyRankingSelection()">&#8594;</button>
                                </div>
                            </div>
                            <div class="rankings-btn-end">
                                <button id="rankings-toggle-btn" class="rankings-toggle-btn" onclick="toggleRankingsScope()">Show ARG</button>
                            </div>
                        </div>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="rankings-table">
                                <thead>
                                    <tr>
                                        <th style="width:55px">RANK</th>
                                        <th>PLAYER</th>
                                        <th style="width:60px">NAT</th>
                                        <th style="width:70px">POINTS</th>
                                        <th style="width:100px">DOB</th>
                                    </tr>
                                </thead>
                                <tbody id="rankings-body">{rankings_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-history" class="single-layout" style="display: none;">
                    <div class="history-layout">
                        <div class="filter-panel">
                            <h3>Filters</h3>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Surface <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-surface"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Round <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-round"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Result <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-result"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Year <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-year"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Tournament <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options scrollable" id="filter-tournament"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Category <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options scrollable" id="filter-category"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Opponent <span class="collapse-icon"></span>
                                </div>
                                <div class="opponent-select-container" style="padding: 8px; overflow: visible;">
                                    <select id="filter-opponent-select" style="width: 100%;">
                                        <option value="">All Opponents</option>
                                    </select>
                                </div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Opp. Country <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-opponent-country"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Player Entry <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-player-entry"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Seed <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-seed"></div>
                            </div>

                            <div class="filter-group collapsed">
                                <div class="filter-group-title" onclick="toggleFilterGroup(this)">
                                    Match Type <span class="collapse-icon"></span>
                                </div>
                                <div class="filter-options" id="filter-match-type"></div>
                            </div>

                            <div class="filter-actions">
                                <div class="filter-instructions">Ctrl+Click to select multiple options.</div>
                                <button class="filter-btn filter-btn-clear" onclick="clearHistoryFilters()">Reset Filters</button>
                            </div>
                        </div>

                        <div class="history-content">
                            <div class="table-header-section">
                                <div class="player-select-container">
                                    <select id="playerHistorySelect">
                                        <option value="">Select Player...</option>
                                        {"".join([f'<option value="{name}">{name}</option>' for name in history_players_sorted])}
                                    </select>
                                </div>
                                <h1 class="table-title">Match History</h1>
                                <div class="history-summary-container">
                                    <span id="history-wl-counter" class="history-wl-counter">Matches: 0 (0-0)</span>
                                </div>
                            </div>

                            <div class="content-card">
                                <div class="table-wrapper">
                                    <table id="history-table">
                                        <thead id="history-head"></thead>
                                        <tbody id="history-body">
                                            <tr><td colspan="100%" style="padding: 20px; color: #64748b;">Select a player to view their matches</td></tr>
                                        </tbody>
                                    </table>
                                </div>
                            </div>
                        </div>
                    </div>
                </div>

                <div id="view-national" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>Argentina NT - Player Debuts</h1>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="national-table">
                                <thead>
                                    <tr>
                                        {national_header_html}
                                    </tr>
                                </thead>
                                <tbody id="national-body">{national_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-captains" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>Argentina NT - Captains</h1>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="captains-table">
                                <thead>
                                    <tr>
                                        {captains_header_html}
                                    </tr>
                                </thead>
                                <tbody id="captains-body">{captains_rows}</tbody>
                            </table>
                        </div>
                    </div>
                </div>

                <div id="view-calendar" class="single-layout" style="display: none;">
                    <div class="content-card calendar-container">
                        <div class="table-wrapper">
                            {calendar_html}
                        </div>
                    </div>
                </div>

                <div id="view-roadtogs" class="single-layout" style="display: none;">
                    <div class="header-row">
                        <h1>Points Breakdown</h1>
                    </div>
                    <div class="roadtogs-controls">
                        <div class="player-select-container">
                            <select id="roadtogsPlayerSelect">
                                <option value="">Select Player...</option>
                                {"".join([f'<option value="{name}">{name}</option>' for name in roadtogs_players_sorted])}
                            </select>
                        </div>
                        <div id="roadtogs-points-total" style="font-size: 16px; font-weight: bold; color: #1e293b; padding-right: 12px;">Points: 0</div>
                    </div>
                    <div class="roadtogs-cutoffs">
                        {gs_tables_html}
                    </div>
                    <div class="roadtogs-legend">
                        <div>ACC. PTS = Points accumulated that count towards the ranking on the cutoff date.</div>
                        <div>EST. NEED = Estimated points needed to qualify for the Grand Slam: 330 for Q, 780 for MD (based on previous year's).</div>
                    </div>
                    <div class="content-card">
                        <div class="table-wrapper">
                            <table id="roadtogs-table">
                                <thead>
                                    <tr>
                                        <th>Date</th>
                                        <th>Tournament</th>
                                        <th>Round</th>
                                        <th>PTS</th>
                                        <th>Drop Date</th>
                                    </tr>
                                </thead>
                                <tbody id="roadtogs-body">
                                    <tr><td colspan="5" style="padding: 20px; color: #64748b;">Select a player to view their results</td></tr>
                                </tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            const tournamentData = {json.dumps(tournament_store)};
            const historyData = {json.dumps(cleaned_history)};
            const playerMapping = {json.dumps(PLAYER_MAPPING)};
            const pointsDistribution = {json.dumps(points_distribution)};
            const itfDrawSizes = {json.dumps(itf_draw_sizes)};
            const wtaDrawSizes = {json.dumps(wta_draw_sizes)};
            const gsCutoffs = {gs_cutoffs_json};
            function toggleMobileMenu() {{
                const sidebar = document.getElementById('sidebar');
                sidebar.classList.toggle('mobile-hidden');
            }}

            // Close mobile menu when clicking outside
            document.addEventListener('click', function(event) {{
                const sidebar = document.getElementById('sidebar');
                const menuToggle = document.querySelector('.mobile-menu-toggle');

                if (window.innerWidth <= 768) {{
                    if (!sidebar.contains(event.target) && !menuToggle.contains(event.target)) {{
                        sidebar.classList.add('mobile-hidden');
                    }}
                }}
            }});

            // Close mobile menu when tab is clicked
            function switchTab(tabName) {{
                document.querySelectorAll('.menu-item').forEach(el => el.classList.remove('active'));
                document.getElementById('btn-' + tabName).classList.add('active');

                document.getElementById('view-upcoming').style.display = (tabName === 'upcoming') ? 'flex' : 'none';
                document.getElementById('view-entrylists').style.display = (tabName === 'entrylists') ? 'flex' : 'none';
                document.getElementById('view-rankings').style.display = (tabName === 'rankings') ? 'flex' : 'none';
                document.getElementById('view-history').style.display = (tabName === 'history') ? 'flex' : 'none';
                document.getElementById('view-national').style.display = (tabName === 'national') ? 'flex' : 'none';
                document.getElementById('view-captains').style.display = (tabName === 'captains') ? 'flex' : 'none';
                document.getElementById('view-calendar').style.display = (tabName === 'calendar') ? 'flex' : 'none';
                document.getElementById('view-roadtogs').style.display = (tabName === 'roadtogs') ? 'flex' : 'none';

                if (tabName === 'entrylists') updateEntryList();
                applyMobileHistoryLayout();

                // Close mobile menu after selecting
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}
            }}

            function applyMobileHistoryLayout() {{
                const historyLayout = document.querySelector('#view-history .history-layout');
                if (!historyLayout) return;

                const filterPanel = historyLayout.querySelector('.filter-panel');
                const historyContent = historyLayout.querySelector('.history-content');
                if (!filterPanel || !historyContent) return;

                const headerSection = historyContent.querySelector('.table-header-section');
                if (!headerSection) return;

                if (window.innerWidth <= 768) {{
                    // Mobile order: header/search -> filters -> table
                    if (!historyContent.contains(filterPanel)) {{
                        headerSection.insertAdjacentElement('afterend', filterPanel);
                    }} else if (headerSection.nextElementSibling !== filterPanel) {{
                        headerSection.insertAdjacentElement('afterend', filterPanel);
                    }}
                }} else {{
                    // Desktop order: filters left -> content right
                    if (historyContent.contains(filterPanel)) {{
                        historyLayout.insertBefore(filterPanel, historyContent);
                    }}
                }}
            }}

            function reverseScore(score) {{
                if (!score) return '';
                return score.split(' ').map(set => {{
                    const m = set.match(/^(\\d+)-(\\d+)(.*)$/);
                    if (!m) return set;
                    return m[2] + '-' + m[1] + m[3];
                }}).join(' ');
            }}

            function formatSeed(seed) {{
                if (seed === null || seed === undefined) return '';
                const text = String(seed).trim();
                if (!text) return '';
                const num = Number(text);
                if (!Number.isNaN(num) && Number.isInteger(num)) {{
                    return String(num);
                }}
                return text;
            }}

            function buildPrefix(seed, entry) {{
                const parts = [];
                const formattedSeed = formatSeed(seed);
                if (formattedSeed) parts.push(formattedSeed);
                if (entry) parts.push(entry);
                if (parts.length === 0) return '';
                return '(' + parts.join('/') + ') ';
            }}

            function displayRound(round, tournament) {{
                return round || '';
            }}

            // Format date string to yyyy-MM-dd
            function formatDate(dateStr) {{
                if (!dateStr) return '';
                const parts = dateStr.split('/');
                if (parts.length === 3) {{
                    return parts[2] + '-' + parts[1].padStart(2, '0') + '-' + parts[0].padStart(2, '0');
                }}
                const d = new Date(dateStr);
                if (isNaN(d)) return dateStr;
                return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
            }}

            // Helper function to get display name from player mapping
            // Build reverse lookup cache for O(1) name resolution
            const _displayNameCache = {{}};
            (function() {{
                for (const [displayName, aliases] of Object.entries(playerMapping)) {{
                    for (const alias of aliases) {{
                        _displayNameCache[alias.toUpperCase()] = displayName;
                    }}
                }}
            }})();

            function getDisplayName(upperCaseName) {{
                const cached = _displayNameCache[upperCaseName];
                if (cached) return cached;
                // If not found, convert to title case (handling hyphens)
                const result = upperCaseName.split(' ').map(word => {{
                    if (word.includes('-')) {{
                        return word.split('-').map(part =>
                            part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
                        ).join('-');
                    }}
                    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
                }}).join(' ');
                _displayNameCache[upperCaseName] = result;
                return result;
            }}

            $(document).ready(function() {{
                // Initialize sidebar state for mobile
                if (window.innerWidth <= 768) {{
                    document.getElementById('sidebar').classList.add('mobile-hidden');
                }}

                $('#playerHistorySelect').select2({{
                    placeholder: 'Select a player...',
                    allowClear: true,
                    width: '250px'
                }});

                $('#playerHistorySelect').on('change', function() {{
                    filterHistoryByPlayer();
                }});

                renderHistoryTable();
                applyMobileHistoryLayout();

                // Handle window resize
                window.addEventListener('resize', function() {{
                    if (window.innerWidth > 768) {{
                        document.getElementById('sidebar').classList.remove('mobile-hidden');
                    }} else {{
                        document.getElementById('sidebar').classList.add('mobile-hidden');
                    }}
                    applyMobileHistoryLayout();
                }});
            }});

            function filter() {{
                const q = document.getElementById('s').value.toLowerCase();
                document.querySelectorAll('#tb tr').forEach(row => {{
                    const matches = row.getAttribute('data-name').includes(q);
                    row.classList.toggle('hidden', !matches);
                }});
            }}
            let showArgOnly = false;
            function toggleRankingsScope() {{
                showArgOnly = !showArgOnly;
                const btn = document.getElementById('rankings-toggle-btn');
                const view = document.getElementById('view-rankings');
                if (btn) btn.textContent = showArgOnly ? 'Show ALL' : 'Show ARG';
                if (view) view.classList.toggle('rankings-show-all', !showArgOnly);
                filterRankings();
            }}
            function filterRankings() {{
                const q = document.getElementById('rankings-search').value.toLowerCase();
                document.querySelectorAll('#rankings-body tr').forEach(row => {{
                    const text = row.textContent.toLowerCase();
                    const nat = row.children[2] ? row.children[2].textContent.trim().toUpperCase() : '';
                    const matchesSearch = text.includes(q);
                    const matchesCountry = !showArgOnly || nat === 'ARG';
                    row.classList.toggle('hidden', !(matchesSearch && matchesCountry));
                }});
            }}
            const _csvFileCaches = {{}};
            const _csvFilePromises = {{}};
            function _csvFileForYear(year) {{
                const y = parseInt(year);
                if (y >= 2020) return 'data/wta_rankings_20_29.csv';
                if (y >= 2010) return 'data/wta_rankings_10_19.csv';
                return 'data/wta_rankings_00_09.csv';
            }}
            function _parseCsvText(text) {{
                const cache = {{}};
                const lines = text.split('\\n');
                for (let i = 1; i < lines.length; i++) {{
                    const line = lines[i].trim();
                    if (!line) continue;
                    const cols = line.split(',');
                    if (cols.length < 5) continue;
                    const date = cols[0].trim();
                    if (!cache[date]) cache[date] = [];
                    cache[date].push({{
                        r: parseInt(cols[1]) || null,
                        pts: parseInt(cols[2]) || 0,
                        n: cols[3] || '',
                        c: cols[4] || '',
                        d: (cols[5] || '').replace(/\\r/g, '').trim()
                    }});
                }}
                return cache;
            }}
            function _loadCsvForYear(year) {{
                const file = _csvFileForYear(year);
                if (_csvFileCaches[file]) return Promise.resolve(_csvFileCaches[file]);
                if (_csvFilePromises[file]) return _csvFilePromises[file];
                _csvFilePromises[file] = fetch(file)
                    .then(r => {{
                        if (!r.ok) throw new Error('HTTP ' + r.status);
                        return r.text();
                    }})
                    .then(text => {{
                        _csvFileCaches[file] = _parseCsvText(text);
                        _csvFilePromises[file] = null;
                        return _csvFileCaches[file];
                    }})
                    .catch(err => {{
                        console.error('Failed to load ' + file + ':', err);
                        _csvFilePromises[file] = null;
                        throw err;
                    }});
                return _csvFilePromises[file];
            }}
            function _renderRankingRows(players) {{
                const tbody = document.getElementById('rankings-body');
                let html = '';
                players.forEach(p => {{
                    const dob = (p.d || '').split('T')[0];
                    const name = (p.n || '').toLowerCase().replace(/(^|\\s)(\\S)/g, (_, b, c) => b + c.toUpperCase());
                    const isArg = (p.c || '').toUpperCase() === 'ARG';
                    html += `<tr class="${{isArg ? 'arg-player-row' : ''}}"><td>${{p.r || ''}}</td><td style="text-align:left;font-weight:bold;">${{name}}</td><td>${{p.c || ''}}</td><td>${{p.pts || ''}}</td><td>${{dob}}</td></tr>`;
                }});
                tbody.innerHTML = html;
                filterRankings();
            }}
            const _rankingsDatesIndex = {rankings_dates_index_json};
            const _rankingMonthNames = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
            function _populateRankingMonths(year, selectMonth, selectDay) {{
                const sel = document.getElementById('rankings-month-select');
                const months = Object.keys(_rankingsDatesIndex[year] || {{}}).map(Number).sort((a,b)=>a-b);
                const chosenM = (selectMonth != null && months.includes(+selectMonth)) ? +selectMonth : months[months.length-1];
                sel.innerHTML = months.map(m => {{
                    const isSel = (m === chosenM) ? ' selected' : '';
                    return `<option value="${{m}}"${{isSel}}>${{_rankingMonthNames[m-1]}}</option>`;
                }}).join('');
                _populateRankingDays(year, chosenM, selectDay);
            }}
            function _populateRankingDays(year, monthNum, selectDay) {{
                const sel = document.getElementById('rankings-day-select');
                const days = ((_rankingsDatesIndex[year] || {{}})[String(monthNum)] || []).slice().sort((a,b)=>a-b);
                const chosenD = (selectDay != null && days.includes(+selectDay)) ? +selectDay : days[days.length-1];
                sel.innerHTML = days.map(d => {{
                    const isSel = (d === chosenD) ? ' selected' : '';
                    return `<option value="${{d}}"${{isSel}}>${{d}}</option>`;
                }}).join('');
            }}
            function onRankingYearChange(year) {{
                _populateRankingMonths(year, null, null);
            }}
            function onRankingMonthChange() {{
                const year = document.getElementById('rankings-year-select').value;
                const month = +document.getElementById('rankings-month-select').value;
                _populateRankingDays(year, month, null);
            }}
            function applyRankingSelection() {{
                const year = document.getElementById('rankings-year-select').value;
                const month = document.getElementById('rankings-month-select').value;
                const day = document.getElementById('rankings-day-select').value;
                if (!year || !month || !day) return;
                const mm = month.toString().padStart(2,'0');
                const dd = day.toString().padStart(2,'0');
                switchRankingWeek(`${{year}}-${{mm}}-${{dd}}`);
            }}
            function switchRankingWeek(dateStr) {{
                const year = dateStr.split('-')[0];
                const controls = ['rankings-year-select','rankings-month-select','rankings-day-select','rankings-load-btn'].map(id => document.getElementById(id));
                controls.forEach(el => {{ if(el) {{ el.disabled = true; el.style.opacity = '0.5'; }} }});
                _loadCsvForYear(year)
                    .then(data => {{
                        const players = data[dateStr];
                        if (players) _renderRankingRows(players);
                    }})
                    .catch(() => {{}})
                    .finally(() => {{
                        controls.forEach(el => {{ if(el) {{ el.disabled = false; el.style.opacity = '1'; }} }});
                    }});
            }}
            _populateRankingMonths('{rankings_latest_year_str}', {rankings_latest_month}, {rankings_latest_day});
            function filterNational() {{
                const q = document.getElementById('national-search').value.toLowerCase();
                document.querySelectorAll('#national-body tr').forEach(row => {{
                    const text = row.textContent.toLowerCase();
                    row.classList.toggle('hidden', !text.includes(q));
                }});
            }}
            function selectEntryTournament(el) {{
                document.querySelectorAll('.entry-menu-item').forEach(item => item.classList.remove('active'));
                el.classList.add('active');
                updateEntryList(el.getAttribute('data-key'), el.textContent);
            }}

            let _prioFilterActive = false;

            function togglePrio1() {{
                _prioFilterActive = !_prioFilterActive;
                document.getElementById('btn-prio1').textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                updateEntryList();
            }}

            function renderRows(list, isMain, isITF, renumber) {{
                const prioCell = p => isITF ? `<td>${{p.priority||''}}</td>` : '';
                let html = '';
                list.forEach((p, i) => {{
                    const displayPos = renumber ? (i + 1) : p.pos;
                    const bold = isMain ? 'font-weight:bold;' : '';
                    html += `<tr class="${{p.country==='ARG'?'row-arg':''}}"><td>${{displayPos}}</td><td style="text-align:left;${{bold}}">${{p.name}}</td><td>${{p.country}}</td><td>${{p.rank}}</td>${{prioCell(p)}}</tr>`;
                }});
                return html;
            }}

            function updateEntryList(key, name) {{
                if (!key) {{
                    const active = document.querySelector('.entry-menu-item.active');
                    if (!active) return;
                    key = active.getAttribute('data-key');
                    name = active.textContent;
                }}
                const body = document.getElementById('entry-body');
                document.getElementById('entry-title').textContent = name || 'Entry List';
                if (!tournamentData[key]) return;
                const players = tournamentData[key];
                const isITF = !key.startsWith('http');
                document.getElementById('entry-prio-header').style.display = isITF ? '' : 'none';
                const btn = document.getElementById('btn-prio1');
                btn.style.display = isITF ? '' : 'none';
                if (!isITF) _prioFilterActive = false;
                btn.textContent = _prioFilterActive ? 'Show All' : 'Show Prio 1';
                let html = '';
                const main = players.filter(p => p.type === 'MAIN');
                const qual = players.filter(p => p.type === 'QUAL');
                const alt = players.filter(p => p.type === 'ALT');
                const cols = isITF ? 5 : 4;

                if (_prioFilterActive) {{
                    // JR prio1 players go at the bottom; non-prio1 JR spots filled from qual/alt
                    const mainJRPrio1 = main.filter(p => p.entry === 'JR' && p.priority === '1');
                    const mainRegular = main.filter(p => p.entry !== 'JR');
                    const regularSpots = main.length - mainJRPrio1.length;
                    const pool = [
                        ...mainRegular.filter(p => p.priority === '1'),
                        ...qual.filter(p => p.priority === '1'),
                        ...alt.filter(p => p.priority === '1'),
                    ];
                    const displayMain = [
                        ...pool.slice(0, regularSpots),
                        ...mainJRPrio1,
                    ];
                    const remainingPool = pool.slice(regularSpots);
                    const displayQual = remainingPool.slice(0, qual.length);
                    const displayAlt  = remainingPool.slice(qual.length);
                    html += renderRows(displayMain, true, isITF, true);
                    if (displayQual.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">QUALIFYING</td></tr>`;
                        html += renderRows(displayQual, false, isITF, true);
                    }}
                    if (displayAlt.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">ALTERNATES</td></tr>`;
                        html += renderRows(displayAlt, false, isITF, true);
                    }}
                }} else {{
                    html += renderRows(main, true, isITF, false);
                    if (qual.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">QUALIFYING</td></tr>`;
                        html += renderRows(qual, false, isITF, false);
                    }}
                    if (alt.length > 0) {{
                        html += `<tr class="divider-row"><td colspan="${{cols}}">ALTERNATES</td></tr>`;
                        html += renderRows(alt, false, isITF, false);
                    }}
                }}
                body.innerHTML = html;
            }}


            function renderHistoryTable() {{
                const thead = document.getElementById('history-head');
                const tbody = document.getElementById('history-body');

                if (!historyData || historyData.length === 0) return;

                // Define column headers (excluding hidden _ columns)
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];
                let headHtml = '<tr>';
                displayColumns.forEach(col => {{
                    headHtml += `<th>${{col.replace('_', ' ')}}</th>`;
                }});
                headHtml += '</tr>';
                thead.innerHTML = headHtml;

                // Set initial placeholder message
                tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px; color: #64748b;">Select a player to view their matches</td></tr>`;
            }}

            let currentPlayerData = [];

            function toggleFilterGroup(element) {{
                element.parentElement.classList.toggle('collapsed');
            }}

            function getRowMatchType(row) {{
                const explicit = (row['MATCH_TYPE'] || row['matchType'] || '').toString().trim();
                if (explicit) return explicit;

                // Backward-compatible fallback for older rows without matchType.
                const tournament = (row['TOURNAMENT'] || '').toString();
                const isITF = tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                              tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                              tournament.includes('W75') || tournament.includes('W100');
                return isITF ? 'ITF' : 'WTA';
            }}

            function getRowYear(row) {{
                const dateStr = (row['DATE'] || '').toString().trim();
                const match = dateStr.match(/^(\\d{{4}})/);
                return match ? match[1] : '';
            }}

            function getResultLabel(row, isWinner) {{
                const statusDesc = (row['_resultStatusDesc'] || '').toString().toLowerCase();
                const scoreText = (row['SCORE'] || '').toString().toLowerCase();
                const isRet = statusDesc.includes('retired') || statusDesc.includes('ret.') || scoreText.includes('ret.');
                const isDef = statusDesc.includes('default') || statusDesc.includes('def.') || scoreText.includes('def.');

                if (isWinner) {{
                    if (isRet) return 'Wins by RET';
                    if (isDef) return 'Wins by DEF';
                    return 'Wins';
                }}
                if (isRet) return 'Losses by RET';
                if (isDef) return 'Losses by DEF';
                return 'Losses';
            }}

            function isTeamEventRow(row) {{
                const matchType = (row['MATCH_TYPE'] || row['matchType'] || '').toString();
                const category = (row['CATEGORY'] || row['tournamentCategory'] || '').toString();
                const tournament = (row['TOURNAMENT'] || row['tournamentName'] || '').toString();
                return (
                    matchType === 'Fed/BJK Cup' ||
                    category.includes('Fed/BJK Cup') ||
                    tournament.includes('BJK') ||
                    tournament.includes('Fed Cup')
                );
            }}

            function getRoundFilterLabel(row) {{
                const roundValue = (row['ROUND'] || '').toString().trim();
                if (!roundValue) return '';
                if (isTeamEventRow(row)) return roundValue.startsWith('Team - ') ? roundValue : `Team - ${{roundValue}}`;
                return roundValue;
            }}

            function populateFilters(playerMatches) {{
                // Extract unique values for each filter
                const surfaces = new Set();
                const rounds = new Set();
                const results = new Set();
                const years = new Set();
                const tournaments = new Set();
                const categories = new Set();
                const opponents = new Set();
                const opponentCountries = new Set();
                const playerEntries = new Set();
                const seeds = new Set(['Yes', 'No']);
                const matchTypes = new Set();

                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();

                playerMatches.forEach(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;
                    const resultLabel = getResultLabel(row, isWinner);
                    if (resultLabel) results.add(resultLabel);

                    // Surface
                    if (row['SURFACE']) surfaces.add(row['SURFACE']);

                    // Round (Team events are prefixed only in filter labels)
                    const roundFilterLabel = getRoundFilterLabel(row);
                    if (roundFilterLabel) rounds.add(roundFilterLabel);

                    // Year
                    const year = getRowYear(row);
                    if (year) years.add(year);

                    // Tournament
                    if (row['TOURNAMENT']) tournaments.add(row['TOURNAMENT']);

                    // Category
                    const category = row['CATEGORY'] || '';
                    if (category) categories.add(category);

                    // Opponent
                    const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    if (opponentName) opponents.add(getDisplayName(opponentName.toUpperCase()));

                    // Opp. Country
                    const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    if (opponentCountry) opponentCountries.add(opponentCountry);

                    // Player Entry
                    const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    if (playerEntry) playerEntries.add(playerEntry);

                    // Match Type (from CSV matchType column; fallback for legacy rows)
                    const matchType = getRowMatchType(row);
                    if (matchType) matchTypes.add(matchType);
                }});

                // Populate filter options
                const orderedResults = [
                    'Wins',
                    'Losses',
                    'Wins by RET',
                    'Losses by RET',
                    'Wins by DEF',
                    'Losses by DEF'
                ].filter(r => results.has(r));
                const orderedYears = Array.from(years).sort((a, b) => Number(b) - Number(a));
                orderedYears.unshift('Last 52');
                orderedYears.unshift('Career');

                populateFilterOptions('filter-surface', Array.from(surfaces).sort());
                const roundOrderForFilter = {{
                    'QR1': 1, 'QR2': 2, 'QR3': 3, 'QR4': 4,
                    '1st Round': 5, '2nd Round': 6, '3rd Round': 7, '4th Round': 8, '5th Round': 9,
                    'Quarter-finals': 10, 'Semi-finals': 11, 'Final': 12,
                    'Team - Round Robin': 13, 'Team - Last 32': 14, 'Team - Last 16': 15,
                    'Team - Quarter Finals': 16, 'Team - Semi Finals': 17, 'Team - Final': 18,
                }};
                const orderedRounds = Array.from(rounds).sort((a, b) => {{
                    const oa = roundOrderForFilter[a] ?? 99;
                    const ob = roundOrderForFilter[b] ?? 99;
                    return oa !== ob ? oa - ob : a.localeCompare(b);
                }});
                populateFilterOptions('filter-round', orderedRounds);
                populateFilterOptions('filter-result', orderedResults);
                populateFilterOptions('filter-year', orderedYears);
                populateFilterOptions('filter-tournament', Array.from(tournaments).sort((a, b) => a.localeCompare(b)));
                populateFilterOptions('filter-category', Array.from(categories).sort((a, b) => a.localeCompare(b)));
                populateOpponentSelect(Array.from(opponents).sort());
                populateFilterOptions('filter-opponent-country', Array.from(opponentCountries).sort());
                populateFilterOptions('filter-player-entry', Array.from(playerEntries).sort());
                populateFilterOptions('filter-seed', Array.from(seeds));
                populateFilterOptions('filter-match-type', Array.from(matchTypes).sort());
            }}

            function populateFilterOptions(filterId, values) {{
                const container = document.getElementById(filterId);
                let html = '';
                values.forEach(value => {{
                    if (value) {{
                        html += `<div class="filter-option" data-value="${{value}}" onclick="toggleFilterOption(event, this)">${{value}}</div>`;
                    }}
                }});
                container.innerHTML = html || '<div style="padding: 5px; color: #94a3b8; font-size: 11px;">No options</div>';
            }}

            function populateOpponentSelect(opponents) {{
                const select = document.getElementById('filter-opponent-select');

                // Destroy existing Select2 if it exists
                if ($(select).data('select2')) {{
                    $(select).select2('destroy');
                }}

                // Clear and populate options
                let html = '<option value="">All Opponents</option>';
                opponents.forEach(opponent => {{
                    if (opponent) {{
                        html += `<option value="${{opponent}}">${{opponent}}</option>`;
                    }}
                }});
                select.innerHTML = html;

                // Initialize Select2 with search
                $(select).select2({{
                    placeholder: 'All Opponents',
                    allowClear: true,
                    width: '100%'
                }});

                // Auto-apply filters when selection changes
                $(select).off('change').on('change', function() {{
                    const selectedText = this.options[this.selectedIndex] ? this.options[this.selectedIndex].text : 'All Opponents';
                    const rendered = this.nextElementSibling
                        ? this.nextElementSibling.querySelector('.select2-selection__rendered')
                        : null;
                    if (rendered) {{
                        rendered.textContent = selectedText;
                        rendered.title = selectedText;
                    }}
                    applyHistoryFilters();
                }});
            }}

            function toggleFilterOption(event, element) {{
                // Support Ctrl+Click for multi-select
                if (!event.ctrlKey && !event.metaKey) {{
                    // Single click without Ctrl - deselect all others in this group first
                    const siblings = element.parentElement.querySelectorAll('.filter-option');
                    siblings.forEach(sib => {{
                        if (sib !== element) sib.classList.remove('selected');
                    }});
                }}

                // Toggle this option
                element.classList.toggle('selected');

                // Auto-apply filters
                applyHistoryFilters();
            }}

            function getSelectedFilterValues(filterId) {{
                const container = document.getElementById(filterId);
                const selectedOptions = container.querySelectorAll('.filter-option.selected');
                return Array.from(selectedOptions).map(option => option.getAttribute('data-value'));
            }}

            function updateHistoryCounter(matches, selectedPlayer) {{
                const counter = document.getElementById('history-wl-counter');
                if (!counter) return;

                const nonWO = (matches || []).filter(row => !['Walkover', 'Bye'].includes(row['_resultStatusDesc'] || ''));
                const total = nonWO.length;
                if (!selectedPlayer || total === 0) {{
                    counter.textContent = `Matches: ${{total}} (0-${{total}})`;
                    return;
                }}

                let wins = 0;
                nonWO.forEach(row => {{
                    const wName = (row['_winnerName'] || '').toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    if (wNameNormalized === selectedPlayer) wins += 1;
                }});
                const losses = total - wins;
                counter.textContent = `Matches: ${{total}} (${{wins}}-${{losses}})`;
            }}

            function applyHistoryFilters() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                if (!selectedPlayer) return;

                // Get selected filter values
                const selectedSurfaces = getSelectedFilterValues('filter-surface');
                const selectedRounds = getSelectedFilterValues('filter-round');
                const selectedResults = getSelectedFilterValues('filter-result');
                const selectedYears = getSelectedFilterValues('filter-year');
                const selectedTournaments = getSelectedFilterValues('filter-tournament');
                const selectedCategories = getSelectedFilterValues('filter-category');
                const selectedOpponent = document.getElementById('filter-opponent-select').value;
                const selectedOpponentCountries = getSelectedFilterValues('filter-opponent-country');
                const selectedPlayerEntries = getSelectedFilterValues('filter-player-entry');
                const selectedSeeds = getSelectedFilterValues('filter-seed');
                const selectedMatchTypes = getSelectedFilterValues('filter-match-type');

                // Filter the data (if nothing selected in a category, show all)
                const filtered = currentPlayerData.filter(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const lName = (row['_loserName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const lNameNormalized = getDisplayName(lName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;

                    // Surface filter
                    if (selectedSurfaces.length > 0 && !selectedSurfaces.includes(row['SURFACE'] || '')) return false;

                    // Round filter
                    const roundFilterLabel = getRoundFilterLabel(row);
                    if (selectedRounds.length > 0 && !selectedRounds.includes(roundFilterLabel)) return false;

                    // Result filter
                    const result = getResultLabel(row, isWinner);
                    if (selectedResults.length > 0 && !selectedResults.includes(result)) return false;

                    // Year filter
                    const rowYear = getRowYear(row);
                    if (selectedYears.length > 0 && !selectedYears.includes('Career')) {{
                        const wantLast52 = selectedYears.includes('Last 52');
                        const otherYears = selectedYears.filter(y => y !== 'Last 52');
                        let pass = false;
                        if (wantLast52) {{
                            const today = new Date();
                            const dayOfWeek = today.getDay() === 0 ? 6 : today.getDay() - 1; // Mon=0
                            const weekStart = new Date(today);
                            weekStart.setDate(today.getDate() - dayOfWeek);
                            weekStart.setHours(0, 0, 0, 0);
                            const cutoff = new Date(weekStart);
                            cutoff.setDate(weekStart.getDate() - 51 * 7);
                            const rowDate = new Date(row['DATE'] || '');
                            if (!isNaN(rowDate) && rowDate >= cutoff) pass = true;
                        }}
                        if (!pass && otherYears.length > 0 && otherYears.includes(rowYear)) pass = true;
                        if (!pass) return false;
                    }}

                    // Tournament filter
                    if (selectedTournaments.length > 0 && !selectedTournaments.includes(row['TOURNAMENT'] || '')) return false;

                    // Category filter
                    const rowCategory = row['CATEGORY'] || '';
                    if (selectedCategories.length > 0 && !selectedCategories.includes(rowCategory)) return false;

                    // Opponent filter (single select from dropdown)
                    if (selectedOpponent) {{
                        const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                        const opponentDisplay = opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                        if (opponentDisplay !== selectedOpponent) return false;
                    }}

                    // Opp. Country filter
                    const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    if (selectedOpponentCountries.length > 0 && !selectedOpponentCountries.includes(opponentCountry)) return false;

                    // Player Entry filter
                    const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    if (selectedPlayerEntries.length > 0 && !selectedPlayerEntries.includes(playerEntry)) return false;

                    // Seed filter
                    const playerSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const hasSeed = playerSeed ? 'Yes' : 'No';
                    if (selectedSeeds.length > 0 && !selectedSeeds.includes(hasSeed)) return false;

                    // Match Type filter
                    const matchType = getRowMatchType(row);
                    if (selectedMatchTypes.length > 0 && !selectedMatchTypes.includes(matchType)) return false;

                    return true;
                }});

                updateHistoryCounter(filtered, selectedPlayer);
                renderFilteredMatches(filtered, selectedPlayer);
            }}

            function clearHistoryFilters() {{
                // Remove selected class from all filter options
                document.querySelectorAll('.filter-option.selected').forEach(option => {{
                    option.classList.remove('selected');
                }});
                // Reset opponent select dropdown
                $('#filter-opponent-select').val('').trigger('change');
                // Auto-apply filters (which will show all matches since nothing is selected)
                applyHistoryFilters();
            }}

            function renderFilteredMatches(matches, selectedPlayer) {{
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];
                updateHistoryCounter(matches, selectedPlayer);

                if (matches.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No matches found with the selected filters.</td></tr>`;
                    return;
                }}

                // Round priority (lower = higher in table)
                const roundOrder = {{
                    'Final': 1, 'Semi-finals': 2, 'Quarter-finals': 3,
                    '4th Round': 4, '3rd Round': 5, '2nd Round': 6, '1st Round': 7,
                    'QR4': 8, 'QR3': 9, 'QR2': 10, 'QR1': 11,
                    'Semi Finals': 12, 'Quarter Finals': 13,
                    'Last 16': 14, 'Last 32': 15, 'Round Robin': 16
                }};
                function getRoundOrder(round) {{
                    return roundOrder[round] || 99;
                }}

                // Sort by date descending, then by round order ascending
                matches.sort((a, b) => {{
                    const dateA = formatDate(a['DATE'] || '1900-01-01');
                    const dateB = formatDate(b['DATE'] || '1900-01-01');
                    if (dateA !== dateB) return dateB.localeCompare(dateA);
                    return getRoundOrder(displayRound(a['ROUND'], a['TOURNAMENT'])) - getRoundOrder(displayRound(b['ROUND'], b['TOURNAMENT']));
                }});

                const parts = [];
                const playerDisplayName = getDisplayName(selectedPlayer);
                for (let i = 0; i < matches.length; i++) {{
                    const row = matches[i];
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const isWinner = getDisplayName(wName).toUpperCase() === selectedPlayer;

                    const rivalName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    const rivalDisplayName = rivalName ? getDisplayName(rivalName.toUpperCase()) : '';

                    const pSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const pEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    const rSeed = isWinner ? (row['_loserSeed'] || '') : (row['_winnerSeed'] || '');
                    const rEntry = isWinner ? (row['_loserEntry'] || '') : (row['_winnerEntry'] || '');

                    const rivalCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    const opponentName = rivalDisplayName + (rivalCountry ? ` [${{rivalCountry}}]` : '');

                    parts.push('<tr><td>', formatDate(row['DATE'] || ''),
                        '</td><td>', row['TOURNAMENT'] || '',
                        '</td><td>', row['SURFACE'] || '',
                        '</td><td>', displayRound(row['ROUND'] || '', row['TOURNAMENT'] || ''),
                        '</td><td>', buildPrefix(pSeed, pEntry) + playerDisplayName,
                        '</td><td>', isWinner ? 'W' : 'L',
                        '</td><td>', isWinner ? (row['SCORE'] || '') : reverseScore(row['SCORE'] || ''),
                        '</td><td>', buildPrefix(rSeed, rEntry) + opponentName,
                        '</td></tr>');
                }}
                tbody.innerHTML = parts.join('');
            }}

            function filterHistoryByPlayer() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];

                if (!selectedPlayer) {{
                    currentPlayerData = [];
                    ['filter-surface', 'filter-round', 'filter-result', 'filter-year', 'filter-tournament', 'filter-category', 'filter-opponent-country', 'filter-player-entry', 'filter-seed', 'filter-match-type']
                        .forEach(id => {{
                            const el = document.getElementById(id);
                            if (el) el.innerHTML = '';
                        }});
                    const oppSelect = document.getElementById('filter-opponent-select');
                    if (oppSelect) {{
                        if ($(oppSelect).data('select2')) {{
                            $(oppSelect).select2('destroy');
                        }}
                        oppSelect.innerHTML = '<option value="">All Opponents</option>';
                    }}
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">Select a player...</td></tr>`;
                    updateHistoryCounter([], '');
                    return;
                }}

                const filtered = historyData.filter(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const lName = (row['_loserName'] || "").toString().toUpperCase();
                    // Normalize names using the player mapping to match aliases
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const lNameNormalized = getDisplayName(lName).toUpperCase();
                    return wNameNormalized === selectedPlayer || lNameNormalized === selectedPlayer;
                }});

                if (filtered.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No matches found for this player.</td></tr>`;
                    updateHistoryCounter([], selectedPlayer);
                    return;
                }}

                // Store current player data for filtering
                currentPlayerData = filtered;

                // Populate filters with this player's data
                populateFilters(filtered);

                // Render all matches (filters start with all checked)
                renderFilteredMatches(filtered, selectedPlayer);
            }}

            // Road to GS: shared lookups (initialised once, reused by renderRoadToGS + computeBest18)
            const _rtgs_roundOrder = {{'QR1':1,'QR2':2,'QR3':3,'QR4':4,'Round Robin':4.5,'1st Round':5,'2nd Round':6,'3rd Round':7,'4th Round':8,'5th Round':9,'Quarter Finals':10,'Quarter-finals':10,'Semi-finals':11,'Final':12}};
            const _rtgs_categoryToDesc = {{
                'GS':'Grand Slam','WTA 1000':'WTA 1000 (56M, 32Q)','WTA 500':'WTA 500 (30/28M, 24/16Q)',
                'WTA 250':'WTA 250 (32M, 24/16Q)','WTA 125':'WTA 125 (32M, 8Q)',
                '125K':'WTA 125 (32M, 8Q)','125K Series':'WTA 125 (32M, 8Q)',
                'W100':'W100 (32M, 32Q)','W75':'W75 (32M, 32Q)','W50':'W50 (32M, 32Q)',
                'W35':'W35 (32M, 64/48/32/24Q)','W15':'W15 (32M, 64/48/32/24Q)'
            }};
            const _rtgs_categoryDrawSize = {{'GS':128,'WTA 1000':64,'WTA 500':32,'WTA 250':32,'WTA 125':32,'125K':32,'125K Series':32,'W100':32,'W75':32,'W50':32,'W35':32,'W15':32}};
            const _rtgs_mandatory1000Names = ['Indian Wells','Miami','Madrid','Rome','Toronto','Montreal','Cincinnati','Beijing'];
            const _rtgs_optional1000Names  = ['Doha','Dubai','Wuhan'];
            let _rtgs_pointsLookup = null, _rtgs_itfDrawLookup = null, _rtgs_wtaDrawLookup = null;

            function _rtgs_initLookups() {{
                if (_rtgs_pointsLookup) return;
                _rtgs_pointsLookup = {{}};
                pointsDistribution.forEach(p => {{ _rtgs_pointsLookup[p.Description] = p; }});
                _rtgs_itfDrawLookup = {{}};
                itfDrawSizes.forEach(t => {{
                    const key = (t.tournamentName||'') + '|' + (t.date||'');
                    _rtgs_itfDrawLookup[key] = {{description:t.description, mainDrawSize:t.mainDrawSize}};
                    const wm = (t.tournamentName||'').match(/^(.+?)\s*\(Week \d+\)$/);
                    if (wm) _rtgs_itfDrawLookup[wm[1].trim()+'|'+(t.date||'')] = _rtgs_itfDrawLookup[key];
                }});
                _rtgs_wtaDrawLookup = {{}};
                wtaDrawSizes.forEach(t => {{
                    if (!t.description || !t.tournamentId) return;
                    _rtgs_wtaDrawLookup[String(parseInt(t.tournamentId)||t.tournamentId)] = {{description:t.description, mainDrawSize:t.mainDrawSize}};
                }});
            }}

            function _rtgs_monday(dateStr) {{
                const d = new Date(dateStr), day = d.getUTCDay();
                const m = new Date(d);
                m.setUTCDate(d.getUTCDate() + (day===0 ? -6 : 1-day));
                return m.toISOString().slice(0,10);
            }}

            // 2-week tournaments that freeze rankings for 2 consecutive weeks
            const _rtgs_twoWeekNames = ['Australian Open','Roland Garros','Wimbledon','US Open','Indian Wells','Miami','Madrid','Internazionali','Rome'];
            // Main-draw mondays of genuine 2-week tournaments (GS + WTA 1000 only).
            // Qualifying and lower-tier tournaments excluded so week-1 detection works correctly.
            // Each main-draw monday also adds mon+7 to cover both weeks of the freeze.
            const _rtgs_twoWeekFreezeMondays = (() => {{
                const s = new Set();
                historyData.forEach(r => {{
                    const tName = r['TOURNAMENT'] || '';
                    const draw = (r['DRAW'] || '').toUpperCase();
                    const cat = (r['CATEGORY'] || '').trim();
                    const mt = (r['MATCH_TYPE'] || '').trim();
                    const isGenuine2Week = mt === 'GS' || cat === 'WTA 1000' || cat === 'Premier Mandatory' || cat === 'Premier 5';
                    if (draw === 'M' && isGenuine2Week && _rtgs_twoWeekNames.some(n => tName.includes(n))) {{
                        const mon = _rtgs_monday(r['DATE'] || '');
                        if (mon) {{
                            s.add(mon);
                            const w2 = new Date(mon);
                            w2.setUTCDate(w2.getUTCDate() + 7);
                            s.add(w2.toISOString().slice(0, 10));
                        }}
                    }}
                }});
                return s;
            }})();

            function _rtgs_mdKey(round, result, drawSize) {{
                if (round==='Final') return result==='W'?'W':'F';
                if (round==='Semi-finals') return 'SF';
                if (round==='Quarter-finals') return 'QF';
                if (drawSize===128) {{ if (round==='4th Round') return 'R16'; if (round==='3rd Round') return 'R32'; if (round==='2nd Round') return 'R64'; if (round==='1st Round') return 'R128'; }}
                else if (drawSize===64) {{ if (round==='3rd Round') return 'R16'; if (round==='2nd Round') return 'R32'; if (round==='1st Round') return 'R64'; }}
                else {{ if (round==='2nd Round') return 'R16'; if (round==='1st Round') return 'R32'; }}
                return null;
            }}

            function _rtgs_qKey(round, result, hasMain) {{ return (hasMain||result==='W') ? 'QLFR' : round; }}

            function computeBest18(selectedPlayer, windowEndStr) {{
                _rtgs_initLookups();
                const windowEnd = new Date(windowEndStr);
                const windowStart = new Date(windowEnd);
                windowStart.setDate(windowStart.getDate() - 385); // 55 weeks: covers up to 54-week drop periods

                const matches = historyData.filter(row => {{
                    const mt = (row['MATCH_TYPE']||'').trim();
                    if (mt==='Fed/BJK Cup') return false;
                    const wn = getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const ln = getDisplayName((row['_loserName']||'').toString().toUpperCase()).toUpperCase();
                    if (wn!==selectedPlayer && ln!==selectedPlayer) return false;
                    const ds = row['DATE']||''; if (!ds) return false;
                    const md = new Date(ds);
                    return md>=windowStart && md<=windowEnd;
                }});
                if (!matches.length) return 0;

                const tMap = new Map();
                matches.forEach(row => {{
                    const tName=row['TOURNAMENT']||'', ds=row['DATE']||'';
                    const mt=(row['MATCH_TYPE']||'').trim(), cat=(row['CATEGORY']||'').trim();
                    const isGS=mt==='GS', isUC=tName.toUpperCase().includes('UNITED CUP');
                    const mon=_rtgs_monday(ds), draw=(row['DRAW']||'').toUpperCase();
                    const round=row['ROUND']||'', rOrd=_rtgs_roundOrder[round]||0;
                    const wn=getDisplayName((row['_winnerName']||'').toString().toUpperCase()).toUpperCase();
                    const res=wn===selectedPlayer?'W':'L';
                    const tid=(row['TOURNAMENT_ID']||'').trim();
                    // Group by tournamentId so a tournament spanning multiple weeks is one entry
                    const key=(isGS||isUC)?(mt+'|'+tName):(tid?(tid+'|'+tName):(mon+'|'+tName));
                    if (!tMap.has(key)) tMap.set(key, {{date:mon,tournament:tName,tournamentId:tid,category:cat,isGS:isGS,isUnitedCup:isUC,bestMainRound:'',bestMainOrder:0,bestMainResult:'',bestQualRound:'',bestQualOrder:0,bestQualResult:'',qualMonday:'',mainMonday:'',ucWins:0,ucTotal:0,ucHasKnockout:false}});
                    const e=tMap.get(key);
                    if (isUC) {{ e.ucTotal++; if (res==='W') e.ucWins++; if (round!=='Round Robin'&&res==='W') e.ucHasKnockout=true; }}
                    if (draw==='Q') {{
                        if (rOrd>e.bestQualOrder) {{e.bestQualRound=round;e.bestQualOrder=rOrd;e.bestQualResult=res;}}
                        if (!e.qualMonday||mon<e.qualMonday) e.qualMonday=mon;
                    }} else {{
                        if (rOrd>e.bestMainOrder) {{e.bestMainRound=round;e.bestMainOrder=rOrd;e.bestMainResult=res;}}
                        if (!e.mainMonday||mon<e.mainMonday) e.mainMonday=mon;
                    }}
                }});

                tMap.forEach(t => {{
                    if (t.isGS) {{
                        if (t.mainMonday) {{ t.date=t.mainMonday; }}
                        else if (t.qualMonday) {{ const q=new Date(t.qualMonday); q.setUTCDate(q.getUTCDate()+7); t.date=q.toISOString().slice(0,10); }}
                    }} else if (t.isUnitedCup&&t.mainMonday) {{ t.date=t.mainMonday; }}
                    else if (t.mainMonday) {{ t.date=t.mainMonday; }} // set to main-draw week for multi-week tournaments
                }});

                // Filter: only include tournaments whose points are still live at windowEnd
                // (dropDate > windowEnd). Mirrors the drop date logic in renderRoadToGS.
                // W15/W35: points go live 1 week after tournament (effective date = monday+7).
                const _cb18ItfCats=['W100','W75','W60','W50','W40','W35','W25','W15','W10','W80'];
                const ts=Array.from(tMap.values()).filter(t => {{
                    if (!t.date) return false;
                    const isW1535=t.category==='W15'||t.category==='W35';
                    const effMon=new Date(t.date+'T00:00:00Z');
                    if (isW1535) effMon.setUTCDate(effMon.getUTCDate()+7);
                    const effStr=effMon.toISOString().slice(0,10);
                    if (effStr>windowEndStr) return false; // points not yet live at cutoff
                    const is2w=t.isGS||(!_cb18ItfCats.includes(t.category)&&_rtgs_twoWeekNames.some(n=>t.tournament.includes(n)));
                    const isCF=!is2w&&_rtgs_twoWeekFreezeMondays.has(effStr);
                    let dr;
                    if (is2w) {{
                        dr=new Date(effMon); dr.setUTCDate(effMon.getUTCDate()+54*7);
                    }} else if (isCF) {{
                        const prev=new Date(effMon); prev.setUTCDate(effMon.getUTCDate()-7);
                        const w1=_rtgs_twoWeekFreezeMondays.has(prev.toISOString().slice(0,10))?prev:effMon;
                        dr=new Date(w1); dr.setUTCDate(w1.getUTCDate()+54*7);
                    }} else {{
                        dr=new Date(effMon); dr.setUTCDate(effMon.getUTCDate()+53*7);
                    }}
                    return dr>windowEnd;
                }});
                if (!ts.length) return 0;

                const itfCats=['W100','W75','W60','W50','W35','W25','W15'];
                const wtaCats=['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                ts.forEach(t => {{
                    if (t.isUnitedCup) {{
                        const uc=_rtgs_pointsLookup['United Cup']; t.points=0;
                        if (uc) {{ const w=t.ucWins,ko=t.ucHasKnockout;
                            if(w>=5)t.points=uc['5W']; else if(w===4)t.points=uc['4W']; else if(w===3)t.points=uc['3W'];
                            else if(w===2&&ko)t.points=uc['2W_KO']; else if(w===2)t.points=uc['2W_RR'];
                            else if(w===1&&ko)t.points=uc['1W_KO']; else if(w===1)t.points=uc['1W_RR'];
                            else t.points=uc['0W']; }}
                    }} else {{
                        const qual=t.bestQualRound&&t.bestQualResult==='W';
                        const ll=t.bestQualRound&&t.bestQualResult==='L'&&!!t.bestMainRound;
                        let desc,drawSize;
                        if (itfCats.includes(t.category)) {{
                            const di=_rtgs_itfDrawLookup[t.tournament+'|'+t.date];
                            if(di){{desc=di.description;drawSize=di.mainDrawSize>32?64:32;}}
                            else{{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}}
                        }} else {{
                            const nid=t.tournamentId?String(parseInt(t.tournamentId)||t.tournamentId):'';
                            const wi=(wtaCats.includes(t.category)&&nid)?_rtgs_wtaDrawLookup[nid]:null;
                            if(wi){{desc=wi.description;drawSize=wi.mainDrawSize>64?128:wi.mainDrawSize>32?64:32;}}
                            else{{desc=_rtgs_categoryToDesc[t.category]||'';drawSize=_rtgs_categoryDrawSize[t.category]||32;}}
                        }}
                        const pt=_rtgs_pointsLookup[desc]; t.points=0;
                        if (pt) {{
                            if (t.bestMainRound) {{
                                const qfl=qual&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                const lfl=ll&&t.bestMainRound==='1st Round'&&t.bestMainResult==='L';
                                if (!qfl&&!lfl) {{ const k=_rtgs_mdKey(t.bestMainRound,t.bestMainResult,drawSize); if(k&&pt[k]!=null)t.points+=pt[k]; }}
                            }}
                            if (t.bestQualRound) {{
                                if (ll) {{ if(pt[t.bestQualRound]!=null)t.points+=pt[t.bestQualRound]; }}
                                else {{ const k=_rtgs_qKey(t.bestQualRound,t.bestQualResult,!!t.bestMainRound); if(k&&pt[k]!=null)t.points+=pt[k]; }}
                            }}
                        }}
                    }}
                }});

                const mGS=[],m1000=[],opt=[],rest=[];
                ts.forEach(t => {{
                    const hasMD=!!t.bestMainRound, up=t.tournament.toUpperCase();
                    if(t.isGS&&hasMD) mGS.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_mandatory1000Names.some(n=>up.includes(n.toUpperCase()))) m1000.push(t);
                    else if(t.category==='WTA 1000'&&hasMD&&_rtgs_optional1000Names.some(n=>up.includes(n.toUpperCase()))) opt.push(t);
                    else rest.push(t);
                }});
                m1000.sort((a,b)=>b.points-a.points); opt.sort((a,b)=>b.points-a.points); rest.sort((a,b)=>b.points-a.points);
                const c1000=m1000.slice(0,6), cOpt=opt.slice(0,1);
                const mandatory=[...mGS,...c1000,...cOpt];
                const fillPool=[...m1000.slice(6),...opt.slice(1),...rest];
                fillPool.sort((a,b)=>b.points-a.points);
                const countable=[...mandatory,...fillPool.slice(0,Math.max(0,18-mandatory.length))];
                return countable.reduce((s,t)=>s+t.points,0);
            }}

            function updateGSCutoffTables(selectedPlayer) {{
                gsCutoffs.forEach(gs => {{
                    ['q','md'].forEach(type => {{
                        const cutoff = type==='q' ? gs.qCutoff : gs.mdCutoff;
                        const accEl = document.getElementById('gs-acc-'+type+'-'+gs.id);
                        const estEl = document.getElementById('gs-est-'+type+'-'+gs.id);
                        if (!accEl||!estEl) return;
                        if (!selectedPlayer||cutoff==='N/A') {{ accEl.textContent='-'; estEl.textContent='-'; estEl.style.color=''; estEl.style.fontWeight=''; return; }}
                        const pts = computeBest18(selectedPlayer, cutoff);
                        accEl.textContent = pts;
                        const est = pts - (type==='q' ? 330 : 780);
                        estEl.textContent = est;
                        estEl.style.fontWeight = 'bold';
                        estEl.style.color = est > 0 ? '#1a7a1a' : est >= -10 ? '#b8860b' : est >= -25 ? '#cc5500' : '#cc0000';
                    }});
                }});
            }}

            // Road to GS
            function abbrevRound(r) {{
                return r
                    .replace('WINNER', 'W')
                    .replace('Final', 'F')
                    .replace('Semi-finals', 'SF')
                    .replace('Quarter-finals', 'QF')
                    .replace('4th Round', '4th')
                    .replace('3rd Round', '3rd')
                    .replace('2nd Round', '2nd')
                    .replace('1st Round', '1st');
            }}

            function initRoadToGS() {{
                const select = document.getElementById('roadtogsPlayerSelect');
                if (!select) return;
                $(select).select2({{ placeholder: 'Select Player...', allowClear: true, width: '100%' }});
                $(select).on('change', renderRoadToGS);
            }}

            function renderRoadToGS() {{
                const selectedPlayer = document.getElementById('roadtogsPlayerSelect').value.toUpperCase();
                const tbody = document.getElementById('roadtogs-body');

                if (!selectedPlayer) {{
                    tbody.innerHTML = '<tr><td colspan="5" style="padding: 20px; color: #64748b;">Select a player to view their results</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    updateGSCutoffTables('');
                    return;
                }}

                // Round ordering for determining the "last" (deepest) round
                const roundOrder = {{'QR1':1,'QR2':2,'QR3':3,'QR4':4,'Round Robin':4.5,'1st Round':5,'2nd Round':6,'3rd Round':7,'4th Round':8,'5th Round':9,'Quarter Finals':10,'Quarter-finals':10,'Semi-finals':11,'Final':12}};

                // Get current date and 52 weeks ago
                const now = new Date();
                const fiftyTwoWeeksAgo = new Date(now);
                fiftyTwoWeeksAgo.setDate(fiftyTwoWeeksAgo.getDate() - 364);

                // Category to points distribution description mapping (use lower M draw size)
                const categoryToDesc = {{
                    'GS': 'Grand Slam',
                    'WTA 1000': 'WTA 1000 (56M, 32Q)',
                    'WTA 500': 'WTA 500 (30/28M, 24/16Q)',
                    'WTA 250': 'WTA 250 (32M, 24/16Q)',
                    'WTA 125': 'WTA 125 (32M, 8Q)',
                    '125K': 'WTA 125 (32M, 8Q)',
                    '125K Series': 'WTA 125 (32M, 8Q)',
                    'W100': 'W100 (32M, 32Q)',
                    'W75': 'W75 (32M, 32Q)',
                    'W50': 'W50 (32M, 32Q)',
                    'W35': 'W35 (32M, 64/48/32/24Q)',
                    'W15': 'W15 (32M, 64/48/32/24Q)'
                }};

                // Build points lookup: description -> {{ W, F, SF, ... }}
                const pointsLookup = {{}};
                pointsDistribution.forEach(p => {{ pointsLookup[p.Description] = p; }});

                // Build ITF draw size lookup: "name|date" -> {{ description, mainDrawSize }}
                const itfDrawLookup = {{}};
                itfDrawSizes.forEach(t => {{
                    const key = (t.tournamentName || '') + '|' + (t.date || '');
                    itfDrawLookup[key] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                    // For multi-week entries with "(Week N)", also store with base name
                    const weekMatch = (t.tournamentName || '').match(/^(.+?)\s*\(Week \d+\)$/);
                    if (weekMatch) {{
                        const baseKey = weekMatch[1].trim() + '|' + (t.date || '');
                        itfDrawLookup[baseKey] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                    }}
                }});

                // Build WTA draw size lookup by tournament ID (strip leading zeros)
                const wtaDrawLookup = {{}};
                wtaDrawSizes.forEach(t => {{
                    if (!t.description || !t.tournamentId) return;
                    const normId = String(parseInt(t.tournamentId) || t.tournamentId);
                    wtaDrawLookup[normId] = {{ description: t.description, mainDrawSize: t.mainDrawSize }};
                }});

                // Draw size per category for mapping round names to point keys
                // GS=128, WTA 1000 (56M)=64, everything else=32
                const categoryDrawSize = {{
                    'GS': 128, 'WTA 1000': 64,
                    'WTA 500': 32, 'WTA 250': 32, 'WTA 125': 32,
                    '125K': 32, '125K Series': 32,
                    'W100': 32, 'W75': 32, 'W50': 32, 'W35': 32, 'W15': 32
                }};

                // Map a main draw round name to a point key based on draw size
                function getMainDrawPointKey(round, result, drawSize) {{
                    if (round === 'Final') return result === 'W' ? 'W' : 'F';
                    if (round === 'Semi-finals') return 'SF';
                    if (round === 'Quarter-finals') return 'QF';
                    // Numbered rounds depend on draw size
                    if (drawSize === 128) {{
                        if (round === '4th Round') return 'R16';
                        if (round === '3rd Round') return 'R32';
                        if (round === '2nd Round') return 'R64';
                        if (round === '1st Round') return 'R128';
                    }} else if (drawSize === 64) {{
                        if (round === '3rd Round') return 'R16';
                        if (round === '2nd Round') return 'R32';
                        if (round === '1st Round') return 'R64';
                    }} else {{
                        if (round === '2nd Round') return 'R16';
                        if (round === '1st Round') return 'R32';
                    }}
                    return null;
                }}

                // Map a qualifying round to a point key
                function getQualPointKey(round, result, hasMainDraw) {{
                    if (hasMainDraw || result === 'W') return 'QLFR';
                    return round; // QR1, QR2, QR3
                }}

                // Filter matches for selected player in last 52 weeks, exclude Fed/BJK Cup
                const playerMatches = historyData.filter(row => {{
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    if (matchType === 'Fed/BJK Cup') return false;

                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const lName = getDisplayName((row['_loserName'] || '').toString().toUpperCase()).toUpperCase();
                    if (wName !== selectedPlayer && lName !== selectedPlayer) return false;

                    const dateStr = row['DATE'] || '';
                    if (!dateStr) return false;
                    const matchDate = new Date(dateStr);
                    return matchDate >= fiftyTwoWeeksAgo && matchDate <= now;
                }});

                // Helper: compute Monday of a date's week
                function getMonday(dateStr) {{
                    const d = new Date(dateStr);
                    const day = d.getUTCDay();
                    const diff = (day === 0) ? -6 : 1 - day;
                    const monday = new Date(d);
                    monday.setUTCDate(d.getUTCDate() + diff);
                    return monday.toISOString().slice(0, 10);
                }}

                // Group by tournament + week, track best round per draw type (M/Q)
                // For Grand Slams and United Cup, group by tournament name only (combine weeks)
                const tournamentMap = new Map();
                playerMatches.forEach(row => {{
                    const tName = row['TOURNAMENT'] || '';
                    const dateStr = row['DATE'] || '';
                    const matchType = (row['MATCH_TYPE'] || '').trim();
                    const category = (row['CATEGORY'] || '').trim();
                    const isGS = matchType === 'GS';
                    const isUnitedCup = tName.toUpperCase().includes('UNITED CUP');
                    const mondayStr = getMonday(dateStr);
                    const draw = (row['DRAW'] || '').toUpperCase();
                    const round = row['ROUND'] || '';
                    const rOrder = roundOrder[round] || 0;

                    // Determine if selected player won or lost this match
                    const wName = getDisplayName((row['_winnerName'] || '').toString().toUpperCase()).toUpperCase();
                    const playerResult = (wName === selectedPlayer) ? 'W' : 'L';

                    const tournamentId = (row['TOURNAMENT_ID'] || '').trim();
                    // Group by tournamentId so a tournament spanning multiple weeks is one entry
                    const key = (isGS || isUnitedCup) ? (matchType + '|' + tName) : (tournamentId ? (tournamentId + '|' + tName) : (mondayStr + '|' + tName));

                    if (!tournamentMap.has(key)) {{
                        tournamentMap.set(key, {{
                            date: mondayStr,
                            tournament: tName,
                            tournamentId: tournamentId,
                            category: category,
                            isGS: isGS,
                            isUnitedCup: isUnitedCup,
                            bestMainRound: '',
                            bestMainOrder: 0,
                            bestMainResult: '',
                            bestQualRound: '',
                            bestQualOrder: 0,
                            bestQualResult: '',
                            qualMonday: '',
                            mainMonday: '',
                            ucWins: 0,
                            ucTotal: 0,
                            ucHasKnockout: false
                        }});
                    }}
                    const entry = tournamentMap.get(key);

                    // United Cup: track win counts and knockout participation
                    if (isUnitedCup) {{
                        entry.ucTotal++;
                        if (playerResult === 'W') entry.ucWins++;
                        if (round !== 'Round Robin' && playerResult === 'W') entry.ucHasKnockout = true;
                    }}

                    if (draw === 'Q') {{
                        if (rOrder > entry.bestQualOrder) {{
                            entry.bestQualRound = round;
                            entry.bestQualOrder = rOrder;
                            entry.bestQualResult = playerResult;
                        }}
                        if (!entry.qualMonday || mondayStr < entry.qualMonday) {{
                            entry.qualMonday = mondayStr;
                        }}
                    }} else {{
                        if (rOrder > entry.bestMainOrder) {{
                            entry.bestMainRound = round;
                            entry.bestMainOrder = rOrder;
                            entry.bestMainResult = playerResult;
                        }}
                        if (!entry.mainMonday || mondayStr < entry.mainMonday) {{
                            entry.mainMonday = mondayStr;
                        }}
                    }}
                }});

                // Compute final date for each tournament
                tournamentMap.forEach(t => {{
                    if (t.isGS) {{
                        if (t.mainMonday) {{
                            t.date = t.mainMonday;
                        }} else if (t.qualMonday) {{
                            const qMon = new Date(t.qualMonday);
                            qMon.setUTCDate(qMon.getUTCDate() + 7);
                            t.date = qMon.toISOString().slice(0, 10);
                        }}
                    }} else if (t.isUnitedCup && t.mainMonday) {{
                        t.date = t.mainMonday;
                    }} else if (t.mainMonday) {{
                        t.date = t.mainMonday; // set to main-draw week for multi-week tournaments
                    }}
                }});

                const tournaments = Array.from(tournamentMap.values());

                if (tournaments.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="5" style="padding: 20px; color: #64748b;">No tournaments found in the last 52 weeks.</td></tr>';
                    document.getElementById('roadtogs-points-total').textContent = 'Points: 0';
                    return;
                }}

                // Mandatory tournament names for WTA 1000
                const mandatory1000Names = ['Indian Wells', 'Miami', 'Madrid', 'Rome', 'Toronto', 'Montreal', 'Cincinnati', 'Beijing'];

                // Calculate points and round display for each tournament
                tournaments.forEach(t => {{
                    // United Cup: special win-count based points
                    if (t.isUnitedCup) {{
                        const ucTable = pointsLookup['United Cup'];
                        t.roundDisplay = t.ucWins + 'W-' + (t.ucTotal - t.ucWins) + 'L';
                        t.points = 0;
                        if (ucTable) {{
                            const w = t.ucWins;
                            const ko = t.ucHasKnockout;
                            if (w >= 5) t.points = ucTable['5W'];
                            else if (w === 4) t.points = ucTable['4W'];
                            else if (w === 3) t.points = ucTable['3W'];
                            else if (w === 2 && ko) t.points = ucTable['2W_KO'];
                            else if (w === 2) t.points = ucTable['2W_RR'];
                            else if (w === 1 && ko) t.points = ucTable['1W_KO'];
                            else if (w === 1) t.points = ucTable['1W_RR'];
                            else t.points = ucTable['0W'];
                        }}
                    }} else {{

                    // Determine qualifier vs lucky loser status
                    const qualified = t.bestQualRound && t.bestQualResult === 'W';
                    const isLuckyLoser = t.bestQualRound && t.bestQualResult === 'L' && !!t.bestMainRound;

                    // Qualifying display
                    const qualDisplay = qualified ? 'QLFR' : t.bestQualRound;

                    // Main draw display: "WINNER" if won the final
                    let mainDisplay = t.bestMainRound;
                    if (t.bestMainRound === 'Final' && t.bestMainResult === 'W') {{
                        mainDisplay = 'WINNER';
                    }}

                    // Build round display
                    if (t.bestMainRound && t.bestQualRound) {{
                        t.roundDisplay = abbrevRound(mainDisplay) + ' + ' + qualDisplay;
                    }} else {{
                        t.roundDisplay = abbrevRound(mainDisplay || qualDisplay || '');
                    }}

                    // Calculate points
                    // For ITF tournaments, look up actual draw size description
                    const itfCategories = ['W100','W75','W60','W50','W35','W25','W15'];
                    let desc, drawSize;
                    if (itfCategories.includes(t.category)) {{
                        const dsInfo = itfDrawLookup[t.tournament + '|' + t.date];
                        if (dsInfo) {{
                            desc = dsInfo.description;
                            drawSize = dsInfo.mainDrawSize > 32 ? 64 : 32;
                        }} else {{
                            console.warn(`[Road to GS] ITF draw size fallback: "${{t.tournament}}" (${{t.date}}) not found in itfDrawSizes, using default`);
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }}
                    }} else {{
                        // For WTA tournaments, look up actual draw size description by tournament ID
                        const wtaCategories = ['WTA 1000','WTA 500','WTA 250','WTA 125','125K','125K Series'];
                        const wtaNormId = t.tournamentId ? String(parseInt(t.tournamentId) || t.tournamentId) : '';
                        const wtaInfo = (wtaCategories.includes(t.category) && wtaNormId) ? wtaDrawLookup[wtaNormId] : null;
                        if (wtaInfo) {{
                            desc = wtaInfo.description;
                            drawSize = wtaInfo.mainDrawSize > 64 ? 128 : (wtaInfo.mainDrawSize > 32 ? 64 : 32);
                        }} else {{
                            if (wtaCategories.includes(t.category)) {{
                                console.warn(`[Road to GS] WTA draw size fallback: "${{t.tournament}}" (${{t.date}}) not found in wtaDrawSizes, using default`);
                            }}
                            desc = categoryToDesc[t.category] || '';
                            drawSize = categoryDrawSize[t.category] || 32;
                        }}
                    }}
                    const pTable = pointsLookup[desc];
                    t.points = 0;
                    if (pTable) {{
                        // Main draw points
                        if (t.bestMainRound) {{
                            // Qualifier who lost 1st round: no MD points
                            const qualFirstRoundLoss = qualified && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            // Lucky loser who lost 1st round: no MD points
                            const llFirstRoundLoss = isLuckyLoser && t.bestMainRound === '1st Round' && t.bestMainResult === 'L';
                            if (!qualFirstRoundLoss && !llFirstRoundLoss) {{
                                const mdKey = getMainDrawPointKey(t.bestMainRound, t.bestMainResult, drawSize);
                                if (mdKey && pTable[mdKey] != null) t.points += pTable[mdKey];
                            }}
                        }}
                        // Qualifying points
                        if (t.bestQualRound) {{
                            if (isLuckyLoser) {{
                                // Lucky loser: points for best qualifying round lost, not QLFR
                                const qKey = t.bestQualRound; // QR1, QR2, QR3
                                if (pTable[qKey] != null) t.points += pTable[qKey];
                            }} else {{
                                const qKey = getQualPointKey(t.bestQualRound, t.bestQualResult, !!t.bestMainRound);
                                if (qKey && pTable[qKey] != null) t.points += pTable[qKey];
                            }}
                        }}
                    }}
                    }} // end else (non-United Cup)

                    // Drop date rules:
                    //   GS / genuine WTA 1000 2-week events: date + 54 weeks
                    //   W15/W35: points go live 1 week after tournament (effective date = monday+7),
                    //            then apply the same 53/54-week rules from the effective date
                    //   Concurrent with a 2-week freeze: share week1Mon + 54 weeks
                    //   All others: date + 53 weeks
                    const _itfCats = ['W100','W75','W60','W50','W40','W35','W25','W15','W10','W80'];
                    const monday = new Date(t.date);
                    const isW15W35 = t.category === 'W15' || t.category === 'W35';
                    const effectiveMonday = new Date(monday);
                    if (isW15W35) effectiveMonday.setUTCDate(monday.getUTCDate() + 7);
                    const effectiveDateStr = effectiveMonday.toISOString().slice(0, 10);
                    const dropDate = new Date(effectiveMonday);
                    const is2WeekEvent = t.isGS || (!_itfCats.includes(t.category) && _rtgs_twoWeekNames.some(n => t.tournament.includes(n)));
                    const isConcurrentFreeze = !is2WeekEvent && _rtgs_twoWeekFreezeMondays.has(effectiveDateStr);
                    if (is2WeekEvent) {{
                        dropDate.setUTCDate(effectiveMonday.getUTCDate() + 54 * 7);
                    }} else if (isConcurrentFreeze) {{
                        // Use week1Mon of the concurrent 2-week event so all concurrent
                        // tournaments share the same drop date as that event.
                        const prevMon = new Date(effectiveMonday);
                        prevMon.setUTCDate(effectiveMonday.getUTCDate() - 7);
                        const week1Mon = _rtgs_twoWeekFreezeMondays.has(prevMon.toISOString().slice(0, 10)) ? prevMon : effectiveMonday;
                        dropDate.setTime(week1Mon.getTime());
                        dropDate.setUTCDate(dropDate.getUTCDate() + 54 * 7);
                    }} else {{
                        dropDate.setUTCDate(effectiveMonday.getUTCDate() + 53 * 7);
                    }}
                    t.dropDate = dropDate.toISOString().slice(0, 10);
                }});

                const optional1000Names = ['Doha', 'Dubai', 'Wuhan'];

                // Classify tournaments
                const mandatoryGS = [];
                const mandatory1000 = [];
                const optional1000 = [];
                const rest = [];

                tournaments.forEach(t => {{
                    const hasMD = !!t.bestMainRound;
                    const tUpper = t.tournament.toUpperCase();

                    if (t.isGS && hasMD) {{
                        t.mandatory = true;
                        mandatoryGS.push(t);
                    }} else if (t.category === 'WTA 1000' && hasMD && mandatory1000Names.some(n => tUpper.includes(n.toUpperCase()))) {{
                        mandatory1000.push(t);
                    }} else if (t.category === 'WTA 1000' && hasMD && optional1000Names.some(n => tUpper.includes(n.toUpperCase()))) {{
                        optional1000.push(t);
                    }} else {{
                        rest.push(t);
                    }}
                }});

                // Sort each group by points descending
                mandatory1000.sort((a, b) => b.points - a.points);
                optional1000.sort((a, b) => b.points - a.points);
                rest.sort((a, b) => b.points - a.points);

                // Best 6 mandatory WTA 1000
                const counted1000 = mandatory1000.slice(0, 6);
                counted1000.forEach(t => {{ t.mandatory = true; }});
                const uncounted1000 = mandatory1000.slice(6);

                // Best 1 optional WTA 1000
                const countedOpt = optional1000.slice(0, 1);
                countedOpt.forEach(t => {{ t.mandatory = true; }});
                const uncountedOpt = optional1000.slice(1);

                // Combine mandatory countable tournaments
                const mandatoryAll = [...mandatoryGS, ...counted1000, ...countedOpt];
                const mandatoryCount = mandatoryAll.length;

                // Fill remaining spots up to 18 from rest + uncounted WTA 1000s
                const fillPool = [...uncounted1000, ...uncountedOpt, ...rest];
                fillPool.sort((a, b) => b.points - a.points);
                const fillSlots = Math.max(0, 18 - mandatoryCount);
                const filledCountable = fillPool.slice(0, fillSlots);
                const nonCountable = fillPool.slice(fillSlots);

                // Build final ordered list grouped by tier, each sorted by points desc
                mandatoryGS.sort((a, b) => b.points - a.points);
                const allMandatory1000 = [...counted1000, ...countedOpt];
                allMandatory1000.sort((a, b) => b.points - a.points);
                filledCountable.sort((a, b) => b.points - a.points);
                const countable = [...mandatoryGS, ...allMandatory1000, ...filledCountable];
                nonCountable.sort((a, b) => b.points - a.points);

                const totalPoints = countable.reduce((sum, t) => sum + t.points, 0);
                document.getElementById('roadtogs-points-total').textContent = 'Points: ' + totalPoints;
                updateGSCutoffTables(selectedPlayer);

                // Render table
                const _today = new Date(); _today.setUTCHours(0,0,0,0);
                const _in14 = new Date(_today); _in14.setUTCDate(_today.getUTCDate() + 14);
                const _in28 = new Date(_today); _in28.setUTCDate(_today.getUTCDate() + 28);
                function _dropStyle(dropDateStr) {{
                    if (!dropDateStr) return '';
                    const d = new Date(dropDateStr);
                    if (d <= _in14) return ' style="color:#cc0000;font-weight:bold;"';
                    if (d <= _in28) return ' style="color:#cc5500;font-weight:bold;"';
                    return '';
                }}
                const parts = [];
                countable.forEach(t => {{
                    parts.push(`<tr><td>${{t.date}}</td><td>${{t.tournament}}</td><td>${{t.roundDisplay}}</td><td>${{t.points}}</td><td${{_dropStyle(t.dropDate)}}>${{t.dropDate}}</td></tr>`);
                }});
                if (nonCountable.length > 0) {{
                    parts.push('<tr class="roadtogs-separator"><td colspan="5">NON-COUNTABLE TOURNAMENTS</td></tr>');
                    nonCountable.forEach(t => {{
                        parts.push(`<tr><td>${{t.date}}</td><td>${{t.tournament}}</td><td>${{t.roundDisplay}}</td><td>${{t.points}}</td><td${{_dropStyle(t.dropDate)}}>${{t.dropDate}}</td></tr>`);
                    }});
                }}

                tbody.innerHTML = parts.join('');
            }}

            document.addEventListener('DOMContentLoaded', initRoadToGS);
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)
