import json
import os
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pandas as pd
import requests
import yaml

from canonical_data import CanonicalConstraintError, source_match_key
from http_client import request_with_retry
from pipeline_errors import DataPromotionError, SourceRequestError
import pipeline_transaction
import populate_data.load_weekly_ranking as weekly_ranking
from populate_data.load_weekly_ranking import csv_date_is_complete
from run_state import initialize_run_state, load_run_state, record_run_issue
from transactional_io import atomic_write_csv, atomic_write_dataframe
from generate_run_report import render_email_markdown


PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_csv_date_completeness_checks_every_row():
    complete = [
        {"id": "1", "points": "100", "dob": "2000-01-01"},
        {"id": "2", "points": "90", "dob": "2001-02-02"},
    ]
    assert csv_date_is_complete(complete)
    assert not csv_date_is_complete([complete[0], {**complete[1], "dob": ""}])
    assert not csv_date_is_complete([complete[0], {**complete[1], "id": "1"}])


def test_new_week_ranking_sync_uses_player_table_path(tmp_path, monkeypatch):
    player_table = tmp_path / "players.json"
    current_rows = [{"id": "1", "rank": "1", "points": "100", "player": "Player One", "dob": "2000-01-01"}]
    captured = {}

    monkeypatch.setattr(weekly_ranking, "PLAYER_ALIASES_WTA_ITF_FILE", str(player_table))
    monkeypatch.setattr(weekly_ranking, "load_csv_by_date", lambda: {})
    monkeypatch.setattr(weekly_ranking, "load_status", lambda: {})
    monkeypatch.setattr(weekly_ranking, "now_eastern", lambda: datetime(2026, 7, 27, 11))
    monkeypatch.setattr(weekly_ranking, "fetch_from_api", lambda date_str: current_rows)
    monkeypatch.setattr(weekly_ranking, "ranking_is_valid", lambda rows: True)
    monkeypatch.setattr(weekly_ranking, "save_status", lambda status: True)
    monkeypatch.setattr(weekly_ranking, "rewrite_csv", lambda by_date: None)

    def capture_sync(path, rows):
        captured["path"] = path
        captured["rows"] = rows
        return 0

    monkeypatch.setattr(weekly_ranking, "sync_wta_players", capture_sync)

    weekly_ranking.main()

    assert captured == {"path": player_table, "rows": current_rows}


def test_itf_natural_key_is_not_match_id_alone():
    first = {"tournamentId": "100", "date": "2025-02-01", "matchId": "42"}
    reused = {"tournamentId": "200", "date": "2025-02-01", "matchId": "42"}
    next_season = {"tournamentId": "100", "date": "2026-02-01", "matchId": "42"}
    assert source_match_key(first, "itf") != source_match_key(reused, "itf")
    assert source_match_key(first, "itf") != source_match_key(next_season, "itf")


def test_itf_natural_key_rejects_missing_tournament_context():
    with pytest.raises(CanonicalConstraintError):
        source_match_key({"date": "2026-02-01", "matchId": "42"}, "itf")


def test_atomic_csv_failure_preserves_existing_file():
    with TemporaryDirectory() as directory:
        path = Path(directory) / "rows.csv"
        path.write_text("id,name\n1,original\n", encoding="utf-8")
        before = path.read_bytes()

        with pytest.raises(ValueError):
            atomic_write_csv(
                path,
                ["id", "name"],
                [{"id": "2", "name": "replacement", "unexpected": "field"}],
            )

        assert path.read_bytes() == before
        assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_atomic_dataframe_write_uses_windows_compatible_fsync(tmp_path):
    path = tmp_path / "rows.csv"

    atomic_write_dataframe(pd.DataFrame([{"id": 1, "name": "updated"}]), path, index=False)

    assert path.read_text(encoding="utf-8") == "id,name\n1,updated\n"


