import json
import sys
from datetime import UTC, datetime, timedelta

import pytest

from populate_data import update_itf_player_details
from run_state import initialize_run_state, load_run_state, report_run_issue
from time_utils import utc_now


def test_optional_profile_fetch_is_configured_as_a_degraded_warning(monkeypatch):
    request_options = {}

    def unavailable_profile(_browser, _url, **kwargs):
        request_options.update(kwargs)
        return None

    monkeypatch.setattr(update_itf_player_details.itf, "_fetch_itf_json", unavailable_profile)

    with pytest.raises(ValueError, match="Unexpected ITF profile response"):
        update_itf_player_details._profile_row(None, "800789876", "Example Player")

    assert request_options["failure_severity"] == "degraded"
    assert request_options["failure_component"] == "itf-player-profile"
    assert request_options["failure_operation"] == "fetch player profile"


def test_profile_fetch_failure_warns_and_finishes_without_failing(monkeypatch, tmp_path):
    status_path = tmp_path / "run-status.json"
    output_path = tmp_path / "itf_player_details.json"
    initialize_run_state(status_path, "test-run", tmp_path / "stage")
    monkeypatch.setenv("WTARG_RUN_STATUS_PATH", str(status_path))
    monkeypatch.setattr(update_itf_player_details, "OUTPUT_PATH", output_path)
    monkeypatch.setattr(
        update_itf_player_details,
        "players_with_recent_matches",
        lambda: [("Example Player", "800789876")],
    )

    def unavailable_profile(_browser, _url, **kwargs):
        report_run_issue(
            kwargs["failure_component"],
            kwargs["failure_operation"],
            RuntimeError("unexpected XML response"),
            severity=kwargs["failure_severity"],
        )
        return None

    monkeypatch.setattr(update_itf_player_details.itf, "_fetch_itf_json", unavailable_profile)
    monkeypatch.setattr(sys, "argv", ["update_itf_player_details.py", "--delay", "0"])

    update_itf_player_details.main()

    state = load_run_state(status_path)
    assert state["status"] == "degraded"
    assert state["issues"][0]["severity"] == "degraded"
    assert state["issues"][0]["component"] == "itf-player-profile"
    profiles = json.loads(output_path.read_text(encoding="utf-8"))
    assert len(profiles) == 1
    assert profiles[0]["playerId"] == "800789876"
    assert profiles[0]["fetchStatus"] == "unavailable"
    assert profiles[0]["consecutiveFailures"] == 1
    assert datetime.fromisoformat(profiles[0]["retryAfter"]) > utc_now()


def test_unavailable_profiles_retry_when_due_without_refetching_successes(monkeypatch, tmp_path):
    output_path = tmp_path / "itf_player_details.json"
    output_path.write_text(
        json.dumps(
            [
                {"playerId": "800000001", "displayName": "Legacy", "fetchStatus": "unavailable"},
                {
                    "playerId": "800000002",
                    "displayName": "Waiting",
                    "fetchStatus": "unavailable",
                    "retryAfter": (utc_now() + timedelta(days=2)).isoformat(),
                },
                {"playerId": "800000003", "displayName": "Complete", "birthYear": 2000},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(update_itf_player_details, "OUTPUT_PATH", output_path)
    monkeypatch.setattr(
        update_itf_player_details,
        "players_with_recent_matches",
        lambda: [("Legacy", "800000001"), ("Waiting", "800000002"), ("Complete", "800000003")],
    )
    requested = []

    def fetch_profile(_browser, player_id, name):
        requested.append(player_id)
        return {"playerId": player_id, "displayName": name, "birthYear": 2001}

    monkeypatch.setattr(update_itf_player_details, "_profile_row", fetch_profile)
    monkeypatch.setattr(sys, "argv", ["update_itf_player_details.py", "--delay", "0"])

    update_itf_player_details.main()

    assert requested == ["800000001"]
    rows = {row["playerId"]: row for row in json.loads(output_path.read_text(encoding="utf-8"))}
    assert rows["800000001"]["birthYear"] == 2001
    assert rows["800000002"]["fetchStatus"] == "unavailable"
    assert rows["800000003"]["birthYear"] == 2000


def test_failed_profile_retries_back_off_up_to_seven_days():
    now = datetime(2026, 10, 1, tzinfo=UTC)
    previous = None
    for failures, days in [(1, 1), (2, 2), (3, 4), (4, 7), (5, 7)]:
        previous = update_itf_player_details._unavailable_profile_row("800000001", "Example", previous, now)
        assert previous["consecutiveFailures"] == failures
        assert datetime.fromisoformat(previous["retryAfter"]) == now + timedelta(days=days)
        assert not update_itf_player_details._retry_due(previous, now)
        assert update_itf_player_details._retry_due(previous, now + timedelta(days=days))
