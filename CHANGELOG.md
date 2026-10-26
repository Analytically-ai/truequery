# Changelog

## 0.1.0 (unreleased)

First preview: the `/truequery` skill (build, ask, analyze,
metric, review, harden, init, hooks), the offline checker with 25
checks (15 on by default), the receipt, and Claude Code hooks.

- Files over the 10 MB per-file limit are reported as not checked,
  never as clean.
- Pinned dependencies (`pyyaml==6.0.3`; the demo uses
  `duckdb>=1.1,<2`).
- The checker installs from PyPI: `pip install truequery`
  (or `uv tool install truequery`; no install at all with
  `uvx truequery`).
