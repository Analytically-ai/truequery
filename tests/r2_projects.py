"""Reviewer 2's four data errors as small projects on disk, broken and
fixed, for running `truequery verify` and `review --data` end to end.

The same data as the PR B fixtures in test_assertions_and_safety.py, laid
out as a project (DATA.md, contracts/, logs/, data/) so the CLI reads it
the way a user's project is read. (Reviewer 2's fifth case, the ratio
metric, is a static check: `verify` doesn't compute metrics.)
"""

from __future__ import annotations

import os
from pathlib import Path

CASES = ("base", "sizes", "tz", "lifecycle")

DATA_MD = """# Data

```tq-config
dialect: duckdb
bases:
  data_root: ./data
```
"""

FETCH_CONTRACT = """version: 2
dataset: fetch_log
kind: files
location: {path: logs/fetch/*.csv, format: csv}
grain: one row per fetch attempt
keys: [[run_id, url]]
columns:
  run_id: {type: string, nullable: false}
  url: {type: string, nullable: false}
"""


def _sparse(path: Path, size: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        os.truncate(handle.fileno(), size)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(root: Path, case: str, broken: bool) -> Path:
    """Write the `case` project under `root` and return it."""
    root.mkdir(parents=True, exist_ok=True)
    _write(root / "DATA.md", DATA_MD)
    if case == "base":
        # Paths in the log already start with the base folder's name, so
        # they resolve to data/data/... : the wrong base.
        _write(root / "data" / "2026-10-01" / "a.warc", "warc-payload")
        prefix = "data/" if broken else ""
        _write(root / "logs" / "fetch" / "2026-10-01.csv",
               "run_id,url,local_path\n"
               f"r1,https://example.org/1,{prefix}2026-10-01/a.warc\n")
        _write(root / "contracts" / "fetch_log.yaml", FETCH_CONTRACT
               + "  local_path: {type: path, relative_to: data_root}\n")
    elif case == "sizes":
        # Two 1 GiB files; the log writes 1.07 (GB) under a GiB contract.
        _sparse(root / "data" / "shard1.bin", 2**30)
        _sparse(root / "data" / "shard2.bin", 2**30)
        size = "1.07" if broken else "1.00"
        _write(root / "logs" / "shards.csv",
               "shard_id,local_path,bytes_logged\n"
               f"s1,shard1.bin,{size}\ns2,shard2.bin,{size}\n")
        _write(root / "contracts" / "shards.yaml",
               "version: 2\ndataset: shards\nkind: files\n"
               "location: {path: logs/shards.csv, format: csv}\n"
               "grain: one row per shard\nkeys: [shard_id]\ncolumns:\n"
               "  shard_id: {type: string, nullable: false}\n"
               "  local_path: {type: path, relative_to: data_root}\n"
               "  bytes_logged: {type: number, unit: GiB, size_of: local_path}\n")
    elif case == "tz":
        rows = ("1,2026-10-01T08:00:00Z\n"
                + ("2,2026-10-01 09:00:00\n3,2026-10-01T10:00:00+02:00\n"
                   if broken else
                   "2,2026-10-01T09:00:00Z\n3,2026-10-01T08:00:00+00:00\n")
                + "4,2026-10-01T11:00:00+00:00\n")
        _write(root / "logs" / "events.csv", "id,fetched_at\n" + rows)
        _write(root / "contracts" / "events.yaml",
               "version: 2\ndataset: events\nkind: files\n"
               "location: {path: logs/events.csv, format: csv}\n"
               "grain: one row per event\nkeys: [id]\ncolumns:\n"
               "  id: {type: string, nullable: false}\n"
               "  fetched_at: {type: timestamp, time_zone: UTC}\n")
    elif case == "lifecycle":
        # Rows removed on purpose, released or refetchable have no file:
        # that's expected and must raise nothing. Broken: a `fetched` row
        # and a `failed` row whose files are missing.
        _write(root / "data" / "run1" / "ok.warc", "x" * 1000)
        rows = ("r1,https://ex.org/1,fetched,run1/ok.warc,1.00\n"
                "r1,https://ex.org/2,removed_on_purpose,run1/removed.warc,5.00\n"
                "r1,https://ex.org/3,released,run1/released.warc,2.00\n"
                "r1,https://ex.org/4,refetchable,run1/refetch.warc,0.00\n")
        if broken:
            rows += ("r1,https://ex.org/5,fetched,run1/gone.warc,1.00\n"
                     "r1,https://ex.org/6,failed,run1/failed.warc,\n")
        _write(root / "logs" / "fetch" / "2026-10-01.csv",
               "run_id,url,status,local_path,bytes_saved\n" + rows)
        _write(root / "contracts" / "fetch_log.yaml", FETCH_CONTRACT
               + "  status:\n    type: string\n    values:\n"
               "      fetched: {means: present}\n"
               "      removed_on_purpose: {means: absent_expected}\n"
               "      released: {means: absent_expected}\n"
               "      refetchable: {means: absent_expected}\n"
               "      failed: {means: absent_unexpected}\n"
               "  local_path: {type: path, relative_to: data_root, "
               "status_column: status}\n"
               "  bytes_saved: {type: number, unit: KB, size_of: local_path}\n")
    else:  # pragma: no cover
        raise ValueError(case)
    return root
