"""Tests for PR B: assertion compiler, local runners, Reviewer 2 fixtures, and safety.

Covers:
1. Reviewer 2 broken + corrected twin fixtures executed on DuckDB, SQLite, and
   temporary filesystems:
   - Wrong base folder (`path_resolves`)
   - Size unit mismatch (`size_matches_disk`, `1.07` GiB vs GB + MiB/MB + KiB/KB)
   - Timestamp timezone marker (`tz_marker` on `UTC`, `local:<zone>`, `naive`
     across CSV, JSONL, Parquet physical types, DuckDB, and SQLite)
   - Status lifecycle (`exists_per_status` with `removed_on_purpose`,
     `released`, `refetchable` raising zero false alarms)
   - Governed ratio metric (`collection_rate` via `compile_metric_sql` on
     DuckDB and SQLite)
2. Core SQL assertions (`not_null`, `unique` on single & composite keys,
   `accepted_values` with `nullable: false` vs `nullable: true`, `freshness`
   with deterministic `now` clock, and v1 contract compatibility).
3. Security and safety invariants (PLAN-0.2 §3):
   - DuckDB file-mode lockdown (`open_duckdb_for_files`: `COPY TO` new or
     allowed file blocked, unlisted file read blocked, config locked)
   - DuckDB `.duckdb` database lockdown (`open_duckdb_database`: read-only
     direct connect, writes/COPY/ATTACH/external reads blocked)
   - SQLite read-only URI construction safe on `#` in paths
     (`Path(p).resolve().as_uri() + "?mode=ro"` + `PRAGMA query_only = ON`)
   - Untrusted paths in data (`..`, absolute, `~`, Windows drive, symlinks
     escaping base) fail `path_resolves` without ever calling `os.stat()` or
     `os.path.exists()` on outside paths
   - Identifier quoting and `assert_single_select` invariant across v1/v2
     contracts and `compile_metric_sql`
   - `pii: true` key privacy (no sample keys or values in reports or
     `.truequery/evidence.jsonl`)
   - Draft contract provenance tagging (`(from draft contract <name>, inferred from ...)`)
   - `max_rows` and `max_bytes` limits
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3

import duckdb
import pytest
from sqlglot import exp

from truequery import assertions as A
from truequery import contract_v2
from truequery import project as P
from truequery import verify as V


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_data_md(root: Path, bases_yaml: str = "") -> None:
    block = "dialect: duckdb\n"
    if bases_yaml:
        block += bases_yaml.rstrip() + "\n"
    (root / "DATA.md").write_text(
        f"# Data\n\n```yaml tq-config\n{block}```\n",
        encoding="utf-8",
    )


def _create_sparse_file(path: Path, size_bytes: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        os.truncate(f.fileno(), size_bytes)


def _find_assertion(report: dict, name: str, target=None) -> dict:
    for item in report["assertions"]:
        if item["assertion"] == name and (target is None or item["target"] == target):
            return item
    raise AssertionError(f"assertion {name!r} (target={target!r}) not found in {report['assertions']}")


# ---------------------------------------------------------------------------
# 1. Reviewer 2 Broken + Corrected Twin Fixtures
# ---------------------------------------------------------------------------


class TestReviewer2Fixtures:
    """Broken + corrected twin fixtures for all five Reviewer 2 failure modes."""

    def test_r2_1_wrong_base_folder_broken_and_corrected_twins(self, tmp_path: Path):
        """Broken: CSV rows have `data/2026-10-01/a.warc` while `data_root` is
        already `./data` (duplicated prefix / wrong base) -> fails `path_resolves`.
        Corrected twin: rows have `2026-10-01/a.warc` -> passes `path_resolves`."""
        _write_data_md(tmp_path, "bases:\n  data_root: ./data")
        actual_file = tmp_path / "data" / "2026-10-01" / "a.warc"
        actual_file.parent.mkdir(parents=True, exist_ok=True)
        actual_file.write_bytes(b"warc-payload")

        broken_csv = tmp_path / "logs" / "broken.csv"
        broken_csv.parent.mkdir(parents=True, exist_ok=True)
        broken_csv.write_text(
            "run_id,url,local_path\n"
            "r1,https://example.org/1,data/2026-10-01/a.warc\n",
            encoding="utf-8",
        )
        contract_broken = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "fetch_log",
                "kind": "files",
                "location": {"path": "logs/broken.csv", "format": "csv"},
                "grain": "one row per fetch attempt",
                "keys": [["run_id", "url"]],
                "columns": {
                    "run_id": {"type": "string", "nullable": False},
                    "url": {"type": "string", "nullable": False},
                    "local_path": {"type": "path", "relative_to": "data_root"},
                },
            },
            path="contracts/fetch_log.yaml",
        )
        rep_broken = V.verify_contract(
            contract_broken,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_broken["valid"] is False
        pr_broken = _find_assertion(rep_broken, "path_resolves", "local_path")
        assert pr_broken["status"] == "failed"
        assert pr_broken["failed_count"] == 1
        assert pr_broken["sample_keys"] == [["r1", "https://example.org/1"]]

        # Corrected twin: path is relative to `data_root` (`2026-10-01/a.warc`)
        fixed_csv = tmp_path / "logs" / "fixed.csv"
        fixed_csv.write_text(
            "run_id,url,local_path\n"
            "r1,https://example.org/1,2026-10-01/a.warc\n",
            encoding="utf-8",
        )
        contract_fixed = {
            **contract_broken,
            "location": {"path": "logs/fixed.csv", "format": "csv"},
        }
        rep_fixed = V.verify_contract(
            contract_fixed,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_fixed["valid"] is True
        pr_fixed = _find_assertion(rep_fixed, "path_resolves", "local_path")
        assert pr_fixed["status"] == "passed"
        assert pr_fixed["failed_count"] == 0

    def test_r2_2_size_unit_mismatch_1_07_gib_vs_gb_twins(self, tmp_path: Path):
        """Broken: file on disk is 1 GiB (2^30 = 1,073,741,824 bytes = 1.0737 GB),
        log says `1.07`, contract declares `unit: GiB` -> fails `size_matches_disk`
        with `"2 sizes match GB, contract says GiB"`.
        Corrected twins:
          (a) log says `1.00` under `unit: GiB` -> passes.
          (b) contract says `unit: GB` with `1.07` (half-ULP rounding) -> passes."""
        _write_data_md(tmp_path, "bases:\n  data_root: ./data")
        one_gib_bytes = 2**30  # 1,073,741,824 bytes
        _create_sparse_file(tmp_path / "data" / "shard1.bin", one_gib_bytes)
        _create_sparse_file(tmp_path / "data" / "shard2.bin", one_gib_bytes)

        broken_csv = tmp_path / "logs" / "sizes_broken.csv"
        broken_csv.parent.mkdir(parents=True, exist_ok=True)
        broken_csv.write_text(
            "shard_id,local_path,bytes_logged\n"
            "s1,shard1.bin,1.07\n"
            "s2,shard2.bin,1.07\n",
            encoding="utf-8",
        )
        contract_broken = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "shards",
                "kind": "files",
                "location": {"path": "logs/sizes_broken.csv", "format": "csv"},
                "grain": "one row per shard",
                "keys": ["shard_id"],
                "columns": {
                    "shard_id": {"type": "string", "nullable": False},
                    "local_path": {"type": "path", "relative_to": "data_root"},
                    "bytes_logged": {
                        "type": "number",
                        "unit": "GiB",
                        "size_of": "local_path",
                    },
                },
            },
            path="contracts/shards.yaml",
        )
        rep_broken = V.verify_contract(
            contract_broken,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_broken["valid"] is False
        sz_broken = _find_assertion(rep_broken, "size_matches_disk", "bytes_logged")
        assert sz_broken["status"] == "failed"
        assert sz_broken["failed_count"] == 2
        assert sz_broken["message"] == "2 sizes match GB, contract says GiB"
        assert sz_broken["sample_keys"] == ["s1", "s2"]

        # Corrected twin A: contract says `unit: GB` (1.07 GB matches 1,073,741,824 within ±0.005 GB)
        contract_gb = {
            **contract_broken,
            "columns": {
                **contract_broken["columns"],
                "bytes_logged": {
                    "type": "number",
                    "unit": "GB",
                    "size_of": "local_path",
                },
            },
        }
        rep_gb = V.verify_contract(
            contract_gb,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_gb["valid"] is True
        assert _find_assertion(rep_gb, "size_matches_disk")["status"] == "passed"

        # Corrected twin B: log records `1.00` GiB under `unit: GiB`
        fixed_csv = tmp_path / "logs" / "sizes_fixed.csv"
        fixed_csv.write_text(
            "shard_id,local_path,bytes_logged\n"
            "s1,shard1.bin,1.00\n"
            "s2,shard2.bin,1.00\n",
            encoding="utf-8",
        )
        contract_gib_fixed = {
            **contract_broken,
            "location": {"path": "logs/sizes_fixed.csv", "format": "csv"},
        }
        rep_gib = V.verify_contract(
            contract_gib_fixed,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_gib["valid"] is True
        assert _find_assertion(rep_gib, "size_matches_disk")["status"] == "passed"

        # Also check MiB vs MB and KiB vs KB unit twin diagnosis directly
        res_mib = V.check_size_matches_disk("1.05", 2**20, "MiB")
        assert res_mib["matches"] is False
        assert res_mib["matched_twin_unit"] == "MB"

        res_kib = V.check_size_matches_disk("1.02", 2**10, "KiB")
        assert res_kib["matches"] is False
        assert res_kib["matched_twin_unit"] == "KB"

    def test_r2_3_tz_marker_utc_local_naive_across_csv_jsonl_parquet_sqlite(
        self, tmp_path: Path
    ):
        """Verify `tz_marker` semantics across CSV, JSONL (`all_varchar`),
        Parquet physical types (`TIMESTAMP` vs `TIMESTAMP WITH TIME ZONE`),
        and SQLite."""
        # 1. CSV + JSONL with `time_zone: UTC`: mixed Z and naive string MUST FAIL
        csv_mixed = tmp_path / "events_mixed.csv"
        csv_mixed.write_text(
            "id,fetched_at\n"
            "1,2026-10-01T08:00:00Z\n"
            "2,2026-10-01 09:00:00\n"
            "3,2026-10-01T10:00:00+02:00\n"
            "4,2026-10-01T11:00:00+00:00\n",
            encoding="utf-8",
        )
        jsonl_mixed = tmp_path / "events_mixed.jsonl"
        jsonl_mixed.write_text(
            '{"id": "1", "fetched_at": "2026-10-01T08:00:00Z"}\n'
            '{"id": "2", "fetched_at": "2026-10-01 09:00:00"}\n'
            '{"id": "3", "fetched_at": "2026-10-01T10:00:00+02:00"}\n'
            '{"id": "4", "fetched_at": "2026-10-01T11:00:00+00:00"}\n',
            encoding="utf-8",
        )

        for fmt, fname in [("csv", "events_mixed.csv"), ("jsonl", "events_mixed.jsonl")]:
            c_utc = contract_v2.validate_contract_v2(
                {
                    "version": 2,
                    "dataset": "events",
                    "kind": "files",
                    "location": {"path": fname, "format": fmt},
                    "grain": "one row per event",
                    "keys": ["id"],
                    "columns": {
                        "id": {"type": "string", "nullable": False},
                        "fetched_at": {"type": "timestamp", "time_zone": "UTC"},
                    },
                },
                path="contracts/events.yaml",
            )
            rep = V.verify_contract(c_utc, project_root=str(tmp_path))
            assert rep["valid"] is False, f"{fmt} should fail on naive/+02:00 rows"
            tz_res = _find_assertion(rep, "tz_marker", "fetched_at")
            assert tz_res["status"] == "failed"
            assert tz_res["failed_count"] == 2
            assert tz_res["sample_keys"] == ["2", "3"]

        # Corrected UTC twin: all rows have Z or +00:00
        csv_utc_clean = tmp_path / "events_utc_clean.csv"
        csv_utc_clean.write_text(
            "id,fetched_at\n"
            "1,2026-10-01T08:00:00Z\n"
            "2,2026-10-01T09:00:00+00:00\n",
            encoding="utf-8",
        )
        c_utc_clean = {
            **c_utc,
            "location": {"path": "events_utc_clean.csv", "format": "csv"},
        }
        rep_clean = V.verify_contract(c_utc_clean, project_root=str(tmp_path))
        assert rep_clean["valid"] is True
        assert _find_assertion(rep_clean, "tz_marker")["status"] == "passed"

        # 2. `time_zone: local:Europe/Prague`: naive strings and +02:00 PASS; `Z` FAILS
        csv_local_clean = tmp_path / "events_local_clean.csv"
        csv_local_clean.write_text(
            "id,fetched_at\n"
            "1,2026-10-01 09:00:00\n"
            "2,2026-10-01T10:00:00+02:00\n",
            encoding="utf-8",
        )
        c_local_clean = {
            **c_utc,
            "location": {"path": "events_local_clean.csv", "format": "csv"},
            "columns": {
                "id": {"type": "string", "nullable": False},
                "fetched_at": {
                    "type": "timestamp",
                    "time_zone": "local:Europe/Prague",
                },
            },
        }
        rep_local_clean = V.verify_contract(c_local_clean, project_root=str(tmp_path))
        assert rep_local_clean["valid"] is True
        assert _find_assertion(rep_local_clean, "tz_marker")["status"] == "passed"

        # Broken local twin: row 1 has `Z` marker
        c_local_broken = {
            **c_local_clean,
            "location": {"path": "events_mixed.csv", "format": "csv"},
        }
        rep_local_broken = V.verify_contract(c_local_broken, project_root=str(tmp_path))
        assert rep_local_broken["valid"] is False
        tz_local_broken = _find_assertion(rep_local_broken, "tz_marker")
        assert tz_local_broken["status"] == "failed"
        assert tz_local_broken["failed_count"] == 1
        assert tz_local_broken["sample_keys"] == ["1"]

        # 3. `time_zone: naive`: naive string passes; `Z` or `+02:00` or `+00:00` fails
        c_naive_broken = {
            **c_utc,
            "location": {"path": "events_mixed.csv", "format": "csv"},
            "columns": {
                "id": {"type": "string", "nullable": False},
                "fetched_at": {"type": "timestamp", "time_zone": "naive"},
            },
        }
        rep_naive_broken = V.verify_contract(c_naive_broken, project_root=str(tmp_path))
        assert rep_naive_broken["valid"] is False
        tz_naive_broken = _find_assertion(rep_naive_broken, "tz_marker")
        assert tz_naive_broken["failed_count"] == 3  # rows 1, 3, 4 have Z or offset

        # 4. Parquet physical column types (`TIMESTAMP` vs `TIMESTAMP WITH TIME ZONE`)
        pq_naive = tmp_path / "naive_ts.parquet"
        pq_tz = tmp_path / "tz_ts.parquet"
        con = duckdb.connect(":memory:")
        con.execute(
            f"COPY (SELECT '1' AS id, TIMESTAMP '2026-10-01 08:00:00' AS fetched_at) "
            f"TO '{pq_naive.as_posix()}' (FORMAT PARQUET);"
        )
        con.execute(
            f"COPY (SELECT '1' AS id, TIMESTAMPTZ '2026-10-01 08:00:00Z' AS fetched_at) "
            f"TO '{pq_tz.as_posix()}' (FORMAT PARQUET);"
        )
        con.close()

        # Parquet naive TIMESTAMP + contract `UTC` -> `not_verified` (NOT a false failure!)
        c_pq_naive_utc = {
            **c_utc,
            "location": {"path": "naive_ts.parquet", "format": "parquet"},
        }
        rep_pq_naive_utc = V.verify_contract(c_pq_naive_utc, project_root=str(tmp_path))
        assert rep_pq_naive_utc["valid"] is True
        tz_pq_naive = _find_assertion(rep_pq_naive_utc, "tz_marker")
        assert tz_pq_naive["status"] == "not_verified"
        assert "naive TIMESTAMP type; cannot verify UTC marker" in tz_pq_naive["message"]

        # Parquet TIMESTAMPTZ + contract `UTC` -> `passed`
        c_pq_tz_utc = {
            **c_utc,
            "location": {"path": "tz_ts.parquet", "format": "parquet"},
        }
        rep_pq_tz_utc = V.verify_contract(c_pq_tz_utc, project_root=str(tmp_path))
        assert rep_pq_tz_utc["valid"] is True
        assert _find_assertion(rep_pq_tz_utc, "tz_marker")["status"] == "passed"

        # Parquet TIMESTAMPTZ + contract `naive` -> `failed`
        c_pq_tz_naive = {
            **c_naive_broken,
            "location": {"path": "tz_ts.parquet", "format": "parquet"},
        }
        rep_pq_tz_naive = V.verify_contract(c_pq_tz_naive, project_root=str(tmp_path))
        assert rep_pq_tz_naive["valid"] is False
        assert _find_assertion(rep_pq_tz_naive, "tz_marker")["status"] == "failed"

        # Parquet TIMESTAMPTZ + contract `local:Europe/Prague` -> `not_verified`
        c_pq_tz_local = {
            **c_local_clean,
            "location": {"path": "tz_ts.parquet", "format": "parquet"},
        }
        rep_pq_tz_local = V.verify_contract(c_pq_tz_local, project_root=str(tmp_path))
        assert rep_pq_tz_local["valid"] is True
        assert _find_assertion(rep_pq_tz_local, "tz_marker")["status"] == "not_verified"

    def test_r2_4_status_lifecycle_absent_expected_zero_false_alarms(
        self, tmp_path: Path
    ):
        """Rows with `removed_on_purpose`, `released`, or `refetchable`
        (`means: absent_expected`) do NOT exist on disk and must pass
        `path_resolves`, `exists_per_status`, and `size_matches_disk` with
        zero false alarms. A missing `fetched` (`means: present`) or `failed`
        (`means: absent_unexpected`) row must fail `exists_per_status`."""
        _write_data_md(tmp_path, "bases:\n  data_root: ./data")
        present_file = tmp_path / "data" / "run1" / "ok.warc"
        present_file.parent.mkdir(parents=True, exist_ok=True)
        present_file.write_bytes(b"x" * 1000)

        clean_csv = tmp_path / "logs" / "lifecycle_clean.csv"
        clean_csv.parent.mkdir(parents=True, exist_ok=True)
        clean_csv.write_text(
            "run_id,url,status,local_path,bytes_saved\n"
            "r1,https://ex.org/1,fetched,run1/ok.warc,1.00\n"
            "r1,https://ex.org/2,removed_on_purpose,run1/removed.warc,5.00\n"
            "r1,https://ex.org/3,released,run1/released.warc,2.00\n"
            "r1,https://ex.org/4,refetchable,run1/refetch.warc,0.00\n",
            encoding="utf-8",
        )
        contract_clean = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "fetch_log",
                "kind": "files",
                "location": {"path": "logs/lifecycle_clean.csv", "format": "csv"},
                "grain": "one row per fetch attempt",
                "keys": [["run_id", "url"]],
                "columns": {
                    "run_id": {"type": "string", "nullable": False},
                    "url": {"type": "string", "nullable": False},
                    "status": {
                        "type": "string",
                        "values": {
                            "fetched": {"means": "present"},
                            "removed_on_purpose": {"means": "absent_expected"},
                            "released": {"means": "absent_expected"},
                            "refetchable": {"means": "absent_expected"},
                            "failed": {"means": "absent_unexpected"},
                        },
                    },
                    "local_path": {
                        "type": "path",
                        "relative_to": "data_root",
                        "status_column": "status",
                    },
                    "bytes_saved": {
                        "type": "number",
                        "unit": "KB",
                        "size_of": "local_path",
                    },
                },
            },
            path="contracts/fetch_log.yaml",
        )
        rep_clean = V.verify_contract(
            contract_clean,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_clean["valid"] is True
        assert rep_clean["findings"] == []
        ex_clean = _find_assertion(rep_clean, "exists_per_status", "local_path")
        assert ex_clean["status"] == "passed"
        assert ex_clean["details"]["absent_expected_count"] == 3
        assert ex_clean["details"]["absent_expected_statuses"] == [
            "refetchable",
            "released",
            "removed_on_purpose",
        ]
        assert "3 rows absent as expected" in ex_clean["message"]

        # Broken twin: a `fetched` (`present`) row (including one in a new
        # subdir `run2/` and one with an empty path) and a `failed`
        # (`absent_unexpected`) row are missing on disk -> `path_resolves`
        # still passes (since `run1/ok.warc` proves `data_root` is the right
        # base), while `exists_per_status` fails on the 3 missing rows.
        broken_csv = tmp_path / "logs" / "lifecycle_broken.csv"
        broken_csv.write_text(
            "run_id,url,status,local_path,bytes_saved\n"
            "r1,https://ex.org/1,fetched,run1/ok.warc,1.00\n"
            "r1,https://ex.org/2,removed_on_purpose,,0.00\n"
            "r1,https://ex.org/5,fetched,run2/missing_present.warc,1.00\n"
            "r1,https://ex.org/6,failed,,0.00\n"
            "r1,https://ex.org/7,fetched,,1.00\n",
            encoding="utf-8",
        )
        contract_broken = {
            **contract_clean,
            "location": {"path": "logs/lifecycle_broken.csv", "format": "csv"},
        }
        rep_broken = V.verify_contract(
            contract_broken,
            project_root=str(tmp_path),
            bases={"data_root": "./data"},
        )
        assert rep_broken["valid"] is False
        assert _find_assertion(rep_broken, "path_resolves", "local_path")["status"] == "passed"
        ex_broken = _find_assertion(rep_broken, "exists_per_status", "local_path")
        assert ex_broken["status"] == "failed"
        assert ex_broken["failed_count"] == 3
        assert ex_broken["details"]["present_missing"] == 2
        assert ex_broken["details"]["absent_unexpected"] == 1
        assert ex_broken["details"]["absent_expected_count"] == 1
        assert ex_broken["sample_keys"] == [
            ["r1", "https://ex.org/5"],
            ["r1", "https://ex.org/6"],
            ["r1", "https://ex.org/7"],
        ]

    def test_r2_5_collection_rate_ratio_metric_duckdb_and_sqlite(self, tmp_path: Path):
        """Compile `collection_rate` (`numerator: status = 'fetched'`,
        `denominator: status <> 'removed_on_purpose'`, `grain: per run_id`)
        and execute on both DuckDB and SQLite."""
        metric = {
            "version": 1,
            "metric": "collection_rate",
            "dataset": "fetch_log",
            "kind": "ratio",
            "grain": "per run_id",
            "numerator": {
                "agg": "count",
                "column": "*",
                "where": "status = 'fetched'",
            },
            "denominator": {
                "agg": "count",
                "column": "*",
                "where": "status <> 'removed_on_purpose'",
            },
        }
        allowed_cols = {"run_id", "url", "status"}
        rows = [
            # run r1: 2 fetched, 1 failed, 1 removed_on_purpose -> 2 / 3
            ("r1", "u1", "fetched"),
            ("r1", "u2", "fetched"),
            ("r1", "u3", "failed"),
            ("r1", "u4", "removed_on_purpose"),
            # run r2: 1 fetched, 1 removed_on_purpose -> 1 / 1 = 1.0
            ("r2", "u5", "fetched"),
            ("r2", "u6", "removed_on_purpose"),
        ]

        # DuckDB execution
        duck_sql = A.compile_metric_sql(
            metric, dialect="duckdb", allowed_columns=allowed_cols
        )
        A.assert_single_select(duck_sql, dialect="duckdb")
        dcon = duckdb.connect(":memory:")
        dcon.execute('CREATE TABLE "fetch_log" ("run_id" VARCHAR, "url" VARCHAR, "status" VARCHAR);')
        dcon.executemany('INSERT INTO "fetch_log" VALUES (?, ?, ?);', rows)
        duck_res = dcon.execute(duck_sql).fetchall()
        dcon.close()
        assert len(duck_res) == 2
        assert duck_res[0][0] == "r1" and pytest.approx(duck_res[0][1]) == 2.0 / 3.0
        assert duck_res[1][0] == "r2" and pytest.approx(duck_res[1][1]) == 1.0

        # SQLite execution
        sqlite_sql = A.compile_metric_sql(
            metric, dialect="sqlite", allowed_columns=allowed_cols
        )
        A.assert_single_select(sqlite_sql, dialect="sqlite")
        scon = sqlite3.connect(":memory:")
        scon.execute('CREATE TABLE "fetch_log" ("run_id" TEXT, "url" TEXT, "status" TEXT);')
        scon.executemany('INSERT INTO "fetch_log" VALUES (?, ?, ?);', rows)
        sqlite_res = scon.execute(sqlite_sql).fetchall()
        scon.close()
        assert len(sqlite_res) == 2
        assert sqlite_res[0][0] == "r1" and pytest.approx(sqlite_res[0][1]) == 2.0 / 3.0
        assert sqlite_res[1][0] == "r2" and pytest.approx(sqlite_res[1][1]) == 1.0


# ---------------------------------------------------------------------------
# 2. Core SQL Assertions Across DuckDB & SQLite
# ---------------------------------------------------------------------------


class TestCoreSQLAssertions:
    """Test `not_null`, `unique`, `accepted_values`, `freshness`, and v1 contracts."""

    def test_composite_key_unique_and_accepted_values_nullable_rules(
        self, tmp_path: Path
    ):
        """Verify composite `keys: [[run_id, url]]`, `accepted_values` with
        default `nullable: false` vs explicit `nullable: true`, and `freshness`
        with injected deterministic `now` across DuckDB and SQLite."""
        sqlite_path = tmp_path / "app.sqlite"
        scon = sqlite3.connect(str(sqlite_path))
        scon.execute(
            'CREATE TABLE "attempts" ('
            '"run_id" TEXT, "url" TEXT, "status" TEXT, '
            '"opt_tag" TEXT, "created_at" TEXT);'
        )
        scon.executemany(
            'INSERT INTO "attempts" VALUES (?, ?, ?, ?, ?);',
            [
                ("r1", "u1", "ok", None, "2026-10-01T10:00:00Z"),
                ("r1", "u1", "bad_status", "alpha", "2026-10-01T11:00:00Z"),
                ("r1", "u2", None, "undeclared_tag", "2026-10-01T11:30:00Z"),
            ],
        )
        scon.commit()
        scon.close()

        contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "attempts",
                "kind": "table",
                "location": {"database": "app.sqlite", "relation": "attempts"},
                "grain": "one row per attempt",
                "keys": [["run_id", "url"]],
                "freshness_sla": {"column": "created_at", "max_age": "24h"},
                "columns": {
                    "run_id": {"type": "string", "nullable": False},
                    "url": {"type": "string", "nullable": False},
                    "status": {"type": "string", "values": ["ok", "retry"]},
                    "opt_tag": {
                        "type": "string",
                        "nullable": True,
                        "values": ["alpha", "beta"],
                    },
                    "created_at": {"type": "timestamp", "time_zone": "UTC"},
                },
            },
            path="contracts/attempts.yaml",
        )

        # Fresh at 2026-10-01T12:00:00Z (age 30m <= 24h), stale at 2026-10-05T00:00:00Z
        rep = V.verify_contract(
            contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
        )
        assert rep["valid"] is False
        assert rep["checked_at"] == "2026-10-01T12:00:00+00:00"

        uniq = _find_assertion(rep, "unique", ["run_id", "url"])
        assert uniq["status"] == "failed"
        assert uniq["failed_count"] == 1
        assert uniq["sample_keys"] == [["r1", "u1"]]

        # `status` has `values` without `nullable: true` -> NULL and 'bad_status' both fail (2 rows)
        av_status = _find_assertion(rep, "accepted_values", "status")
        assert av_status["status"] == "failed"
        assert av_status["failed_count"] == 2

        # `opt_tag` has `nullable: true` -> NULL passes, only 'undeclared_tag' fails (1 row)
        # Private by default (DESIGN §5): non-key column value 'undeclared_tag' is NEVER shown!
        av_tag = _find_assertion(rep, "accepted_values", "opt_tag")
        assert av_tag["status"] == "failed"
        assert av_tag["failed_count"] == 1
        assert av_tag["sample_keys"] == [["r1", "u2"]]
        assert "undeclared_tag" not in json.dumps(rep)
        assert "bad_status" not in json.dumps(rep)

        fresh_ok = _find_assertion(rep, "freshness", "created_at")
        assert fresh_ok["status"] == "passed"

        # Re-run with stale clock -> `freshness` fails deterministically
        rep_stale = V.verify_contract(
            contract,
            project_root=str(tmp_path),
            now=datetime(2026, 10, 5, 0, 0, 0, tzinfo=timezone.utc),
        )
        fresh_stale = _find_assertion(rep_stale, "freshness", "created_at")
        assert fresh_stale["status"] == "failed"
        assert "older than 24h" in fresh_stale["message"]

        # Also test `freshness` on a `type: date` column (`YYYY-MM-DD`) and a dotted `dataset` name
        date_csv = tmp_path / "daily.csv"
        date_csv.write_text("day_id,report_date\nd1,2026-10-01\n", encoding="utf-8")
        date_contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "raw.daily_reports",
                "kind": "files",
                "location": {"path": "daily.csv", "format": "csv"},
                "grain": "one row per day",
                "keys": ["day_id"],
                "freshness_sla": {"column": "report_date", "max_age": "7d"},
                "columns": {
                    "day_id": {"type": "string", "nullable": False},
                    "report_date": {"type": "date", "nullable": False},
                },
            },
            path="contracts/daily.yaml",
        )
        rep_date = V.verify_contract(
            date_contract,
            project_root=str(tmp_path),
            now="2026-10-03T00:00:00Z",
        )
        assert rep_date["valid"] is True
        assert _find_assertion(rep_date, "freshness", "report_date")["status"] == "passed"

    def test_v1_contract_compatibility_on_duckdb(self, tmp_path: Path):
        """v1 contract (`table`, `keys`, `allowed_values`, `pii`) compiles and
        runs through `verify_contract` when pointed at a `.duckdb` database,
        including v1 names with backticks, spaces, and hyphens."""
        db_path = tmp_path / "shop.duckdb"
        dcon = duckdb.connect(str(db_path))
        dcon.execute(
            'CREATE TABLE "orders" ("order_id" VARCHAR, "email" VARCHAR, "status" VARCHAR);'
        )
        dcon.execute(
            "INSERT INTO \"orders\" VALUES ('o1', 'a@x.com', 'paid'), ('o1', 'b@x.com', 'bogus');"
        )
        dcon.close()

        v1_contract = {
            "table": "orders",
            "keys": ["order_id"],
            "allowed_values": {"status": ["paid", "refunded"]},
            "pii": ["email"],
            "location": {"database": "shop.duckdb", "relation": "orders"},
        }
        rep = V.verify_contract(v1_contract, project_root=str(tmp_path))
        assert rep["valid"] is False
        assert _find_assertion(rep, "unique", "order_id")["failed_count"] == 1
        assert _find_assertion(rep, "accepted_values", "status")["failed_count"] == 1

        # Verify v1 names with backticks, per-part quotes, hyphens, and spaces compile to single quoted SELECTs
        for v1_ex in [
            {"table": "my-project.ds.orders", "keys": ["order_id"]},
            {"table": "`my-project.ds.orders`", "keys": ["order_id"]},
            {"table": "`my-project`.`ds`.`orders`", "keys": ["order_id"]},
            {"table": '"schema"."Order Items"', "keys": ["Order ID"]},
            {"table": "Order Items", "keys": ["Order ID"]},
        ]:
            for dialect in ("duckdb", "sqlite"):
                comps = A.compile_contract_assertions(v1_ex, dialect=dialect)
                assert len(comps) == 2  # not_null + unique
                for comp in comps:
                    stmt = A.assert_single_select(comp.sql, dialect=dialect)
                    for ident in stmt.find_all(exp.Identifier):
                        assert ident.args.get("quoted") is True
                        assert "`" not in ident.name
                        assert '"' not in ident.name

    def test_duckdb_missing_error_message(self, monkeypatch: pytest.MonkeyPatch):
        """When `duckdb` is not installed, `DuckDBMissingError` instructs the
        user to run `truequery setup data`."""
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "duckdb":
                raise ImportError("No module named 'duckdb'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        with pytest.raises(
            V.DuckDBMissingError,
            match="the data reader isn't installed. Run: truequery setup data",
        ):
            V.open_duckdb_for_files([])


# ---------------------------------------------------------------------------
# 3. Security & Safety Invariants (PLAN-0.2 §3)
# ---------------------------------------------------------------------------


class TestSecurityAndSafety:
    """Lockdown tests for DuckDB, SQLite, untrusted paths, AST allowlist, PII, and drafts."""

    def test_duckdb_files_connection_lockdown(self, tmp_path: Path):
        """`open_duckdb_for_files` allows reading only the listed files and blocks:
        - `COPY ... TO` a new file
        - `COPY ... TO` an allowed file (overwrite attempt)
        - reading any unlisted file on disk
        - unlocking `enable_external_access`"""
        allowed_csv = tmp_path / "allowed.csv"
        allowed_csv.write_text("id,val\n1,hello\n", encoding="utf-8")
        secret_csv = tmp_path / "secret.csv"
        secret_csv.write_text("secret\ntop_secret\n", encoding="utf-8")
        out_file = tmp_path / "pwned.csv"

        con = V.open_duckdb_for_files([str(allowed_csv)])
        try:
            # Reading the allowed file succeeds
            rows = con.execute(
                f"SELECT * FROM read_csv('{allowed_csv.resolve().as_posix()}', all_varchar=true);"
            ).fetchall()
            assert rows == [("1", "hello")]

            # 1. COPY TO a new file is blocked
            with pytest.raises(duckdb.Error):
                con.execute(f"COPY (SELECT 1) TO '{out_file.as_posix()}';")
            assert not out_file.exists()

            # 2. COPY TO the allowed file itself (overwrite attempt) is blocked
            with pytest.raises(duckdb.Error):
                con.execute(
                    f"COPY (SELECT 'pwned') TO '{allowed_csv.resolve().as_posix()}';"
                )
            assert allowed_csv.read_text(encoding="utf-8") == "id,val\n1,hello\n"

            # 3. Reading an unlisted file is blocked
            with pytest.raises(duckdb.Error):
                con.execute(
                    f"SELECT * FROM read_csv('{secret_csv.resolve().as_posix()}');"
                )

            # 4. Re-enabling external access is blocked by `lock_configuration = true`
            with pytest.raises(duckdb.Error):
                con.execute("SET enable_external_access = true;")
        finally:
            con.close()

    def test_duckdb_database_connection_lockdown(self, tmp_path: Path):
        """`open_duckdb_database` opens `.duckdb` in read-only mode with
        `allowed_paths = []`, `enable_external_access = false`, and
        `lock_configuration = true`."""
        db_file = tmp_path / "warehouse.duckdb"
        outside_csv = tmp_path / "outside.csv"
        outside_csv.write_text("a\n1\n", encoding="utf-8")

        wcon = duckdb.connect(str(db_file))
        wcon.execute("CREATE TABLE items (id INTEGER, name VARCHAR);")
        wcon.execute("INSERT INTO items VALUES (1, 'widget');")
        wcon.close()

        con = V.open_duckdb_database(str(db_file))
        try:
            assert con.execute("SELECT * FROM items;").fetchall() == [(1, "widget")]

            # Writes to the database fail
            with pytest.raises(duckdb.Error):
                con.execute("INSERT INTO items VALUES (2, 'bad');")
            with pytest.raises(duckdb.Error):
                con.execute("DROP TABLE items;")

            # Writing or reading external files fails
            with pytest.raises(duckdb.Error):
                con.execute(f"COPY items TO '{(tmp_path / 'dump.csv').as_posix()}';")
            with pytest.raises(duckdb.Error):
                con.execute(
                    f"SELECT * FROM read_csv('{outside_csv.resolve().as_posix()}');"
                )
            with pytest.raises(duckdb.Error):
                con.execute("SET enable_external_access = true;")
        finally:
            con.close()

    def test_sqlite_readonly_uri_with_hash_in_path(self, tmp_path: Path):
        """A `#` in the folder or file name must be percent-encoded via
        `Path.resolve().as_uri() + '?mode=ro'` so `?mode=ro` is never swallowed
        as a URI fragment."""
        hash_dir = tmp_path / "dir#with#hash"
        hash_dir.mkdir(parents=True, exist_ok=True)
        db_path = hash_dir / "db#1.sqlite"

        wcon = sqlite3.connect(str(db_path))
        wcon.execute("CREATE TABLE users (id TEXT, name TEXT);")
        wcon.execute("INSERT INTO users VALUES ('u1', 'Ada');")
        wcon.commit()
        wcon.close()

        uri = V.sqlite_readonly_uri(str(db_path))
        assert "%23" in uri
        assert uri.endswith("?mode=ro")

        rcon = V.open_sqlite_readonly(str(db_path))
        try:
            assert rcon.execute("SELECT * FROM users;").fetchall() == [("u1", "Ada")]
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                rcon.execute("INSERT INTO users VALUES ('u2', 'Bob');")
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                rcon.execute("DROP TABLE users;")
        finally:
            rcon.close()

    def test_untrusted_paths_in_data_never_stat_outside_base(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Rows containing `..`, absolute paths, `~`, Windows drive letters, or
        symlinks escaping `real_base` must fail `path_resolves` immediately and
        NEVER pass the outside path to `os.stat()` or `os.path.exists()`."""
        base_dir = tmp_path / "project" / "data"
        base_dir.mkdir(parents=True, exist_ok=True)
        outside_secret = tmp_path / "outside_secret.txt"
        outside_secret.write_text("secret-bytes", encoding="utf-8")

        # Create a symlink inside `base_dir` pointing to `outside_secret`
        escape_link = base_dir / "evil_link.txt"
        escape_link.symlink_to(outside_secret)

        proj_root = tmp_path / "project"
        _write_data_md(proj_root, "bases:\n  data_root: ./data")
        csv_path = proj_root / "manifest.csv"
        csv_path.write_text(
            "id,status,local_path,sz\n"
            f"1,fetched,../outside_secret.txt,12\n"
            f"2,fetched,{outside_secret.as_posix()},12\n"
            "3,fetched,~/outside_secret.txt,12\n"
            "4,fetched,C:\\outside_secret.txt,12\n"
            "5,fetched,evil_link.txt,12\n",
            encoding="utf-8",
        )

        contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "manifest",
                "kind": "files",
                "location": {"path": "manifest.csv", "format": "csv"},
                "grain": "one row per file",
                "keys": ["id"],
                "columns": {
                    "id": {"type": "string", "nullable": False},
                    "status": {
                        "type": "string",
                        "values": {"fetched": {"means": "present"}},
                    },
                    "local_path": {
                        "type": "path",
                        "relative_to": "data_root",
                        "status_column": "status",
                    },
                    "sz": {"type": "integer", "unit": "bytes", "size_of": "local_path"},
                },
            },
            path="contracts/manifest.yaml",
        )

        real_outside = os.path.realpath(str(outside_secret))
        real_base = os.path.realpath(str(base_dir))
        orig_stat = os.stat
        orig_exists = os.path.exists
        touched_outside: list[str] = []

        def guarded_stat(p, *args, **kwargs):
            if isinstance(p, (str, Path)):
                rp = os.path.realpath(str(p))
                if rp == real_outside:
                    touched_outside.append(f"stat:{rp}")
            return orig_stat(p, *args, **kwargs)

        def guarded_exists(p):
            if isinstance(p, (str, Path)):
                rp = os.path.realpath(str(p))
                if rp == real_outside:
                    touched_outside.append(f"exists:{rp}")
            return orig_exists(p)

        monkeypatch.setattr(os, "stat", guarded_stat)
        monkeypatch.setattr(os.path, "exists", guarded_exists)

        rep = V.verify_contract(
            contract,
            project_root=str(proj_root),
            bases={"data_root": "./data"},
        )
        assert touched_outside == [], (
            f"os.stat/os.path.exists was called on outside path: {touched_outside}"
        )
        pr = _find_assertion(rep, "path_resolves", "local_path")
        assert pr["status"] == "failed"
        assert pr["failed_count"] == 5
        assert pr["details"]["escaped_count"] == 5
        assert pr["sample_keys"] == ["1", "2", "3", "4", "5"]
        assert V.resolve_path_within_base("bad\x00path.txt", str(base_dir)) is None

        # Also verify `expand_contract_files` rejects a glob matching a symlink outside base
        bad_contract = {
            **contract,
            "location": {" base": "data_root", "path": "*.txt", "format": "csv"},
        }
        bad_contract["location"] = {"base": "data_root", "path": "*.txt", "format": "csv"}
        with pytest.raises(V.VerificationError, match="points outside"):
            V.expand_contract_files(
                bad_contract, bases={"data_root": "./data"}, project_root=str(proj_root)
            )
        assert os.path.realpath(str(base_dir)) == real_base

    def test_identifier_quoting_and_single_select_invariant(self):
        """Every compiled assertion and metric query quotes all identifiers and
        parses as a single `exp.Select` statement (`assert_single_select`),
        including names with spaces, hyphens, `$`, and Unicode characters."""
        contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "order-items$v2",
                "kind": "table",
                "location": {
                    "database": "local.duckdb",
                    "relation": "sales.order-items$v2",
                },
                "grain": "one row per line item",
                "keys": [["Order ID", "número"]],
                "freshness_sla": {"column": "fetched at", "max_age": "12h"},
                "columns": {
                    "Order ID": {"type": "string", "nullable": False},
                    "número": {"type": "integer", "nullable": False},
                    "file size": {"type": "number", "nullable": False},
                    "status-code": {"type": "string", "values": ["ok", "err"]},
                    "fetched at": {"type": "timestamp", "time_zone": "UTC"},
                },
            },
            path="contracts/orders.yaml",
        )
        for dialect in ("duckdb", "sqlite"):
            assertions = A.compile_contract_assertions(contract, dialect=dialect)
            assert len(assertions) >= 5
            for comp in assertions:
                if comp.sql:
                    stmt = A.assert_single_select(comp.sql, dialect=dialect)
                    assert type(stmt) is exp.Select
                    assert '"sales"."order-items$v2"' in comp.sql
                    for ident in stmt.find_all(exp.Identifier):
                        assert ident.args.get("quoted") is True
                if comp.count_sql:
                    stmt_cnt = A.assert_single_select(comp.count_sql, dialect=dialect)
                    assert type(stmt_cnt) is exp.Select
                    for ident in stmt_cnt.find_all(exp.Identifier):
                        assert ident.args.get("quoted") is True

        # Metric compilation quotes every identifier and builds strictly from AST
        metric = {
            "version": 1,
            "metric": "net-revenue$usd",
            "dataset": "order-items$v2",
            "kind": "simple",
            "grain": ["Order ID"],
            "measure": {
                "agg": "sum",
                "column": "file size",
                "where": "número > -1 AND \"status-code\" IN ('ok') AND \"Order ID\" LIKE 'ORD%'",
            },
        }
        for dialect in ("duckdb", "sqlite"):
            sql = A.compile_metric_sql(
                metric,
                dialect=dialect,
                allowed_columns={"Order ID", "número", "file size", "status-code"},
            )
            stmt = A.assert_single_select(sql, dialect=dialect)
            assert type(stmt) is exp.Select
            assert '"file size"' in sql
            assert '"número"' in sql
            assert '"status-code"' in sql
            assert '"net-revenue$usd"' in sql

        # Reject injection / disallowed AST constructs in `compile_metric_sql`
        for bad_where in [
            "1 = 1",
            "número > 0; DROP TABLE x",
            "read_text('/etc/passwd') = 'a'",
            "número IN (SELECT 1)",
            "t.número > 0",
        ]:
            bad_m = {
                **metric,
                "measure": {"agg": "count", "column": "*", "where": bad_where},
            }
            with pytest.raises(ValueError):
                A.compile_metric_sql(bad_m, dialect="duckdb")

    def test_pii_key_suppression_and_draft_provenance_in_evidence(
        self, tmp_path: Path
    ):
        """1. When a key or column is marked `pii: true`, `sample_keys` is `[]`
        and no PII value appears anywhere in the report or `.truequery/evidence.jsonl`.
        2. When a contract is `status: draft`, every finding, summary, and
        evidence record carries `(from draft contract <name>, inferred from ...)`
        and `means: TODO` status values are skipped without false alarms."""
        csv_file = tmp_path / "users.csv"
        csv_file.write_text(
            "email,status,local_path\n"
            "alice-secret@example.com,mystery_status,missing.bin\n"
            "alice-secret@example.com,undeclared_val,missing.bin\n",
            encoding="utf-8",
        )
        draft_contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "users",
                "status": "draft",
                "inferred_from": "100-row sample",
                "kind": "files",
                "location": {"path": "users.csv", "format": "csv"},
                "grain": "unknown",
                "keys": ["email"],
                "columns": {
                    "email": {"type": "string", "nullable": False, "pii": True},
                    "status": {
                        "type": "string",
                        "values": {"mystery_status": {"means": "TODO"}},
                    },
                    "local_path": {"type": "path", "status_column": "status"},
                },
            },
            path="contracts/users.yaml",
        )

        rep = V.verify_contract(
            draft_contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
            write_evidence=True,
        )
        assert rep["valid"] is False
        assert rep["contract_status"] == "draft"
        expected_prov = "(from draft contract users, inferred from 100-row sample)"
        assert rep["provenance_note"] == expected_prov
        assert expected_prov in rep["summary"]

        # `exists_per_status` skipped `means: TODO` with zero failures
        ex_res = _find_assertion(rep, "exists_per_status", "local_path")
        assert ex_res["status"] == "passed"
        assert ex_res["details"]["unconfirmed_todo_statuses"] == ["mystery_status"]
        assert ex_res["details"]["unconfirmed_todo_rows"] == 1

        # `unique` and `accepted_values` failed, but PII redacted all sample keys/values!
        uniq_res = _find_assertion(rep, "unique", "email")
        assert uniq_res["status"] == "failed"
        assert uniq_res["failed_count"] == 1
        assert uniq_res["pii_redacted"] is True
        assert uniq_res["sample_keys"] == []

        for finding in rep["findings"]:
            assert finding["sample_keys"] == []
            assert finding["pii_redacted"] is True
            assert expected_prov in finding["message"]
            assert "alice-secret@example.com" not in json.dumps(finding)

        ev_path = tmp_path / ".truequery" / "evidence.jsonl"
        assert ev_path.is_file()
        ev_text = ev_path.read_text(encoding="utf-8")
        assert "alice-secret@example.com" not in ev_text
        ev_lines = [json.loads(line) for line in ev_text.splitlines() if line.strip()]
        assert len(ev_lines) == len(rep["assertions"])
        for rec in ev_lines:
            assert rec["checked_at"] == "2026-10-01T12:00:00+00:00"
            assert rec["contract_status"] == "draft"
            assert rec["provenance_note"] == expected_prov
            assert rec["sample_keys"] == []

    def test_max_rows_and_max_bytes_limits_and_verify_project(self, tmp_path: Path):
        """`max_bytes` blocks oversized inputs; `max_rows` caps scanned rows and
        sets `source.sampled = True`; `verify_project` runs across a project."""
        _write_data_md(tmp_path)
        csv_path = tmp_path / "rows.csv"
        csv_path.write_text(
            "id\n1\n2\n3\n4\n5\n6\n7\n8\n9\n10\n",
            encoding="utf-8",
        )
        contract_dir = tmp_path / "contracts"
        contract_dir.mkdir(parents=True, exist_ok=True)
        (contract_dir / "rows.yaml").write_text(
            "version: 2\n"
            "dataset: rows\n"
            "kind: files\n"
            "location:\n"
            "  path: rows.csv\n"
            "  format: csv\n"
            "grain: one row per id\n"
            "keys: [id]\n"
            "columns:\n"
            "  id: {type: integer, nullable: false}\n",
            encoding="utf-8",
        )
        contracts, invalid = P.load_contracts_reporting(str(tmp_path))
        assert not invalid
        c = contracts["rows"]

        # Exceeding max_bytes raises VerificationError
        with pytest.raises(V.VerificationError, match="exceeds max_bytes limit"):
            V.verify_contract(c, project_root=str(tmp_path), max_bytes=5)

        # Setting max_rows=4 marks sampled=True, checks 4 rows ordered by key,
        # and reports `unique` as `not_verified` for the whole dataset
        rep_sampled = V.verify_contract(c, project_root=str(tmp_path), max_rows=4)
        assert rep_sampled["valid"] is True
        assert rep_sampled["source"]["rows"] == 10
        assert rep_sampled["source"]["sampled"] is True
        nn_sampled = _find_assertion(rep_sampled, "not_null", "id")
        assert nn_sampled["rows_checked"] == 4
        assert "in the first 4 rows by id" in nn_sampled["message"]
        uniq_sampled = _find_assertion(rep_sampled, "unique", "id")
        assert uniq_sampled["rows_checked"] == 4
        assert uniq_sampled["status"] == "not_verified"
        assert (
            uniq_sampled["message"]
            == "key id not verified for the whole dataset (no duplicates in the first 4 rows by id)"
        )

        # `verify_project` verifies all local contracts in `tmp_path`
        proj_rep = V.verify_project(str(tmp_path), now="2026-10-01T00:00:00Z")
        assert proj_rep["valid"] is True
        assert len(proj_rep["datasets"]) == 1
        assert proj_rep["datasets"][0]["dataset"] == "rows"


