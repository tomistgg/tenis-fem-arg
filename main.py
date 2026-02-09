import requests
import time
import json
import random
import pandas as pd
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager
import os

def format_player_name(text):
    if not text: return ""
    return " ".join([word.capitalize() for word in text.split()])

def load_player_mapping(filename="player_aliases.json"):
    if not os.path.exists(filename):
        print(f"Alerta: No se encontró {filename}.")
        return {}
    with open(filename, "r", encoding="utf-8") as f:
        return json.load(f)

PLAYER_MAPPING = load_player_mapping()

NAME_LOOKUP = {}
for display_name, aliases in PLAYER_MAPPING.items():
    for alias in aliases:
        NAME_LOOKUP[alias.strip().upper()] = display_name.upper()

TOURNAMENT_GROUPS = {
    "Semana 16 Febrero": {
        "https://www.wtatennis.com/tournaments/dubai/player-list": "WTA 1000 Dubai",
        "https://www.wtatennis.com/tournaments/2051/midland-125/2026/player-list": "WTA 125 Midland",
        "https://www.wtatennis.com/tournaments/1156/oeiras-125-indoor-2/2026/player-list": "WTA 125 Oeiras 2",
        "https://www.wtatennis.com/tournaments/1157/les-sables-d-olonne-125/2026/player-list": "WTA 125 Les Sables",
    },
    "Semana 23 Febrero": {
        "https://www.wtatennis.com/tournaments/2085/m-rida/2026/player-list": "WTA 500 Merida",
        "https://www.wtatennis.com/tournaments/2082/austin/2026/player-list": "WTA 250 Austin",
        "https://www.wtatennis.com/tournaments/1124/antalya-125-1/2026/player-list": "WTA 125 Antalya 1",
    },
    "Semana 2 Marzo": {
        "https://www.wtatennis.com/tournaments/609/indian-wells/2026/player-list": "WTA 1000 Indian Wells",
        "https://www.wtatennis.com/tournaments/1107/antalya-125-2/2026/player-list": "WTA 125 Antalya 2",
    },
    "Semana 9 Marzo": {
        "https://www.wtatennis.com/tournaments/1161/austin-125-1/2026/player-list": "WTA 125 Austin",
        "https://www.wtatennis.com/tournaments/1125/antalya-125-3/2026/player-list": "WTA 125 Antalya 3",
    }
}

API_URL = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"}

def get_monday_offset(date_str, weeks_back):
    dt = pd.to_datetime(date_str)
    monday = dt - timedelta(days=dt.weekday())
    return (monday - timedelta(weeks=weeks_back)).strftime('%Y-%m-%d')

def get_itf_players(tournament_key, driver):
    url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetAcceptanceList?tournamentKey={tournament_key}&circuitCode=WT"
    try:
        driver.get(url)
        time.sleep(random.uniform(4, 6))
        raw_content = driver.find_element("tag name", "body").text
        start = raw_content.find('[')
        end = raw_content.rfind(']') + 1
        if start == -1: return [], {}
        
        data = json.loads(raw_content[start:end])
        
        root_data = data[0].get("entryClassifications", []) if data else []
        
        name_map = {}
        for classification in root_data:
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: continue 
            
            for entry in classification.get("entries") or []:
                pos = entry.get("positionDisplay", "")
                suffix = "" if code == "MDA" else (f" (ALT {pos})" if code == "ALT" or "ALTERNATE" in desc else " (Q)")
                players = entry.get("players") or []
                for p in players:
                    full_name = f"{p.get('givenName', '')} {p.get('familyName', '')}".strip().upper()
                    matched_name = NAME_LOOKUP.get(full_name, full_name)
                    name_map[matched_name] = suffix
                        
        return root_data, name_map 
    except Exception as e:
        print(f"Error en {tournament_key}: {e}")
        return [], {}

    final_list = {}
    if not isinstance(data, list): return {}
    for item in data:
        if not isinstance(item, dict): continue
        for classification in item.get("entryClassifications") or []:
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: break 
            for entry in classification.get("entries") or []:
                pos = entry.get("positionDisplay", "")
                suffix = "" if code == "MDA" else (f" (ALT {pos})" if code == "ALT" or "ALTERNATE" in desc else " (Q)")
                players = entry.get("players")
                if not players or not isinstance(players, list): continue
                for player in players:
                    full_name = f"{player.get('givenName', '')} {player.get('familyName', '')}".strip().upper()
                    if full_name:
                        matched_name = NAME_LOOKUP.get(full_name, full_name)
                        final_list[matched_name] = suffix
    return final_list

