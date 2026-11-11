# The checker

The checker (`truequery`, short alias `tq`) is the part of TrueQuery that
reads your code and looks for SQL that silently returns the wrong answer.
The `/truequery` skill runs it for the agent; you can also run it on its own
in a terminal or CI. Back to the [README](../README.md).

## What it finds

```
$ truequery detect samples/bad_orders.sql
samples/bad_orders.sql:7:19 [error] sql/accidental-cross-join — no predicate links o: every row pairs with every row on the other side
    fix: add ON <keys>, or CROSS JOIN if cartesian is intended
samples/bad_orders.sql:13:7 [error] sql/not-in-nullable-subquery — a NULL in the subquery makes NOT IN return zero rows
    fix: rewrite as NOT EXISTS, or add WHERE col IS NOT NULL
samples/bad_orders.sql:17:14 [error] sql/count-star-after-left-join — COUNT(*) over an outer join counts 1 for unmatched rows
    fix: count the nullable-side key instead, e.g. COUNT(o.<key>)
...
5 errors, 2 warnings in 1 file checked offline
```

`truequery review` adds the receipt. Its `Not checked` lines are the
point: they name what this pass could not verify. Here that's the data
itself; a limit such as integer division is listed only when a file
could hit it.

```
TrueQuery receipt
Checked      1 SQL file · contracts for 0 of 3 tables
Found        5 errors · 2 warnings
Not checked  your data: no contract points at a local data file, so none was read (remote databases: not until 0.3)
No contract  customers, orders, staging_orders
```

When a contract points at local data files or a SQLite or DuckDB database,
`truequery review --data` also reads that data, read-only, and adds a
`Verified     against the data` line: see [Checking your data](data.md).

## What it promises

- **Offline and deterministic.** No LLM in the check path and no network
  calls. Your code never leaves your machine.
- **Never a false "clean".** A file it couldn't parse, a statement it
  could read only as an opaque command, or a function body in procedural
  code (PL/pgSQL, `BEGIN … END`) is listed as not checked, and the exit
  code says so. The query inside `EXPLAIN`, `PREPARE`, `DECLARE … CURSOR`
  and `CREATE … AS`, and the statements in a SQL function body, are
  checked.
- **Checks the SQL inside Python too.** SQL strings passed to `execute`,
  `read_sql`, `text()`, `.sql()` or `sql=` (including f-strings,
  `.format()`, `%` and a query held in a same-file variable) get the same
  checks as `.sql` files, and findings point at the Python line and column.
  SQL your program builds at run time (in a loop, in another function or
  module) can't be read from the file: it's listed as not checked, never
  as clean.
- **Checks pandas pipelines too.** A Python file that imports pandas gets
  the pandas checks (see "pandas" below), alongside any SQL inside it.
- **Runs as the agent works.** Hooks check each edit to a SQL file (or a
  Python file with SQL inside, or that imports pandas) for bugs that are never intended (`= NULL`,
  an unguarded `DELETE`, a `WHERE` that empties a left join). At the end
  of each turn they run every enabled check over the files touched. New
  findings go back to the agent once, a clean pass is silent, and no edit
  is ever refused.
- **Works anywhere SQL lives.** In a terminal and in CI, for any agent or
  person: text, JSON or SARIF output. Exit codes: 0 means clean; 1 means
  findings at or above `--fail-on` (default `error`); 2 is a usage or config
  error; 3 means some files couldn't be checked (`--allow-unverified` turns
  3 into 0).

## pandas

A `.py` file that imports pandas is read with Python's own parser; pandas
is never imported and your code never runs. The checks look for pandas
code that runs without an error and gives the wrong data:

- a write through a chain (`df[mask]["col"] = v`,
  `df["col"].fillna(0, inplace=True)`), which never reaches the DataFrame
  under pandas 3 (checked on every edit);
- a write to the row of an `iterrows()` loop, which changes a copy;
- `groupby(...).first()` / `.last()`, which can build a row from values of
  different input rows;
- `sort_values` on one column (an unstable sort) deciding which row
  `drop_duplicates` keeps;
- `to_sql(if_exists="replace")`, which drops the table's keys, constraints
  and column types;
