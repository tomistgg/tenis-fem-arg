import json
import sys

import pytest

from populate_data import update_itf_player_details
from run_state import initialize_run_state, load_run_state, report_run_issue


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
    assert profiles == [
        {
            "playerId": "800789876",
            "displayName": "Example Player",
            "birthYear": None,
            "playHand": "",
            "backHandStyle": "",
            "fetchStatus": "unavailable",
        }
    ]
