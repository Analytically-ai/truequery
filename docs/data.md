# Checking your data

The checker reads your code. `truequery verify` reads your **data**: the
local files and database files a contract points to, read-only, on your
machine. It checks that the data says what the contract says: keys unique,
statuses declared, timestamps in the declared zone, paths under the
declared folder, sizes in the declared unit. Back to the
[README](../README.md) or [the checker](checker.md).

## Run it

```bash
truequery verify                 # every contract with local data
truequery verify fetch_log       # one dataset
truequery review --data          # the checks on your code, plus the data, in one receipt
```

On a project with two data errors:

```
$ truequery verify
TrueQuery data check
Verified     fetch_log (1 file, 6 rows): no empty run_id and url, key (run_id, url) unique, status values all declared, every local_path under data_root, bytes_saved matches the size on disk (1 file) · 3 rows absent as expected (refetchable, released, removed_on_purpose)
Verified     shards (1 file, 2 rows): no empty shard_id, key shard_id unique, every local_path under data_root
Found        fetch_log: 1 file missing where the status says present; 1 file missing where the status means a failure (logs/fetch/*.csv; e.g. key (r1, https://ex.org/5), (r1, https://ex.org/6))
             fix: fetch the missing files again, or correct the status of those rows
Found        shards: 2 values of bytes_logged are in GB, the contract says GiB (logs/shards.csv; e.g. key s1, s2)
             fix: if the data really is in GB, set `unit: GB` in the contract; else convert the sizes where they're written
Evidence     .truequery/evidence.jsonl (+11 records)
2 data failures in 2 of 2 datasets read
```

The rows removed on purpose, released or refetchable have no file on disk,
and the contract says that's expected (`means: absent_expected`), so they're
counted, not flagged.

`truequery review --data` puts the same lines in the receipt:

```
TrueQuery receipt
Checked      1 SQL file · contracts for 1 of 1 table
Verified     against the data: fetch_log (1 file, 6 rows): no empty run_id and url, …
             shards (1 file, 2 rows): no empty shard_id, key shard_id unique, every local_path under data_root
Found        2 data failures:
             fetch_log: 1 file missing where the status says present; … Fix: fetch the missing files again, or correct the status of those rows
             shards: 2 values of bytes_logged are in GB, the contract says GiB (logs/shards.csv; e.g. key s1, s2). Fix: …
```

Without `--data`, the receipt says the data wasn't read and how to read it.
The receipt is built only from the run in front of you, never from an
earlier run's evidence: the data may have changed since.

## What it checks

A contract (`contracts/<name>.yaml`, `version: 2`) with local data either
lists files (`kind: files`, `location: {path: logs/*.csv, format: csv}`;
CSV, JSONL, JSON or Parquet) or names a database file
(`kind: table`, `location: {database: app.sqlite, relation: users}`; SQLite
or DuckDB). From what the contract declares:

| The contract says | `verify` checks |
|---|---|
| `keys` | no key is empty, and each key is unique |
| `nullable: false` | no empty values in that column |
| `values` | every value in the data is a declared one |
| `time_zone` | timestamps carry the declared zone (`UTC`: `Z` or `+00:00`; `local:<zone>`: not marked UTC; `naive`: no marker) |
| `type: path` + `relative_to` | every path resolves under the declared base folder (`bases:` in `DATA.md`), never outside it |
| `values` with `means` + `status_column` | rows whose status means present have their file; rows absent on purpose are counted, not flagged |
| `unit` + `size_of` | each size matches the file on disk; GiB written as GB (and the other binary/decimal twins) is named as such |
| `freshness_sla: {column, max_age}` | the newest timestamp is within `max_age` of now (`--now` sets "now") |

A contract that's a draft (`status: draft`) still runs, and every line that
relies on it says `(from draft contract <name>, inferred from …)`.

## The data reader