# ---------------------------------------------------------------------------
# 4. PR #31 Review Fixes (PROMPT-0.2-PR-B-REVIEW.md)
# ---------------------------------------------------------------------------


class TestPRBReviewFixes:
    """Regression tests for every review item in PROMPT-0.2-PR-B-REVIEW.md."""

    def test_review_1_freshness_parsed_timestamps_both_orders_and_local_zone(
        self, tmp_path: Path
    ):
        """Review §1:
        - `2026-10-01T22:00:00Z` and `2026-10-02T01:00:00+05:00` (which is 20:00Z)
          at `now = 22:30Z`, `max_age: 1h`: newest is 22:00Z (fresh) in BOTH row orders.
        - Stale values with different offsets fail in BOTH row orders.
        - Mixed `Z`/offset/naive file on `local:Europe/Prague`: naive value is
          converted with `zoneinfo` (never read as UTC).
        - Unparseable values or unavailable `zoneinfo` zones report `not_verified`
          (never dropped silently)."""
        base_contract = {
            "version": 2,
            "dataset": "events",
            "kind": "files",
            "location": {"path": "events.csv", "format": "csv"},
            "grain": "one row per event",
            "keys": ["id"],
            "freshness_sla": {"column": "ts", "max_age": "1h"},
            "columns": {
                "id": {"type": "string", "nullable": False},
                "ts": {"type": "timestamp", "time_zone": "UTC"},
            },
        }
        csv_file = tmp_path / "events.csv"

        # 1a. Fresh case in both row orders (`2026-10-02T01:00:00+05:00` is 20:00Z; `22:00Z` is newer)
        for rows_text in [
            "id,ts\n1,2026-10-01T22:00:00Z\n2,2026-10-02T01:00:00+05:00\n",
            "id,ts\n1,2026-10-02T01:00:00+05:00\n2,2026-10-01T22:00:00Z\n",
        ]:
            csv_file.write_text(rows_text, encoding="utf-8")
            c = contract_v2.validate_contract_v2(base_contract, "contracts/events.yaml")
            rep = V.verify_contract(
                c, project_root=str(tmp_path), now="2026-10-01T22:30:00Z"
            )
            fresh = _find_assertion(rep, "freshness", "ts")
            assert fresh["status"] == "passed"
            assert fresh["details"]["max_timestamp"] == "2026-10-01T22:00:00+00:00"

        # 1b. Stale case in both row orders (`2026-10-01T21:00:00Z` and `2026-10-02T00:00:00+03:00` = 21:00Z)
        for rows_text in [
            "id,ts\n1,2026-10-01T21:00:00Z\n2,2026-10-02T00:00:00+03:00\n",
            "id,ts\n1,2026-10-02T00:00:00+03:00\n2,2026-10-01T21:00:00Z\n",
        ]:
            csv_file.write_text(rows_text, encoding="utf-8")
            c = contract_v2.validate_contract_v2(base_contract, "contracts/events.yaml")
            rep = V.verify_contract(
                c, project_root=str(tmp_path), now="2026-10-01T22:30:00Z"
            )
            fresh = _find_assertion(rep, "freshness", "ts")
            assert fresh["status"] == "failed"
            assert fresh["details"]["max_timestamp"] == "2026-10-01T21:00:00+00:00"

        # 1c. Mixed Z / offset / naive file on `local:Europe/Prague` (UTC+2 on 2026-10-01):
        # Naive `2026-10-01 23:00:00` in Prague is `2026-10-01T21:00:00+00:00` (NOT 23:00Z!).
        for mixed_rows in [
            "id,ts\n1,2026-10-01T20:30:00Z\n2,2026-10-02T01:00:00+05:00\n3,2026-10-01 23:00:00\n",
            "id,ts\n1,2026-10-01 23:00:00\n2,2026-10-02T01:00:00+05:00\n3,2026-10-01T20:30:00Z\n",
        ]:
            csv_file.write_text(mixed_rows, encoding="utf-8")
            local_contract = contract_v2.validate_contract_v2(
                {
                    **base_contract,
                    "columns": {
                        "id": {"type": "string", "nullable": False},
                        "ts": {"type": "timestamp", "time_zone": "local:Europe/Prague"},
                    },
                },
                "contracts/events.yaml",
            )
            rep_local = V.verify_contract(
                local_contract, project_root=str(tmp_path), now="2026-10-01T22:30:00Z"
            )
            fresh_local = _find_assertion(rep_local, "freshness", "ts")
            assert fresh_local["status"] == "failed"
            assert fresh_local["details"]["max_timestamp"] == "2026-10-01T21:00:00+00:00"

        # 1d. Unparseable timestamp row is reported as `not_verified` (never silently dropped)
        csv_file.write_text(
            "id,ts\n1,2026-10-01T22:00:00Z\n2,not-a-timestamp\n",
            encoding="utf-8",
        )
        c_unparseable = contract_v2.validate_contract_v2(
            base_contract, "contracts/events.yaml"
        )
        rep_unp = V.verify_contract(
            c_unparseable, project_root=str(tmp_path), now="2026-10-01T22:30:00Z"
        )
        fresh_unp = _find_assertion(rep_unp, "freshness", "ts")
        assert fresh_unp["status"] == "not_verified"
        assert fresh_unp["failed_count"] == 0
        assert "1 value(s) could not be parsed" in fresh_unp["message"]

        # 1e. Unknown zoneinfo zone on naive local timestamp -> `not_verified`
        csv_file.write_text("id,ts\n1,2026-10-01 23:00:00\n", encoding="utf-8")
        unknown_zone_contract = {
            **local_contract,
            "columns": {
                "id": {"type": "string", "nullable": False},
                "ts": {"type": "timestamp", "time_zone": "local:Mars/Olympus_Mons"},
            },
        }
        rep_unk_tz = V.verify_contract(
            unknown_zone_contract,
            project_root=str(tmp_path),
            now="2026-10-01T22:30:00Z",
        )
        fresh_unk_tz = _find_assertion(rep_unk_tz, "freshness", "ts")
        assert fresh_unk_tz["status"] == "not_verified"
        assert "not available in zoneinfo" in fresh_unk_tz["message"]

    def test_review_2_tz_marker_on_sqlite_checks_values_and_duckdb_names_source(
        self, tmp_path: Path
    ):
        """Review §2:
        - SQLite column declared `TIMESTAMP` or `DATETIME` with values
          `2026-10-01T09:00:00Z`, `2026-10-01T10:00:00+02:00` must check values:
          - `time_zone: naive` -> `failed` (failed_count == 2)
          - `time_zone: UTC` -> `failed` (failed_count == 1)
        - DuckDB table with native naive `TIMESTAMP` column under `time_zone: UTC`
          says `"DuckDB table column 'ts' has naive TIMESTAMP type; cannot verify UTC marker"`."""
        sqlite_db = tmp_path / "events.sqlite"
        scon = sqlite3.connect(str(sqlite_db))
        scon.execute('CREATE TABLE "events" ("id" TEXT, "ts" TIMESTAMP);')
        scon.executemany(
            'INSERT INTO "events" VALUES (?, ?);',
            [
                ("1", "2026-10-01T09:00:00Z"),
                ("2", "2026-10-01T10:00:00+02:00"),
            ],
        )
        scon.commit()
        scon.close()

        for tz, expected_failed, expected_keys in [
            ("naive", 2, ["1", "2"]),
            ("UTC", 1, ["2"]),
        ]:
            c_sqlite = contract_v2.validate_contract_v2(
                {
                    "version": 2,
                    "dataset": "events",
                    "kind": "table",
                    "location": {"database": "events.sqlite", "relation": "events"},
                    "grain": "one row per event",
                    "keys": ["id"],
                    "columns": {
                        "id": {"type": "string", "nullable": False},
                        "ts": {"type": "timestamp", "time_zone": tz},
                    },
                },
                "contracts/events.yaml",
            )
            rep = V.verify_contract(c_sqlite, project_root=str(tmp_path))
            assert rep["valid"] is False
            tz_res = _find_assertion(rep, "tz_marker", "ts")
            assert tz_res["status"] == "failed"
            assert tz_res["failed_count"] == expected_failed
            assert tz_res["sample_keys"] == expected_keys
            assert "Parquet" not in tz_res["message"]

        # DuckDB table with native `TIMESTAMP` column under `time_zone: UTC`
        duck_db = tmp_path / "events.duckdb"
        dcon = duckdb.connect(str(duck_db))
        dcon.execute(
            "CREATE TABLE events (id VARCHAR, ts TIMESTAMP);"
            "INSERT INTO events VALUES ('1', TIMESTAMP '2026-10-01 09:00:00');"
        )
        dcon.close()
        c_duck = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "events",
                "kind": "table",
                "location": {"database": "events.duckdb", "relation": "events"},
                "grain": "one row per event",
                "keys": ["id"],
                "columns": {
                    "id": {"type": "string", "nullable": False},
                    "ts": {"type": "timestamp", "time_zone": "UTC"},
                },
            },
            "contracts/events.yaml",
        )
        rep_duck = V.verify_contract(c_duck, project_root=str(tmp_path))
        tz_duck = _find_assertion(rep_duck, "tz_marker", "ts")
        assert tz_duck["status"] == "not_verified"
        assert (
            tz_duck["message"]
            == "DuckDB table column 'ts' has naive TIMESTAMP type; cannot verify UTC marker"
        )

    def test_review_3_deterministic_sampling_and_byte_identical_evidence(
        self, tmp_path: Path
    ):
        """Review §3:
        - Every sample (`max_rows` and `LIMIT 5` sample keys) is ordered by key
          columns (or all selected columns when there is no key).
        - When sampled, every message states `"in the first N rows by <key>"`,
          and `unique` is reported as `not_verified` for the whole dataset.
        - Two runs on the same data produce byte-identical evidence records."""
        csv_path = tmp_path / "unsorted.csv"
        # Rows written in reverse/shuffled key order with >5 failures
        csv_path.write_text(
            "k,status,val\n"
            "k09,bad,\n"
            "k03,bad,\n"
            "k07,bad,\n"
            "k01,bad,\n"
            "k05,bad,\n"
            "k02,bad,\n"
            "k08,bad,\n"
            "k04,bad,\n"
            "k06,bad,\n",
            encoding="utf-8",
        )
        contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "unsorted",
                "kind": "files",
                "location": {"path": "unsorted.csv", "format": "csv"},
                "grain": "one row per k",
                "keys": ["k"],
                "columns": {
                    "k": {"type": "string", "nullable": False},
                    "status": {"type": "string", "values": ["ok"]},
                    "val": {"type": "string", "nullable": False},
                },
            },
            "contracts/unsorted.yaml",
        )

        # Full run: sample_keys must be the first 5 sorted by `k`
        rep1 = V.verify_contract(
            contract, project_root=str(tmp_path), now="2026-10-01T12:00:00Z"
        )
        rep2 = V.verify_contract(
            contract, project_root=str(tmp_path), now="2026-10-01T12:00:00Z"
        )
        assert _find_assertion(rep1, "not_null", "val")["sample_keys"] == [
            "k01", "k02", "k03", "k04", "k05",
        ]
        assert _find_assertion(rep1, "accepted_values", "status")["sample_keys"] == [
            "k01", "k02", "k03", "k04", "k05",
        ]
        assert json.dumps(rep1["evidence_records"], sort_keys=True) == json.dumps(
            rep2["evidence_records"], sort_keys=True
        )

        # Sampled run (`max_rows=4`): scans k01..k04 deterministically, reports
        # `in the first 4 rows by k` in every message, and `unique` is `not_verified`
        s1 = V.verify_contract(
            contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
            max_rows=4,
        )
        s2 = V.verify_contract(
            contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
            max_rows=4,
        )
        assert json.dumps(s1["evidence_records"], sort_keys=True) == json.dumps(
            s2["evidence_records"], sort_keys=True
        )
        for item in s1["assertions"]:
            assert "in the first 4 rows by k" in item["message"]
        uniq_s1 = _find_assertion(s1, "unique", "k")
        assert uniq_s1["status"] == "not_verified"
        assert _find_assertion(s1, "not_null", "val")["sample_keys"] == [
            "k01", "k02", "k03", "k04",
        ]

        # Contract with no keys (`keys: []`): orders by all columns deterministically
        nokey_contract = contract_v2.validate_contract_v2(
            {**contract, "keys": []},
            "contracts/unsorted.yaml",
        )
        nk1 = V.verify_contract(
            nokey_contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
            max_rows=4,
        )
        nk2 = V.verify_contract(
            nokey_contract,
            project_root=str(tmp_path),
            now="2026-10-01T12:00:00Z",
            max_rows=4,
        )
        assert json.dumps(nk1["evidence_records"], sort_keys=True) == json.dumps(
            nk2["evidence_records"], sort_keys=True
        )
        for item in nk1["assertions"]:
            assert "in the first 4 rows by (k, status, val)" in item["message"]

    def test_review_4_escaping_path_without_relative_to_caught_and_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Review §4:
        When a `type: path` column has `status_column` but no `relative_to`,
        a row with `../../etc/hosts` is never dropped silently: both
        `path_resolves` (compiled with `project_root` as base) and
        `exists_per_status` report the escaping row, and `exists_per_status`
        counts both `../../etc/hosts` and `missing.bin` (`failed_count == 2`)."""
        csv_file = tmp_path / "files.csv"
        csv_file.write_text(
            "id,status,local_path\n"
            "r1,fetched,../../etc/hosts\n"
            "r2,fetched,missing.bin\n",
            encoding="utf-8",
        )
        contract = contract_v2.validate_contract_v2(
            {
                "version": 2,
                "dataset": "files",
                "kind": "files",
                "location": {"path": "files.csv", "format": "csv"},
                "grain": "one row per file",
                "keys": ["id"],
                "columns": {
                    "id": {"type": "string", "nullable": False},
                    "status": {
                        "type": "string",
                        "values": {"fetched": {"means": "present"}},
                    },
                    "local_path": {"type": "path", "status_column": "status"},
                },
            },
            "contracts/files.yaml",
        )
        rep = V.verify_contract(contract, project_root=str(tmp_path))
        assert rep["valid"] is False

        pr = _find_assertion(rep, "path_resolves", "local_path")
        assert pr["status"] == "failed"
        assert pr["failed_count"] == 1
        assert pr["details"]["escaped_count"] == 1
        assert pr["sample_keys"] == ["r1"]

        ex = _find_assertion(rep, "exists_per_status", "local_path")
        assert ex["status"] == "failed"
        assert ex["failed_count"] == 2
        assert ex["details"]["present_missing"] == 1
        assert ex["details"]["escaped_count"] == 1
        assert ex["sample_keys"] == ["r1", "r2"]
