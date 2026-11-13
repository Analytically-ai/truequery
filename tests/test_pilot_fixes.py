"""The three fixes the 0.2 benchmark pilot found.

1. `verify` names paths that repeat the base folder's own name
   (`data/2026-10-17/x.warc` under base `data/`) as their own failure,
   instead of calling the files missing.
2. `means` says what an absence means, not whether `verify` fails: rows
   whose status means a failure (`absent_unexpected`) with no file are
   counted and reported, never failed.
3. `truequery context` says when a contract in scope points at local data.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from truequery import verify as V

sys.path.insert(0, os.path.dirname(__file__))
from test_data_verify import NOW, no_reader, tq, write  # noqa: E402

DATA_MD = ("# Data\n\n```tq-config\ndialect: duckdb\nbases:\n"
           "  data_root: ./data\n```\n")

CONTRACT = """version: 2
dataset: fetch_log
kind: files
location: {path: logs/*.csv, format: csv}
grain: one row per fetch attempt
keys: [url]
columns:
  url: {type: string, nullable: false}
  status:
    type: string
    values:
      fetched: {means: present}
      skipped: {means: absent_expected}
      failed: {means: absent_unexpected}
  local_path: {type: path, relative_to: data_root, status_column: status}
"""


def project(root: Path, rows: list[tuple[str, str, str]],
            files: list[str] = (), contract: str = CONTRACT) -> Path:
    """A fetch log under `root`: rows of (url, status, local_path), and the
    files (relative to data/) that exist."""
    write(root / "DATA.md", DATA_MD)
    write(root / "contracts" / "fetch_log.yaml", contract)
    (root / "data").mkdir(parents=True, exist_ok=True)
    for f in files:
        write(root / "data" / f, "warc")
    write(root / "logs" / "fetch.csv",
          "url,status,local_path\n"
          + "".join(f"{u},{s},{p}\n" for u, s, p in rows))
    return root


def found_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("Found")]


# ---------------------------------------------------------------------------
# 1. The doubled base folder
# ---------------------------------------------------------------------------


class TestDoubledBase:
    def test_pilot_shape_some_days_prefixed(self, tmp_path, capsys):
        """Day 1 written right, day 2 with the base's name twice, one file
        really missing: two failures, each with its own fix."""
        rows = [("u1", "fetched", "2026-10-16/a.warc"),
                ("u2", "fetched", "2026-10-16/b.warc"),
                ("u3", "fetched", "data/2026-10-17/c.warc"),
                ("u4", "fetched", "data/2026-10-17/d.warc"),
                ("u5", "fetched", "data/2026-10-17/e.warc"),
                ("u6", "fetched", "2026-10-16/gone.warc"),
                ("u7", "failed", "2026-10-16/f.warc")]
        root = project(tmp_path, rows, ["2026-10-16/a.warc",
                                        "2026-10-16/b.warc",
                                        "2026-10-17/c.warc",
                                        "2026-10-17/d.warc",
                                        "2026-10-17/e.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1, out
        found = found_lines(out)
        assert len(found) == 2, out
        assert ("fetch_log: 3 paths in local_path start with data/, the base "
                "folder's own name, so they point to data/data/…; the files "
                "are there without it (logs/*.csv; e.g. key u3, u4, u5)"
                ) in found[0]
        assert ("fix: correct the paths where they're written, or check how "
                "`relative_to` is declared for local_path") in out
        assert ("fetch_log: 1 file missing where the status says present "
                "(logs/*.csv; e.g. key u6)") in found[1]
        assert "fix: fetch the missing files again" in out
        assert "2 data failures in 1 of 1 dataset read" in out
        # With a status column, the path check proves containment only, and
        # says so: it never claims every path is right
        assert "no local_path outside data_root" in out
        assert "every local_path under data_root" not in out

    def test_every_row_prefixed(self, tmp_path, capsys):
        rows = [(f"u{i}", "fetched", f"data/d/{i}.warc") for i in range(4)]
        root = project(tmp_path, rows, [f"d/{i}.warc" for i in range(4)])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        found = found_lines(out)
        assert len(found) == 1, out  # said once, not by two checks
        assert "4 paths in local_path start with data/" in found[0]
        assert "missing" not in out

    def test_status_less_path_column(self, tmp_path, capsys):
        contract = CONTRACT.replace(", status_column: status", "")
        rows = [("u1", "fetched", "data/d/1.warc"),
                ("u2", "fetched", "d/2.warc"),
                ("u3", "fetched", "d/gone.warc")]
        root = project(tmp_path, rows, ["d/1.warc", "d/2.warc"], contract)
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        found = found_lines(out)
        assert len(found) == 2, out
        assert ("1 path in local_path starts with data/, the base folder's own "
                "name, so it points to data/data/…; the file is there without "
                "it (logs/*.csv; e.g. key u1)") in found[0]
        assert ("1 path in local_path doesn't exist under data_root "
                "(logs/*.csv; e.g. key u3)") in found[1]

    def test_prefix_that_is_another_folders_name_is_missing(self, tmp_path,
                                                            capsys):
        rows = [("u1", "fetched", "d/1.warc"),
                ("u2", "fetched", "raw/d/2.warc")]
        root = project(tmp_path, rows, ["d/1.warc", "d/2.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 file missing where the status says present" in out
        assert "base folder's own name" not in out

    def test_prefixed_but_not_there_without_it_either(self, tmp_path, capsys):
        rows = [("u1", "fetched", "d/1.warc"),
                ("u2", "fetched", "data/d/never.warc")]
        root = project(tmp_path, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 file missing where the status says present" in out
        assert "base folder's own name" not in out

    @pytest.mark.skipif(os.name != "posix", reason="symlinks")
    def test_prefixed_link_out_of_the_base_is_refused(self, tmp_path, capsys):
        """`data/out/x` doesn't exist; without the prefix, `out` is a link
        that leaves the base. It's never followed: the row is missing."""
        outside = tmp_path / "outside"
        write(outside / "x.warc", "secret")
        rows = [("u1", "fetched", "d/1.warc"),
                ("u2", "fetched", "data/out/x.warc")]
        root = project(tmp_path / "p", rows, ["d/1.warc"])
        os.symlink(outside, root / "data" / "out")
        assert not V.found_without_base_name(
            "data/out/x.warc", os.path.realpath(root / "data"))
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 file missing where the status says present" in out
        assert "base folder's own name" not in out
        # written as `out/x.warc`, the same link is refused as today
        rows[1] = ("u2", "fetched", "out/x.warc")
        project(root, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 path in local_path points outside data_root" in out
        assert len(found_lines(out)) == 1, out

    def test_windows_separators_count_as_slashes(self, tmp_path, capsys):
        """`data\\d\\1.warc` is read as `data/d/1.warc`, as every path in the
        log already is: the same doubled base."""
        rows = [("u1", "fetched", "d/2.warc"),
                ("u2", "fetched", "data\\d\\1.warc")]
        root = project(tmp_path, rows, ["d/1.warc", "d/2.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 path in local_path starts with data/" in out

    def test_helper(self, tmp_path):
        base = str(tmp_path / "data")
        assert V.without_base_name("data/a/b.warc", base) == "a/b.warc"
        assert V.without_base_name("data\\a\\b.warc", base) == "a/b.warc"
        assert V.without_base_name(" data//a.warc", base) == "a.warc"
        assert V.without_base_name("data", base) is None
        assert V.without_base_name("data/", base) is None
        assert V.without_base_name("database/a", base) is None
        assert V.without_base_name("Data/a", base) is None  # exact name
        assert V.without_base_name("a/data/b", base) is None
        assert V.without_base_name("data/a", base + os.sep) == "a"

    def test_pii_keys_hidden_in_both_failures(self, tmp_path, capsys):
        contract = CONTRACT.replace("url: {type: string, nullable: false}",
                                    "url: {type: string, nullable: false, "
                                    "pii: true}")
        rows = [("u1", "fetched", "d/1.warc"),
                ("u2", "fetched", "data/d/2.warc"),
                ("u3", "fetched", "d/gone.warc")]
        root = project(tmp_path, rows, ["d/1.warc", "d/2.warc"], contract)
        _, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert "u2" not in out and "u3" not in out
        assert out.count("keys hidden: marked pii") == 2


# ---------------------------------------------------------------------------
# 2. A status that means a failure isn't a data failure
# ---------------------------------------------------------------------------


class TestFailureStatusCounted:
    def test_only_failed_rows(self, tmp_path, capsys):
        rows = [("u1", "failed", ""), ("u2", "failed", "d/2.warc")]
        root = project(tmp_path, rows)
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 0, out
        assert "Found" not in out
        assert ("2 rows whose status means a failure (failed): no file, as "
                "the status says") in out
        # nothing was judged about files: said, never as verified
        assert "files exist where the status says present" not in out
        assert ("nothing to compare: files per status (no row whose status "
                "says present)") in out

    def test_present_rows_missing_still_fail(self, tmp_path, capsys):
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "fetched", "d/2.warc"),
                ("u3", "failed", ""), ("u4", "skipped", "")]
        root = project(tmp_path, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert ("Found        fetch_log: 1 file missing where the status says "
                "present (logs/*.csv; e.g. key u2)") in out
        assert "u3" not in out
        assert "1 data failure in 1 of 1 dataset read" in out

    def test_counts_on_the_verified_line(self, tmp_path, capsys):
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "failed", ""),
                ("u3", "failed", ""), ("u4", "skipped", "")]
        root = project(tmp_path, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 0, out
        assert ("files exist where the status says present · 1 row absent as "
                "expected (skipped) · 2 rows whose status means a failure "
                "(failed): no file, as the status says") in out
        _, out, _ = tq(capsys, root, "verify", "--json", "--now", NOW)
        doc = json.loads(out)
        det = [a for a in doc["datasets"][0]["assertions"]
               if a["assertion"] == "exists_per_status"][0]["details"]
        assert det["absent_unexpected_count"] == 2
        assert det["absent_unexpected_statuses"] == ["failed"]
        assert det["compared"] == 1  # only `present` rows are judged

    def test_todo_unchanged(self, tmp_path, capsys):
        contract = CONTRACT.replace(
            "failed: {means: absent_unexpected}", "failed: {means: TODO}"
        ).replace("kind: files\n", "kind: files\nstatus: draft\ninferred_from: logs/fetch.csv\n")
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "failed", "")]
        root = project(tmp_path, rows, ["d/1.warc"], contract)
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 3, out
        assert ("1 row with status failed not judged: what the status means "
                "is still TODO in the contract") in out

    def test_failure_row_path_out_of_the_base_still_fails(self, tmp_path,
                                                          capsys):
        """Containment comes first, whatever the status means."""
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "failed", "../x.warc")]
        root = project(tmp_path, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        assert "1 path in local_path points outside data_root" in out
        assert len(found_lines(out)) == 1, out


# ---------------------------------------------------------------------------
# 3. `truequery context` and local data
# ---------------------------------------------------------------------------


class TestContextData:
    def test_with_local_data(self, tmp_path, capsys):
        root = project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        code, out, _ = tq(capsys, root, "context")
        assert code == 0
        assert "EVIDENCE_AVAILABLE: file, data" in out
        assert ("DATA: fetch_log (local data; check it before writing code: "
                "truequery verify fetch_log)") in out
        _, out, _ = tq(capsys, root, "context", "--json")
        doc = json.loads(out)
        assert doc["evidence_available"] == ["file", "data"]
        assert doc["data"] == {"datasets": ["fetch_log"], "reader_missing": []}

    def test_without_local_data(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", DATA_MD)
        write(tmp_path / "contracts" / "orders.yaml",
              "version: 2\ndataset: orders\ngrain: one row per order\n"
              "keys: [order_id]\ncolumns:\n  order_id: {type: string}\n")
        write(tmp_path / "q.sql", "SELECT order_id FROM orders\n")
        for argv in (("context",), ("context", "--target", "q.sql")):
            code, out, _ = tq(capsys, tmp_path, *argv)
            assert code == 0
            assert "EVIDENCE_AVAILABLE: file\n" in out
            assert "DATA" not in out.replace("DATA.md", "")

    def test_target_scopes_the_datasets(self, tmp_path, capsys):
        root = project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        write(root / "contracts" / "orders.yaml",
              "version: 2\ndataset: orders\ngrain: one row per order\n"
              "keys: [order_id]\ncolumns:\n  order_id: {type: string}\n")
        write(root / "q.sql", "SELECT order_id FROM orders\n")
        write(root / "f.sql", "SELECT url FROM fetch_log\n")
        _, out, _ = tq(capsys, root, "context", "--target", "q.sql")
        assert "EVIDENCE_AVAILABLE: file\n" in out
        _, out, _ = tq(capsys, root, "context", "--target", "f.sql")
        assert "EVIDENCE_AVAILABLE: file, data" in out
        assert "truequery verify fetch_log" in out
        # a target that reads no table names no dataset
        write(root / "none.sql", "SELECT 1\n")
        _, out, _ = tq(capsys, root, "context", "--target", "none.sql")
        assert "EVIDENCE_AVAILABLE: file\n" in out
        assert "\nDATA" not in out

    def test_reader_missing(self, tmp_path, capsys, monkeypatch):
        root = project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        no_reader(monkeypatch)
        code, out, _ = tq(capsys, root, "context")
        assert code == 0
        assert "EVIDENCE_AVAILABLE: file\n" in out
        assert ("DATA_READER_MISSING: fetch_log (local data, but the data "
                "reader isn't installed; with the user's yes, run: truequery "
                "setup data)") in out
        _, out, _ = tq(capsys, root, "context", "--json")
        assert json.loads(out)["data"] == {"datasets": [],
                                           "reader_missing": ["fetch_log"]}

    def test_reader_declined(self, tmp_path, capsys, monkeypatch):
        root = project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        write(root / "DATA.md", DATA_MD.replace(
            "```\n", "setup:\n  data: false\n```\n"))
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, root, "context")
        assert "DATA.md says this project doesn't want it" in out
        assert "truequery setup data" not in out

    def test_sqlite_needs_no_reader(self, tmp_path, capsys, monkeypatch):
        import sqlite3
        write(tmp_path / "DATA.md", DATA_MD)
        (tmp_path / "db").mkdir()
        con = sqlite3.connect(tmp_path / "db" / "app.sqlite")
        con.execute("CREATE TABLE users (id INTEGER)")
        con.commit()
        con.close()
        write(tmp_path / "contracts" / "users.yaml",
              "version: 2\ndataset: users\nkind: table\n"
              "location: {database: db/app.sqlite, relation: users}\n"
              "grain: one row per user\nkeys: [id]\ncolumns:\n"
              "  id: {type: integer}\n")
        project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, tmp_path, "context")
        assert "EVIDENCE_AVAILABLE: file, data" in out
        assert "DATA: users (" in out
        assert "DATA_READER_MISSING: fetch_log (" in out

    def test_context_never_imports_the_reader(self, tmp_path, capsys):
        root = project(tmp_path, [("u1", "fetched", "d/1.warc")], ["d/1.warc"])
        sys.modules.pop("duckdb", None)
        tq(capsys, root, "context")
        assert "duckdb" not in sys.modules


# ---------------------------------------------------------------------------
# A path outside the base folder: reported once, with its own fix
# ---------------------------------------------------------------------------

OUTSIDE_WHAT = ("1 path in local_path points outside data_root, through an "
                "absolute path, ~, .. or a link")
OUTSIDE_FIX = ("fix: write each path relative to data_root, with no absolute "
               "path, ~ or ..; a link that leaves the folder is never followed")


class TestOutsideBase:
    def forms(self, tmp_path):
        outside = tmp_path / "outside"
        write(outside / "x.warc", "secret")
        forms = {"dotdot-after-name": "data/../x.warc",
                 "dotdot": "../x.warc",
                 "absolute": str(outside / "x.warc"),
                 "home": "~/x.warc",
                 "drive": "C:\\x.warc"}
        if os.name == "posix":
            forms["link-out"] = "out/x.warc"
        return outside, forms

    @pytest.mark.parametrize("status,row_status", [
        (True, "fetched"), (True, "failed"), (False, "fetched")],
        ids=["status-present", "status-failure", "no-status-column"])
    def test_each_form_once(self, tmp_path, capsys, status, row_status):
        outside, forms = self.forms(tmp_path)
        contract = (CONTRACT if status
                    else CONTRACT.replace(", status_column: status", ""))
        for name, path in forms.items():
            rows = [("u1", "fetched", "d/1.warc"), ("u2", row_status, path)]
            root = project(tmp_path / name, rows, ["d/1.warc"], contract)
            if name == "link-out":
                os.symlink(outside, root / "data" / "out")
            code, out, _ = tq(capsys, root, "verify", "--now", NOW)
            assert code == 1, (name, out)
            found = found_lines(out)
            assert len(found) == 1, (name, out)
            assert OUTSIDE_WHAT in found[0], (name, out)
            assert OUTSIDE_FIX in out, (name, out)
            assert "fetch the missing files" not in out, (name, out)
            assert "1 data failure in 1 of 1 dataset read" in out, (name, out)
            assert "secret" not in out

    def test_outside_and_missing_are_separate(self, tmp_path, capsys):
        """With a status column: the path outside comes from the path check,
        the missing file from the status check, each with its own fix."""
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "fetched", "../x.warc"),
                ("u3", "fetched", "d/gone.warc")]
        root = project(tmp_path, rows, ["d/1.warc"])
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        found = found_lines(out)
        assert len(found) == 2, out
        assert (OUTSIDE_WHAT + " (logs/*.csv; e.g. key u2)") in found[0]
        assert ("1 file missing where the status says present (logs/*.csv; "
                "e.g. key u3)") in found[1]
        assert OUTSIDE_FIX in out
        assert "fix: fetch the missing files again" in out

    def test_outside_missing_and_doubled_without_status(self, tmp_path, capsys):
        """No status column: three failures from one check, each with its own
        count, keys and fix."""
        contract = CONTRACT.replace(", status_column: status", "")
        rows = [("u1", "fetched", "d/1.warc"), ("u2", "fetched", "/etc/x"),
                ("u3", "fetched", "d/gone.warc"),
                ("u4", "fetched", "data/d/1.warc")]
        root = project(tmp_path, rows, ["d/1.warc"], contract)
        code, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert code == 1
        found = found_lines(out)
        assert len(found) == 3, out
        assert (OUTSIDE_WHAT + " (logs/*.csv; e.g. key u2)") in found[0]
        assert "1 path in local_path starts with data/" in found[1]
        assert "e.g. key u4" in found[1]
        assert ("1 path in local_path doesn't exist under data_root "
                "(logs/*.csv; e.g. key u3)") in found[2]
        assert "3 data failures in 1 of 1 dataset read" in out


# ---------------------------------------------------------------------------
# `context` names at most 10 datasets
# ---------------------------------------------------------------------------


def many_datasets(root: Path, n: int) -> list[str]:
    write(root / "DATA.md", DATA_MD)
    names = [f"d{i:02d}" for i in range(n)]
    for name in names:
        write(root / "contracts" / f"{name}.yaml",
              f"version: 2\ndataset: {name}\nkind: files\n"
              f"location: {{path: logs/{name}.csv, format: csv}}\n"
              "grain: one row per id\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n")
    return names


class TestContextCap:
    def test_ten_are_all_named(self, tmp_path, capsys):
        names = many_datasets(tmp_path, 10)
        _, out, _ = tq(capsys, tmp_path, "context")
        assert (f"DATA: {', '.join(names)} (local data; check it before "
                f"writing code: truequery verify {' '.join(names)})") in out
        assert "more" not in out

    def test_more_than_ten(self, tmp_path, capsys):
        names = many_datasets(tmp_path, 15)
        _, out, _ = tq(capsys, tmp_path, "context")
        assert (f"DATA: {', '.join(names[:10])} and 5 more (truequery verify "
                "checks them all); local data, so run truequery verify before "
                "writing code\n") in out
        assert "d10" not in out and "d14" not in out
        _, out, _ = tq(capsys, tmp_path, "context", "--json")
        assert json.loads(out)["data"]["datasets"] == names  # JSON: all

    def test_reader_missing_capped_too(self, tmp_path, capsys, monkeypatch):
        many_datasets(tmp_path, 12)
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, tmp_path, "context")
        assert ("DATA_READER_MISSING: d00, d01, d02, d03, d04, d05, d06, d07, "
                "d08, d09 and 2 more (truequery verify checks them all); local "
                "data, but the data reader isn't installed; with the user's "
                "yes, run: truequery setup data\n") in out

    def test_target_lists_only_what_it_uses(self, tmp_path, capsys):
        many_datasets(tmp_path, 15)
        write(tmp_path / "q.sql",
              "SELECT a.id FROM d03 a JOIN d12 b ON a.id = b.id\n")
        _, out, _ = tq(capsys, tmp_path, "context", "--target", "q.sql")
        assert ("DATA: d03, d12 (local data; check it before writing code: "
                "truequery verify d03 d12)") in out
        assert "d00" not in out and "more" not in out