- off by default (they fire on most code and depend on data the checker
  can't see): `merge`/`join` with no `validate=`, and `groupby` with no
  `dropna=`.

A DataFrame is a name the file builds from pandas (`pd.read_*`,
`pd.DataFrame(...)`, `pd.concat`, `pd.merge`, a method of another known
DataFrame) or annotates as `pd.DataFrame`. A name the checker can't follow
(a parameter with no annotation, a value from another module) isn't
reported on. The receipt counts these files as `pandas in N Python files`.
Checks that need to know what a column means (keys, money, units, time
zones, statuses) read your contracts: see the next section.

## Checks that read your contracts

These checks compare SQL and pandas code with what your contracts in `contracts/` and your
metric files in `metrics/` declare. Each one speaks only where a contract
states the fact it needs, so a project without contracts sees nothing new:

- **A status compared to a value the contract doesn't declare,** such as
  `status = 'downloaded'` when the values are `fetched`, `released` and
  `failed`. The filter quietly matches nothing, and in the `WHERE` of an
  `UPDATE` or `DELETE` the statement quietly changes nothing.
- **Expected absences counted as missing:** `status <> 'fetched'` also counts
  rows the contract marks as absent on purpose (`means: absent_expected`).
- **Different units or currencies combined** without a conversion, such as
  cents minus dollars.
- **Different time zones compared** without a conversion.
- **Paths with different declared bases compared** directly, or with a
  literal put in front that isn't the declared base.
- **A metric computed differently from its definition** in `metrics/`, when
  the query names its column after the metric. Filtering the whole query (to
  a run, a date window or a region) isn't a difference.

### In pandas code

The same five facts are checked in pandas code: a status compared with
`==`, `!=`, `.eq`, `.ne`, `.isin` or inside a `query()` string; a filter
like `status != "fetched"` that is then counted (`len`, `.shape[0]`,
`.count()`, or `.sum()` of the test); units added, subtracted or compared;
two timestamp columns still as read text, in different zones, compared
(once both are parsed with `pd.to_datetime`, pandas compares the instants
or raises, so that isn't flagged); a literal folder put in front of a path
column (`"other/" + df["path"]`, or inside an `apply`), and path columns
with different bases compared or merged on (a URL in front, such as
`s3://…`, is another place, not a wrong folder). And pandas has contract
checks of its own:

- **A contracted CSV read without a `dtype` for a string key,** so `007`
  becomes 7 and joins on the key quietly match nothing.
- **`fillna(0)` on a money column** (minor or major units) that may be
  empty, which changes `mean()`, `count()` and `min()` (`.fillna(0).sum()`
  is left alone: the sum doesn't change).
- **A write that contradicts the output contract's write mode:** an
  append-only table written with `if_exists="replace"`, a merge-on-key
  table appended to, or a table overwritten one partition at a time
  replaced whole.

**Which DataFrame is linked to which contract.** Only an exact link counts,
so a finding never rests on a guess:

| Linked | Not linked (no finding) |
|---|---|
| `pd.read_csv("data/orders.csv")`, `read_table`, `read_fwf`, `read_json`, `read_parquet`, when the path matches one contract's `location.path` glob (from the project folder, or from the declared base's `default`) | A path built from a variable, a parameter or an f-string; a path that matches no contract, or more than one |
| The same with a constant from the same file (`ORDERS = "data/orders.csv"`, assigned once), `Path("data") / "orders.csv"` or `os.path.join("data", "orders.csv")` | `**kwargs` in the call; `names=` (the code names the columns) |
| `pd.read_sql("SELECT ... FROM orders")` with one contracted table, or `pd.read_sql_table("orders", con)` | A query with a join or a CTE; a column renamed in the `SELECT` (that column only) |
| A frame made from a linked one by a row filter, `query`, `head`, `sort_values`, `drop_duplicates`, `dropna`, `copy`, `reset_index` or a column list | `rename`, `assign`, `fillna`, `astype`, `merge`, `concat`; a column assigned (`df["status"] = ...`), `df.columns = ...` |
| A write: `to_sql("orders", ...)` by table name, `to_csv` / `to_json` / `to_parquet` by path | |

Names are followed inside one function (or the module) at a time, as for
the other pandas checks. A finding from a v1 contract needs only what v1
declares (allowed values); units, time zones, paths and write modes need
`version: 2`.

They're all off by default until they're measured on real projects. Switch
them on with `truequery rules set <rule> --enabled on`. A finding that relies
on a draft contract says so.

## Install it on its own

```bash
pip install truequery          # or: uv tool install truequery
uvx truequery detect path/to/code/   # or run it without installing
```

That installs `truequery` and the short alias `tq`. If another tool in the
same environment already provides a `tq` command, use `truequery`. Contributors working from a
clone can install the local engine instead, from the repo root:

```bash
uv tool install ./plugin/engine
```

## You control the rules

Every rule can be switched on or off, moved between the per-edit and
end-of-turn checks, or given a different severity: per project in
`DATA.md`, or for yourself in `~/.config/truequery/config.yaml`.

```bash
truequery rules                             # what's on, and where each setting came from
truequery rules sql/accidental-cross-join   # one rule: what it means, with a proof
truequery rules set sql/between-date-on-timestamp --enabled off
```

Agents never change rule settings: the skill forbids it, and if settings
change during a session, the agent is told to check with you. An agent may
only ignore a single line, and must give a reason
(`truequery ignore add <rule> <path:line> --reason "..."`). Every rule
earns its place on precision: one that misfires gets fixed or switched off
by default.

## All 41 checks

`immediate` checks run after every edit; `stop` checks run at the end of the
turn. Checks that are off by default aren't yet precise enough to run
unasked; switch them on with `truequery rules set`.

| Rule | Default | Build severity | What it catches |
|---|---|---|---|
| `dq/expected-absence-counted-as-missing` | off (stop) | error | Missing-data counts exclude statuses the contract says are absent on purpose |
| `dq/undeclared-status-value` | off (stop) | error | Compare a status column only to values its contract declares |
| `dq/uniqueness-key-no-duplicates` | off (stop) | error | Declared keys carry a zero-duplicate uniqueness check |
| `ingest/declare-source-freshness` | off (stop) | warning | Every ingested source declares a freshness threshold |
| `model/unique-key-merge-key` | on (stop) | error | Incremental merge models declare a non-null unique merge key |
| `orch/explicit-catchup-policy` | off (stop) | warning | Scheduled DAGs set catchup explicitly |
| `py/pandas-dedupe-unstable-sort` | on (stop) | warning | Dedupe after a sort with a unique tiebreaker or a stable sort |
| `py/pandas-fillna-zero-on-money` | off (stop) | warning | Don't fill missing money amounts with 0 |
| `py/pandas-groupby-dropna` | off (stop) | warning | Say what happens to missing group keys with dropna= |
| `py/pandas-groupby-first-mixes-rows` | on (stop) | warning | Pick whole rows per group, not groupby().first() / .last() |
| `py/pandas-merge-validate` | off (stop) | warning | State the expected cardinality of every merge with validate= |
| `py/pandas-no-chained-assignment` | on (immediate) | error | Change a DataFrame in one step on the DataFrame itself, never through a chain |
| `py/pandas-no-row-loop-mutations` | on (stop) | error | Never write to the row of an iterrows() loop |
| `py/pandas-read-csv-contract-dtype` | off (stop) | warning | Read a contracted file's string keys as strings |
| `py/pandas-to-sql-replace` | on (stop) | warning | Load into an existing table without to_sql(if_exists="replace") |
| `py/pandas-write-mode-violation` | off (stop) | error | Write an output the way its contract says it's written |
| `sem/metric-drift` | off (stop) | error | A column named after a metric in `metrics/` computes it as declared |
| `sem/relative-path-base-mismatch` | off (stop) | error | Normalize paths with different declared bases before comparing them |
| `sem/timezone-mismatch` | off (stop) | error | Convert timestamps with different declared time zones before comparing them |
| `sem/unit-mismatch` | off (stop) | error | Convert columns with different declared units or currencies before combining them |
| `sql/accidental-cross-join` | on (stop) | error | Every join of two tables carries an explicit join predicate |
| `sql/average-of-ratios` | off (stop) | warning | Aggregate a ratio as SUM(numerator) / SUM(denominator), not AVG of row ratios |
| `sql/between-date-on-timestamp` | on (stop) | warning | Filter timestamps with a half-open range, not BETWEEN date literals |
| `sql/case-without-else-in-aggregate` | on (stop) | advisory | A CASE that feeds arithmetic or an aggregate states its ELSE |
| `sql/count-star-after-left-join` | on (stop) | error | Count child rows with COUNT(child.key), not COUNT(*), after a LEFT JOIN |
| `sql/distinct-dedup-bandaid` | off (stop) | warning | SELECT DISTINCT never stands in for a grain fix |
| `sql/group-by-bare-column` | on (stop) | warning | In engines that allow it, GROUP BY queries never project bare ungrouped columns |
| `sql/insert-explicit-column-list` | on (stop) | warning | INSERT INTO names its target columns |
| `sql/integer-division-truncation` | on (stop) | warning | Divide integers only after casting to a non-integer type |
| `sql/keyset-pagination` | off (stop) | warning | Paginated reads use keyset seeks, not OFFSET |
| `sql/left-join-null-rejecting-where` | on (immediate) | warning | Filters on the nullable side of an outer join live in ON |
| `sql/limit-requires-order-by` | off (stop) | warning | Every LIMIT carries an ORDER BY |
| `sql/natural-join-explicit-keys` | on (stop) | warning | Joins name their keys explicitly, never NATURAL JOIN |
| `sql/negative-filter-drops-nulls` | off (stop) | warning | Negative filters on nullable columns state what happens to NULLs |
| `sql/nondeterministic-merge-keys` | on (stop) | error | Merge and dedupe keys never use nondeterministic functions |
| `sql/not-in-nullable-subquery` | on (stop) | error | NOT IN is never used against a nullable subquery |
| `sql/null-equality-comparison` | on (immediate) | error | Test for NULL with IS NULL, never with = NULL or <> NULL |
| `sql/row-number-dedupe-tiebreaker` | on (stop) | warning | ROW_NUMBER dedupe orders by a unique tiebreaker |
| `sql/set-operation-column-position` | off (stop) | error | Set-operation branches align columns explicitly by position |
| `sql/unguarded-update-delete` | on (immediate) | error | UPDATE and DELETE always carry a filter or an explicit full-table guard |
| `sql/union-all-default` | off (stop) | warning | Bare UNION states its dedup intent; UNION ALL is the default for appends |
