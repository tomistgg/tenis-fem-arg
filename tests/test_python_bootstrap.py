import sys

import pytest

import python_bootstrap


def test_ready_environment_does_not_restart(monkeypatch, tmp_path):
    monkeypatch.setattr(python_bootstrap, "_environment_is_ready", lambda required: True)
    monkeypatch.setattr(python_bootstrap.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected restart"))
    python_bootstrap.ensure_project_environment(tmp_path)


def test_incomplete_environment_restarts_with_project_venv(monkeypatch, tmp_path):
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"fixture")
    captured = {}

    def fake_run(arguments, *, env, check):
        captured.update(arguments=arguments, environment=env, check=check)
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(python_bootstrap, "_environment_is_ready", lambda required: False)
    monkeypatch.setattr(python_bootstrap, "_same_interpreter", lambda left, right: False)
    monkeypatch.delenv("WTARG_ENV_BOOTSTRAPPED", raising=False)
    monkeypatch.setattr(python_bootstrap.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["main.py", "--check-environment"])

    with pytest.raises(RuntimeError, match="exec intercepted"):
        python_bootstrap.ensure_project_environment(tmp_path)

    assert captured["arguments"] == [str(interpreter), "main.py", "--check-environment"]
    assert captured["environment"]["WTARG_ENV_BOOTSTRAPPED"] == "1"
    assert captured["check"] is False


def test_failed_restart_has_actionable_instructions(monkeypatch, tmp_path):
    monkeypatch.setattr(python_bootstrap, "_environment_is_ready", lambda required: False)
    monkeypatch.setenv("WTARG_ENV_BOOTSTRAPPED", "1")
    with pytest.raises(SystemExit, match=r"py -3\.11 -m venv \.venv"):
        python_bootstrap.ensure_project_environment(tmp_path, required_imports=("missing_fixture_package",))
