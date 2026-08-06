from pathlib import Path

from html_generator import _ranking_display_name, _write_wta_ranking_bundles
import wta
from wta import WtaRankingsCsvStore


HEADER = "week_date,id,rank,points,player,country,dob\n"


def _write_rankings(path: Path, rows: list[str]) -> None:
    path.write_text(HEADER + "".join(rows), encoding="utf-8")


def test_ranking_store_indexes_dates_without_retaining_all_row_dicts(tmp_path):
    current = tmp_path / "wta_rankings_20_29.csv"
    older = tmp_path / "wta_rankings_83_99.csv"
    _write_rankings(
        current,
        [
            "2026-07-20,1,1,1000,Current Player,ARG,2000-01-01\n",
            "2025-12-29,2,2,900,Higher Priority,ESP,2001-02-02\n",
        ],
    )
    _write_rankings(
        older,
        [
            "2025-12-29,3,3,800,Lower Priority,USA,2002-03-03\n",
            "1999-12-27,4,4,700,Historic Player,FRA,1980-04-04\n",
        ],
    )

    store = WtaRankingsCsvStore([current, older])

    assert sorted(store) == ["1999-12-27", "2025-12-29", "2026-07-20"]
    assert store._overrides == {}
    assert store._date_cache == {}
    assert store["2025-12-29"][0]["Player"] == "HIGHER PRIORITY"
    assert list(store._date_cache) == ["2025-12-29"]


def test_ranking_bundles_are_latest_plus_lazy_year_files(tmp_path):
    legacy = tmp_path / "wta_rankings_20_29_bundle.js"
    legacy.write_text("legacy", encoding="utf-8")
    rankings = {
        "2025-12-29": [{"Rank": 2, "Points": 900, "Player": "OLDER", "Country": "ARG", "DOB": ""}],
        "2026-07-13": [{"Rank": 2, "Points": 950, "Player": "PREVIOUS", "Country": "ARG", "DOB": ""}],
        "2026-07-20": [{"Rank": 1, "Points": 1000, "Player": "LATEST", "Country": "ARG", "DOB": ""}],
    }

    latest_date, files = _write_wta_ranking_bundles(rankings, tmp_path)

    assert latest_date == "2026-07-20"
    assert files == {
        "wta_rankings_latest_bundle.js",
        "wta_rankings_2025_bundle.js",
        "wta_rankings_2026_bundle.js",
    }
    assert not legacy.exists()
    latest = (tmp_path / "wta_rankings_latest_bundle.js").read_text(encoding="utf-8")
    assert "__WTA_RANKINGS_LATEST__" in latest
    assert "2026-07-20" in latest
    assert "2025-12-29" not in latest
    assert (tmp_path / "wta_rankings_latest_bundle.js").stat().st_size < (
        tmp_path / "wta_rankings_2026_bundle.js"
    ).stat().st_size


def test_ranking_bundles_present_names_without_identity_suffixes(tmp_path):
    rankings = {
        "2026-07-20": [{
            "Rank": 40,
            "Points": 1200,
            "Player": "YUE YUAN (1998)",
            "Id": "324325",
            "OfficialPlayer": "YUAN YUE",
            "Country": "CHN",
            "DOB": "1998-09-25",
        }],
    }

    _write_wta_ranking_bundles(rankings, tmp_path)

    latest = (tmp_path / "wta_rankings_latest_bundle.js").read_text(encoding="utf-8")
    assert "YUE YUAN" in latest
    assert "YUAN YUE" not in latest
    assert "YUE YUAN (1998)" not in latest


def test_ranking_display_name_removes_only_identity_disambiguators():
    assert _ranking_display_name({"Id": "70300", "Player": "CAROLINA GARCÍA (ARG)"}) == "CAROLINA GARCÍA"
    assert _ranking_display_name({"Id": "337674", "Player": "SLOANE STEPHENS (WTA 337674)"}) == "SLOANE STEPHENS"
    assert _ranking_display_name({"Id": "20006", "Player": "DIANNE FROMHOLTZ (BALESTRAT)"}) == "DIANNE FROMHOLTZ (BALESTRAT)"


def test_new_ranking_week_is_streamed_into_atomic_csv(tmp_path, monkeypatch):
    path = tmp_path / "wta_rankings_20_29.csv"
    _write_rankings(
        path,
        ["2026-07-13,1,1,900,Existing Player,ARG,2000-01-01\n"],
    )
    monkeypatch.setattr(wta, "WTA_RANKINGS_CSV", str(path))

    wta._save_wta_csv_date(
        "2026-07-20",
        [{
            "Id": "2",
            "Rank": 1,
            "Points": 1000,
            "OfficialPlayer": "New Player",
            "Country": "ARG",
            "DOB": "2001-02-02",
        }],
    )

    text = path.read_text(encoding="utf-8-sig")
    assert "2026-07-13,1,1,900,Existing Player,ARG,2000-01-01" in text
    assert "2026-07-20,2,1,1000,New Player,ARG,2001-02-02" in text
    assert not list(tmp_path.glob("*.tmp"))