def get_dynamic_itf_calendar(driver):
    try:
        url = "https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?circuitCode=WT&dateFrom=2026-02-16&dateTo=2026-03-31&skip=0&take=500"
        driver.get(url)
        time.sleep(5)
        raw_content = driver.find_element("tag name", "body").text
        data = json.loads(raw_content)
        return data.get('items', [])
    except Exception as e:
        print(f"Error calendario: {e}")
        return []

def get_all_rankings(date_str):
    all_players, page = [], 0
    while True:
        params = {"metric": "SINGLES", "type": "rankSingles", "sort": "asc", "at": date_str, "pageSize": 100, "page": page}
        try:
            r = requests.get(API_URL, params=params, headers=HEADERS, timeout=10)
            data = r.json()
            items = data.get('content', []) if isinstance(data, dict) else data
            if not items: break
            all_players.extend(items)
            page += 1
            time.sleep(0.1)
        except: break
    
    ranking_results = []
    for p in all_players:
        if not p.get('player'): continue
        wta_name = p.get('player', {}).get('fullName').strip().upper()
        display_name = NAME_LOOKUP.get(wta_name, wta_name)
        ranking_results.append({
            "Player": display_name, 
            "Rank": p.get('ranking'), 
            "Country": p.get('player', {}).get('countryCode', ''), 
            "Key": display_name 
        })
    return ranking_results

def scrape_tournament_players(url, md_rankings, qual_rankings):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, 'html.parser')
    except Exception as e:
        print(f"Error scraping {url}: {e}")
        return [], {}
    
    main_draw_names = set()
    qualifying_names = set()
    
    current_state = "MAIN" 
    
    all_elements = soup.find_all(['div', 'span', 'button', 'a'], recursive=True)
    
    for tag in all_elements:
        text = tag.get_text().strip()
        ui_tab = tag.get('data-ui-tab', '').lower()
        
        if "qualifying" in text.lower() or "qualifying" in ui_tab:
            current_state = "QUAL"
        elif "main draw" in text.lower() or "main draw" in ui_tab:
            current_state = "MAIN"
        elif "doubles" in text.lower() or "doubles" in ui_tab:
            current_state = "IGNORE"
            
        p_name = tag.get('data-tracking-player-name')
        if p_name:
            name_key = p_name.strip().upper()
            matched_name = NAME_LOOKUP.get(name_key, name_key)
            
            if current_state == "MAIN":
                main_draw_names.add(matched_name)
            elif current_state == "QUAL":
                qualifying_names.add(matched_name)

    def get_p_rank(name, rank_list):
        return next((item for item in rank_list if item["Player"] == name), {"Rank": 9999, "Country": "-"})

    md_list = []
    for name in main_draw_names:
        info = get_p_rank(name, md_rankings)
        md_list.append({
            "name": format_player_name(name),
            "country": info["Country"],
            "rank_num": info["Rank"],
            "rank": f"WTA {info['Rank']}" if info['Rank'] < 9999 else "-",
            "type": "MAIN"
        })
    md_list.sort(key=lambda x: x["rank_num"])
    for idx, p in enumerate(md_list, 1):
        p["pos"] = str(idx)

    qual_list = []
    for name in qualifying_names:
        info = get_p_rank(name, qual_rankings)
        qual_list.append({
            "name": format_player_name(name),
            "country": info["Country"],
            "rank_num": info["Rank"],
            "rank": f"WTA {info['Rank']}" if info['Rank'] < 9999 else "-",
            "type": "QUAL"
        })
    qual_list.sort(key=lambda x: x["rank_num"])
    for idx, p in enumerate(qual_list, 1):
        p["pos"] = str(idx)

    final_tourney_list = md_list + qual_list
    
    suffix_map = {p: "" for p in main_draw_names}
    suffix_map.update({p: " (Q)" for p in qualifying_names})
    
    return final_tourney_list, suffix_map

