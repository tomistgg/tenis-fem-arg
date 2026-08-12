import logging
import os
import subprocess
import sys
from pathlib import Path

import generate_run_report
from runtime_logging import configure_logging, get_logger

PROJECT_DIR = Path(__file__).resolve().parents[1]


def test_default_logging_hides_per_item_debug_details(monkeypatch):
    monkeypatch.delenv("WTARG_LOG_LEVEL", raising=False)
    monkeypatch.delenv("WTARG_VERBOSE", raising=False)

    configure_logging(verbose=False)
    logger = get_logger("test")

    assert logger.isEnabledFor(logging.INFO)
    assert not logger.isEnabledFor(logging.DEBUG)


def test_verbose_logging_enables_debug_details():
    configure_logging(verbose=True)
    logger = get_logger("test")

    assert logger.isEnabledFor(logging.DEBUG)

    # Do not let this test leave the process-wide project logger verbose.
    configure_logging(verbose=False)


def _run_logging_probe(*, verbose):
    environment = os.environ.copy()
    environment.pop("WTARG_LOG_LEVEL", None)
    environment.pop("WTARG_VERBOSE", None)
    if verbose:
        environment["WTARG_VERBOSE"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from runtime_logging import get_logger; "
                "logger = get_logger('probe'); "
                "logger.info('phase summary'); "
                "logger.debug('per-item detail')"
            ),
        ],
        cwd=PROJECT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )


def test_logs_use_stderr_and_keep_stdout_clean():
    result = _run_logging_probe(verbose=False)

    assert result.stdout == ""
    assert "INFO wtarg.probe: phase summary" in result.stderr
    assert "per-item detail" not in result.stderr


def test_verbose_environment_shows_debug_details():
    result = _run_logging_probe(verbose=True)

    assert "DEBUG wtarg.probe: per-item detail" in result.stderr


def test_run_report_logs_its_path_instead_of_full_markdown(monkeypatch, tmp_path):
    output = tmp_path / "run-report.md"
    logged = []
    monkeypatch.setattr(generate_run_report, "compute_report", lambda before, after: {})
    monkeypatch.setattr(generate_run_report, "render_markdown", lambda report: "full report body")
    monkeypatch.setattr(generate_run_report.logger, "info", lambda *args: logged.append(args))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_run_report.py",
            "--before",
            str(tmp_path / "before"),
            "--after",
            str(tmp_path / "after"),
            "--output",
            str(output),
        ],
    )

    generate_run_report.main()

    assert output.read_text(encoding="utf-8") == "full report body"
    assert logged == [("Run report written to %s", str(output))]
