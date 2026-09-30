import shutil
from datetime import datetime
from pathlib import Path

import pytest

import html_generator
import main
import tstrength
from ranking_publication import accepted_ranking_dates, effective_wta_ranking_date, ranking_date_is_accepted
from time_utils import NEW_YORK


@pytest.mark.parametrize(
    ("eastern_now", "status", "expected"),
    [
        (datetime(2026, 8, 17, 11, 59, tzinfo=NEW_YORK), {}, "2026-08-10"),
        (datetime(2026, 8, 17, 12, 0, tzinfo=NEW_YORK), {}, "2026-08-10"),
        (
            datetime(2026, 8, 17, 12, 1, tzinfo=NEW_YORK),
            {
                "requested_date": "2026-08-17",
                "previous_date": "2026-08-10",
                "status": "pending_publication",
            },
            "2026-08-10",
        ),
        (
            datetime(2026, 8, 17, 12, 1, tzinfo=NEW_YORK),
            {
                "requested_date": "2026-08-17",
                "previous_date": "2026-08-10",
                "status": "confirmed_frozen",
            },
            "2026-08-17",
        ),
    ],
)
def test_effective_wta_ranking_date_respects_publication_state(eastern_now, status, expected):
    assert effective_wta_ranking_date(eastern_now, status).isoformat() == expected


def test_unaccepted_or_future_ranking_dates_are_never_publishable():
    now = datetime(2026, 8, 17, 12, 1, tzinfo=NEW_YORK)
    pending = {"requested_date": "2026-08-17", "status": "pending_publication"}
    accepted = {"requested_date": "2026-08-17", "status": "confirmed_changed"}

    assert not ranking_date_is_accepted("2026-08-17", now, pending)
    assert not ranking_date_is_accepted("2026-08-24", now, accepted)
    assert ranking_date_is_accepted("2026-08-17", now, accepted)
    assert accepted_ranking_dates({"2026-08-10": [], "2026-08-17": []}, now, accepted) == ["2026-08-10"]


def test_generated_site_excludes_pending_ranking_from_all_bundles(tmp_path, monkeypatch):
    data_dir = tmp_path / "source"
    site_dir = tmp_path / "site"
    data_dir.mkdir()
    site_dir.mkdir()
    (data_dir / "points_distribution.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "tournament_draw_sizes.json").write_text("[]\n", encoding="utf-8")
    (data_dir / "wta_full_calendar_cache.json").write_text('{"items": []}\n', encoding="utf-8")
    (data_dir / "wta_rankings_20_29.csv").write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "2026-07-20,1,1,900,Previous Player,ARG,2000-01-01\n"
        "2026-07-27,2,1,1000,Unaccepted Player,ARG,2001-01-01\n",
        encoding="utf-8",
    )
    (data_dir / "wta_ranking_refresh_status.json").write_text(
        '{"requested_date":"2026-07-27","status":"pending_publication"}', encoding="utf-8"
    )
    match_columns = (
        "matchType,matchId,date,tournamentId,tournamentName,tournamentCategory,surface,"
        "inOrOutdoor,tournamentCountry,roundName,draw,result,resultStatusDesc,winnerId,"
        "winnerEntry,winnerSeed,winnerName,winnerCountry,loserId,loserEntry,loserSeed,"
        "loserName,loserCountry\n"
    )
    for filename in ("bjkc_matches_arg.csv", "manually_added_matches.csv"):
        (data_dir / filename).write_text(match_columns, encoding="utf-8")
    shutil.copytree(Path(__file__).resolve().parents[1] / "assets", site_dir / "assets")
    monkeypatch.setattr(html_generator, "new_york_now", lambda: datetime(2026, 7, 27, 12, 1, tzinfo=NEW_YORK))
    monkeypatch.setattr(html_generator, "load_player_mapping", lambda: {})

    html_generator.generate_html(
        {}, {}, [], {}, [], [], [],
        wta_rankings=[], national_team_data=[], captains_data=[], draws_data={},
        tstrength_data=[], monday_map={}, data_dir=data_dir, site_root=site_dir,
    )

    for filename in (
        "assets/js/generated-data.js",
        "data/wta_rankings_latest_bundle.js",
        "data/wta_rankings_2026_bundle.js",
    ):
        output = (site_dir / filename).read_text(encoding="utf-8")
        assert "2026-07-20" in output
        assert "2026-07-27" not in output


def test_strength_and_match_history_ignore_pending_ranking(tmp_path, monkeypatch):
    ranking_file = tmp_path / "wta_rankings_20_29.csv"
    ranking_file.write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "2026-07-20,1,1,900,Previous Player,ARG,2000-01-01\n"
        "2026-07-27,2,1,1000,Unaccepted Player,ARG,2001-01-01\n",
        encoding="utf-8",
    )
    (tmp_path / "wta_ranking_refresh_status.json").write_text(
        '{"requested_date":"2026-07-27","status":"pending_publication"}', encoding="utf-8"
    )
    def now():
        return datetime(2026, 7, 27, 12, 1, tzinfo=NEW_YORK)
    monkeypatch.setattr(tstrength, "RANKINGS_CSV", str(ranking_file))
    monkeypatch.setattr(tstrength, "new_york_now", now)
    monkeypatch.setattr(main, "new_york_now", now)

    assert set(tstrength._load_rankings_index()) == {"2026-07-20"}
    history = [{"DATE": "2026-07-28", "_winnerName": "Unaccepted Player", "_winnerId": "2"}]
    main.enrich_history_with_wta_ranks(history, tmp_path)
    assert history[0]["_winnerRank"] == ""
