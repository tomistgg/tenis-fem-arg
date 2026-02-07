import requests
import time
import json
import pandas as pd
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

# --- CONFIGURATION ---
# Mantenemos las WTA fijas, el script añadirá las ITF dinámicamente
TOURNAMENT_GROUPS = {
    "Semana 16 Febrero": {
        "https://www.wtatennis.com/tournaments/dubai/player-list": "WTA 1000 DUBAI",
        "https://www.wtatennis.com/tournaments/2051/midland-125/2026/player-list": "WTA 125 MIDLAND",
        "https://www.wtatennis.com/tournaments/1156/oeiras-125-indoor-2/2026/player-list": "WTA 125 OEIRAS 2",
        "https://www.wtatennis.com/tournaments/1157/les-sables-d-olonne-125/2026/player-list": "WTA 125 LES SABLES",
    },
    "Semana 23 Febrero": {
        "https://www.wtatennis.com/tournaments/2085/m-rida/2026/player-list": "WTA 500 MERIDA",
        "https://www.wtatennis.com/tournaments/2082/austin/2026/player-list": "WTA 250 AUSTIN",
        "https://www.wtatennis.com/tournaments/1124/antalya-125-1/2026/player-list": "WTA 125 ANTALYA 1",
    },
    "Semana 2 Marzo": {
        "https://www.wtatennis.com/tournaments/609/indian-wells/2026/player-list": "WTA 1000 INDIAN WELLS",
        "https://www.wtatennis.com/tournaments/1107/antalya-125-2/2026/player-list": "WTA 125 ANTALYA 2",
    },
    "Semana 9 Marzo": {
        "https://www.wtatennis.com/tournaments/1161/austin-125-1/2026/player-list": "WTA 125 AUSTIN",
        "https://www.wtatennis.com/tournaments/1125/antalya-125-3/2026/player-list": "WTA 125 ANTALYA 3",
    }
}

API_URL = "https://api.wtatennis.com/tennis/players/ranked"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36"}

# --- HELPER FUNCTIONS ---

def get_dynamic_itf_calendar():
    """Usa Selenium para obtener el calendario ITF y mapearlo a semanas."""
    chrome_options = Options()
    chrome_options.add_argument("--headless")
    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=chrome_options)
    
    itf_tourneys = []
    try:
        print("Obteniendo calendario ITF dinámico...")
        url = "https://www.itftennis.com/tennis/api/TournamentApi/GetCalendar?circuitCode=WT&dateFrom=2026-02-16&dateTo=2026-03-31&skip=0&take=500"
        driver.get(url)
        time.sleep(5)
        content = driver.find_element("tag name", "body").text
        data = json.loads(content)
        itf_tourneys = data.get('items', [])
    except Exception as e:
        print(f"Error cargando calendario ITF: {e}")
    finally:
        driver.quit()
    return itf_tourneys

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
            time.sleep(0.05)
        except: break
    return [{"Player": p.get('player', {}).get('fullName').strip(), "Rank": p.get('ranking'), "Country": p.get('player', {}).get('countryCode', ''), "Key": p.get('player', {}).get('fullName').strip().upper()} for p in all_players if p.get('player')]

def get_itf_players(tournament_key):
    url = f"https://www.itftennis.com/tennis/api/TournamentApi/GetAcceptanceList?tournamentKey={tournament_key}&circuitCode=WT"
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        data = r.json()
    except Exception as e:
        print(f"Error en {tournament_key}: {e}")
        return {}

    final_list = {}
    for item in data:
        for classification in item.get("entryClassifications", []):
            desc = classification.get("entryClassification", "").upper()
            code = classification.get("entryClassificationCode", "")
            if "WITHDRAWAL" in desc: break 
            for entry in classification.get("entries", []):
                pos = entry.get("positionDisplay", "")
                if code == "MDA": suffix = ""
                elif code == "ALT" or "ALTERNATE" in desc: suffix = f" (ALT {pos})"
                else: suffix = " (Q)"
                for player in (entry.get("players") or []):
                    full_name = f"{player['givenName']} {player['familyName']}".strip().upper()
                    final_list[full_name] = suffix
    return final_list