def test_atomic_promotion_copy_uses_windows_compatible_fsync(tmp_path):
    source = tmp_path / "source.txt"
    destination = tmp_path / "destination.txt"
    source.write_text("promoted", encoding="utf-8")

    pipeline_transaction._atomic_copy(source, destination, "test-run")

    assert destination.read_text(encoding="utf-8") == "promoted"
    assert not list(tmp_path.glob("*.promote"))


def test_csv_validation_supports_semicolon_delimiter(tmp_path):
    path = tmp_path / "national_team_order.csv"
    path.write_text(
        "N;Player;Country\n1;Mariana Díaz Oliva;Korea, Rep.\n",
        encoding="utf-8",
    )

    assert pipeline_transaction._validate_csv(path) == 1


def test_http_retries_are_bounded_and_use_timeouts():
    calls = []

    def unavailable(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 503
        response.url = url
        return response

    with patch("http_client.time.sleep"), patch("http_client.random.uniform", return_value=0):
        with pytest.raises(SourceRequestError):
            request_with_retry(
                "GET",
                "https://example.invalid/data",
                component="test-source",
                attempts=3,
                requester=unavailable,
            )

    assert len(calls) == 3
    assert all(call[2]["timeout"] == (10.0, 30.0) for call in calls)


def test_http_does_not_retry_non_retryable_status():
    calls = []

    def not_found(method, url, **kwargs):
        calls.append((method, url, kwargs))
        response = requests.Response()
        response.status_code = 404
        response.url = url
        return response

    with pytest.raises(SourceRequestError) as captured:
        request_with_retry(
            "GET",
            "https://example.invalid/missing",
            component="test-source",
            attempts=4,
            requester=not_found,
        )

    assert len(calls) == 1
    assert captured.value.context["attempts"] == 1


def test_run_state_records_degraded_and_partial_outcomes(monkeypatch):
    with TemporaryDirectory() as directory:
        status_path = Path(directory) / "run.json"
        initialize_run_state(status_path, "test-run", Path(directory) / "stage")
        monkeypatch.setenv("WTARG_RUN_STATUS_PATH", str(status_path))

        record_run_issue("optional-cache", RuntimeError("stale"), severity="degraded")
        assert load_run_state(status_path)["status"] == "degraded"

        record_run_issue("required-source", RuntimeError("missing"), severity="partial")
        state = load_run_state(status_path)
        assert state["status"] == "partial"
        assert [issue["severity"] for issue in state["issues"]] == ["degraded", "partial"]


def test_failure_status_is_included_in_email_report():
    markdown = render_email_markdown(
        {
            "run_status": {
                "run_id": "run-123",
                "status": "partial",
                "finished_at": "2026-08-04T12:00:00Z",
                "promotion": "blocked",
                "issues": [
                    {
                        "component": "itf-loader",
                        "operation": "fetch drawsheet",
                        "message": "source unavailable",
                    }
                ],
            }
        }
    )

    assert "Website not updated — required information was incomplete" in markdown
    assert "Publishing was intentionally blocked" in markdown
    assert "Tournament draws could not be downloaded from the provider" in markdown
    assert "itf-loader / fetch drawsheet: source unavailable" in markdown


def test_degraded_draw_failure_email_says_other_updates_were_published():
    markdown = render_email_markdown(
        {
            "run_status": {
                "run_id": "run-456",
                "status": "degraded",
                "finished_at": "2026-08-04T12:00:00Z",
                "promotion": {"deploy_site_promoted": True},
                "issues": [
                    {
                        "component": "itf-loader",
                        "operation": "fetch drawsheet",
                        "message": "drawsheet blocked with no cached fallback",
                    }
                ],
            }
        }
    )

    assert "## Tournament draws that may be out of date" in markdown
    assert "Ready to publish; the public website is not confirmed yet" in markdown
    assert "The rest of the update passed its checks" in markdown
    assert "The affected draws may be older or missing" in markdown


def test_dataset_swap_rolls_back_when_later_promotion_fails(monkeypatch):
    with TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        production = project / "data"
        staging_root = project / ".run_staging" / "test-run"
        staging_data = staging_root / "data"
        staging_site = staging_root / "generated-site"
        staging_deploy = staging_root / "deploy-site"
        production.mkdir(parents=True)
        staging_data.mkdir(parents=True)
        staging_site.mkdir(parents=True)
        (production / "value.txt").write_text("old", encoding="utf-8")
        (staging_data / "value.txt").write_text("new", encoding="utf-8")

        monkeypatch.setattr(pipeline_transaction, "PROJECT_ROOT", project)
        monkeypatch.setattr(pipeline_transaction, "PRODUCTION_DATA_DIR", production)
        monkeypatch.setattr(
            pipeline_transaction,
            "_promote_site_files",
            lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("site failed")),
        )

        with pytest.raises(DataPromotionError):
            pipeline_transaction._promote_all(
                staging_root,
                staging_data,
                staging_site,
                staging_deploy,
                "test-run",
            )

        assert (production / "value.txt").read_text(encoding="utf-8") == "old"


