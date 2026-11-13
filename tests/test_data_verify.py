"""0.2 part D: `truequery verify`, `review --data`, `setup data`, the data
reader in `truequery status`, and the plugin launcher.

Everything here runs the real CLI (in-process, so the data reader can be
made to look missing) on small projects written to tmp_path.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from truequery import cli
from truequery import datasetup as DS
from truequery import dataverify as DV
from truequery import project as P
from truequery import verify as V

sys.path.insert(0, os.path.dirname(__file__))
import r2_projects as R2  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
NOW = "2026-10-02T00:00:00Z"


def tq(capsys, root: Path, *argv: str) -> tuple[int, str, str]:
    """Run the CLI in `root`; return (exit code, stdout, stderr)."""
    old = os.getcwd()
    os.chdir(root)
    try:
        code = cli.main(list(argv))
    finally:
        os.chdir(old)
    out = capsys.readouterr()
    return code, out.out, out.err


def no_reader(monkeypatch) -> None:
    """Make `import duckdb` fail the way it does when it isn't installed."""
    monkeypatch.setitem(sys.modules, "duckdb", None)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def sqlite_users(root: Path, rows=((1, "a@x"), (2, "b@x"))) -> None:
    import sqlite3
    (root / "db").mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(root / "db" / "app.sqlite")
    con.execute("CREATE TABLE users (id INTEGER, email TEXT)")
    con.executemany("INSERT INTO users VALUES (?, ?)", rows)
    con.commit()
    con.close()
    write(root / "contracts" / "users.yaml",
          "version: 2\ndataset: users\nkind: table\n"
          "location: {database: db/app.sqlite, relation: users}\n"
          "grain: one row per user\nkeys: [id]\ncolumns:\n"
          "  id: {type: integer}\n  email: {type: string, pii: true}\n")


