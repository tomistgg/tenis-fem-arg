import shutil
from pathlib import Path

import pytest

import html_generator


PROJECT_ROOT = Path(__file__).resolve().parents[1]

MATCH_COLUMNS = (
    "matchType,matchId,date,tournamentId,tournamentName,tournamentCategory,surface,"
    "inOrOutdoor,tournamentCountry,roundName,draw,result,resultStatusDesc,winnerId,"
    "winnerEntry,winnerSeed,winnerName,winnerCountry,loserId,loserEntry,loserSeed,"
    "loserName,loserCountry\n"
)


@pytest.fixture(scope="session")
def offline_generated_site(tmp_path_factory):
    """Generate a complete site using only deterministic local fixture data."""
    root = tmp_path_factory.mktemp("offline-site")
    data_dir = root / "data-source"
    site_dir = root / "site"
    data_dir.mkdir()
    site_dir.mkdir()
    (data_dir / "points_distribution.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "tournament_draw_sizes.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "wta_rankings_20_29.csv").write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "2026-07-20,1,1,1000,Fixture Player,ARG,2000-01-01\n",
        encoding="utf-8",
    )
    (data_dir / "wta_full_calendar_cache.json").write_text('{"items": []}\n', encoding="utf-8")
    (data_dir / "bjkc_matches_arg.csv").write_text(MATCH_COLUMNS, encoding="utf-8")
    (data_dir / "manually_added_matches.csv").write_text(MATCH_COLUMNS, encoding="utf-8")

    previous_data_dir = html_generator.RUNTIME_DATA_DIR
    previous_site_root = html_generator.RUNTIME_SITE_ROOT
    previous_loader = html_generator.load_player_mapping
    try:
        shutil.copytree(PROJECT_ROOT / "assets", site_dir / "assets")
        html_generator.RUNTIME_DATA_DIR = data_dir
        html_generator.RUNTIME_SITE_ROOT = site_dir
        html_generator.load_player_mapping = lambda: {}
        html_generator.generate_html(
            {}, {}, [], {}, [], [], [],
            wta_rankings=[],
            national_team_data=[],
            captains_data=[],
            draws_data={},
            tstrength_data=[],
            monday_map={},
        )
    finally:
        html_generator.RUNTIME_DATA_DIR = previous_data_dir
        html_generator.RUNTIME_SITE_ROOT = previous_site_root
        html_generator.load_player_mapping = previous_loader

    deploy_data = site_dir / "data"
    deploy_data.mkdir()
    for source in data_dir.glob("*_bundle.js"):
        shutil.copy2(source, deploy_data / source.name)
    for filename in ("site.webmanifest",):
        shutil.copy2(PROJECT_ROOT / filename, site_dir / filename)
    return site_dir
