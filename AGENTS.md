# Testing policy

Do not add tests by default.

Add or modify tests only when a change affects data integrity, destructive operations, security, complex business logic, parsers, external API boundaries, or a confirmed bug likely to regress.

For text, labels, styling, configuration, straightforward refactors, and other reversible low-impact changes:

- Do not add tests that merely repeat the implementation.
- Run existing focused checks when useful.
- Do not run the full suite unless the change is cross-cutting or risky.

If uncertain whether a new test is valuable, do not add it.