def orders_csv(root: Path, body: str = "order_id,status\n1,paid\n2,paid\n",
               extra: str = "") -> None:
    write(root / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
    write(root / "data" / "orders.csv", body)
    write(root / "contracts" / "orders.yaml",
          "version: 2\ndataset: orders\nkind: files\n"
          "location: {path: data/orders*.csv, format: csv}\n"
          "grain: one row per order\nkeys: [order_id]\ncolumns:\n"
          "  order_id: {type: string}\n"
          "  status:\n    type: string\n    values: [paid, refunded]\n"
          + extra)


# ---------------------------------------------------------------------------
# truequery verify
# ---------------------------------------------------------------------------


class TestVerifyCommand:
    def test_reviewer2_projects_broken_fail_fixed_pass(self, tmp_path, capsys):
        expected = {
            "base": ("1 path in local_path starts with data/, the base "
                     "folder's own name, so it points to data/data/…; the "
                     "file is there without it"),
            "sizes": "2 values of bytes_logged are in GB, the contract says GiB",
            "tz": "2 timestamps in fetched_at aren't UTC",
            # The `failed` row has no file, as its status says: counted, not
            # failed (`means` says what an absence means, not whether verify
            # fails). The `fetched` row with no file still fails.
            "lifecycle": ("1 file missing where the status says present "
                          "(logs/fetch/*.csv; e.g. key (r1, https://ex.org/5))"),
        }
        for case in R2.CASES:
            broken = R2.build(tmp_path / f"{case}-broken", case, True)
            code, out, err = tq(capsys, broken, "verify", "--now", NOW)
            assert code == 1, out
            assert "Found        " in out and expected[case] in out, out
            assert "fix: " in out
            assert err == ""
            assert "1 data failure in 1 of 1 dataset read" in out, out
            fixed = R2.build(tmp_path / f"{case}-fixed", case, False)
            code, out, _ = tq(capsys, fixed, "verify", "--now", NOW)
            assert code == 0, out
            assert "Found" not in out
            assert "matches its contract" in out
        # Expected absences are named, never flagged.
        _, out, _ = tq(capsys, tmp_path / "lifecycle-fixed", "verify",
                       "--now", NOW)
        assert ("3 rows absent as expected (refetchable, released, "
                "removed_on_purpose)") in out
        _, out, _ = tq(capsys, tmp_path / "lifecycle-broken", "verify",
                       "--now", NOW)
        assert ("1 row whose status means a failure (failed): no file, as "
                "the status says") in out, out
        assert "ex.org/6" not in out  # the failed row isn't a failure

    def test_failure_lines_name_the_problem_not_the_assertion(self, tmp_path, capsys):
        orders_csv(tmp_path, "order_id,status\n1,paid\n1,paid\n2,lost\n,paid\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        assert ("Found        orders: 1 row with no order_id (data/orders*.csv)"
                in out)
        assert "a key can't be empty" in out
        assert ("orders: key order_id is not unique: 1 value appears more "
                "than once (data/orders*.csv; e.g. key 1)") in out
        assert ("orders: 1 row with a status value the contract doesn't "
                "declare (data/orders*.csv; e.g. key 2)") in out
        for internal in ("not_null", "accepted_values", "unique on",
                         "tz_marker", "exists_per_status"):
            assert internal not in out

    def test_exit_codes_and_json(self, tmp_path, capsys):
        orders_csv(tmp_path)
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert out.splitlines()[1] == (
            "Verified     orders (1 file, 2 rows): no empty order_id, "
            "key order_id unique, status values all declared")
        assert out.splitlines()[-1] == "1 dataset read, it matches its contract"
        code, out, _ = tq(capsys, tmp_path, "verify", "--json", "--now", NOW)
        doc = json.loads(out)
        assert code == 0 and doc["exit_code"] == 0
        assert doc["datasets"][0]["status"] == "passed"
        assert "not_checked_detail" not in doc
        # A dataset name with no contract is a usage error.
        code, _, err = tq(capsys, tmp_path, "verify", "nope", "--now", NOW)
        assert code == 2 and "no valid contract for nope" in err
        code, _, err = tq(capsys, tmp_path, "verify", "--now", "yesterday")
        assert code == 2 and "--now must be an ISO time" in err
        # Naming a dataset verifies only that one.
        code, out, _ = tq(capsys, tmp_path, "verify", "orders", "--now", NOW)
        assert code == 0 and "orders (1 file" in out

    def test_empty_glob_is_not_checked_and_exits_3(self, tmp_path, capsys):
        orders_csv(tmp_path)
        (tmp_path / "data" / "orders.csv").unlink()
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert ("Not checked  orders: no files matched location.path "
                "'data/orders*.csv'") in out
        code, _, _ = tq(capsys, tmp_path, "verify", "--now", NOW,
                        "--allow-unverified")
        assert code == 0

    def test_zero_row_and_zero_byte_files(self, tmp_path, capsys):
        orders_csv(tmp_path, "order_id,status\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert ("Not checked  orders (data/orders*.csv): no rows, so nothing "
                "was checked against the contract") in out
        assert "Verified" not in out
        assert out.splitlines()[-1] == "1 dataset read, no failures; 1 with no rows"
        # A 0-byte file has no header either: the contract's columns are
        # missing, which is a failure, not a crash.
        (tmp_path / "data" / "orders.csv").write_text("", encoding="utf-8")
        code, out, err = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        assert ("Found        orders: columns order_id and status are in the "
                "contract but not in the data") in out
        assert "fix: add them where the data is written" in out
        assert "Traceback" not in out + err

    def test_file_that_vanishes_mid_run(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        real = V.expand_contract_files

        def expand_then_delete(*args, **kwargs):
            base, files = real(*args, **kwargs)
            for f in files:
                os.remove(f)
            return base, files

        monkeypatch.setattr(V, "expand_contract_files", expand_then_delete)
        code, out, err = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert ("Not checked  orders: orders.csv disappeared while it was "
                "being read") in out
        assert "Traceback" not in err

    def test_draft_contract_says_so(self, tmp_path, capsys):
        orders_csv(tmp_path, "order_id,status\n1,paid\n1,paid\n",
                   extra="status: draft\ninferred_from: a 2-row sample\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        note = "(from draft contract orders, inferred from a 2-row sample)"
        verified = [l for l in out.splitlines() if l.startswith("Verified")]
        found = [l for l in out.splitlines() if l.startswith("Found")]
        assert verified and all(l.endswith(note) for l in verified)
        # one bracket: the draft note joins the where-and-keys bracket
        assert found and all(
            l.endswith("; from draft contract orders, inferred from a 2-row "
                       "sample)") and ") (" not in l for l in found)

    def test_pii_key_is_never_shown(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        write(tmp_path / "people.csv", "email,name\na@x,A\na@x,B\n")
        write(tmp_path / "contracts" / "people.yaml",
              "version: 2\ndataset: people\nkind: files\n"
              "location: {path: people.csv, format: csv}\n"
              "grain: one row per person\nkeys: [email]\ncolumns:\n"
              "  email: {type: string, pii: true}\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        assert "a@x" not in out
        assert "keys hidden: marked pii" in out
        evidence = (tmp_path / ".truequery" / "evidence.jsonl").read_text()
        assert "a@x" not in evidence
        code, out, _ = tq(capsys, tmp_path, "verify", "--json", "--now", NOW)
        assert "a@x" not in out

    def test_reader_errors_never_quote_the_data(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        write(tmp_path / "bad.parquet", "SECRET-VALUE not a parquet file\n")
        write(tmp_path / "contracts" / "p.yaml",
              "version: 2\ndataset: p\nkind: files\n"
              "location: {path: bad.parquet, format: parquet}\n"
              "grain: one row per id\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n")
        code, out, err = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert "Not checked  p: the data reader couldn't read it (" in out
        assert "SECRET-VALUE" not in out + err
        _, out, _ = tq(capsys, tmp_path, "verify", "--json", "--now", NOW)
        assert "SECRET-VALUE" not in out

    def test_declared_column_missing_from_the_data(self, tmp_path, capsys):
        orders_csv(tmp_path, "order_id\n1\n2\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        assert ("Found        orders: column status is in the contract but not "
                "in the data, so the checks on it didn't run (data/orders*.csv)"
                in out)
        # The checks that don't need it still ran.
        assert ("Verified     orders (1 file, 2 rows): no empty order_id, key "
                "order_id unique") in out

    def test_missing_table_corrupt_database_and_escaping_link(self, tmp_path,
                                                              capsys):
        import sqlite3
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        (tmp_path / "data").mkdir()
        con = sqlite3.connect(tmp_path / "data" / "app.db")
        con.execute("CREATE TABLE other (x)")
        con.commit()
        con.close()
        write(tmp_path / "contracts" / "users.yaml",
              "version: 2\ndataset: users\nkind: table\n"
              "location: {database: data/app.db, relation: users}\n"
              "grain: one row per user\nkeys: [id]\ncolumns:\n"
              "  id: {type: integer}\n")
        write(tmp_path / "data" / "wh.duckdb", "not a database")
        write(tmp_path / "contracts" / "wh.yaml",
              "version: 2\ndataset: wh\nkind: table\n"
              "location: {database: data/wh.duckdb, relation: t}\n"
              "grain: one row per id\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n")
        outside = tmp_path.parent / f"{tmp_path.name}-outside.csv"
        outside.write_text("id\nTOPSECRET\n", encoding="utf-8")
        try:
            os.symlink(outside, tmp_path / "data" / "link.csv")
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")
        write(tmp_path / "contracts" / "linked.yaml",
              "version: 2\ndataset: linked\nkind: files\n"
              "location: {path: data/link.csv, format: csv}\n"
              "grain: one row per id\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n")
        code, out, err = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert "Not checked  users: table 'users' isn't in data/app.db" in out
        assert ("Not checked  wh: the data reader couldn't read it "
                "(IO Error)") in out
        assert ("Not checked  linked: data/link.csv points outside the "
                "project folder (a link?), so nothing was read") in out
        assert "TOPSECRET" not in out + err
        evidence = (tmp_path / ".truequery" / "evidence.jsonl").read_text()
        assert "TOPSECRET" not in evidence

    def test_skipped_remote_tables_and_no_local_data(self, tmp_path, capsys):
        write(tmp_path / "contracts" / "orders.yaml",
              "version: 2\ndataset: orders\nkind: table\n"
              "location: {relation: analytics.orders}\n"
              "grain: one row per order\nkeys: [order_id]\ncolumns:\n"
              "  order_id: {type: string}\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert ("Skipped      orders: the contract doesn't say where the data "
                "is (add `location.database` for a local database file; remote "
                "databases aren't read until 0.3)") in out
        assert "1 skipped (no location)" in out
        shutil.rmtree(tmp_path / "contracts")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert "no contract points at local data" in out

    def test_invalid_contract_is_listed_and_exits_3(self, tmp_path, capsys):
        orders_csv(tmp_path)
        write(tmp_path / "contracts" / "broken.yaml", "version: 2\ndataset: x\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert "Not checked  invalid contract: contracts/broken.yaml" in out
        assert "1 invalid contract" in out.splitlines()[-1]

    def test_same_data_and_now_give_identical_output(self, tmp_path, capsys):
        root = R2.build(tmp_path / "p", "lifecycle", True)
        sqlite_users(root, rows=((1, "a@x"), (1, "b@x")))
        outs, jsons = [], []
        for _ in range(3):
            outs.append(tq(capsys, root, "verify", "--now", NOW))
            jsons.append(tq(capsys, root, "verify", "--json", "--now", NOW)[1])
        assert outs[0] == outs[1] == outs[2]
        assert jsons[0] == jsons[1] == jsons[2]
        # Six runs appended the same records; only the wall-clock run time
        # (`run_at`) may differ.
        lines = (root / ".truequery" / "evidence.jsonl").read_text().splitlines()
        assert len(lines) % 6 == 0
        per_run = len(lines) // 6
        records = [json.loads(line) for line in lines]
        for rec in records:
            assert rec.pop("run_at")
        first = records[:per_run]
        for k in range(1, 6):
            assert records[k * per_run:(k + 1) * per_run] == first

    def test_evidence_rotates(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        monkeypatch.setattr(V, "MAX_EVIDENCE_BYTES", 200)
        tq(capsys, tmp_path, "verify", "--now", NOW)
        tq(capsys, tmp_path, "verify", "--now", NOW)
        ev = tmp_path / ".truequery"
        assert (ev / "evidence.jsonl.1").exists()
        assert (ev / "evidence.jsonl").stat().st_size < (
            (ev / "evidence.jsonl.1").stat().st_size + 2000)
        ignore = (ev / ".gitignore").read_text()
        assert "evidence.jsonl\n" in ignore and "evidence.jsonl.1\n" in ignore


# ---------------------------------------------------------------------------
# The data reader missing
# ---------------------------------------------------------------------------


class TestReaderMissing:
    def test_sqlite_still_verified_files_listed(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        sqlite_users(tmp_path)
        no_reader(monkeypatch)
        code, out, err = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert "Verified     users (db/app.sqlite, table users, 2 rows)" in out
        assert ("Not checked  data files: the data reader isn't installed. "
                "Run: truequery setup data · orders") in out
        assert "ImportError" not in out + err and "Traceback" not in err

    def test_require_data_fails_even_with_allow_unverified(self, tmp_path, capsys,
                                                           monkeypatch):
        orders_csv(tmp_path)
        no_reader(monkeypatch)
        assert tq(capsys, tmp_path, "verify", "--require", "data")[0] == 3
        assert tq(capsys, tmp_path, "verify", "--require", "data",
                  "--allow-unverified")[0] == 3
        assert tq(capsys, tmp_path, "review", "--require", "data")[0] == 3

    def test_runner_import_failure_is_reported_not_raised(self, tmp_path, capsys,
                                                          monkeypatch):
        """Even when the reader looks installed but won't import."""
        orders_csv(tmp_path)
        no_reader(monkeypatch)
        monkeypatch.setattr(V, "duckdb_status", lambda: {
            "installed": True, "version": "9.9.9", "too_old": False})
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert "data files: the data reader isn't installed" in out

    def test_too_old_reader(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        monkeypatch.setattr(V, "duckdb_status", lambda: {
            "installed": False, "version": "1.1.3", "too_old": True})
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0
        assert ("data files: the data reader is too old (DuckDB 1.1.3). "
                "Run: truequery setup data · orders") in out

    def test_declined_in_data_md(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        write(tmp_path / "DATA.md",
              "# Data\n\n```tq-config\ndialect: duckdb\nsetup:\n  data: false\n```\n")
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert ("DATA.md says this project doesn't want it: `setup: data: "
                "false`") in out
        assert "Run: truequery setup data" not in out


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


class TestReceipt:
    def test_review_data_adds_verified_and_found(self, tmp_path, capsys):
        root = R2.build(tmp_path / "p", "sizes", True)
        write(root / "q.sql", "SELECT shard_id FROM shards;\n")
        code, out, _ = tq(capsys, root, "review", "--data", "--now", NOW)
        assert code == 1
        lines = out.splitlines()
        assert lines[0] == ("no findings in the code: 1 file checked offline "
                            "(the data is in the receipt)")
        assert ("Verified     against the data: shards (1 file, 2 rows): "
                "no empty shard_id, key shard_id unique, every local_path "
                "under data_root") in lines
        i = lines.index("Found        1 data failure:")
        assert lines[i + 1].startswith(
            "             shards: 2 values of bytes_logged are in GB, the "
            "contract says GiB (logs/shards.csv; e.g. key s1, s2). Fix: ")
        assert not any("your data" in l for l in lines)

    def test_review_data_clean(self, tmp_path, capsys):
        root = R2.build(tmp_path / "p", "sizes", False)
        code, out, _ = tq(capsys, root, "review", "--data", "--now", NOW)
        assert code == 0
        assert "Verified     against the data: shards (1 file, 2 rows)" in out
        assert "Found        no problems" in out

    def test_without_data_the_line_says_how_to_read_it(self, tmp_path, capsys):
        root = R2.build(tmp_path / "p", "sizes", True)
        code, out, _ = tq(capsys, root, "review")
        assert code == 0
        assert ("Not checked  your data: not read in this run. Run `truequery "
                "review --data` to check shards against the contracts") in out
        assert "Verified" not in out

    def test_receipt_never_reads_old_evidence(self, tmp_path, capsys):
        root = R2.build(tmp_path / "p", "sizes", False)
        write(root / ".truequery" / "evidence.jsonl", json.dumps({
            "dataset": "shards", "assertion": "unique", "status": "failed",
            "message": "OLD FAILURE"}) + "\n")
        for argv in (("review",), ("review", "--data", "--now", NOW)):
            _, out, _ = tq(capsys, root, *argv)
            assert "OLD FAILURE" not in out
        _, out, _ = tq(capsys, root, "review", "--data", "--now", NOW)
        assert "Found        no problems" in out

    def test_review_data_json_and_unreadable(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        sqlite_users(tmp_path)
        no_reader(monkeypatch)
        code, out, _ = tq(capsys, tmp_path, "review", "--data", "--json",
                          "--now", NOW)
        doc = json.loads(out)["receipt"]
        assert code == 0
        assert doc["data"]["datasets"][0]["dataset"] == "users"
        assert doc["not_checked"]["data"].startswith("1 dataset read")
        code, out, _ = tq(capsys, tmp_path, "review", "--data", "--now", NOW)
        assert ("Not checked  data files: the data reader isn't installed. "
                "Run: truequery setup data · orders") in out
        assert "Verified     against the data: users" in out

    def test_review_data_draft_contract(self, tmp_path, capsys):
        orders_csv(tmp_path, extra="status: draft\ninferred_from: sample.csv\n")
        _, out, _ = tq(capsys, tmp_path, "review", "--data", "--now", NOW)
        assert ("(from draft contract orders, inferred from sample.csv)"
                in out)

    def test_hooks_never_load_the_runner(self):
        code = ("import sys, truequery.cli, truequery.hook, truequery.report;"
                "bad=[m for m in ('duckdb','truequery.verify',"
                "'truequery.dataverify','truequery.datasetup') "
                "if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, env=dict(os.environ, PYTHONPATH=str(
                                 REPO / "plugin" / "engine" / "src")))
        assert out.stdout.strip() == "[]", out.stderr


# ---------------------------------------------------------------------------
# setup data
# ---------------------------------------------------------------------------


def _args(**kw):
    return SimpleNamespace(yes=kw.get("yes", False), no=kw.get("no", False))


class FakeRunner:
    def __init__(self, code=0):
        self.code, self.calls = code, []

    def __call__(self, cmd, check=False):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.code)


class TestSetupData:
    def test_detects_each_installer(self, tmp_path):
        prefix = tmp_path / "env"
        prefix.mkdir()
        assert DS.detect_install({"TRUEQUERY_LAUNCHER": "plugin"}, str(prefix),
                                 "/base")["kind"] == "plugin"
        assert DS.detect_install({}, str(prefix), str(prefix))["kind"] == "pip"
        assert DS.detect_install({}, str(prefix), "/base")["kind"] == "venv"
        # `uv venv` writes CACHEDIR.TAG into ordinary venvs: still a venv.
        (prefix / "CACHEDIR.TAG").write_text("x")
        assert DS.detect_install({}, str(prefix), "/base")["kind"] == "venv"
        ephemeral = tmp_path / "uv" / "archive-v0" / "Ab12"
        ephemeral.mkdir(parents=True)
        assert DS.detect_install({}, str(ephemeral), "/base")["kind"] == "uvx"
        builds = tmp_path / "uv" / "builds-v0" / ".tmpX"
        builds.mkdir(parents=True)
        assert DS.detect_install({}, str(builds), "/base")["kind"] == "uvx"
        (prefix / "pipx_metadata.json").write_text("{}")
        assert DS.detect_install({}, str(prefix), "/base")["kind"] == "pipx"
        (prefix / "uv-receipt.toml").write_text("[tool]\n")
        assert DS.detect_install({}, str(prefix), "/base")["kind"] == "uv-tool"

    def test_uv_tool_command_repeats_the_source(self, tmp_path):
        r = tmp_path / "uv-receipt.toml"
        r.write_text('[tool]\nrequirements = [{ name = "truequery", '
                     'directory = "/src/engine" }, { name = "rich" }, '
                     '{ name = "duckdb", specifier = ">=1" }]\n')
        assert DS._uv_tool_source(str(r)) == [
            "/src/engine", "--with", "rich", "--with", V.DUCKDB_SPEC]
        r.write_text('[tool]\nrequirements = [{ name = "truequery", '
                     'specifier = "==0.2.0" }]\n')
        assert DS._uv_tool_source(str(r)) == [
            "truequery==0.2.0", "--with", V.DUCKDB_SPEC]
        r.write_text('[tool]\nrequirements = [{ name = "truequery", '
                     'git = "https://example.org/x.git" }]\n')
        assert DS._uv_tool_source(str(r)) is None
        step = DS.plan({"kind": "uv-tool", "receipt": str(r)})
        assert step["command"][:3] == ["uv", "pip", "install"]
        assert "drop it" in step["note"]

    def test_without_yes_nothing_changes(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        data_md = (tmp_path / "DATA.md").read_text()
        no_reader(monkeypatch)
        for env in ({"TRUEQUERY_LAUNCHER": "plugin",
                     "XDG_CONFIG_HOME": str(tmp_path / "cfg")}, {}):
            runner = FakeRunner()
            monkeypatch.setattr(DS, "detect_install", lambda e=None, kind=(
                "plugin" if env else "venv"): {"kind": kind, "where": kind})
            code = DS.run_setup(_args(), str(tmp_path), env=env, runner=runner)
            out = capsys.readouterr().out
            assert code == 0 and runner.calls == []
            assert "Nothing changed" in out
            assert "15–25 MB" in out
        assert not (tmp_path / "cfg").exists()
        assert (tmp_path / "DATA.md").read_text() == data_md

    def test_venv_yes_runs_the_installer_and_records(self, tmp_path, capsys,
                                                     monkeypatch):
        orders_csv(tmp_path)
        no_reader(monkeypatch)
        monkeypatch.setattr(DS, "detect_install", lambda e=None: {
            "kind": "venv", "where": "a venv"})
        monkeypatch.setattr(DS, "_has_pip", lambda: True)
        runner = FakeRunner()
        assert DS.run_setup(_args(yes=True), str(tmp_path), runner=runner) == 0
        assert runner.calls == [[sys.executable, "-m", "pip", "install",
                                 V.DUCKDB_SPEC]]
        assert P.load_config(str(tmp_path))["setup"] == {"data": True}
        assert DS.recorded_answer(str(tmp_path)) is True
        # A failed install records nothing.
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        assert DS.run_setup(_args(yes=True), str(tmp_path),
                            runner=FakeRunner(code=1)) == 2
        assert DS.recorded_answer(str(tmp_path)) is None
        capsys.readouterr()

    def test_plugin_yes_writes_the_marker(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        no_reader(monkeypatch)
        env = {"TRUEQUERY_LAUNCHER": "plugin",
               "XDG_CONFIG_HOME": str(tmp_path / "cfg")}
        runner = FakeRunner()
        assert DS.run_setup(_args(yes=True), str(tmp_path), env=env,
                            runner=runner) == 0
        marker = tmp_path / "cfg" / "truequery" / "data-reader"
        assert marker.read_text().strip().endswith(V.DUCKDB_SPEC)
        assert runner.calls == []
        assert DS.recorded_answer(str(tmp_path)) is True
        capsys.readouterr()

    def test_uvx_prints_the_with_form(self, tmp_path, capsys, monkeypatch):
        no_reader(monkeypatch)
        monkeypatch.setattr(DS, "detect_install", lambda e=None: {
            "kind": "uvx", "where": "uvx"})
        runner = FakeRunner()
        assert DS.run_setup(_args(yes=True), str(tmp_path), runner=runner) == 0
        out = capsys.readouterr().out
        assert f'uvx --with "{V.DUCKDB_SPEC}" truequery verify' in out
        assert runner.calls == []

    def test_no_records_false(self, tmp_path, capsys):
        orders_csv(tmp_path)
        code, out, _ = tq(capsys, tmp_path, "setup", "data", "--no")
        assert code == 0 and "setup: data: false" in out
        assert DS.recorded_answer(str(tmp_path)) is False
        code, _, err = tq(capsys, tmp_path, "setup", "data", "--yes", "--no")
        assert code == 2 and "not both" in err

    def test_setup_key_is_validated(self, tmp_path):
        for bad in ("setup: yes\n", "setup:\n  data: maybe\n",
                    "setup:\n  excel: true\n"):
            write(tmp_path / "DATA.md", f"```tq-config\n{bad}```\n")
            with pytest.raises(P.ConfigError, match="setup"):
                P.load_config(str(tmp_path))

    def test_config_schema_documents_setup(self, tmp_path):
        jsonschema = pytest.importorskip("jsonschema")
        schema = json.loads((REPO / "plugin" / "engine" / "src" / "truequery"
                             / "schemas" / "tq-config.schema.json").read_text())
        good = P.default_config("plain", "duckdb")
        good["setup"] = {"data": True}
        good["bases"] = {"data_root": "./data"}
        jsonschema.validate(good, schema)
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({"setup": {"data": "yes"}}, schema)

    def test_one_spec_everywhere(self):
        pyproject = (REPO / "plugin" / "engine" / "pyproject.toml").read_text()
        assert f'data = ["{V.DUCKDB_SPEC}"]' in pyproject
        for name in ("truequery", "truequery.cmd"):
            text = (REPO / "plugin" / "bin" / name).read_text()
            assert f'--with "{V.DUCKDB_SPEC}"' in text
            assert "data-reader" in text
        # Hooks run their own shim, which never adds the reader.
        hook = (REPO / "plugin" / "scripts" / "tq-hook.sh").read_text()
        assert "duckdb" not in hook and "data-reader" not in hook


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher")
def test_launcher_adds_the_reader_only_with_the_marker(tmp_path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    (fake / "uv").write_text('#!/bin/sh\necho "LAUNCHER=$TRUEQUERY_LAUNCHER $*"\n')
    (fake / "uv").chmod(0o755)
    env = dict(os.environ, PATH=f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin",
               XDG_CONFIG_HOME=str(tmp_path / "cfg"), HOME=str(tmp_path))
    launcher = REPO / "plugin" / "bin" / "truequery"
    out = subprocess.run(["sh", str(launcher), "verify"], env=env,
                         capture_output=True, text=True).stdout
    assert "LAUNCHER=plugin" in out and "duckdb" not in out
    marker = tmp_path / "cfg" / "truequery" / "data-reader"
    marker.parent.mkdir(parents=True)
    marker.write_text(V.DUCKDB_SPEC + "\n")
    out = subprocess.run(["sh", str(launcher), "verify"], env=env,
                         capture_output=True, text=True).stdout
    assert f"--with {V.DUCKDB_SPEC} -- truequery verify" in out


# ---------------------------------------------------------------------------
# truequery status
# ---------------------------------------------------------------------------


class TestStatus:
    def test_reader_part(self, tmp_path, capsys, monkeypatch):
        orders_csv(tmp_path)
        no_reader(monkeypatch)
        code, out, _ = tq(capsys, tmp_path, "status")
        assert code == 0
        assert "truequery status: data reader: not installed" in out
        assert "needed here: 1 contract(s) read data files (orders)" in out
        assert "to install: truequery setup data" in out
        assert not (tmp_path / ".truequery").exists()  # read-only

    def test_not_needed_and_installed(self, tmp_path, capsys, monkeypatch):
        sqlite_users(tmp_path)
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, tmp_path, "status")
        assert ("not needed here: no data files, and SQLite needs nothing "
                "extra") in out
        assert "to install" not in out
        monkeypatch.setattr(V, "duckdb_status", lambda: {
            "installed": True, "version": "1.5.6", "too_old": False})
        _, out, _ = tq(capsys, tmp_path, "status")
        assert "data reader: installed (DuckDB 1.5.6)" in out

    def test_data_files_without_contracts(self, tmp_path, capsys, monkeypatch):
        write(tmp_path / "logs" / "a.csv", "x\n1\n")
        no_reader(monkeypatch)
        _, out, _ = tq(capsys, tmp_path, "status")
        assert ("useful here: 1 data file(s) (e.g. logs/a.csv), but no "
                "contract points at them yet") in out


def test_dataverify_summary_words():
    doc = {"datasets": [], "not_checked": [], "skipped": [],
           "invalid_contracts": []}
    assert DV.summary(doc) == "no data read"


# ---------------------------------------------------------------------------
# Fixes from the independent review of part D
# ---------------------------------------------------------------------------


def _nested_same_quotes(path: Path) -> list[int]:
    """Lines where an f-string holds a string in its own quote character:
    valid only on Python 3.12+ (PEP 701), a SyntaxError on 3.11."""
    import io
    import tokenize
    bad, stack = [], []
    toks = tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
    for tok in toks:
        name = tokenize.tok_name[tok.type]
        if name == "FSTRING_START":
            stack.append(tok.string.lstrip("rRbBfFuU")[:1])
        elif name == "FSTRING_END":
            stack.pop()
        elif name == "STRING" and stack:
            quote = tok.string.lstrip("rRbBfFuU")[:1]
            if quote == stack[-1]:
                bad.append(tok.start[0])
        elif name == "FSTRING_START" and stack:
            pass
    return bad


@pytest.mark.skipif(sys.version_info < (3, 12),
                    reason="3.11 itself refuses these files")
def test_sources_parse_on_python_311():
    """requires-python is >=3.11: no f-string may nest its own quotes."""
    files = sorted((REPO / "plugin" / "engine" / "src").rglob("*.py"))
    files += sorted((REPO / "tools").glob("*.py"))
    files += sorted((REPO / "tests").glob("*.py"))
    found = {str(f.relative_to(REPO)): _nested_same_quotes(f) for f in files}
    assert {k: v for k, v in found.items() if v} == {}


def test_nested_quote_detector_catches_the_311_trap(tmp_path):
    if sys.version_info < (3, 12):
        pytest.skip("the sample itself doesn't parse on 3.11")
    sample = tmp_path / "s.py"
    sample.write_text('x = f"{y("a")} b"\nz = f"{y(\'a\')} b"\n')
    assert _nested_same_quotes(sample) == [1]


class TestReviewFixes:
    def test_pii_freshness_value_is_never_shown(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        write(tmp_path / "data" / "p.csv",
              "id,seen_at\n1,2020-01-01T00:00:00Z\n2,2020-01-02T00:00:00Z\n")
        write(tmp_path / "contracts" / "people.yaml",
              "version: 2\ndataset: people\nkind: files\n"
              "location: {path: data/p.csv, format: csv}\n"
              "grain: one row per person\nkeys: [id]\n"
              "freshness_sla: {column: seen_at, max_age: 1d}\ncolumns:\n"
              "  id: {type: string}\n"
              "  seen_at: {type: timestamp, time_zone: UTC, pii: true}\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 1
        # one bracket: where, then why the value isn't shown
        assert ("people: stale: the newest seen_at is more than 1d before "
                "2026-10-02T00:00:00+00:00 (data/p.csv; its value isn't "
                "shown: marked pii)") in out
        _, js, _ = tq(capsys, tmp_path, "verify", "--json", "--now", NOW)
        _, rv, _ = tq(capsys, tmp_path, "review", "--data", "--json",
                      "--now", NOW)
        evidence = (tmp_path / ".truequery" / "evidence.jsonl").read_text()
        for text in (out, js, rv, evidence):
            assert "2020-01-02" not in text
        # not pii: the newest timestamp is shown, as the docs say
        contract = tmp_path / "contracts" / "people.yaml"
        contract.write_text(contract.read_text().replace(", pii: true", ""))
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert "the newest seen_at is 2020-01-02T00:00:00+00:00" in out

    def test_nothing_checkable_is_never_a_match(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        write(tmp_path / "data" / "a.csv", "amount\nabc\n\n")
        write(tmp_path / "contracts" / "a.yaml",
              "version: 2\ndataset: amounts\nkind: files\n"
              "location: {path: data/a.csv, format: csv}\n"
              "grain: one row per amount\nkeys: []\ncolumns:\n"
              "  amount: {type: number}\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert "Verified" not in out and "matches its contract" not in out
        assert ("Not verified amounts: read 1 file, 2 rows, but the contract "
                "declares nothing `truequery verify` checks") in out
        assert "column types aren't checked" in out
        assert out.rstrip().endswith(
            "1 dataset read, no failures; 1 with nothing to compare")
        code, out, _ = tq(capsys, tmp_path, "review", "--data", "--now", NOW)
        assert code == 3
        assert "amounts: read 1 file, 2 rows, but the contract declares" in out
        code, _, _ = tq(capsys, tmp_path, "verify", "--allow-unverified",
                        "--now", NOW)
        assert code == 0

    def test_all_empty_column_is_nothing_to_compare(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        write(tmp_path / "data" / "e.csv", "id,ts,kind\n1,,\n2,,\n")
        write(tmp_path / "contracts" / "e.yaml",
              "version: 2\ndataset: events\nkind: files\n"
              "location: {path: data/e.csv, format: csv}\n"
              "grain: one row per event\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n"
              "  ts: {type: timestamp, time_zone: UTC}\n"
              "  kind: {type: string, nullable: true, values: [a, b]}\n")
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0  # empty is allowed: nothing breaks the contract
        line = next(l for l in out.splitlines() if l.startswith("Verified"))
        assert "timestamps UTC" not in line and "values all declared" not in line
        assert line.endswith("key id unique · nothing to compare: kind values "
                             "(all empty), ts time zone (all empty)")
        # only the empty column: nothing was compared, so not verified
        contract = tmp_path / "contracts" / "e.yaml"
        contract.write_text(contract.read_text().replace("keys: [id]", "keys: []")
                            .replace("  kind: {type: string, nullable: true, "
                                     "values: [a, b]}\n", ""))
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 3
        assert ("Not verified events: read 1 file, 2 rows, but nothing to "
                "compare: ts time zone (all empty)") in out

    def test_sizes_and_status_with_no_file_to_compare(self, tmp_path, capsys):
        root = R2.build(tmp_path / "sizes", "sizes", False)
        for f in ("shard1.bin", "shard2.bin"):
            (root / "data" / f).unlink()
        _, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert "(0 files)" not in out
        assert "matches the size on disk" not in out
        assert "nothing to compare: bytes_logged sizes (none of the files " \
               "was found)" in out
        root = R2.build(tmp_path / "life", "lifecycle", False)
        log = root / "logs" / "fetch" / "2026-10-01.csv"
        head, *rows = log.read_text().splitlines()
        log.write_text("\n".join([head] + [r for r in rows
                                           if "removed_on_purpose" in r]) + "\n")
        _, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert "files exist where the status says present" not in out
        assert "(0 files)" not in out
        assert "1 row absent as expected (removed_on_purpose)" in out
        assert "files per status (no row whose status says present)" in out

    def test_wal_sqlite_leaves_no_files_beside_it(self, tmp_path, capsys):
        import sqlite3
        sqlite_users(tmp_path)
        db = tmp_path / "db" / "app.sqlite"
        con = sqlite3.connect(db)
        con.execute("PRAGMA journal_mode=wal")
        con.close()
        before = sorted(os.listdir(tmp_path / "db"))
        assert before == ["app.sqlite"]
        code, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert code == 0, out
        assert sorted(os.listdir(tmp_path / "db")) == before
        assert "&immutable=1" in V.sqlite_readonly_uri(str(db))
        # someone has it open: read through the files that are there
        writer = sqlite3.connect(db)
        writer.execute("INSERT INTO users VALUES (3, 'c@x')")
        writer.commit()
        try:
            assert "immutable" not in V.sqlite_readonly_uri(str(db))
            _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
            assert "3 rows" in out
        finally:
            writer.close()

    def test_rollback_journal_sqlite_is_not_opened_immutable(self, tmp_path):
        sqlite_users(tmp_path)
        assert V.sqlite_readonly_uri(str(tmp_path / "db" / "app.sqlite")
                                     ).endswith("?mode=ro")

    def test_path_cache_keeps_the_containment_rule(self, tmp_path):
        base = tmp_path / "base"
        (base / "day").mkdir(parents=True)
        (base / "day" / "a.warc").write_text("x")
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "s.warc").write_text("secret")
        try:
            os.symlink(outside, base / "out")
            os.symlink(outside / "s.warc", base / "day" / "s.warc")
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")
        cache: dict = {}
        real = os.path.realpath(base)
        for _ in range(2):  # the second pass is served from the cache
            assert V.resolve_path_within_base("day/a.warc", str(base), cache) \
                == os.path.join(real, "day", "a.warc")
            assert V.resolve_path_within_base("out/s.warc", str(base), cache) is None
            assert V.resolve_path_within_base("day/s.warc", str(base), cache) is None
            assert V.resolve_path_within_base("day/../a", str(base), cache) is None
            assert V.resolve_path_within_base("day/nope/x", str(base), cache) \
                == os.path.join(real, "day", "nope", "x")

    def test_setup_changes_only_the_setup_line(self, tmp_path):
        text = ("# Data\n\nintro\n\n```tq-config\n# team settings\n"
                "dialect: duckdb   # we use duckdb\nrules:\n"
                "  sql/select-star: off\n```\n\nafter\n")
        write(tmp_path / "DATA.md", text)
        assert DS.record_answer(str(tmp_path), True)
        assert (tmp_path / "DATA.md").read_text() == text.replace(
            "  sql/select-star: off\n", "  sql/select-star: off\nsetup:\n"
            "  data: true\n")
        assert DS.record_answer(str(tmp_path), False)
        assert (tmp_path / "DATA.md").read_text() == text.replace(
            "  sql/select-star: off\n", "  sql/select-star: off\nsetup:\n"
            "  data: false\n")
        # an existing data line keeps its comment and indent
        write(tmp_path / "DATA.md", "```tq-config\nsetup:\n    other: 1\n"
              "    data: false   # said no in May\nzeta: 1\n```\n")
        DS.record_answer(str(tmp_path), True)
        assert (tmp_path / "DATA.md").read_text() == (
            "```tq-config\nsetup:\n    other: 1\n"
            "    data: true   # said no in May\nzeta: 1\n```\n")
        # CRLF stays CRLF
        (tmp_path / "DATA.md").write_bytes(
            b"```tq-config\r\ndialect: duckdb\r\n```\r\n")
        DS.record_answer(str(tmp_path), True)
        assert (tmp_path / "DATA.md").read_bytes() == (
            b"```tq-config\r\ndialect: duckdb\r\nsetup:\r\n  data: true\r\n"
            b"```\r\n")

    def test_setup_leaves_a_form_it_cant_edit(self, tmp_path, capsys):
        text = "```tq-config\nsetup: {other: 1}\n```\n"
        write(tmp_path / "DATA.md", text)
        code = DS.run_setup(_args(yes=False, no=True), str(tmp_path))
        assert code == 0
        assert (tmp_path / "DATA.md").read_text() == text
        assert "couldn't be changed without touching other lines" in \
            capsys.readouterr().out

    def test_wording(self, tmp_path, capsys):
        root = R2.build(tmp_path / "tz", "tz", True)
        log = root / "logs" / "events.csv"
        log.write_text(log.read_text().replace(
            "3,2026-10-01T10:00:00+02:00", "3,2026-10-01T10:00:00Z"))
        _, out, _ = tq(capsys, root, "verify", "--now", NOW)
        assert "1 timestamp in fetched_at isn't UTC" in out
        orders_csv(tmp_path / "o", "order_id,status\n1,paid\n2,\n3,lost\n")
        _, out, _ = tq(capsys, tmp_path / "o", "verify", "--now", NOW)
        assert ("orders: 1 row with a status value the contract doesn't "
                "declare; 1 row with an empty status") in out
        assert "set `nullable: true` on it if empty is allowed" in out
        code, _, err = tq(capsys, tmp_path / "o", "review", "--data", "--now",
                          "garbage")
        assert code == 2 and err.startswith("truequery: --now must be")

    def test_database_missing_or_linked_out(self, tmp_path, capsys):
        sqlite_users(tmp_path)
        db = tmp_path / "db" / "app.sqlite"
        outside = tmp_path.parent / f"{tmp_path.name}-out.sqlite"
        shutil.move(db, outside)
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert ("Not checked  users: database file db/app.sqlite isn't there "
                "(looked in the project folder)") in out
        try:
            os.symlink(outside, db)
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert ("Not checked  users: database file db/app.sqlite points "
                "outside the project folder (a link?), so nothing was read") in out
        assert "under base" not in out

    def test_plain_review_never_loads_the_data_module(self):
        code = ("import sys, os; from truequery import cli; os.chdir(sys.argv[1]);"
                "cli.main(['review', '--json']);"
                "print([m for m in ('truequery.dataverify','duckdb') "
                "if m in sys.modules], file=sys.stderr)")
        out = subprocess.run([sys.executable, "-c", code,
                              str(REPO / "examples" / "shop")],
                             capture_output=True, text=True,
                             env=dict(os.environ, PYTHONPATH=str(
                                 REPO / "plugin" / "engine" / "src")))
        assert out.stderr.strip().splitlines()[-1] == "[]", out.stderr


class TestWordingFollowUps:
    """The three wording follow-ups the main developer found in part D."""

    def test_a_link_out_of_the_base_is_named(self, tmp_path, capsys):
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n"
              "bases:\n  data_root: ./data/files\n```\n")
        (tmp_path / "data" / "files").mkdir(parents=True)
        try:
            os.symlink(tmp_path / "data", tmp_path / "data" / "files" / "up")
        except (OSError, NotImplementedError):
            pytest.skip("no symlinks here")
        write(tmp_path / "data" / "links.csv", "id,p\n1,up/links.csv\n")
        write(tmp_path / "contracts" / "links.yaml",
              "version: 2\ndataset: links\nkind: files\n"
              "location: {path: data/links.csv, format: csv}\n"
              "grain: one row per id\nkeys: [id]\ncolumns:\n"
              "  id: {type: string}\n"
              "  p: {type: path, relative_to: data_root}\n")
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert ("links: 1 path in p points outside data_root, through an "
                "absolute path, ~, .. or a link (data/links.csv; e.g. key 1)"
                ) in out
        assert ") (" not in out

    def test_parquet_time_zones_in_plain_words(self, tmp_path, capsys):
        duckdb = pytest.importorskip("duckdb")
        write(tmp_path / "DATA.md", "# Data\n\n```tq-config\ndialect: duckdb\n```\n")
        (tmp_path / "data").mkdir()
        con = duckdb.connect()
        con.execute("COPY (SELECT 'a' AS id, TIMESTAMP '2026-01-01 00:00:00' "
                    f"AS ts) TO '{(tmp_path / 'data' / 'n.parquet').as_posix()}' "
                    "(FORMAT parquet)")
        con.execute("SET TimeZone = 'UTC'")
        con.execute("COPY (SELECT 'a' AS id, TIMESTAMPTZ '2026-01-01 "
                    "00:00:00+00:00' AS ts) TO "
                    f"'{(tmp_path / 'data' / 'a.parquet').as_posix()}' "
                    "(FORMAT parquet)")
        con.close()
        for name, zone in (("n", "UTC"), ("a", "'local:Europe/Prague'")):
            write(tmp_path / "contracts" / f"{name}.yaml",
                  f"version: 2\ndataset: {name}\nkind: files\n"
                  f"location: {{path: data/{name}.parquet, format: parquet}}\n"
                  "grain: one row per id\nkeys: [id]\ncolumns:\n"
                  "  id: {type: string}\n"
                  f"  ts: {{type: timestamp, time_zone: {zone}}}\n")
        _, out, _ = tq(capsys, tmp_path, "verify", "--now", NOW)
        assert ("Not verified n: ts is stored with no time zone in the Parquet "
                "file, so whether its times are UTC can't be told from the "
                "data") in out
        assert ("Not verified a: ts is stored as an instant (a time with a "
                "zone) in the Parquet file, so the local zone the contract "
                "declares (Europe/Prague) can't be told from the data") in out
        assert "cannot verify" not in out and "TIMESTAMP" not in out
