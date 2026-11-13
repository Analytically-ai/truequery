# Building pipelines from contracts

A contract says what a dataset means: its grain and keys, column types,
time zones, units, path bases and what each status value means. Two
commands help you start from the data you already have and build on it.
Back to the [README](../README.md) or [checking your data](data.md).

- `truequery init --from <files or folders>` writes **draft** contracts from
  CSV, JSONL and Parquet files that already exist, before any SQL or
  warehouse does. You (or your agent, in the `/truequery init` interview)
  then answer what it couldn't settle.
- `truequery scaffold <contract>` writes a pipeline's boundaries in plain
  pandas: a typed reader, an output check and pytest tests. You write the
  transform in between.

## Draft contracts from your files

```
$ truequery init --from logs
wrote contracts/fetch.yaml (draft): logs/fetch/*.csv, 5 columns, 7 rows read
Questions for the interview (/truequery init), or answer them in the contracts:
  fetch: grain: one row per url? It is unique and never empty in all 7 rows read. Confirm the grain and the key
  fetch.local_path: 1 of 6 sampled paths exist under data_root (data/): is that their base?
  fetch.bytes_saved: the name says bytes, but values are written with a decimal point: which unit is it?
  fetch.status: for each value (failed, fetched, refetchable, released, removed_on_purpose), is the file in local_path there (present), absent on purpose (absent_expected), or absent because something went wrong (absent_unexpected)? Values from all 7 rows: there may be others
init --from: 1 draft contract written, 0 kept, 0 not inferred. Not read: READMEs and data dictionaries (init --from doesn't link docs yet)
```

The draft it wrote (the header comment is shortened here):

```yaml
# A draft written by `truequery init --from`. Every fact below was
# inferred from a sample of the data; nobody has confirmed it yet. …
version: 2
dataset: fetch
status: draft
inferred_from: "truequery init --from: the header and 7 rows of logs/fetch/2026-10-01.csv, logs/fetch/2026-10-02.csv (2 of 2 files); sample limit 2,000 rows"
kind: files
location:
  path: "logs/fetch/*.csv"
  format: csv
grain: "TODO: one row per url? It is unique and never empty in all 7 rows read"
keys: [url]
columns:
  run_id:
    type: string
  url:
    type: string
    nullable: false
  status:
    type: string
    status: draft
    values:  # TODO: what each value means for the file; there may be other values
      failed: {means: TODO}
      fetched: {means: TODO}
      refetchable: {means: TODO}
      released: {means: TODO}
      removed_on_purpose: {means: TODO}
  local_path:
    type: path
    relative_to: data_root  # TODO: 1 of 6 sampled paths exist under data_root (data/): is that their base?
    status_column: status
    status: draft
  bytes_saved:
    type: number
    status: draft  # TODO: the name says bytes, but values are written with a decimal point: which unit is it?
```

### What it infers

Everything it writes is a draft: the contract says `status: draft`, and
`inferred_from` names the files and rows it read. A column with an open
question has `status: draft` and a `# TODO` comment.

