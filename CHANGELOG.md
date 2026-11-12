# Changelog

## Unreleased

- A second contract for the same table is now reported as a duplicate, and its
  keys are no longer used.
- Six checks read your contracts and metric files: a status compared to an
  undeclared value, expected absences counted as missing, units or
  currencies combined without a conversion, time zones or path bases compared
  without one, and a metric computed differently from `metrics/`. They're off
  by default until they're measured on real projects.
- If a contract or metric file changes during an agent session (from the
  session's first check on), the hook says so once per change, and the
  receipt names the file. The agent is told to confirm the change with you.
- **The contract checks reach pandas code.** A DataFrame read from a file or
  table a contract describes (a literal path matching its location, or
  `read_sql` of one contracted table) is checked against the contract: a
  status compared to an undeclared value, expected absences counted as
  missing, units combined, text timestamps in different zones compared, and
  a wrong folder put in front of a path. Three new pandas checks: a
  contracted CSV read without a `dtype` for its string key (`007` becomes
  7), `fillna(0)` on a money column that may be empty, and a write that
  contradicts the output contract's `write_mode`. All off by default. See
  "Checks that read your contracts" in `docs/checker.md` for what links a
  DataFrame to a contract.
- The standard has 234 practices (was 225) and the checker 41 checks (was 32).
- **`truequery verify` checks your data against your contracts.** It reads
  the local files (CSV, JSONL, JSON, Parquet) and SQLite or DuckDB database
  files a contract points to, read-only, and says in plain words whether the
  data matches: keys unique and filled, statuses declared, timestamps in the
  declared zone, paths under the declared folder, files present where the
  status says so (expected absences are counted, not flagged), sizes in the
  declared unit, freshness. Failures say what's wrong and what to do. Each
  run is logged to `.truequery/evidence.jsonl` (moved to `.1` past 5 MB;
  git-ignored). Exit codes: 0 verified, 1 the data breaks a contract, 2 a
  usage error, 3 some data couldn't be verified. See `docs/data.md`.
- `truequery review --data` adds `Verified     against the data` lines to
  the receipt, and data failures under `Found`. Without `--data`, the
  receipt's data line now says whether any contract points at local data
  and the command that would read it (it used to say TrueQuery never reads
  data).
- **The data reader is optional.** SQLite needs nothing extra. Data files
  and DuckDB databases need DuckDB 1.2.0 or later: `pip install
  "truequery[data]"`, or `truequery setup data`, which picks the right
  command for how TrueQuery was installed (pip, uv tool, pipx, uvx or the
  Claude Code plugin) and runs it only with `--yes`. `--no` records that the
  project doesn't want it. The answer is kept in `DATA.md`
  (`setup: data: true|false`; the `tq-config` block now has a JSON Schema).
  Without the reader, `verify` checks what it can and names the rest;
  `--require data` makes that fail in CI.
- `truequery status` says whether the data reader is installed, whether this
  project needs it, and the command.
- The skill runs `review --data` at the receipt step when a contract points
  at local data, and `/truequery init` offers the data reader when the
  project has data files.
- **`truequery init --from <files or folders>` drafts contracts from data
  files that already exist.** It reads CSV and JSONL with Python's standard
  library and Parquet with the data reader (without it, Parquet is listed as
  not inferred), and writes version 2 contracts marked `status: draft` with
  what they were inferred from: datasets from the folder layout (globs,
  partition folders), column types (an ID with leading zeros stays text),
  time zones (naive or mixed timestamps become questions), candidate keys,
  status values (`means: TODO` next to a path column), units from column
  names, and path bases declared in `DATA.md`. Every fact read from the
  sample that needs a person (a status's meaning, a zone, a unit, a values
  list, a column some files lack) is a draft column with a question.
  Columns that look like personal data (by name, a person's role such as
  `assignee`, or values that look like emails, phone numbers or names) get
  `pii: true`, and their values are never written. It never writes over a
  contract, refuses paths outside the project or a declared base, can't be
  combined with `--force`, and every draft passes `truequery contract
  validate`. The
  same files give the same bytes. It doesn't read READMEs or data
  dictionaries yet. See `docs/pipelines.md`.
- **`truequery scaffold <contract>` writes a pipeline's boundaries in plain
  pandas:** a typed reader (a dtype for every column, only empty fields
  missing, timestamps parsed in the declared zone), an output check (keys,
  types, accepted values, time zones) and pytest tests on synthetic rows.
  A CSV row with more fields than the header raises instead of shifting
  the columns.
  The code never imports TrueQuery, and files aren't written over without
  `--force`.
- Contracts: a column can be `status: draft` (only in a draft contract),
  and a CSV location can name its `delimiter` (`,` `;` tab or `|`), which
  `truequery verify` now uses. `verify` matches the columns of a dataset's
  CSV and Parquet files by name, so files that lack a column or order them
  differently are read.
- `/truequery init` runs `init --from` when the project has data files and
  no contracts, then asks only what it couldn't settle; `/truequery build`
  scaffolds the boundaries before writing the transform.

## 0.1.7 (2026-11-07)

- An invalid contract no longer hides a finding. Its keys used to suppress
  `sql/count-star-after-left-join`, while the receipt listed the contract as
  invalid. **Heads-up:** a project with an invalid contract may see new
  findings.
- Four project rules (`model/unique-key-merge-key`,
  `dq/uniqueness-key-no-duplicates`, `ingest/declare-source-freshness`,
  `orch/explicit-catchup-policy`) no longer print their rule name twice.
- The "rule settings changed" notice now starts with `[TrueQuery]`.
- The PyPI page's check counts are corrected.
- Windows logs in the GitHub Action show `—` and `·` correctly.

## 0.1.6 (2026-11-04)

pandas pipelines are checked too.

- **pandas checks.** A `.py` file that imports pandas is read with
  Python's own parser (pandas is never imported, the code never runs) for
  pandas code that runs without an error and gives the wrong data. On by
  default:
  - `py/pandas-no-chained-assignment` (error, on every edit): a write
    through a chain (`df[mask]["col"] = v`, `df["col"][mask] = v`,
    `df.query(...)["col"] = v`) or an in-place method on a column
    (`df["col"].fillna(0, inplace=True)`). Under pandas 3 it never
    reaches the DataFrame;
  - `py/pandas-no-row-loop-mutations` (error): a write to the row of an
    `iterrows()` loop that is then thrown away;
  - `py/pandas-groupby-first-mixes-rows` (warning): `groupby(...).first()`
    / `.last()`, which can build one row from values of different rows;
  - `py/pandas-dedupe-unstable-sort` (warning): `sort_values` on one
    column, the default unstable sort, deciding which row
    `drop_duplicates` keeps (the pandas twin of
    `sql/row-number-dedupe-tiebreaker`);
  - `py/pandas-to-sql-replace` (warning): `to_sql(if_exists="replace")`,
    which drops the table's keys, constraints and column types.

  Off by default, because they fire on most code and depend on data the
  checker can't see: `py/pandas-merge-validate` (`merge`/`join` with no
  `validate=`) and `py/pandas-groupby-dropna` (`groupby` with no
  `dropna=`). Turn them on with `truequery rules set`.
