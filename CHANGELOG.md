# Changelog

## 0.1.0 (2026-10-26)

First preview: the `/truequery` skill (build, ask, analyze,
metric, review, harden, init, hooks), the offline checker with 25
checks (15 on by default), the receipt, and Claude Code hooks.

- Files over the 10 MB per-file limit are reported as not checked,
  never as clean.
- Pinned dependencies (`pyyaml==6.0.3`; the demo uses
  `duckdb>=1.1,<2`).
- The receipt is in plain words, and names a limit (integer division;
  dbt, incremental-model and Airflow rules) only when it applies.
- The plugin puts `truequery` on the agent's PATH, so the skill always
  runs the plugin's own checker, never an older install or another `tq`.
- Claude Code: a finding shown right after an edit isn't repeated at the
  end of the turn. Hook messages start with `[TrueQuery]`.
- The plugin runs its own engine as an editable install, so the hooks
  and the agent always run the code in the plugin folder, never a stale
  cached build, even after a reinstall at the same version.
- `truequery check` works as another name for `truequery detect`.
- `/truequery review` follows a full review method after the checker:
  read the whole dependency path, audit every house definition against
  the code, check each model for grain, units, time zones, filters and
  silent defaults, and confirm suspected bugs in the data.
- A file of dbt macro definitions is listed as not checked (macro
  definitions are templates, not queries) instead of failing to parse,
  so `truequery detect` on a dbt project no longer exits 3 because of
  `macros/`.
- Windows: a `truequery.cmd` launcher for PowerShell and cmd, and shell
  scripts keep LF line endings in any checkout.
- `truequery context --target` accepts a folder.
- Messages say `truequery`, not `tq`; `rules set --scope user` says when
  a project setting overrides yours.
- Tested on Python 3.11 to 3.14.
- The checker installs from PyPI: `pip install truequery`
  (or `uv tool install truequery`; no install at all with
  `uvx truequery`).
