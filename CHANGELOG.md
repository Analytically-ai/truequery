# Changelog

## 0.1.4 (2026-11-03)

The queries inside wrappers and SQL function bodies are checked now.

- **Queries inside wrappers are checked.** 0.1.3 listed `EXPLAIN …`,
  `PREPARE … AS …`, `DECLARE … CURSOR FOR …` and `CREATE TABLE`/
  `[MATERIALIZED] VIEW … AS …` as not checked when the SQL parser could
  read them only as an opaque command. Now the query inside is found,
  parsed and checked, with findings at their own line and column
  (`EXPLAIN (ANALYZE, COSTS OFF)` and other option lists included). A
  file that held only those now says checked, and may report findings
  (exit 1). A wrapper whose query isn't found, or doesn't parse, stays
  not checked, as before.
- **SQL function and procedure bodies are checked.** The statements in a
  `LANGUAGE sql` body (`$$…$$`, `$tag$…$tag$`, or a single-quoted body
  with `''` escapes; Snowflake SQL functions too) get every check, and a
  finding points at its line and column in the file. Before 0.1.4, a SQL
  function whose body was one statement was counted as checked and clean
  without anything looking inside it; a body of several statements didn't
  parse at all. BigQuery function bodies were already part of the parse
  and are still checked.
- **Some files say "not checked (procedural code)".** TrueQuery doesn't
  look inside procedural code yet: PL/pgSQL functions and `DO` blocks,
  Snowflake Scripting and T-SQL `BEGIN … END` blocks. A file with one is
  listed as not checked, with that reason (exit 3 with `detect`, unless
  `--allow-unverified`), never as clean. Most such files already said
  "not checked (could not parse)", because the parser cut the body at its
  first `;`; now the reason is the true one. The statements inside a
  procedural body that parse on their own as SQL are still checked, and
  their findings still report. In Python files the SQL is listed as SQL
  that doesn't parse; that never causes exit 3.
- Bodies in another language (`LANGUAGE python`, `javascript`, `c`, …)
  hold no SQL; they are treated as before.
- Statements are split correctly around dollar-quoted strings: a `;`
  inside `$$…$$` no longer ends the statement.
- The receipt names the reason: `1 file with procedural code (PL/pgSQL,
  BEGIN … END), not looked inside yet: f.sql`. JSON adds
  `receipt.not_checked.procedural_files` and `unparsed_files` (the files
  of `unverified_files`, by reason); nothing was removed or renamed.
- Running `truequery` with no command now suggests `truequery detect`,
  not `tq detect`. Help and messages that said `tq install`, `tq rules`
  and `tq init` say `truequery` too.

## 0.1.3 (2026-11-01)

No false "clean" on statements the parser can only read as a command, and
three false alarms fixed.