- **What counts as a DataFrame.** A name the file builds from pandas
  (`pd.read_*`, `pd.DataFrame(...)`, `pd.concat`, `pd.merge`, a method of
  another known DataFrame) or annotates as `pd.DataFrame`. Anything else
  isn't reported on.
- **Receipt and JSON.** A file the pandas checks read is counted as
  checked, with its own receipt entry (`Checked … · pandas in 4 Python
  files`) and a `Not checked` line for what they can't follow. JSON adds
  `pandas_files` (and `pandas_files` / `evidence.pandas` in the receipt);
  nothing is renamed and `schema_version` stays 1. A `.py` file that
  imports pandas is now listed among `files` even with no SQL in it.
- **Hooks, ignores and settings.** An edit to a `.py` file that imports
  pandas is checked like a SQL edit; the end-of-turn pass includes it.
  `truequery ignore add` and `truequery rules set` work for the new rules
  as for the SQL rules; an ignore follows its statement when lines above
  it move.
- **Proofs in pandas.** Each pandas rule has a proof (bad code, wrong
  result; fixed code, right result) that the tests run with pandas 3.1.0
  and numpy 2.4.6. pandas is a test dependency only, never a runtime one.
- **Measured first.** Over 13 public pandas pipeline projects (1,088 files,
  572 findings labelled by hand), the 5 checks on by default found 25,
  with 1 false alarm. Re-running the SQL-in-Python corpus moved none of
  its SQL findings. Numbers in [Evidence and limits](docs/evidence.md).
- **The standard** has 225 practices (5 new pandas records;
  `py/pandas-no-row-loop-mutations` is now about lost writes, not speed).

## 0.1.5 (2026-11-04)

Run the checker in pre-commit and GitHub Actions.

- **pre-commit hooks.** Add `Analytically-ai/truequery` (`rev: v0.1.5`) to
  `.pre-commit-config.yaml`: `truequery` checks staged `.sql` files, and
  `truequery-python` checks `.sql` and `.py` files (for SQL inside Python
  and Airflow DAGs). Both respect the checker's exit codes (`0` clean, `1`
  findings at or above `--fail-on`, `3` some files could not be checked;
  `args: [--allow-unverified]` turns `3` into `0`).
- **GitHub Action.** `uses: Analytically-ai/truequery@v0.1.5` runs
  `truequery review` (or `detect`) in an isolated virtual environment,
  writes the findings and the TrueQuery receipt to `$GITHUB_STEP_SUMMARY`
  with no extra token permissions, and can write a SARIF 2.1.0 report for
  GitHub Code Scanning. See [`docs/ci-and-pre-commit.md`](docs/ci-and-pre-commit.md).

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
- **Muse Code is supported,** with the same plugin as Claude Code:
  `muse plugins marketplace add analytically https://github.com/Analytically-ai/truequery`,
  `muse plugins install truequery@analytically`, `muse plugins approve
  truequery`. Verified in live Muse sessions: findings reach the agent
  after each edit, and the end-of-turn check continues the turn when it
  finds something new.
  - SQL the agent writes from Muse's shell (`> q.sql`, `>> q.sql`,
    `| tee q.sql`) is checked like an edit.
  - Muse's helper agents (its reminder agent) no longer reset what the
    session has touched, and each finding is said once per session.
  - Events that touch no SQL file are skipped before any session state is
    read or written, in every agent.

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