def main():
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)

    try:
        itf_items = get_dynamic_itf_calendar(driver)
        monday_map = {
            "2026-02-16": "Semana 16 Febrero",
            #"2026-02-23": "Semana 23 Febrero",
            #"2026-03-02": "Semana 2 Marzo",
            #"2026-03-09": "Semana 9 Marzo"
        }

        for label in monday_map.values():
            if label not in TOURNAMENT_GROUPS:
                TOURNAMENT_GROUPS[label] = {}

        for item in itf_items:
            s_date = pd.to_datetime(item['startDate'])
            monday_date = (s_date - timedelta(days=s_date.weekday())).strftime('%Y-%m-%d')
            if monday_date in monday_map:
                week_label = monday_map[monday_date]
                TOURNAMENT_GROUPS[week_label][item['tournamentKey'].lower()] = item['tournamentName']

        ranking_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
        players_data = get_all_rankings(ranking_date)
        schedule_map = {}
        tournament_store = {} 

        dropdown_html = ""
        for week, tourneys in TOURNAMENT_GROUPS.items():
            dropdown_html += f'<option disabled class="dropdown-header">{week.upper()}</option>'
            for t_key, t_name in tourneys.items():
                dropdown_html += f'<option value="{t_key}" class="dropdown-item">{t_name}</option>'
            dropdown_html += '</optgroup>'

        ranking_cache = {}
        for week, tourneys in TOURNAMENT_GROUPS.items():
            print(f"Procesando {week}...")
            week_monday = next(k for k, v in monday_map.items() if v == week_label)
            md_date = get_monday_offset(week_monday, 4)
            q_date = get_monday_offset(week_monday, 3)

            if md_date not in ranking_cache: ranking_cache[md_date] = get_all_rankings(md_date)
            if q_date not in ranking_cache: ranking_cache[q_date] = get_all_rankings(q_date)

            for key, t_name in tourneys.items():
                if key.startswith("http"):
                    t_list, status_dict = scrape_tournament_players(key, ranking_cache[md_date], ranking_cache[q_date])
                    tournament_store[key] = t_list
                    
                    for p_name, suffix in status_dict.items():
                        p_key = p_name.upper()
                        if p_key not in schedule_map: 
                            schedule_map[p_key] = {}

                        if week in schedule_map[p_key]:
                            schedule_map[p_key][week] += f'<div style="margin-top: 3px;">{t_name}{suffix}</div>'
                        else:
                            schedule_map[p_key][week] = f"{t_name}{suffix}"
                else:
                    itf_entries, itf_name_map = get_itf_players(key, driver)
                    tourney_players_list = []
                    
                    for classification in itf_entries:
                        class_code = classification.get("entryClassificationCode", "")
                        
                        if class_code in ["MDA", "JR"]:
                            section_type = "MAIN"
                        elif class_code == "Q":
                            section_type = "QUAL"
                        else:
                            continue
                        
                        for entry in classification.get("entries") or []:
                            pos = entry.get("positionDisplay", "-")
                            players = entry.get("players") or []
                            if not players: continue
                            
                            p_node = players[0]
                            given_name = p_node.get('givenName', '')
                            family_name = p_node.get('familyName', '')
                            raw_f_name = f"{given_name} {family_name}".strip()

                            lookup_name = raw_f_name.upper()
                            
                            wta = p_node.get("atpWtaRank", "")
                            itf_rank = p_node.get("itfBTRank")
                            wtn = p_node.get("worldRating", "")
                            
                            erank_str = "-"
                            if wta and str(wta).strip() != "": erank_str = f"WTA {wta}"
                            elif itf_rank is not None and str(itf_rank).strip() != "": erank_str = f"ITF {itf_rank}"
                            elif wtn and str(wtn).strip() != "": erank_str = f"WTN {wtn}"
                                
                            if class_code == "JR": erank_str += " [JE]"
                            
                            try:
                                pos_digits = ''.join(filter(str.isdigit, str(pos)))
                                pos_num = int(pos_digits) if pos_digits else 999
                            except:
                                pos_num = 999

                            tourney_players_list.append({
                                "pos": pos,
                                "name": raw_f_name,
                                "country": p_node.get("nationalityCode", "-"),
                                "rank": erank_str,
                                "type": section_type,
                                "pos_num": pos_num
                            })

                    tourney_players_list.sort(key=lambda x: x["pos_num"])
                    tournament_store[key] = tourney_players_list

                    for p_name, suffix in itf_name_map.items():
                        if p_name not in schedule_map: 
                            schedule_map[p_name] = {}
                        
                        if week in schedule_map[p_name]:
                            if t_name not in schedule_map[p_name][week]:
                                schedule_map[p_name][week] += f"<br>{t_name}{suffix}"
                        else:
                            schedule_map[p_name][week] = f"{t_name}{suffix}"

                time.sleep(random.uniform(1, 2))

    finally:
        driver.quit()

    table_rows = ""
    week_keys = list(TOURNAMENT_GROUPS.keys())
    consolidated_players = {p['Player']: p for p in players_data if p['Country'] == "ARG"}

    for p_name in sorted(consolidated_players.keys(), key=lambda x: consolidated_players[x]['Rank']):
        p = consolidated_players[p_name]
        player_display = format_player_name(p['Player'])
        row = f'<tr data-name="{player_display.lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        row += f'<td class="sticky-col col-name">{player_display}</td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "—")
            is_main = "(Q)" not in val and val != "—"
            row += f'<td class="col-week">{"<b>" if is_main else ""}{val}{"</b>" if is_main else ""}</td>'
        table_rows += row + "</tr>"

    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Próximos Torneos</title>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; height: 100vh; overflow: hidden; }}
            .app-container {{ display: flex; width: 100%; height: 100%; }}
            .sidebar {{ width: 180px; background: #1e293b; color: white; display: flex; flex-direction: column; flex-shrink: 0; }}
            .sidebar-header {{ padding: 25px 15px; font-size: 15px; font-weight: 800; color: #75AADB; border-bottom: 1px solid #475569; }}
            .menu-item {{ padding: 15px 20px; cursor: pointer; color: #cbd5e1; font-size: 14px; border-bottom: 1px solid #334155; }}
            .menu-item.active {{ background: #75AADB; color: white; font-weight: bold; }}
            .main-content {{ flex: 1; overflow-y: auto; background: #f8fafc; padding: 20px; }}
            .dual-layout {{ display: flex; min-height: 80vh; gap: 40px; position: relative; }}
            .column-main {{ flex: 0 0 70%; display: flex; flex-direction: column; align-items: flex-start; position: relative; min-width: 0; }}
            .column-main table {{ table-layout: fixed; width: 100%; }}
            .column-entry {{ flex: 1; display: flex; flex-direction: column; align-items: flex-start; min-width: 0; }}
            .column-main::after {{ content: ""; position: absolute; right: -20px; top: 50px; bottom: 20px; width: 1px; background: #94a3b8; }}
            .header-row {{ width: 100%; margin-bottom: 20px; display: flex; flex-direction: column; align-items: center; position: relative; gap: 10px; }}
            h1 {{ margin: 0; font-size: 22px; color: #1e293b; }}
            .search-container {{ position: absolute; left: 0; top: 50%; transform: translateY(-50%); }}
            input, select {{ padding: 8px 12px; border-radius: 8px; border: 2px solid #94a3b8; font-family: inherit; font-size: 13px; width: 250px; box-sizing: border-box; }}
            select {{ background: white; font-weight: bold; cursor: pointer; appearance: none; background-image: url("data:image/svg+xml;charset=UTF-8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='none' stroke='%23475569' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpolyline points='6 9 12 15 18 9'%3E%3C/polyline%3E%3C/svg%3E"); background-repeat: no-repeat; background-position: right 10px center; }}
            .content-card {{ background: white; box-shadow: 0 4px 20px rgba(0,0,0,0.05); overflow: hidden; width: 100%; border: 1px solid black; }}
            .table-wrapper {{ overflow-x: auto; width: 100%; }}
            table {{ border-collapse: separate; border-spacing: 0; width: 100%; table-layout: fixed; border: 1px solid black; }}
            th {{ position: sticky; top: 0; background: #75AADB !important; color: white; padding: 10px 15px; font-size: 11px; font-weight: bold; border-bottom: 2px solid #1e293b; border-right: 1px solid #1e293b; z-index: 10; text-transform: uppercase; text-align: center; }}
            td {{ padding: 8px 12px; border-bottom: 1px solid #94a3b8; text-align: center; font-size: 13px; border-right: 1px solid #94a3b8; }}
            .column-entry td {{ font-size: 12px; padding: 6px 10px; }}
            .sticky-col {{ position: sticky; background: white !important; z-index: 2; }}
            .row-arg {{ background-color: #e0f2fe !important; }}
            td.col-week {{ width: 150px; font-size: 11px; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            th.sticky-col {{ z-index: 11; background: #75AADB !important; color: white; }}
            .col-rank {{ left: 0; width: 30px; min-width: 45px; max-width: 45px; }}
            .col-name {{ left: 45px; width: 140px; min-width: 140px; max-width: 140px; text-align: left; font-weight: bold; color: #334155; }}
            .col-week {{ width: 130px; font-size: 11px; font-weight: bold; line-height: 1.2; overflow: hidden; text-overflow: ellipsis; }}
            .divider-row td {{ background: #e2e8f0; font-weight: bold; text-align: center; padding: 5px 15px; font-size: 11px; border-right: none; }}
            tr.hidden {{ display: none; }}
            tr:hover td {{ background: #f1f5f9; }}

            .dropdown-header {{
                background-color: #e2e8f0 !important;
                font-weight: bold !important;
                text-align: center !important;
                padding: 12px 0 !important; 
                font-size: 11px;
                display: block;
            }}
            .dropdown-item {{
                padding: 8px 15px;
                text-align: left;
                background-color: #ffffff;
            }}

            #tSelect {{ appearance: none; padding: 10px 30px 10px 12px;  line-height: 1.5; background-color: white; }}
            #tSelect optgroup {{ background-color: #babdc2; color: #ffffff; text-align: center; font-style: normal; font-weight: 800; padding: 10px 0; }}
            #tSelect option {{ background-color: #ffffff; color: #1e293b; text-align: left; padding: 8px 12px; cursor: pointer; }}
            #tSelect option {{ margin-left: -15px; }}
            #tSelect option:hover,
            #tSelect option:focus,
            #tSelect option:checked {{ background-color: #75AADB !important; color: white !important; }}
        </style>
    </head>
    <body onload="updateEntryList()">
        <div class="app-container">
            <div class="sidebar">
                <div class="sidebar-header">Tenistas Argentinas</div>
                <div class="menu-item active">Próximos Torneos</div>
            </div>
            <div class="main-content">
                <div class="dual-layout">
                    <div class="column-main">
                        <div class="header-row">
                            <div class="search-container">
                                <input type="text" id="s" placeholder="Buscar tenista..." oninput="filter()">
                            </div>
                            <h1>Próximos Torneos</h1>
                        </div>
                        <div class="content-card">
                            <div class="table-wrapper">
                                <table>
                                    <thead>
                                        <tr>
                                            <th class="sticky-col col-rank">Rank</th>
                                            <th class="sticky-col col-name">Jugadora</th>
                                            {"".join([f'<th class="col-week">{w}</th>' for w in week_keys])}
                                        </tr>
                                    </thead>
                                    <tbody id="tb">{table_rows}</tbody>
                                </table>
                            </div>
                        </div>
                    </div>

                    <div class="column-entry">
                        <div class="header-row">
                            <h1>Entry List</h1> 
                            <div class="search-container-static"> 
                                <select id="tSelect" onchange="updateEntryList()">
                                    {dropdown_html}
                                </select>
                            </div>
                        </div>
                        <div class="content-card">
                            <table>
                                <thead>
                                    <tr>
                                        <th style="width:20px">Pos.</th>
                                        <th>Jugadora</th>
                                        <th style="width:35px">País</th>
                                        <th style="width:90px">E-Rank</th> 
                                    </tr>
                                </thead>
                                <tbody id="entry-body"></tbody>
                            </table>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        <script>
            const tournamentData = {json.dumps(tournament_store)};
            function filter() {{
                const q = document.getElementById('s').value.toLowerCase();
                document.querySelectorAll('#tb tr').forEach(row => {{
                    const matches = row.getAttribute('data-name').includes(q);
                    row.classList.toggle('hidden', !matches);
                }});
            }}
            function updateEntryList() {{
                const sel = document.getElementById('tSelect').value;
                const body = document.getElementById('entry-body');
                if (!tournamentData[sel]) return;
                const players = tournamentData[sel];
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
        </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f: f.write(html_template)

if __name__ == "__main__":
    main()