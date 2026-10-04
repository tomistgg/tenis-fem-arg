# Testing policy

Do not add tests by default.

Add or modify tests only when a change affects data integrity, destructive operations, security, complex business logic, parsers, external API boundaries, or a confirmed bug likely to regress.

For text, labels, styling, configuration, straightforward refactors, and other reversible low-impact changes:

- Do not add tests that merely repeat the implementation.
- Run existing focused checks when useful.
- Do not run the full suite unless the change is cross-cutting or risky.

If uncertain whether a new test is valuable, do not add it.

# Python lint policy

Before finishing a Python code change, run Ruff on every changed Python file and fix any reported errors. Use the project's Ruff configuration in `pyproject.toml`, including its 120-character line limit. For example: `.venv\Scripts\ruff.exe check --force-exclude <changed Python files>` on Windows.

# WTN scope

WTN checks and profile lookups must concern singles players only. Never search for doubles WTNs or trigger WTN profile lookups because a player appears in a doubles draw.