| From | It writes |
|---|---|
| Folder layout | One dataset per folder of files that share a header: a glob (`logs/fetch/*.csv`), the format, and partition keys from `key=value/` folders (`events/dt=*/*.csv`, `partition_key: dt`). Files in one folder whose headers share at least half their columns are one dataset (files drift: a column added one month); a column some files lack is written `nullable: true` as a question. Files with mostly different headers become separate datasets. The dataset is named after the folder, or the file |
| CSV text | Column types: `integer`, `number`, `boolean`, `date`, `timestamp`, `string`. A number with leading zeros (`007`) or more than 18 digits is a `string`, so it isn't read as a number. A number with a decimal comma (`1,5`) stays a `string`. A delimiter other than a comma is recorded (`location.delimiter`) |
| Timestamps | All marked `Z` or `+00:00`: `time_zone: UTC`. None marked: `time_zone: naive`, a question. A mix: no `time_zone`, a question and the counts in `quirks` |
| Keys | The first column (an `id`-named one first), or else the first pair of columns, that is unique and never empty in the sample. The grain stays `TODO …` until someone confirms it |
| Low-cardinality text | Up to 20 distinct values as `values`, when the column is named like a category (`status`, `type`, `region`, …) or its values repeat (on average twice or more in the sample), and it isn't an `id`-named column. The list is a draft with a question ("are these all of them?"): any other value fails `truequery verify`. A status-like column (`status`, `state`, `outcome`, …) in a dataset with a path column gets `means: TODO` for each value, and the path column gets `status_column`, so `truequery verify` reports those rows as "not judged" until the meanings are filled in |
| Column names | Units, flagged for confirmation, from the name's last word (or its first, for sizes and durations: `bytes_saved`), never for an ID or a count (`kb_article_id`, `tb_count`) or a size next to money (`revenue_gb`): `_bytes`, `_kb`/`_kib` … `_tb`/`_tib`, `_ms`, `_seconds`, `_minutes`, `_hours`, `_days`, `_pct`, `_bps`, `_ratio`, and `_cents` (with a currency column, `unit: minor` and `currency_column`). Never from the size of the values. Bytes or cents written with a decimal point (`1.00`) become a question instead |
| Path columns | `type: path`. Sampled paths are looked up (read-only, never outside the folder) under each base declared in `DATA.md`, the project folder and the data's own folder. `relative_to` is written only for a declared base that finds the most of them; otherwise the output suggests a base. When every path starts with the base folder's own name, it says so: that's the wrong-base error, which `truequery verify` also names |
| Personal data | A text column gets `pii: true` as a draft when its name says so (`email`, `phone`, `name`, `first_name`, `address`, `date_of_birth`, or a person's role: `assignee`, `author`, `owner`, `customer`, `created_by`, `cell`, …; not an `id`-named column) or its values look like email addresses, phone numbers (`+1 415 555 0100`) or people's names (two or three capitalised words, outside place and category columns). Its values are never written to the contract or printed. Name detection by value can't be complete: check the columns it lists |

The sample is the first 2,000 rows of each dataset (files in sorted
order), and at most 64 MB of text; a line over 1 MB stops the sample. A
2 GB file takes a fraction of a second. CSV and JSONL are read with
Python's standard library, as text, so the same files give the same
drafts whether or not the data reader is installed. Parquet's schema and
sample are read with the data reader (DuckDB); without it, Parquet files
are listed as `Not inferred: Parquet needs the data reader. Run: truequery
setup data`. The same files give byte-identical contracts on every run and
every OS.

### What it never does

- **Overwrite a contract.** A dataset whose name or location already has a
  contract is listed as kept. `--force` (which rewrites `DATA.md`) can't be
  combined with `--from`.
- **Read outside the project.** A path outside the project folder, and
  outside every base declared in `DATA.md`, is refused before anything is
  written. A file that links out of the folder, or a linked folder, is
  listed as not read. A relative `--from` path is looked up in the project
  folder first, then in the current folder.
- **Write a contract that doesn't validate.** Every draft passes
  `truequery contract validate`.
- **Read your docs.** READMEs and data dictionaries aren't read yet, so no
  description is linked from them.
- **Change anything else.** It writes `contracts/<name>.yaml` (and
  `DATA.md`, when there isn't one, as `truequery init` does). Nothing is
  installed.

Not read: JSON documents (JSONL is read), Excel, compressed files, Avro,
ORC and Feather. They are listed under `Not inferred`.

### Answering the questions

In an agent, `/truequery init` runs `init --from` when the project has
data files and no contracts, then asks only the open questions: what each
status means, the time zone of naive timestamps, a unit, a path base, the
grain. Each answer goes into the contract, and `status: draft` comes off
what was confirmed. The contract stops being a draft (remove `status:
draft` at the top) once the grain and keys are confirmed and no column
is a draft.

In a terminal, edit the contract yourself. With the answers above
(`fetched` is `present`, `failed` is `absent_unexpected`, the other three
are `absent_expected`; sizes are in KB; the key is `(run_id, url)`),
`truequery verify` finds the file that is really missing (a `fetched` row),
leaves the three absent on purpose alone, and counts the failed fetch, which
has no file, as its status says:

```
Verified     fetch (2 files, 7 rows): no empty run_id and url, key (run_id, url) unique, status values all declared, no local_path outside data_root, bytes_saved matches the size on disk (2 files) · 3 rows absent as expected (refetchable, released, removed_on_purpose) · 1 row whose status means a failure (failed): no file, as the status says
Found        fetch: 1 file missing where the status says present (logs/fetch/*.csv; e.g. key (r1, https://ex.org/5))
             fix: fetch the missing files again, or correct the status of those rows
```

## A pipeline's boundaries from its contracts

```
$ truequery scaffold contracts/fetch.yaml
wrote pipeline/fetch_io.py
wrote pipeline/test_fetch_io.py
scaffold: 1 contract. Write the transform between read_<dataset>() and check_<dataset>(); run the tests with pytest
```

For each contract (version 2, with columns) it writes:

- `<dataset>_io.py`:
  - `read_<dataset>(path)`: reads one file with a dtype for every column
    (an ID like `007` stays text), only empty fields as missing (`NA` and
    `null` stay text), timestamps parsed in the declared zone (a naive value
    in a UTC column raises instead of being read as UTC), and partition
    columns from `key=value` folders. A CSV row with more fields than the
    header raises instead of shifting the columns (a trailing comma is
    fine), and a `nullable: true` column (not a key) that a file lacks is
    read as empty;
  - `coerce_<dataset>(df)`: the same types for a frame read some other way
    (for a `kind: table` contract, there's no file reader);
  - `check_<dataset>(df)`: raises `ValueError` naming every column missing,
    type changed (an integer key that became a float), empty key, repeated
    key, undeclared value and wrong time zone. Run it on your output before
    writing it.
- `test_<dataset>_io.py`: pytest tests on rows made from the contract
  (synthetic, never your data): the rows read back and pass, and each
  check fails when it should.

```python
from pipeline.fetch_io import read_fetch, check_fetch

fetches = read_fetch("logs/fetch/2026-10-01.csv")
result = ...  # your transform
check_fetch(result)  # or the output contract's check_<dataset>
```

The generated code is plain pandas and never imports TrueQuery: uninstall
TrueQuery and it still runs. The same contract gives the same bytes. It
passes `truequery review` with every check on, and its tests pass on
pandas 3.1.0 (Parquet tests need `pyarrow`, and are skipped without it).
Units can't be checked from a DataFrame: they're listed in `COLUMNS`, and
`truequery verify` checks them against the files.

- `--out <dir>` sets where the files go (default `pipeline/`).
- A file that's already there isn't written over without `--force`, and
  then nothing is written.
- A version 1 contract, or one without columns, is refused with what to
  add.
- A draft contract is scaffolded, and the code says so at the top
  (`# DRAFT: contracts/fetch.yaml is a draft contract …`, and the columns
  still to confirm). Regenerate once it's confirmed.
