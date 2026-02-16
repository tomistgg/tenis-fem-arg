import json
from html import escape

from config import PLAYER_MAPPING, CONTINENT_KEYS, CONTINENT_LABELS, NAME_LOOKUP
from utils import format_player_name, get_tournament_sort_order, get_surface_class


def generate_html(tournament_groups, tournament_store, players_data, schedule_map,
                  cleaned_history, calendar_data, match_history_data, wta_rankings=None,
                  national_team_data=None):
    """Generate the complete HTML page and write it to index.html."""

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
        row += f'<td class="sticky-col col-name">{player_display}</td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "\u2014")
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

    # Build rankings table rows
    rankings_rows = ""
    for p in (wta_rankings or []):
        dob = p.get("DOB", "")
        if dob and "T" in dob:
            dob = dob.split("T")[0]
        name = format_player_name(p.get("Player", ""))
        row_class = "arg-player-row" if (p.get("Country") or "").upper() == "ARG" else ""
        rankings_rows += f'<tr class="{row_class}"><td>{p.get("Rank", "")}</td><td style="text-align:left;font-weight:bold;">{name}</td><td>{p.get("Country", "")}</td><td>{p.get("Points", "")}</td><td>{p.get("Played", "")}</td><td>{dob}</td></tr>'

    default_national_columns = ["N", "Player", "Date", "Event", "Round", "Tie", "Partner", "Opponent", "Result", "Score"]
    national_columns = list(national_team_data[0].keys()) if national_team_data else default_national_columns

    header_label_map = {"N": "#"}
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

            national_rows += f'<td{cell_style}>{escape(value)}</td>'
        national_rows += '</tr>'

    # Generate the full HTML template
    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>WT Argentina</title>
        <link href="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/css/select2.min.css" rel="stylesheet" />
        <script src="https://code.jquery.com/jquery-3.6.0.min.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/select2@4.1.0-rc.0/dist/js/select2.min.js"></script>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; min-height: 100vh; overflow-y: auto; overflow-x: auto; }}
            .app-container {{ display: flex; width: 100%; min-height: 100vh; }}
            .sidebar {{ width: 180px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; min-height: 100vh; }}
            .sidebar-header {{ padding: 25px 15px; font-size: 15px; font-weight: 800; color: #75AADB; border-bottom: 1px solid #475569; }}
            .menu-item {{ padding: 15px 20px; cursor: pointer; color: #cbd5e1; font-size: 14px; border-bottom: 1px solid #334155; transition: 0.2s; }}
            .menu-item:hover {{ background: #334155; color: white; }}
            .menu-item.active {{ background: #75AADB; color: white; font-weight: bold; }}
            .main-content {{ flex: 1; overflow-y: visible; background: #f8fafc; padding: 20px; display: flex; flex-direction: column; }}
            .single-layout {{ width: 100%; min-width: 0; display: flex; flex-direction: column; }}
            #view-upcoming {{ max-width: 1200px; margin: 0 auto; }}
            #view-entrylists {{ width: 100%; max-width: 1100px; margin: 0 auto; }}
            #view-rankings {{ max-width: 700px; margin: 0 auto; }}
            #view-national {{ max-width: 1400px; margin: 0 auto; }}
            .header-row {{ width: 100%; margin-bottom: 20px; display: flex; flex-direction: column; align-items: center; position: relative; gap: 10px; }}
            h1 {{ margin: 0; font-size: 22px; color: #1e293b; }}
            .search-container {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-filter-container {{ position: absolute; right: 0; top: 50%; transform: translateY(-50%); }}
            .rankings-toggle-btn {{ padding: 8px 12px; border-radius: 8px; border: 2px solid #94a3b8; background: white; font-family: inherit; font-size: 12px; font-weight: bold; color: #1e293b; cursor: pointer; }}
            .rankings-toggle-btn:hover {{ background: #f1f5f9; }}
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
            #view-entrylists .entry-content {{ align-items: center; }}
            #view-entrylists .content-card {{ width: 100%; max-width: 760px; margin: 0 auto; }}

            /* Entry Lists layout */
            .entry-layout {{ display: flex; gap: 25px; width: 100%; }}
            .entry-menu {{ width: 220px; flex-shrink: 0; background: white; border: 1px solid black; align-self: flex-start; }}
            .entry-menu-header {{ background: #75AADB; color: white; font-size: 14px; font-weight: bold; text-align: center; padding: 12px; }}
            .entry-menu-week {{ background: #e2e8f0; font-size: 11px; font-weight: bold; text-align: center; padding: 8px; color: #475569; border-bottom: 1px solid #cbd5e1; }}
            .entry-menu-item {{ padding: 10px 15px; font-size: 12px; cursor: pointer; border-bottom: 1px solid #e2e8f0; color: #334155; transition: background 0.15s; }}
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
            .col-name {{ left: 45px; width: 160px; min-width: 160px; max-width: 160px; text-align: left; font-weight: bold; }}
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
            #history-table td:nth-child(8) {{ white-space: normal; overflow: visible; text-overflow: clip; }} /* Allow OPPONENT to wrap */

            /* Filter Panel Styles */
            .history-layout {{ display: flex; gap: 20px; width: 100%; }}
            .filter-panel {{ width: 250px; padding: 15px; flex-shrink: 0; border: 2px solid black; background: white; }}
            .filter-panel h3 {{ margin: -15px -15px 15px -15px; font-size: 16px; color: white; text-align: center; font-weight: bold; background: #75AADB; border: none; padding: 12px; border-radius: 0; }}
            .filter-group {{ margin-bottom: 20px; text-align: left; }}
            .filter-group-title {{ font-size: 13px; font-weight: bold; color: #475569; margin-bottom: 8px; cursor: pointer; user-select: none; display: flex; justify-content: center; align-items: center; text-align: center; position: relative; }}
            .filter-group-title:hover {{ color: #75AADB; }}
            .filter-options {{ border: 1px solid #e2e8f0; border-radius: 4px; padding: 8px; background: #f8fafc; text-align: left; }}
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

            /* Calendar Styles */
            #view-calendar {{ width: 100%; min-height: 0; }}
            .calendar-container {{ width: max-content; min-width: 100%; min-height: 0; margin-bottom: 0; display: block; }}
            .calendar-container .table-wrapper {{ display: block; overflow: visible; -webkit-overflow-scrolling: touch; width: max-content; max-width: none; }}
            .calendar-table {{ border-collapse: separate; border-spacing: 0; width: max-content; min-width: max-content; table-layout: auto; border: 1px solid black; }}
            .calendar-table th {{ padding: 4px 4px; vertical-align: top; border-bottom: 2px solid #1e293b; border-right: 1px solid #1e293b; }}
            .calendar-table td {{ padding: 4px 4px; vertical-align: top; border-bottom: 1px solid #94a3b8; border-right: 1px solid #94a3b8; }}
            .cal-week-header {{ background: #75AADB; color: white; font-size: 10px; font-weight: bold; text-align: center; white-space: nowrap; padding: 6px 6px; position: sticky; top: 0; z-index: 10; min-width: 90px; }}
            .cal-cat-header {{ background: #75AADB; color: white; position: sticky; top: 0; z-index: 11; width: 28px; min-width: 28px; }}
            .cal-cont-header {{ background: #75AADB; color: white; position: sticky; top: 0; z-index: 11; min-width: 65px; }}
            .cal-cat-label {{ background: #1e293b; color: white; font-size: 11px; font-weight: bold; text-align: center; vertical-align: middle !important; text-transform: uppercase; writing-mode: vertical-lr; text-orientation: mixed; transform: rotate(180deg); padding: 0; width: 28px; min-width: 28px; max-width: 28px; position: sticky; left: 0; z-index: 2; border-color: #1e293b !important; box-shadow: inset 0 0 0 50px #1e293b; }}
            .cal-cont-label {{ background: #f1f5f9; font-size: 11px; font-weight: 600; color: #475569; text-align: center; vertical-align: middle !important; white-space: nowrap; position: sticky; left: 28px; z-index: 2; }}
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
                body {{ overflow-x: auto; }}
                .mobile-menu-toggle {{ display: block; }}

                .app-container {{ flex-direction: column; }}

                .sidebar {{
                    position: fixed;
                    left: 0;
                    top: 0;
                    height: 100vh;
                    z-index: 999;
                    transition: transform 0.3s ease;
                    box-shadow: 2px 0 10px rgba(0,0,0,0.3);
                }}

                .main-content {{
                    padding: 60px 10px 10px 10px;
                    width: 100%;
                }}

                #view-upcoming, #view-rankings, #view-national {{ max-width: 100%; }}

                .entry-layout {{ flex-direction: column; gap: 15px; }}
                .entry-menu {{ width: 100%; }}

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
                    font-size: 11px;
                    min-width: 600px;
                }}

                th, td {{
                    padding: 6px 8px;
                    font-size: 10px;
                }}

                .col-name {{
                    min-width: 140px;
                    max-width: 140px;
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
                    width: 100%;
                    padding: 10px;
                    margin-bottom: 0;
                    border: 2px solid black;
                    display: flex;
                    flex-wrap: wrap;
                    gap: 8px;
                    align-items: flex-start;
                }}

                .filter-panel h3 {{
                    font-size: 14px;
                    padding: 10px;
                    width: 100%;
                    margin: -10px -10px 8px -10px;
                }}

                .filter-group {{
                    margin-bottom: 0;
                    flex: 1 1 150px;
                    min-width: 140px;
                    border: 1px solid #d1d5db;
                    border-radius: 4px;
                    padding: 6px;
                    background: #f8fafc;
                }}

                .table-header-section {{
                    flex-direction: column;
                    gap: 10px;
                    margin-bottom: 0;
                    align-items: stretch;
                }}

                .player-select-container {{
                    width: 100%;
                }}

                .table-title {{
                    font-size: 18px;
                    text-align: center;
                }}

                .table-header-section > div[style*="width: 250px"] {{
                    display: none;
                }}

                .filter-actions {{
                    width: 100%;
                    margin-top: 4px;
                    justify-content: space-between;
                    order: 99;
                }}

                .filter-instructions {{
                    padding-left: 0;
                    font-size: 9px;
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
                    width: max-content;
                    min-width: 100%;
                    table-layout: auto;
                }}
                #history-table th,
                #history-table td {{
                    font-size: 8px;
                    padding: 4px 6px;
                    white-space: nowrap;
                    line-height: 1.15;
                }}

                #history-table th:nth-child(1), #history-table td:nth-child(1) {{ min-width: 90px; }}
                #history-table th:nth-child(2), #history-table td:nth-child(2) {{ min-width: 220px; }}
                #history-table th:nth-child(3), #history-table td:nth-child(3) {{ min-width: 90px; }}
                #history-table th:nth-child(4), #history-table td:nth-child(4) {{ min-width: 100px; }}
                #history-table th:nth-child(5), #history-table td:nth-child(5) {{ min-width: 210px; }}
                #history-table th:nth-child(6), #history-table td:nth-child(6) {{ min-width: 70px; }}
                #history-table th:nth-child(7), #history-table td:nth-child(7) {{ min-width: 130px; }}
                #history-table th:nth-child(8), #history-table td:nth-child(8) {{ min-width: 260px; white-space: normal; }}

                /* National Team table */
                #national-table {{
                    width: max-content;
                    min-width: 100%;
                    table-layout: auto;
                }}
                #national-table th,
                #national-table td {{
                    font-size: 8px;
                    padding: 3px 3px;
                    white-space: nowrap;
                    overflow-wrap: anywhere;
                    line-height: 1.15;
                }}
                #national-table th:nth-child(1), #national-table td:nth-child(1) {{ width: 4%; }}
                #national-table th:nth-child(2), #national-table td:nth-child(2) {{ width: 13%; min-width: 0; }}
                #national-table th:nth-child(3), #national-table td:nth-child(3) {{ width: 9%; }}
                #national-table th:nth-child(4), #national-table td:nth-child(4) {{ width: 11%; }}
                #national-table th:nth-child(5), #national-table td:nth-child(5) {{ width: 10%; }}
                #national-table th:nth-child(6), #national-table td:nth-child(6) {{ width: 11%; }}
                #national-table th:nth-child(7), #national-table td:nth-child(7) {{ width: 15%; min-width: 0; white-space: normal; }}
                #national-table th:nth-child(8), #national-table td:nth-child(8) {{ width: 17%; min-width: 0; white-space: normal; }}
                #national-table th:nth-child(9), #national-table td:nth-child(9) {{ width: 5%; }}
                #national-table th:nth-child(10), #national-table td:nth-child(10) {{ width: 10%; }}

                /* Calendar mobile */
                .calendar-container .table-wrapper {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}
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
                    font-size: 13px;
                    padding: 12px 15px;
                }}

                th, td {{
                    padding: 4px 6px;
                    font-size: 9px;
                }}

                .col-name {{
                    min-width: 120px;
                    max-width: 120px;
                }}

                .filter-panel h3 {{
                    font-size: 13px;
                }}

                .filter-group-title {{
                    font-size: 12px;
                }}

                .filter-option {{
                    font-size: 11px;
                }}

                #history-table th, #history-table td,
                #national-table th, #national-table td {{
                    font-size: 7px;
                    padding: 2px 2px;
                }}

                .calendar-tournament {{ font-size: 8px; padding: 2px 4px; }}
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
                <div class="menu-item" id="btn-rankings" onclick="switchTab('rankings')">WTA Rankings</div>
                <div class="menu-item" id="btn-history" onclick="switchTab('history')">Match History</div>
                <div class="menu-item" id="btn-national" onclick="switchTab('national')">National Team Order</div>
                <div class="menu-item" id="btn-calendar" onclick="switchTab('calendar')">Calendar</div>
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
                            </div>
                            <div class="content-card">
                                <table>
                                    <thead>
                                        <tr>
                                            <th style="width:15px">#</th>
                                            <th>PLAYER</th>
                                            <th style="width:35px">NAT</th>
                                            <th style="width:70px">E-Rank</th>
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
                        <div class="search-container">
                            <input type="text" id="rankings-search" placeholder="Search player..." oninput="filterRankings()">
                        </div>
                        <div class="rankings-filter-container">
                            <button id="rankings-toggle-btn" class="rankings-toggle-btn" onclick="toggleRankingsScope()">Show ARG</button>
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
                                        <th style="width:65px">PLAYED</th>
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
                                    Opponent Country <span class="collapse-icon"></span>
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
                                <div style="width: 250px;"></div>
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

                <div id="view-calendar" class="single-layout" style="display: none;">
                    <div class="content-card calendar-container">
                        <div class="table-wrapper">
                            {calendar_html}
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            const tournamentData = {json.dumps(tournament_store)};
            const historyData = {json.dumps(cleaned_history)};
            const playerMapping = {json.dumps(PLAYER_MAPPING)};

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
                document.getElementById('view-calendar').style.display = (tabName === 'calendar') ? 'flex' : 'none';

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

            function buildPrefix(seed, entry) {{
                const parts = [];
                if (seed) parts.push(seed);
                if (entry) parts.push(entry);
                if (parts.length === 0) return '';
                return '(' + parts.join('/') + ') ';
            }}

            // Find the last qualifying round per tournament and rename it to QRF
            function getQRFinalMap() {{
                const maxQR = {{}};
                historyData.forEach(row => {{
                    const t = row['TOURNAMENT'] || '';
                    const r = row['ROUND'] || '';
                    const m = r.match(/^QR(\\d+)$/);
                    if (m) {{
                        const num = parseInt(m[1]);
                        if (!maxQR[t] || num > maxQR[t]) maxQR[t] = num;
                    }}
                }});
                return maxQR;
            }}
            const qrFinalMap = getQRFinalMap();

            function displayRound(round, tournament) {{
                const m = (round || '').match(/^QR(\\d+)$/);
                if (m && qrFinalMap[tournament] === parseInt(m[1])) return 'QRF';
                return round;
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
            function getDisplayName(upperCaseName) {{
                // Try to find the display name in playerMapping
                for (const [displayName, aliases] of Object.entries(playerMapping)) {{
                    for (const alias of aliases) {{
                        if (alias.toUpperCase() === upperCaseName) {{
                            return displayName; // Return proper capitalization from mapping
                        }}
                    }}
                }}
                // If not found, convert to title case (handling hyphens)
                return upperCaseName.split(' ').map(word => {{
                    // Handle hyphenated names (e.g., Villagran-Reami)
                    if (word.includes('-')) {{
                        return word.split('-').map(part =>
                            part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
                        ).join('-');
                    }}
                    return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
                }}).join(' ');
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
                let html = '';
                const main = players.filter(p => p.type === 'MAIN');
                const qual = players.filter(p => p.type === 'QUAL');

                main.forEach(p => {{
                    html += `<tr class="${{p.country==='ARG'?'row-arg':''}}"><td>${{p.pos}}</td><td style="text-align:left;font-weight:bold;">${{p.name}}</td><td>${{p.country}}</td><td>${{p.rank}}</td></tr>`;
                }});
                if (qual.length > 0) {{
                    html += `<tr class="divider-row"><td colspan="4">QUALIFYING</td></tr>`;
                    qual.forEach(p => {{
                        html += `<tr class="${{p.country==='ARG'?'row-arg':''}}"><td>${{p.pos}}</td><td style="text-align:left;">${{p.name}}</td><td>${{p.country}}</td><td>${{p.rank}}</td></tr>`;
                    }});
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

            function populateFilters(playerMatches) {{
                // Extract unique values for each filter
                const surfaces = new Set();
                const rounds = new Set();
                const results = new Set(['W', 'L']);
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

                    // Surface
                    if (row['SURFACE']) surfaces.add(row['SURFACE']);

                    // Round
                    if (row['ROUND']) rounds.add(row['ROUND']);

                    // Opponent
                    const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    if (opponentName) opponents.add(getDisplayName(opponentName.toUpperCase()));

                    // Opponent Country
                    const opponentCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    if (opponentCountry) opponentCountries.add(opponentCountry);

                    // Player Entry
                    const playerEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    if (playerEntry) playerEntries.add(playerEntry);

                    // Match Type (determine from tournament)
                    const tournament = row['TOURNAMENT'] || '';
                    if (tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                        tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                        tournament.includes('W75') || tournament.includes('W100')) {{
                        matchTypes.add('ITF');
                    }} else {{
                        matchTypes.add('WTA');
                    }}
                }});

                // Populate filter options
                populateFilterOptions('filter-surface', Array.from(surfaces).sort());
                populateFilterOptions('filter-round', Array.from(rounds).sort());
                populateFilterOptions('filter-result', Array.from(results));
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

            function applyHistoryFilters() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                if (!selectedPlayer) return;

                // Get selected filter values
                const selectedSurfaces = getSelectedFilterValues('filter-surface');
                const selectedRounds = getSelectedFilterValues('filter-round');
                const selectedResults = getSelectedFilterValues('filter-result');
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
                    if (selectedRounds.length > 0 && !selectedRounds.includes(row['ROUND'] || '')) return false;

                    // Result filter
                    const result = isWinner ? 'W' : 'L';
                    if (selectedResults.length > 0 && !selectedResults.includes(result)) return false;

                    // Opponent filter (single select from dropdown)
                    if (selectedOpponent) {{
                        const opponentName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                        const opponentDisplay = opponentName ? getDisplayName(opponentName.toUpperCase()) : '';
                        if (opponentDisplay !== selectedOpponent) return false;
                    }}

                    // Opponent Country filter
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
                    const tournament = row['TOURNAMENT'] || '';
                    const isITF = tournament.includes('ITF') || tournament.includes('W15') || tournament.includes('W25') ||
                                  tournament.includes('W35') || tournament.includes('W50') || tournament.includes('W60') ||
                                  tournament.includes('W75') || tournament.includes('W100');
                    const matchType = isITF ? 'ITF' : 'WTA';
                    if (selectedMatchTypes.length > 0 && !selectedMatchTypes.includes(matchType)) return false;

                    return true;
                }});

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

                if (matches.length === 0) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">No matches found with the selected filters.</td></tr>`;
                    return;
                }}

                // Round priority (lower = higher in table)
                const roundOrder = {{
                    'Final': 1, 'Semi-finals': 2, 'Quarter-finals': 3,
                    '4th Round': 4, '3rd Round': 5, '2nd Round': 6, '1st Round': 7,
                    'QRF': 8, 'QR4': 9, 'QR3': 10, 'QR2': 11, 'QR1': 12,
                    'Semi Finals': 13, 'Quarter Finals': 14,
                    'Last 16': 15, 'Last 32': 16, 'Round Robin': 17
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

                let bodyHtml = '';
                matches.forEach(row => {{
                    const wName = (row['_winnerName'] || "").toString().toUpperCase();
                    const wNameNormalized = getDisplayName(wName).toUpperCase();
                    const isWinner = wNameNormalized === selectedPlayer;

                    const playerDisplayName = getDisplayName(selectedPlayer);
                    const rivalName = isWinner ? (row['_loserName'] || '') : (row['_winnerName'] || '');
                    const rivalDisplayName = rivalName ? getDisplayName(rivalName.toUpperCase()) : '';

                    // Fill in the dynamic columns
                    const pSeed = isWinner ? (row['_winnerSeed'] || '') : (row['_loserSeed'] || '');
                    const pEntry = isWinner ? (row['_winnerEntry'] || '') : (row['_loserEntry'] || '');
                    const rSeed = isWinner ? (row['_loserSeed'] || '') : (row['_winnerSeed'] || '');
                    const rEntry = isWinner ? (row['_loserEntry'] || '') : (row['_winnerEntry'] || '');

                    const rivalCountry = isWinner ? (row['_loserCountry'] || '') : (row['_winnerCountry'] || '');
                    const opponentName = rivalDisplayName + (rivalCountry ? ` [${{rivalCountry}}]` : '');

                    const rowData = {{
                        'DATE': formatDate(row['DATE'] || ''),
                        'TOURNAMENT': row['TOURNAMENT'] || '',
                        'SURFACE': row['SURFACE'] || '',
                        'ROUND': displayRound(row['ROUND'] || '', row['TOURNAMENT'] || ''),
                        'PLAYER': buildPrefix(pSeed, pEntry) + playerDisplayName,
                        'RESULT': isWinner ? 'W' : 'L',
                        'SCORE': isWinner ? (row['SCORE'] || '') : reverseScore(row['SCORE'] || ''),
                        'OPPONENT': buildPrefix(rSeed, rEntry) + opponentName
                    }};

                    bodyHtml += '<tr>';
                    displayColumns.forEach(col => {{
                        bodyHtml += `<td>${{rowData[col] ?? ''}}</td>`;
                    }});
                    bodyHtml += '</tr>';
                }});
                tbody.innerHTML = bodyHtml;
            }}

            function filterHistoryByPlayer() {{
                const selectedPlayer = document.getElementById('playerHistorySelect').value.toUpperCase();
                const tbody = document.getElementById('history-body');
                const displayColumns = ['DATE', 'TOURNAMENT', 'SURFACE', 'ROUND', 'PLAYER', 'RESULT', 'SCORE', 'OPPONENT'];

                if (!selectedPlayer) {{
                    tbody.innerHTML = `<tr><td colspan="${{displayColumns.length}}" style="padding: 20px;">Select a player...</td></tr>`;
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
                    return;
                }}

                // Store current player data for filtering
                currentPlayerData = filtered;

                // Populate filters with this player's data
                populateFilters(filtered);

                // Render all matches (filters start with all checked)
                renderFilteredMatches(filtered, selectedPlayer);
            }}
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(html_template)
