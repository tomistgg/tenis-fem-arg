# Tenis Fem Arg

Data ingestion and static-site generation for Argentine women's tennis. The
repository refreshes WTA/ITF data, validates the canonical data tables, builds
`app.html`, and prepares the GitHub Pages artifact.

## Supported environment

- Python 3.11 only (`.python-version` pins CI and compatible version managers to
  Python 3.11.15).
- Windows, macOS, or Linux for validation and static-site builds.
- Google Chrome is required for the live scraping/update workflow.

The Python package versions and their distribution hashes are committed in
`requirements.lock`. Always install that file; do not install dependencies from
an unpinned `pip install <package>` command.

## Bootstrap

Create the environment with Python 3.11.15. On Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python --version
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m pip install --no-deps --no-build-isolation --editable .
python -m pre_commit install
```

On macOS or Linux:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python --version
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m pip install --no-deps --no-build-isolation --editable .
python -m pre_commit install
```

`python --version` must report Python 3.11.x; CI uses exactly 3.11.15. The
editable install uses only the already locked build tools and does not resolve
or download additional dependencies.

## Reproducible checks

Run the same checks used by CI:

```text
python -m ruff check .
python -m mypy
python -m pytest
python -m pre_commit run --all-files
python -m pip_audit --require-hashes -r requirements.lock
python data_quality.py validate --data-dir data --site-root .
```

Ruff currently enforces high-confidence correctness rules across the legacy
codebase. Mypy starts with the canonical-data and time-handling boundary; both
scopes can be tightened incrementally without hiding existing failures.

## Run the project

Build the deployable static site from the committed data:

```text
python build_deploy_site.py --output .site
```

Historical WTA ranking CSVs are indexed lazily by week. Python keeps a small
byte-offset index and an eight-week row cache instead of materializing the full
2.1-million-row archive as dictionaries. Site generation emits
`wta_rankings_latest_bundle.js` for the initial rankings view and one
`wta_rankings_<year>_bundle.js` file per historical year; the browser fetches a
year only when the user selects one of its weeks. Match history is emitted only
as `history_data_bundle.js`, which is the format consumed by the browser.

Validate canonical player, ranking, match, and tournament data:

```text
python data_quality.py validate --data-dir data --site-root . --report .run_state/data-quality.json
```

The blocking gate validates critical JSON with committed JSON Schemas and
Pydantic models, validates ranking and match CSVs with Pandera, enforces
canonical unique keys and referential integrity, and checks freshness and
minimum row counts. `data_quality_policy.json` contains the reviewed limits.
When comparing a refresh with the previous dataset, pass
`--baseline-dir <snapshot>/data`; the gate then also blocks excessive row drops
or row-count changes. Raising a limit requires a reviewed policy change rather
than an ad hoc workflow exception.

Run the complete live refresh:

```text
python main.py
```

Normal runs show phase summaries and warnings. Add `--verbose` to include
per-tournament, cache, and retry diagnostics:

```text
python main.py --verbose
```

Logs are written to stderr so machine-readable command output can remain on
stdout. `WTARG_VERBOSE=1` provides the same verbose logging for maintenance
scripts that are run directly.

`main.py` automatically restarts with `.venv` when the default `python`
command points at an unsupported or incomplete interpreter. Confirm the
automatic selection without starting a refresh:

```text
python main.py --check-environment
```

The live refresh is transactional. It copies the current dataset into a unique
`.run_staging/<run-id>/` directory, routes the preflight subprocesses and site
generator to that copy, runs the complete blocking quality gate, validates the
generated pages and deploy artifact, and only then promotes the dataset and
generated site. Directly running one of the loader scripts uses the same
staging mechanism.

The final machine-readable status is written atomically to
`.run_state/latest.json` and is one of:

- `success`: validation and promotion completed without source gaps.
- `degraded`: a documented stale-cache fallback was used, but the staged
  dataset passed validation and was promoted.
- `partial`: a required source result is missing; promotion is blocked and the
  staging directory is retained for diagnosis.
- `failed`: execution, validation, timeout, or promotion failed; production is
  unchanged or rolled back and staging is retained.

While a refresh is active its status is `running`. Human-facing reports keep
these stable machine values in the technical details, but present them as
“Update completed successfully,” “Update completed with warnings,” or
“Website not updated,” followed by what went wrong, what information was
affected, and what happens next. Repeated source errors are grouped instead of
being printed one by one.

`success` and `degraded` mean that a checked website package is ready for
publishing; they do not by themselves prove that the public website changed.
The workflow sends its final notification after the separate GitHub Pages job,
so the report can accurately say whether the new version is live, publishing
failed, or the previous version remains online. `partial` and `failed` runs are
never sent to Pages.

The live refresh uses external websites, requires Chrome and network access,
and modifies data and generated site files only after promotion. External
source responses and the current browser release mean the retrieved data itself
is not deterministic even though the Python environment is locked.

## Change dependencies

Edit the exact direct dependency versions in `pyproject.toml`, then regenerate
the universal Python 3.11 lock from the existing development environment:

```text
python -m uv pip compile pyproject.toml --extra dev --python-version 3.11.15 --universal --generate-hashes --output-file requirements.lock
python -m pip install --require-hashes -r requirements.lock
python -m pip check
python -m pip_audit --require-hashes -r requirements.lock
```

Commit `pyproject.toml` and `requirements.lock` together. Review dependency
changes and the audit result before merging.

## Automation

- `Quality` runs for pushes and pull requests. It compares data with the PR base
  commit, runs the blocking gate, Ruff, mypy, pytest, pre-commit, and
  `pip-audit`.
- `Refresh and Deploy` is the only publishing workflow. On pushes to `main`, on
  its two-hour schedule, or when manually started, it checks out the triggering
  source revision and never rebases it. It restores the last validated snapshot
  from the dedicated `data-state` branch, overlays data files changed by a
  source push, then runs extract, transform, data validation, one `.site` build,
  exact-artifact validation, artifact upload, data publication, and Pages
  deployment in that order. This keeps the validated snapshot as refresh state
  without discarding pushed match, entry-list, or tracker data.
- The uploaded Pages artifact is the same immutable `.site` directory validated
  by the refresh transaction; Pages does not rebuild it. A successful run saves
  only `data/` to `data-state` and mirrors that validated `data/` tree in a new
  commit on `main`, so a normal `git pull` receives generated data updates.

The workflow uses GitHub's short-lived `GITHUB_TOKEN`: `contents: write` is
limited to the refresh job for updating `data-state` and `main`, while the deploy
job gets only `pages: write` and `id-token: write`. Pushes made with that token do
not recursively start another workflow run. No PAT or ImageKit secrets are
needed.

Parser tests use saved WTA, ITF, BJKC, and PDF-text fixtures and never call the
network. Browser tests skip locally when Chrome or `chromedriver` is absent; CI
sets `WTARG_REQUIRE_BROWSER_TESTS=1`, which turns missing browser tooling into a
hard failure.

All workflows use the same lock file and Python version, cache downloads using
the lock-file digest, pin third-party actions to full commit SHAs, and have
job-level timeouts. HTTP source calls use bounded retry/backoff policies with
jitter and explicit connect/read timeouts.