def scrape_tournament_players(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(r.text, 'html.parser')
    except: return {}
    main_draw, qualifying = set(), set()
    current_state = "MAIN" 
    for tag in soup.find_all(True):
        text = tag.get_text().strip()
        ui_tab = tag.get('data-ui-tab', '')
        if text == "Doubles" or ui_tab == "Doubles": current_state = "IGNORE"
        elif text == "Qualifying" or ui_tab == "Qualifying": current_state = "QUAL"
        elif text == "Main Draw" or ui_tab == "Main Draw": current_state = "MAIN"
        p_name = tag.get('data-tracking-player-name')
        if p_name:
            name_key = p_name.strip().upper()
            if current_state == "MAIN": main_draw.add(name_key)
            elif current_state == "QUAL": qualifying.add(name_key)
    final_list = {p: " (Q)" for p in qualifying}
    final_list.update({p: "" for p in main_draw})
    return final_list

# --- MAIN PROCESS ---

def main():
    # 1. ACTUALIZACIÓN DINÁMICA DE TOURNAMENT_GROUPS CON ITF
    itf_items = get_dynamic_itf_calendar()
    
    # Mapa de lunes para asignar a los grupos existentes
    monday_map = {
        "2026-02-16": "Semana 16 Febrero",
        "2026-02-23": "Semana 23 Febrero",
        "2026-03-02": "Semana 2 Marzo",
        "2026-03-09": "Semana 9 Marzo"
    }

    for item in itf_items:
        s_date = pd.to_datetime(item['startDate'])
        # Encontrar el lunes de esa semana
        monday_date = (s_date - timedelta(days=s_date.weekday())).strftime('%Y-%m-%d')
        
        if monday_date in monday_map:
            week_label = monday_map[monday_date]
            t_key = item['tournamentKey'].lower()
            t_name = item['tournamentName'].upper()
            # Añadir al diccionario global
            TOURNAMENT_GROUPS[week_label][t_key] = t_name

    # 2. PROCESO ORIGINAL
    ranking_date = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    players_data = get_all_rankings(ranking_date)
    schedule_map = {}
    
    for week, tourneys in TOURNAMENT_GROUPS.items():
        print(f"Procesando {week}...")
        for key, t_name in tourneys.items():
            if key.startswith("http"):
                status_dict = scrape_tournament_players(key)
            else:
                status_dict = get_itf_players(key)
            
            for p_key, suffix in status_dict.items():
                if p_key not in schedule_map: schedule_map[p_key] = {}
                entry = f"{t_name}{suffix}"
                if week in schedule_map[p_key]:
                    if entry not in schedule_map[p_key][week]:
                        schedule_map[p_key][week] += f"<br>{entry}"
                else:
                    schedule_map[p_key][week] = entry

    # 3. GENERACIÓN DE HTML (Tu template original)
    table_rows = ""
    week_keys = list(TOURNAMENT_GROUPS.keys())
    for p in players_data:
        is_arg = "is-arg" if p['Country'] == "ARG" else "is-other"
        row = f'<tr class="{is_arg}" data-name="{p["Player"].lower()}">'
        row += f'<td class="sticky-col col-rank">{p["Rank"]}</td>'
        row += f'<td class="sticky-col col-name">{p["Player"]}</td>'
        row += f'<td><span class="{"arg-pill" if p["Country"] == "ARG" else ""}">{p["Country"]}</span></td>'
        for week in week_keys:
            val = schedule_map.get(p['Key'], {}).get(week, "—")
            row += f'<td class="col-week">{"<b>" if "(Q)" not in val and val != "—" else ""}{val}{"</b>" if "(Q)" not in val and val != "—" else ""}</td>'
        table_rows += row + "</tr>"

    # --- 6. TEMPLATE FINAL ---
    html_template = f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <title>Tenistas Argentinas - Próximos Torneos</title>
        <style>
            @font-face {{ font-family: 'Montserrat'; src: url('Montserrat-SemiBold.ttf'); }}
            body {{ font-family: 'Montserrat', sans-serif; background: #f0f4f8; margin: 0; display: flex; height: 100vh; overflow: hidden; }}
            .app-container {{ display: flex; width: 100%; height: 100%; }}
            .sidebar {{ width: 260px; background: #1e293b; color: white; display: flex; flex-direction: column; }}
            .sidebar-header {{ padding: 25px 20px; font-size: 20px; font-weight: 800; color: #75AADB; border-bottom: 1px solid #334155; }}
            .menu-item {{ padding: 15px 20px; cursor: pointer; color: #cbd5e1; }}
            .menu-item.active {{ background: #75AADB; color: white; font-weight: bold; }}
            .main-content {{ flex: 1; padding: 40px; overflow-y: auto; background: #f8fafc; }}
            .content-card {{ background: white; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.05); overflow: hidden; }}
            .table-nav {{ display: flex; justify-content: space-between; padding: 20px; border-bottom: 1px solid #e2e8f0; }}
            input {{ padding: 10px; border-radius: 8px; border: 1px solid #cbd5e1; width: 250px; }}
            #toggleBtn {{ background: #75AADB; color: white; border: none; padding: 10px 20px; border-radius: 20px; cursor: pointer; font-weight: bold; }}
            .table-wrapper {{ overflow-x: auto; }}
            table {{ width: 100%; border-collapse: collapse; }}
            th {{ position: sticky; top: 0; background: white; padding: 15px; font-size: 11px; border-bottom: 2px solid #e2e8f0; }}
            td {{ padding: 12px; border-bottom: 1px solid #f1f5f9; text-align: center; font-size: 13px; }}
            .sticky-col {{ position: sticky; background: white !important; z-index: 2; }}
            .col-rank {{ left: 0; width: 50px; }}
            .col-name {{ left: 50px; width: 180px; text-align: left; font-weight: bold; }}
            tr.hidden {{ display: none; }}
        </style>
    </head>
    <body>
    <div class="app-container">
        <div class="sidebar">
            <div class="sidebar-header">Tenistas Argentinas</div>
            <div class="menu-item active">Próximos Torneos</div>
        </div>
        <div class="main-content">
            <h1>Próximos Torneos</h1>
            <div class="content-card">
                <div class="table-nav">
                    <input type="text" id="s" placeholder="Buscar..." oninput="filter()">
                    <button id="toggleBtn" onclick="toggleMode()">Mostrar Todas</button>
                </div>
                <div class="table-wrapper">
                    <table>
                        <thead>
                            <tr>
                                <th class="sticky-col col-rank">Rank</th>
                                <th class="sticky-col col-name">Jugadora</th>
                                <th>País</th>
                                {"".join([f'<th>{w}</th>' for w in week_keys])}
                            </tr>
                        </thead>
                        <tbody id="tb">{table_rows}</tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    <script>
        let mode = 'arg';
        function toggleMode() {{
            mode = (mode === 'arg') ? 'all' : 'arg';
            document.getElementById('toggleBtn').innerText = mode === 'arg' ? "Mostrar Todas" : "Mostrar ARG";
            filter();
        }}
        function filter() {{
            const q = document.getElementById('s').value.toLowerCase();
            document.querySelectorAll('#tb tr').forEach(row => {{
                const matches = row.getAttribute('data-name').includes(q);
                const isArg = row.classList.contains('is-arg');
                row.classList.toggle('hidden', !(matches && (mode === 'all' || isArg)));
            }});
        }}
        window.onload = filter;
    </script>
    </body>
    </html>
    """
    with open("index.html", "w", encoding="utf-8") as f: f.write(html_template)
    print("Hecho: index.html generado.")

if __name__ == "__main__": main()