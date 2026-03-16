import json
from pathlib import Path

from config import CONTINENT_KEYS, CONTINENT_LABELS
from utils import get_surface_class, get_tournament_sort_order


BASE_DIR = Path(__file__).resolve().parent
SNAPSHOT_PATH = BASE_DIR / "data" / "calendar_snapshot.json"
INDEX_PATH = BASE_DIR / "index.html"


def _filter_key(level: str) -> str:
    lvl = (level or "").strip().lower().replace(" ", "")
    if lvl == "grandslam":
        return "gs"
    if "wta125" in lvl or lvl == "125" or lvl.endswith("wta125"):
        return "wta125"
    if lvl.startswith("wta"):
        if "125" in lvl:
            return "wta125"
        return "wta_tour"
    if lvl in {"w15", "w35", "w50", "w75", "w100"}:
        return lvl
    if lvl.startswith("w") and lvl[1:].isdigit():
        return "itf_other"
    return "other"

def _surface_key(surface: str) -> str:
    s = (surface or "").lower()
    if "clay" in s:
        return "clay"
    if "grass" in s:
        return "grass"
    return "hard"


def build_calendar_table(calendar_snapshot: list[dict]) -> str:
    week_order: list[str] = []
    weeks: dict[str, dict] = {}
    col_keys = ["wta_tour", "wta_125", "itf"]
    for item in calendar_snapshot:
        wl = item.get("week_label", "")
        if not wl:
            continue
        if wl not in weeks:
            week_order.append(wl)
            weeks[wl] = {
                "columns": {ck: {c: [] for c in CONTINENT_KEYS} for ck in col_keys},
            }
        col = item.get("column", "")
        cont = item.get("continent", "")
        if col not in col_keys or cont not in CONTINENT_KEYS:
            continue
        weeks[wl]["columns"][col][cont].append(
            {
                "name": item.get("name", ""),
                "level": item.get("level", ""),
                "surface": item.get("surface", ""),
            }
        )

    for wl in week_order:
        for ck in col_keys:
            for cont in CONTINENT_KEYS:
                weeks[wl]["columns"][ck][cont].sort(key=lambda x: get_tournament_sort_order(x.get("level", "")))

    col_groups = [
        {"label": "WTA", "keys": ["wta_tour", "wta_125"]},
        {"label": "ITF", "keys": ["itf"]},
    ]

    html = '<table class="calendar-table"><thead><tr>'
    html += '<th class="cal-cat-header"></th><th class="cal-cont-header"></th>'
    for wl in week_order:
        html += f'<th class="cal-week-header">{wl}</th>'
    html += "</tr></thead><tbody>"

    for group in col_groups:
        for ci, cont in enumerate(CONTINENT_KEYS):
            row_cls = "cal-group-first" if ci == 0 else ("cal-group-last" if ci == len(CONTINENT_KEYS) - 1 else "")
            if row_cls:
                html += f'<tr class="{row_cls}" data-cal-row-continent="{cont}">'
            else:
                html += f'<tr data-cal-row-continent="{cont}">'
            if ci == 0:
                html += f'<td class="cal-cat-label" rowspan="{len(CONTINENT_KEYS)}">{group["label"]}</td>'
            html += f'<td class="cal-cont-label">{CONTINENT_LABELS[cont]}</td>'
            for wl in week_order:
                html += '<td class="cal-cell">'
                tournaments: list[dict] = []
                for ck in group["keys"]:
                    tournaments.extend(weeks[wl]["columns"][ck][cont])
                if tournaments:
                    tournaments.sort(key=lambda x: get_tournament_sort_order(x.get("level", "")))
                    for t in tournaments:
                        sc = get_surface_class(t.get("surface", ""))
                        fk = _filter_key(t.get("level", ""))
                        sk = _surface_key(t.get("surface", ""))
                        name = t.get("name", "")
                        html += f'<span class="calendar-tournament {sc}" data-cal-filter="{fk}" data-cal-continent="{cont}" data-cal-surface="{sk}">{name}</span>'
                html += "</td>"
            html += "</tr>"

    html += "</tbody></table>"
    return html


def ensure_filter_bar(view_html: str) -> str:
    if 'id="calendar-toolbar"' in view_html:
        return view_html

    # Remove legacy chip bar if present.
    legacy_start = view_html.find('<div class="calendar-filters"')
    if legacy_start != -1:
        legacy_end = view_html.find("</div>", legacy_start)
        if legacy_end != -1:
            legacy_end += len("</div>")
            view_html = view_html[:legacy_start] + view_html[legacy_end:]

    anchor = '<div class="content-card calendar-container">'
    idx = view_html.find(anchor)
    if idx == -1:
        return view_html

    toolbar = (
        '\n                    <div class="calendar-toolbar" id="calendar-toolbar">\n'
        '                        <div class="cal-dd" data-cal-dd="categories">\n'
        '                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">Categories</button>\n'
        '                            <div class="cal-dd-panel" role="menu">\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="gs" checked><span>GS</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="wta_tour" checked><span>WTA TOUR</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="wta125" checked><span>WTA 125</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w100" checked><span>W100</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w75" checked><span>W75</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w50" checked><span>W50</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w35" checked><span>W35</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-filter-toggle="w15" checked><span>W15</span></label>\n'
        "                            </div>\n"
        "                        </div>\n"
        '                        <div class="cal-dd" data-cal-dd="region">\n'
        '                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">Region</button>\n'
        '                            <div class="cal-dd-panel" role="menu">\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="south_america" checked><span>S America</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="north_central_america" checked><span>N/C America</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="europe" checked><span>Europe</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="africa" checked><span>Africa</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="asia" checked><span>Asia</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-continent-toggle="oceania" checked><span>Oceania</span></label>\n'
        "                            </div>\n"
        "                        </div>\n"
        '                        <div class="cal-dd" data-cal-dd="surface">\n'
        '                            <button type="button" class="cal-dd-btn" data-cal-dd-btn aria-expanded="false">Surface</button>\n'
        '                            <div class="cal-dd-panel" role="menu">\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="hard" checked><span>Hard</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="clay" checked><span>Clay</span></label>\n'
        '                                <label class="cal-dd-item"><input type="checkbox" data-cal-surface-toggle="grass" checked><span>Grass</span></label>\n'
        "                            </div>\n"
        "                        </div>\n"
        "                    </div>\n"
    )
    return view_html[:idx] + toolbar + view_html[idx:]


def main() -> None:
    snapshot = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    new_table = build_calendar_table(snapshot)

    html = INDEX_PATH.read_text(encoding="utf-8")
    view_start = html.find('<div id="view-calendar"')
    if view_start == -1:
        raise SystemExit("Could not find view-calendar block in index.html")
    view_end = html.find('<div id="view-roadtogs"', view_start)
    if view_end == -1:
        raise SystemExit("Could not find end of view-calendar block in index.html")

    view_html = html[view_start:view_end]
    view_html = ensure_filter_bar(view_html)

    table_start = view_html.find('<table class="calendar-table">')
    if table_start == -1:
        raise SystemExit("Could not find calendar table start in view-calendar")
    table_end = view_html.find("</table>", table_start)
    if table_end == -1:
        raise SystemExit("Could not find calendar table end in view-calendar")
    table_end += len("</table>")

    view_html = view_html[:table_start] + new_table + view_html[table_end:]
    html = html[:view_start] + view_html + html[view_end:]
    INDEX_PATH.write_text(html, encoding="utf-8")


if __name__ == "__main__":
    main()