SQLite files need nothing extra: Python reads them, read-only. Data files
and `.duckdb` databases need the data reader, DuckDB (version 1.2.0 or
later). It isn't installed with TrueQuery, so nobody downloads it for
nothing. Until it is, `verify` checks what it can and says what it couldn't:

```
Not checked  data files: the data reader isn't installed. Run: truequery setup data · fetch_log, shards
```

`truequery setup data` installs it the right way for how you installed
TrueQuery. Without `--yes` it only prints the command and what it
downloads (DuckDB from PyPI: 15–25 MB, about 50 MB installed), and changes
nothing:

| TrueQuery installed as | `truequery setup data --yes` |
|---|---|
| The Claude Code plugin | writes `~/.config/truequery/data-reader` (or under `$XDG_CONFIG_HOME`); from then on the plugin's `truequery` command runs with DuckDB, which uv downloads once into its cache. The per-edit checks never load it |
| `uv tool install` | `uv tool install <the same source> --with "duckdb>=1.2.0"` |
| `pipx install` | `pipx inject truequery "duckdb>=1.2.0"` |
| `uvx` | nothing to install; it prints `uvx --with "duckdb>=1.2.0" truequery verify` |
| pip, in a virtualenv or not | `<that python> -m pip install "duckdb>=1.2.0"` (or `uv pip install --python <that python> …` when the environment has no pip) |

You can also install it yourself: `pip install "truequery[data]"`.

`truequery setup data --no` installs nothing and records the answer.
Either answer goes into `DATA.md`, so `/truequery init` doesn't ask again:

````
```tq-config
setup:
  data: true      # false: the team said no
```
````

`truequery status` shows whether the reader is installed, whether this
project needs it, and the command.

## In CI

Exit codes: 0 means every dataset with local data was read and matches its
contract; 1 means the data breaks a contract; 2 is a usage or config error;
3 means some data couldn't be verified (files missing or unreadable, no
rows, only part of a large dataset checked, an invalid contract), and
`--allow-unverified` turns 3 into 0.

When the reader isn't installed, the datasets that need it are listed as
not checked and don't change the exit code. In CI, add `--require data`:
then a missing reader exits 3, even with `--allow-unverified`, so the data
checks can't be skipped quietly. `--json` prints the whole run.

```bash
pip install "truequery[data]"
truequery verify --require data
```

## What it promises

- **Read-only.** It runs only SQL it generated from the contract, never SQL
  from your code or the agent. DuckDB is opened on exactly the files the
  contract names, with everything else (writing, other files, the network,
  extension downloads) switched off and the settings locked. SQLite is
  opened read-only.
- **No network.** It reads local files. Remote databases aren't read until
  0.3.
- **Private.** It reports counts and at most five failing key values, never
  other values. Keys of a column marked `pii: true` are never shown. When the
  reader can't read a file, only the kind of error is shown (its full text
  can quote a line of your data; `--verbose` prints it on stderr).
- **Bounded.** A dataset over 500 MB isn't read (it's listed as not
  checked). In one over 1,000,000 rows, the first 1,000,000 by key are
  checked, the lines say so, and a key is called unique only when the whole
  dataset was checked.
- **Deterministic.** The same data, contracts and `--now` give the same
  output, byte for byte.
- **Never in a hook.** The checks that run as the agent edits never read
  data. The agent runs `verify` (or `review --data`) at the receipt step.

## The evidence log

Each run adds one line per check to `.truequery/evidence.jsonl` in your
project: the dataset, the check, the files, the row count, the result, the
`--now` time and the time it ran. A line is about half a kilobyte, so a
project with 10 datasets, each with 8 things to check, adds about 40 KB per
run. Past
5 MB the file is moved to `evidence.jsonl.1` (replacing the older one), so
the log never takes more than about 10 MB, even with a daily run.
`.truequery/.gitignore` keeps both files out of git.

## Not yet

- Remote databases (Postgres, BigQuery, Snowflake): 0.3.
- Checking that a pipeline's output kept every input row.
- Excel files.