def test_degraded_refresh_validates_builds_and_promotes(tmp_path, monkeypatch):
    project = tmp_path / "project"
    production = project / "data"
    production.mkdir(parents=True)
    (production / "existing.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(pipeline_transaction, "PROJECT_ROOT", project)
    monkeypatch.setattr(pipeline_transaction, "PRODUCTION_DATA_DIR", production)
    monkeypatch.setattr(pipeline_transaction, "STAGING_PARENT", project / ".run_staging")
    monkeypatch.setattr(pipeline_transaction, "STATE_PARENT", project / ".run_state")
    monkeypatch.setattr(
        pipeline_transaction,
        "LATEST_STATE_PATH",
        project / ".run_state" / "latest.json",
    )
    monkeypatch.setattr(pipeline_transaction, "_new_run_id", lambda: "test-run")

    calls = []

    def run_child(*args, **kwargs):
        calls.append("extract-transform")
        monkeypatch.setenv(
            "WTARG_RUN_STATUS_PATH",
            kwargs["env"]["WTARG_RUN_STATUS_PATH"],
        )
        record_run_issue("itf-loader", RuntimeError("draw unavailable"), severity="degraded")
        return SimpleNamespace(returncode=0)

    def validate_dataset(path):
        calls.append("validate-data")
        return {"valid": True}

    def build_site(path, environment):
        calls.append("build-site")
        path.mkdir(parents=True)

    def validate_site(site_root, deploy_root):
        calls.append("validate-site")
        return {"valid": True}

    def promote(*args):
        calls.append("promote")
        return {"deploy_site_promoted": True}

    monkeypatch.setattr(pipeline_transaction.subprocess, "run", run_child)
    monkeypatch.setattr(pipeline_transaction, "validate_staged_dataset", validate_dataset)
    monkeypatch.setattr(pipeline_transaction, "_build_staged_deploy_site", build_site)
    monkeypatch.setattr(pipeline_transaction, "validate_staged_site", validate_site)
    monkeypatch.setattr(pipeline_transaction, "_promote_all", promote)

    result = pipeline_transaction.run_refresh_transaction(
        ["python", "main.py"],
        include_generated_site=True,
    )

    assert result == 0
    assert load_run_state(project / ".run_state" / "latest.json")["status"] == "degraded"
    assert calls == [
        "extract-transform",
        "validate-data",
        "build-site",
        "validate-site",
        "promote",
    ]


def test_all_ci_jobs_have_timeouts():
    workflow_dir = PROJECT_DIR / ".github" / "workflows"
    for workflow in workflow_dir.glob("*.yml"):
        payload = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        for name, job in payload["jobs"].items():
            assert "timeout-minutes" in job, f"{workflow.name}: {name}"
