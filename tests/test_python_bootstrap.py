import pytest

import python_bootstrap


def test_failed_restart_has_actionable_instructions(monkeypatch, tmp_path):
    monkeypatch.setattr(python_bootstrap, "_environment_is_ready", lambda required: False)
    monkeypatch.setenv("WTARG_ENV_BOOTSTRAPPED", "1")
    with pytest.raises(SystemExit, match=r"py -3\.11 -m venv \.venv"):
        python_bootstrap.ensure_project_environment(tmp_path, required_imports=("missing_fixture_package",))
