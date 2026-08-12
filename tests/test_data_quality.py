import json
from datetime import date
from pathlib import Path

import pytest

from data_quality import (
    LimitModel,
    PlayerAliasModel,
    QualityPolicyModel,
    TablePolicyModel,
    FreshnessModel,
    _validate_freshness,
    _validate_json_schema,
    _validate_tabular_file,
    _validate_thresholds,
    validate_site_artifacts,
)
from pipeline_errors import DataValidationError


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pydantic_player_model_rejects_blank_canonical_name():
    row = {
        "player_key": "wta:1", "display_name": "", "country": "ARG", "dob": "",
        "wta_id": "1", "wta_name": "Fixture", "itf_id": "", "itf_name": "",
        "bjkc_id": "", "bjkc_name": "", "aliases": [], "additional_wta_ids": [],
        "additional_itf_ids": [], "additional_bjkc_ids": [],
    }
    with pytest.raises(ValueError, match="must not be blank"):
        PlayerAliasModel.model_validate(row)


def test_json_schema_rejects_unknown_alias_fields(tmp_path):
    payload = [{
        "player_key": "wta:1", "display_name": "Fixture", "country": "ARG", "dob": "",
        "wta_id": "1", "wta_name": "Fixture", "itf_id": "", "itf_name": "",
        "bjkc_id": "", "bjkc_name": "", "aliases": [], "additional_wta_ids": [],
        "additional_itf_ids": [], "additional_bjkc_ids": [], "unexpected": True,
    }]
    data_path = tmp_path / "aliases.json"
    data_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(DataValidationError, match="does not match"):
        _validate_json_schema(payload, PROJECT_ROOT / "schemas" / "player_aliases.schema.json", data_path)


def test_tournament_snapshot_schema_requires_versioned_normalized_rows(tmp_path):
    schema = PROJECT_ROOT / "schemas" / "tournament_snapshot.schema.json"
    data_path = tmp_path / "tournament_snapshot.json"
    invalid_payload = {
        "schemaVersion": 1,
        "fields": ["name", "level", "surface", "country", "startDate", "endDate", "week"],
        "tournaments": {
            "w-itf-srb-2026-016": [
                "W75 Kursumlijska Banja ",
                "W75",
                "Clay",
                "SRB",
                "2026-08-17T00:00:00",
                "2026-08-23",
                "Week of August 17",
            ]
        },
    }

    with pytest.raises(DataValidationError, match="does not match"):
        _validate_json_schema(invalid_payload, schema, data_path)


def test_current_tournament_snapshot_matches_its_schema():
    data_path = PROJECT_ROOT / "data" / "tournament_snapshot.json"
    payload = json.loads(data_path.read_text(encoding="utf-8-sig"))

    _validate_json_schema(
        payload,
        PROJECT_ROOT / "schemas" / "tournament_snapshot.schema.json",
        data_path,
    )


def test_row_drop_threshold_is_blocking(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    rows = [{"id": index} for index in range(100)]
    (baseline / "fixture.json").write_text(json.dumps(rows), encoding="utf-8")
    table = TablePolicyModel(
        kind="json_array",
        minimum_rows=1,
        max_row_drop=LimitModel(absolute=1, fraction=0.01),
    )
    policy = QualityPolicyModel(schema_version=1, tables={"fixture.json": table}, cache_freshness={})
    with pytest.raises(DataValidationError, match="dropped 20 rows"):
        _validate_thresholds(tmp_path, baseline, policy, {"fixture.json": 80})


def test_row_count_increase_is_not_blocking(tmp_path):
    baseline = tmp_path / "baseline"
    baseline.mkdir()
    (baseline / "fixture.json").write_text(json.dumps([{"id": 1}]), encoding="utf-8")
    table = TablePolicyModel(
        kind="json_array",
        minimum_rows=1,
        max_row_drop=LimitModel(absolute=0, fraction=0.0),
    )
    policy = QualityPolicyModel(schema_version=1, tables={"fixture.json": table}, cache_freshness={})

    comparisons = _validate_thresholds(tmp_path, baseline, policy, {"fixture.json": 50_000})

    assert comparisons["fixture.json"]["row_count_change"] == 49_999


def test_minimum_row_count_is_blocking(tmp_path):
    table = TablePolicyModel(
        kind="json_array",
        minimum_rows=100,
        max_row_drop=LimitModel(absolute=0, fraction=0.0),
    )
    policy = QualityPolicyModel(schema_version=1, tables={"fixture.json": table}, cache_freshness={})
    with pytest.raises(DataValidationError, match="minimum is 100"):
        _validate_thresholds(tmp_path, None, policy, {"fixture.json": 99})


def test_pandera_ranking_schema_rejects_invalid_dates(tmp_path):
    path = tmp_path / "rankings.csv"
    path.write_text(
        "week_date,id,rank,points,player,country,dob\n"
        "22/07/2026,1,1,100,Fixture Player,ARG,2000-01-01\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError, match="tabular schema validation failed"):
        _validate_tabular_file(path, "rankings")


def test_stale_table_is_blocking(tmp_path):
    (tmp_path / "fixture.csv").write_text("date\n2026-06-01\n", encoding="utf-8")
    (tmp_path / "cache_state.json").write_text('{"files": {}, "entries": {}}', encoding="utf-8")
    table = TablePolicyModel(
        kind="matches",
        minimum_rows=1,
        max_row_drop=LimitModel(absolute=0, fraction=0.0),
        freshness=FreshnessModel(column="date", max_age_days=7, future_tolerance_days=0),
    )
    policy = QualityPolicyModel(schema_version=1, tables={"fixture.csv": table}, cache_freshness={})
    with pytest.raises(DataValidationError, match="age 51 days"):
        _validate_freshness(tmp_path, policy, date(2026, 7, 22))


@pytest.mark.integration
def test_offline_generation_creates_a_valid_site(offline_generated_site):
    report = validate_site_artifacts(offline_generated_site)
    assert report["generated_files"] >= 14
    app = (offline_generated_site / "app.html").read_text(encoding="utf-8-sig")
    app_js = (offline_generated_site / "assets/js/app.js").read_text(encoding="utf-8")
    assert "Failed to load local rankings data" in app_js  # fallback exists but must not execute
    assert (offline_generated_site / "data" / "wta_rankings_latest_bundle.js").is_file()
    assert (offline_generated_site / "data" / "wta_rankings_2026_bundle.js").is_file()