- **Some files that said "clean" now say "not checked"** (exit 3 with
  `detect`, unless `--allow-unverified`). For syntax it doesn't support,
  the SQL parser doesn't fail: it keeps the statement as an opaque command,
  and no check can look inside one. Before 0.1.3 such a file was reported
  clean. Now a command that holds a query or a data change (`EXPLAIN`,
  `PREPARE … AS`, `DECLARE … CURSOR FOR`, `DO`, `CREATE TABLE`/`VIEW`/
  `FUNCTION`/`RULE`/`TRIGGER`, or a statement TrueQuery doesn't recognise)
  is a parse failure like any other: the file is listed as not checked,
  with the line. The rest of the file is still checked, and its findings
  still report. In Python files the fragment is listed as SQL that doesn't
  parse; that never causes exit 3.
- Session and admin statements that no check is about (`SET`, `RESET`,
  `SHOW`, `GRANT`, `REVOKE`, `VACUUM`, `ANALYZE`, `COMMENT ON`, `ALTER …`,
  `DROP …`, `CREATE INDEX`/`STATISTICS`/`EXTENSION`/`ROLE`/`TYPE`, …) are
  unchanged: nothing was skipped, so they stay checked.
- The parser's own warning ("… contains unsupported syntax. Falling back to
  parsing as a 'Command'.") no longer appears in your terminal or in hook
  output. The checker's output is TrueQuery's only.
- **Fewer false alarms:**
  - `sql/insert-explicit-column-list` no longer fires on
    `INSERT INTO t DEFAULT VALUES`, which takes no column list.
  - `sql/accidental-cross-join` no longer fires on a linked Postgres
    `UPDATE t … FROM a JOIN b ON …` or `DELETE FROM t USING a JOIN b ON …`
    (or MySQL `UPDATE a JOIN b ON … SET …`). The join's other side and the
    `WHERE` against the target count as links now. A source that links to
    nothing still reports.
  - `sql/row-number-dedupe-tiebreaker` no longer fires on a single
    `ORDER BY` key that is the `WITH ORDINALITY` number of the one source
    the query reads, named or the default `ordinality`. Those numbers are
    unique, so there are no ties. It still reports when a join or a
    `LATERAL` over another table can repeat the number.

## 0.1.2 (2026-11-01)

The SQL inside Python files is checked now, not just listed.

- SQL strings passed to `execute`, `executemany`, `executescript`,
  `exec_driver_sql`, `read_sql`, `read_sql_query`, SQLAlchemy `text()`,
  `.sql()` (Spark, DuckDB) or a `sql=` keyword get every SQL check a `.sql`
  file gets. That covers plain and triple-quoted strings, implicit and `+`
  concatenation, a query held in a same-file variable (each value when
  `if`/`else` assigns several), f-strings, `.format()`, `%` and psycopg's
  `sql.SQL("...")`. Placeholders become neutral names, and driver
  parameters (`?`, `:name`, `$1`, `%s`, `%(name)s`) parse.
- **New findings:** Python files that were listed as not checked in 0.1.1
  can now report findings (and exit 1). In the 0.1.1 repro, the 5 Python
  files with `= NULL` now report 5 errors.
- Findings point at the Python file, line and column where the SQL is
  written (for a variable, where the string is assigned), with the SQL line
  as the snippet. `truequery ignore add` works on a Python line, and the
  ignore stays attached when lines above it move.
- The dialect for Python SQL comes from `--dialect`, then DATA.md, then the
  file's imports (`sqlite3`, `duckdb`, `psycopg`/`psycopg2`/`asyncpg`,
  `pymysql`/`mysql.connector`/`MySQLdb`, `pyspark`, `snowflake.connector`/
  `snowflake.snowpark`, `google.cloud.bigquery`), then auto.
- The receipt counts the Python SQL it checked
  (`SQL in 5 Python files (31 queries)`) and names what it couldn't:
  `SQL built at run time in …` and `SQL that doesn't parse in …`. A file
  can be both counted and listed. Neither ever causes exit 3: finding SQL
  in Python is a heuristic, so text like `text("Select an option")` is
  listed, not treated as a broken SQL file.
- JSON adds `python_sql` (per file: `checked`, `static`, `templated`,
  `dynamic`, `unparsed`, and where the unchecked SQL is) and
  `receipt.not_checked.embedded_sql_reasons`; `embedded_sql_files` still
  lists every file with SQL that wasn't checked. Nothing was removed or
  renamed.
- Hooks: editing a Python file with SQL inside is checked like a SQL edit;
  editing one without SQL stays silent. The end-of-turn pass includes the
  Python files touched.
- An Airflow DAG's SQL is checked too; the DAG still counts as a project
  file.

## 0.1.1 (2026-10-31)

Honest counts: the checker only counts files a check actually applies to.

- A `.py` file counts as a project file only when it's an Airflow DAG, and
  a YAML file only when it's a dbt schema/sources file or `dbt_project.yml`.
  Plain app code and other YAML files no longer inflate the "checked" count.
- SQL inside Python files (at `execute`, `read_sql`, `text()`, `.sql()` and
  `sql=` call sites, including a query held in a same-file variable) is
  listed as not checked yet, in the receipt and in the summary line, and
  never as clean. The SQL inside isn't parsed or checked yet; that's a
  later feature. An Airflow DAG that holds SQL stays counted as a checked
  project file and is also listed as not checked.
- SQLite joins the tested synthetic dialects (`INSERT OR REPLACE`,
  `strftime`, `datetime('now')`, `||`, `LIMIT … OFFSET`, `typeof()`,
  `AUTOINCREMENT`), alongside Snowflake and Redshift.
- The SARIF driver name is `truequery` (was `tq`), and so is the JSON
  report's top-level `"tool"` field.

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
