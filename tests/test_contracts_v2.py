"""Contract Model v2, DATA.md bases, Metrics v1, JSON Schemas, and
`truequery contract validate` tests (PR A).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import jsonschema
import pytest

from truequery import contract_v2 as C2
from truequery import keylookup
from truequery import project as P
from truequery.analyzers.sql import check_text

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(ROOT, "plugin", "engine", "src")
SCHEMAS_DIR = os.path.join(SRC, "truequery", "schemas")
CLI = [sys.executable, "-m", "truequery.cli"]
ENV = dict(os.environ, PYTHONPATH=SRC)


def run_cli(*argv, cwd):
    proc = subprocess.run(
        CLI + list(argv), cwd=cwd, capture_output=True, text=True,
        env=ENV, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


# -- 1. Unified v1 & v2 loader in project.py ----------------------------------

def test_v1_contracts_remain_backwards_compatible(tmp_path):
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "orders.yaml").write_text(
        "table: orders\n"
        "grain: one row per order\n"
        "keys: [order_id]\n"
        "partition_key: order_date\n"
        "pii: [{column: email, masking: hash}]\n"
        "freshness_sla: 24h\n"
        "allowed_values:\n  status: [paid, refunded]\n"
        "deprecated: false\n"
        "quirks: null\n",
        encoding="utf-8",
    )
    loaded, problems = P.load_contracts_reporting(str(tmp_path))
    assert problems == []
    assert "orders" in loaded
    assert loaded["orders"]["table"] == "orders"
    assert loaded["orders"]["keys"] == ["order_id"]


def test_v1_contracts_keep_exact_0_1_6_name_rules(tmp_path):
    """v1 contracts keep exact 0.1.6 rules with no new name checks."""
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    examples = {
        "bq_unquoted.yaml": (
            "table: my-project.ds.orders\n"
            "grain: one row per order\n"
            "keys: [order_id]\n"
        ),
        "bq_backticked.yaml": (
            'table: "`my-project.ds.orders`"\n'
            "grain: one row per order\n"
            "keys: [order_id]\n"
        ),
        "spaced_names.yaml": (
            'table: "Order Items"\n'
            "grain: unknown users per day\n"
            'keys: ["Order ID"]\n'
            'partition_key: "Order Date"\n'
        ),
    }
    for fname, content in examples.items():
        path = cdir / fname
        path.write_text(content, encoding="utf-8")
        doc = P.load_contract(str(path))
        assert doc["table"]

    loaded, problems = P.load_contracts_reporting(str(tmp_path))
    assert problems == []
    assert set(loaded) == {
        "my-project.ds.orders",
        "`my-project.ds.orders`",
        "order items",
    }
    assert not P.is_draft_contract(loaded["order items"])

    code, out, err = run_cli("contract", "validate", cwd=tmp_path)
    assert code == 0, err
    assert "3 contracts validated, no errors" in out


def test_v2_and_metric_names_allow_unicode_spaces_hyphens_dollar_and_reject_syntax():
    c_doc = C2.validate_contract_v2({
        "version": 2,
        "dataset": "my-project.ds.Order Items",
        "kind": "table",
        "location": {"relation": "my-project.raw.Order Items"},
        "grain": "unknown users per day",
        "keys": ["order-id", ["file size", "número"]],
        "partition_key": "order-date",
        "freshness_sla": {"column": "updated ts", "max_age": "12h"},
        "columns": {
            "order-id": {"type": "string"},
            "file size": {"type": "number", "unit": "bytes", "size_of": "artifact path"},
            "artifact path": {"type": "path"},
            "número": {"type": "integer"},
            "nu\u0301mero_nfd": {"type": "integer"},
            "$amount": {"type": "number", "unit": "major", "currency": "USD"},
            "order-date": {"type": "date"},
            "updated ts": {"type": "timestamp", "time_zone": "UTC"},
        },
    }, "contracts/order_items.yaml")
    assert c_doc["dataset"] == "my-project.ds.Order Items"

    m_doc = C2.validate_metric({
        "version": 1,
        "metric": "total-file size$",
        "kind": "simple",
        "dataset": "my-project.ds.Order Items",
        "measure": {
            "agg": "sum",
            "column": "file size",
            "where": '"order-id" <> \'test\' AND número > 0',
        },
        "grain": ["order-date", "número"],
        "unit": "bytes",
    }, "metrics/file_size.yaml", allowed_columns=set(c_doc["columns"]))
    assert m_doc["metric"] == "total-file size$"

    for bad_col in [
        "price*qty",
        "x) FROM read_text('/etc/passwd') --",
        "`order_id`",
        '"order_id"',
        "'order_id'",
        "a;b",
        "fn(x)",
        "a.b",
        " leading",
        "trailing ",
    ]:
        with pytest.raises(ValueError, match="plain column identifier"):
            C2.validate_metric({
                "version": 1,
                "metric": "m",
                "kind": "simple",
                "dataset": "orders",
                "measure": {"agg": "sum", "column": bad_col},
            }, "metrics/m.yaml")

    for bad_ds in [
        "`my-project.ds.orders`",
        '"my-project.ds.orders"',
        "orders; DROP TABLE x",
        "x) FROM y --",
        "a..b",
        ".orders",
        "orders.",
    ]:
        with pytest.raises(ValueError, match="plain table"):
            C2.validate_contract_v2({
                "version": 2,
                "dataset": bad_ds,
                "grain": "one row per id",
                "keys": ["id"],
            }, "contracts/bad.yaml")


def test_grain_unconfirmed_only_for_todo_word_or_exact_unknown(tmp_path):
    # Real grain starting with 'unknown' is confirmed, not draft
    doc = C2.validate_contract_v2({
        "version": 2,
        "dataset": "daily_traffic",
        "grain": "unknown users per day",
        "keys": ["day"],
    }, "contracts/daily_traffic.yaml")
    assert not P.is_draft_contract(doc)

    # Exact 'unknown', 'TODO', and the `unknown  # TODO: ...` form `init` writes
    # are unconfirmed:
    for unconfirmed in [
        "unknown",
        "  UNKNOWN  ",
        "TODO",
        "one row per TODO",
        "unknown  # TODO: one row per <what>",
    ]:
        assert C2.is_unconfirmed_grain(unconfirmed)
        with pytest.raises(ValueError, match="is unconfirmed"):
            C2.validate_contract_v2({
                "version": 2,
                "dataset": "daily_traffic",
                "grain": unconfirmed,
                "keys": ["day"],
            }, "contracts/daily_traffic.yaml")

    # Verify the unquoted YAML form written by `truequery init` (`grain: unknown  # TODO: ...`)
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "seeded.yaml").write_text(
        "table: seeded\n"
        "grain: unknown  # TODO: one row per <what>\n"
        "keys: [id]\n",
        encoding="utf-8",
    )
    loaded = P.load_contract(str(cdir / "seeded.yaml"))
    assert loaded["grain"] == "unknown"
    assert P.is_draft_contract(loaded)


def test_v1_version_true_is_rejected(tmp_path):
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "orders.yaml").write_text(
        "version: true\n"
        "table: orders\n"
        "grain: one row per order\n"
        "keys: [order_id]\n",
        encoding="utf-8",
    )
    loaded, problems = P.load_contracts_reporting(str(tmp_path))
    assert loaded == {}
    assert len(problems) == 1
    assert "unsupported contract version True" in problems[0]


def test_v2_contract_full_valid_and_normalizes_dataset_and_table(tmp_path):
    (tmp_path / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\n"
        "version: 1\nstack: plain\ndialect: null\nmodes: []\n"
        "bases:\n"
        "  data_root:\n"
        "    env: DATA_ROOT\n"
        "    default: ./data\n"
        "  archive_root: ./archive\n"
        "```\n",
        encoding="utf-8",
    )
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "fetch_log.yaml").write_text(
        "version: 2\n"
        "dataset: fetch_log\n"
        "meaning: Per-attempt log of file fetches\n"
        "kind: files\n"
        "status: confirmed\n"
        "location:\n"
        "  path: logs/fetch/*.csv\n"
        "  base: data_root\n"
        "  format: csv\n"
        "grain: one row per fetch attempt\n"
        "keys:\n"
        "  - [run_id, url]\n"
        "  - attempt_id\n"
        "freshness_sla:\n"
        "  column: fetched_at\n"
        "  max_age: 30min\n"
        "write_mode: append\n"
        "columns:\n"
        "  run_id: {type: string, nullable: false}\n"
        "  url: {type: string, nullable: false}\n"
        "  attempt_id: {type: string, nullable: false}\n"
        "  path:\n"
        "    type: path\n"
        "    relative_to: data_root\n"
        "    status_column: status\n"
        "  size:\n"
        "    type: number\n"
        "    unit: GiB\n"
        "    size_of: path\n"
        "  cost_cents:\n"
        "    type: integer\n"
        "    unit: minor\n"
        "    currency: USD\n"
        "  local_fee:\n"
        "    type: number\n"
        "    unit: major\n"
        "    currency_column: fee_currency\n"
        "  fee_currency:\n"
        "    type: string\n"
        "  fetched_at:\n"
        "    type: timestamp\n"
        "    time_zone: UTC\n"
        "  local_ts:\n"
        "    type: timestamp\n"
        "    time_zone: local:Europe/Prague\n"
        "  naive_ts:\n"
        "    type: timestamp\n"
        "    time_zone: naive\n"
        "  status:\n"
        "    type: string\n"
        "    nullable: false\n"
        "    values:\n"
        "      fetched: {means: present}\n"
        "      released: {means: absent_expected}\n"
        "      refetchable: {means: absent_expected}\n"
        "      removed_on_purpose: {means: absent_expected}\n"
        "      failed: {means: absent_unexpected}\n"
        "  operator_email:\n"
        "    type: string\n"
        "    pii: true\n",
        encoding="utf-8",
    )
    loaded, problems = P.load_contracts_reporting(str(tmp_path))
    assert problems == []
    contract = loaded["fetch_log"]
    assert contract["dataset"] == "fetch_log"
    assert contract["table"] == "fetch_log"
    assert contract["kind"] == "files"
    assert contract["status"] == "confirmed"
    assert P.match_contract("raw.fetch_log", loaded) == "fetch_log"


def test_v2_composite_and_single_keys_work_with_keylookup(tmp_path):
    keylookup._CACHE.clear()
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "fetch_log.yaml").write_text(
        "version: 2\n"
        "dataset: fetch_log\n"
        "grain: one row per (run_id, url)\n"
        "keys:\n"
        "  - [run_id, url]\n"
        "  - attempt_id\n",
        encoding="utf-8",
    )
    sql = (
        "SELECT o.id, COUNT(*) AS n\n"
        "FROM runs o\n"
        "LEFT JOIN fetch_log f ON f.run_id = o.run_id AND f.url = o.url\n"
        "GROUP BY 1;\n"
    )
    res = check_text(sql, path=str(tmp_path / "q.sql"))
    rules = [f["rule"] for f in res["findings"]]
    assert "sql/count-star-after-left-join" not in rules

    # Partial composite key still fires
    bad_sql = (
        "SELECT o.id, COUNT(*) AS n\n"
        "FROM runs o\n"
        "LEFT JOIN fetch_log f ON f.run_id = o.run_id\n"
        "GROUP BY 1;\n"
    )
    res_bad = check_text(bad_sql, path=str(tmp_path / "q.sql"))
    assert "sql/count-star-after-left-join" in [
        f["rule"] for f in res_bad["findings"]]


def test_draft_and_invalid_contracts_never_suppress_findings_and_receipt_names_draft(
    tmp_path
):
    """Review item #2:
    1. No contract -> 1 error
    2. status: draft -> 1 error (draft keys never suppress), receipt says `(1 draft)`
    3. Invalid contract (`bogus: 1`) -> 1 error, receipt lists invalid contract
    """
    q_sql = (
        "SELECT o.region, COUNT(*) FROM customers c\n"
        "LEFT JOIN orders o ON c.id = o.customer_id GROUP BY 1;\n"
    )
    (tmp_path / "q.sql").write_text(q_sql, encoding="utf-8")
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "customers.yaml").write_text(
        "version: 2\ndataset: customers\ngrain: one row per id\nkeys: [id]\n",
        encoding="utf-8",
    )

    # Row 1: No contract for orders -> 1 error
    keylookup._CACHE.clear()
    code, out, _ = run_cli("review", "q.sql", cwd=tmp_path)
    assert code == 1
    assert "sql/count-star-after-left-join" in out
    assert "contracts for 1 of 2 tables" in out

    # Row 2: Draft contract for orders claiming customer_id is a key -> still 1 error,
    # and receipt says `contracts for 2 of 2 tables (1 draft)`
    (cdir / "orders.yaml").write_text(
        "version: 2\n"
        "dataset: orders\n"
        "status: draft\n"
        "inferred_from: 10-row sample\n"
        "grain: TODO\n"
        "keys: [customer_id]\n",
        encoding="utf-8",
    )
    keylookup._CACHE.clear()
    code, out, _ = run_cli("review", "q.sql", cwd=tmp_path)
    assert code == 1
    assert "sql/count-star-after-left-join" in out
    assert "contracts for 2 of 2 tables (1 draft)" in out

    # Row 3: Invalid contract (`bogus: 1`) claiming customer_id is a key -> still 1 error,
    # and receipt lists the invalid contract
    (cdir / "orders.yaml").write_text(
        "table: orders\ngrain: one row per customer\nkeys: [customer_id]\nbogus: 1\n",
        encoding="utf-8",
    )
    keylookup._CACHE.clear()
    code, out, _ = run_cli("review", "q.sql", cwd=tmp_path)
    assert code == 1
    assert "sql/count-star-after-left-join" in out
    assert "invalid contract: contracts/orders.yaml: unknown keys ['bogus']" in out


def test_v2_draft_contract_requires_inferred_from_and_allows_todo_means():
    raw_draft = {
        "version": 2,
        "dataset": "events",
        "status": "draft",
        "inferred_from": "2,000-row sample of events.csv",
        "grain": "TODO",
        "keys": ["event_id"],
        "columns": {
            "event_id": {"type": "string"},
            "status": {
                "type": "string",
                "values": {
                    "ok": {"means": "TODO"},
                    "dropped": {"means": "TODO"},
                },
            },
        },
    }
    validated = C2.validate_contract_v2(raw_draft, "contracts/events.yaml")
    assert validated["status"] == "draft"

    # Missing inferred_from on draft fails
    bad_draft = dict(raw_draft, inferred_from=None)
    with pytest.raises(ValueError, match="inferred_from is required"):
        C2.validate_contract_v2(bad_draft, "contracts/events.yaml")

    # grain: TODO on confirmed contract fails
    confirmed_with_todo_grain = dict(raw_draft, status="confirmed")
    with pytest.raises(ValueError, match="grain 'TODO' is unconfirmed"):
        C2.validate_contract_v2(
            confirmed_with_todo_grain, "contracts/events.yaml")

    # means: TODO on confirmed contract fails
    confirmed_with_todo = dict(
        raw_draft, status="confirmed", grain="one row per event_id")
    with pytest.raises(
        ValueError, match="only allowed when contract status is 'draft'"
    ):
        C2.validate_contract_v2(confirmed_with_todo, "contracts/events.yaml")


@pytest.mark.parametrize("bad_patch,match", [
    ({"version": 3}, "version must be 2"),
    ({"version": True}, "version must be 2"),
    ({"dataset": "a", "table": "b"}, "dataset .* and table .* must match"),
    ({"dataset": ""}, "dataset .* is required"),
    ({"dataset": "orders; DROP TABLE x"}, "must be a plain table"),
    ({"grain": ""}, "grain must be a non-empty string"),
    ({"grain": "TODO"}, "grain 'TODO' is unconfirmed"),
    ({"grain": "unknown  # TODO"}, "is unconfirmed"),
    ({"keys": "id"}, "keys must be a list"),
    ({"keys": [[]]}, "composite key .* must be a non-empty list"),
    ({"keys": ["a*b"]}, "must be a plain column identifier"),
    ({"keys": ["run_idd"], "columns": {"run_id": {"type": "string"}}},
     "keys\\[0\\] \\('run_idd'\\) is not in columns"),
    ({"partition_key": "bad;col"}, "must be a plain column identifier"),
    ({"keys": ["a"], "partition_key": "b", "columns": {"a": {"type": "string"}}},
     "partition_key \\('b'\\) is not in columns"),
    ({"kind": "stream"}, "kind must be one of"),
    ({"kind": "files"}, "location is required"),
    ({"kind": "files", "location": {"path": "../outside.csv", "format": "csv"}},
     "location.path must be a non-empty relative path without '\\.\\.' or '~'"),
    ({"kind": "files", "location": {"path": "~/.ssh/*", "format": "csv"}},
     "location.path must be a non-empty relative path without '\\.\\.' or '~'"),
    ({"kind": "files", "location": {"path": "/abs/file.csv", "format": "csv"}},
     "location.path must be a non-empty relative path"),
    ({"kind": "files", "location": {"path": "a.csv", "format": "xlsx"}},
     "location.format must be one of"),
    ({"kind": "table", "location": {"relation": "x) FROM y --"}},
     "location.relation .* must be a plain table"),
    ({"write_mode": "truncate"}, "write_mode must be one of"),
    ({"freshness_sla": {"column": "ts", "max_age": "24hours"}},
     "freshness_sla.max_age must match"),
    ({"freshness_sla": {"column": "ts", "max_age": "10m"}},
     "freshness_sla.max_age must match"),
    ({"freshness_sla": {"column": "bad*col", "max_age": "10min"}},
     "freshness_sla.column .* must be a plain column identifier"),
    ({"freshness_sla": {"column": "id", "max_age": "12h"},
      "columns": {"order_id": {"type": "string"}, "id": {"type": "string"}}},
     "freshness_sla.column 'id' must have type 'timestamp' or 'date'"),
    ({"columns": {"order_id": {"type": "string"},
                  "ts": {"type": "timestamp", "time_zone": "EST"}}},
     "time_zone must be 'UTC', 'naive', or 'local:<Area>/<Location>'"),
    ({"columns": {"order_id": {"type": "string"},
                  "ts": {"type": "integer", "time_zone": "UTC"}}},
     "time_zone requires type: timestamp"),
    ({"columns": {"order_id": {"type": "string"},
                  "sz": {"type": "string", "unit": "GiB"}}},
     "unit 'GiB' requires numeric type 'integer' or 'number'"),
    ({"columns": {"order_id": {"type": "string"},
                  "amt": {"type": "integer", "unit": "minor"}}},
     "requires currency or currency_column"),
    ({"columns": {"order_id": {"type": "string"},
                  "amt": {"type": "integer", "currency": "USD"}}},
     "requires unit 'minor' or 'major'"),
    ({"columns": {"order_id": {"type": "string"},
                  "amt": {"type": "integer", "unit": "minor", "currency": "us"}}},
     "3-letter uppercase ISO-4217"),
    ({"columns": {"order_id": {"type": "string"},
                  "amt": {"type": "integer", "unit": "minor",
                          "currency": "USD", "currency_column": "c"}}},
     "cannot set both currency and currency_column"),
    ({"columns": {"order_id": {"type": "string"},
                  "amt": {"type": "integer", "unit": "minor",
                          "currency_column": "missing_col"}}},
     "currency_column references unknown column 'missing_col'"),
    ({"columns": {"order_id": {"type": "string"},
                  "sz": {"type": "number", "size_of": "p"},
                  "p": {"type": "path"}}},
     "size_of requires a size unit"),
    ({"columns": {"order_id": {"type": "string"},
                  "sz": {"type": "number", "unit": "ms", "size_of": "p"},
                  "p": {"type": "path"}}},
     "size_of requires a size unit"),
    ({"columns": {"order_id": {"type": "string"},
                  "sz": {"type": "number", "unit": "GiB", "size_of": "p"},
                  "p": {"type": "string"}}},
     "size_of references 'p', which must be a column with type: path"),
    ({"columns": {"order_id": {"type": "string"},
                  "p": {"type": "string", "status_column": "st"},
                  "st": {"type": "string", "values": {"ok": {"means": "present"}}}}},
     "status_column requires type: path"),
    ({"columns": {"order_id": {"type": "string"},
                  "p": {"type": "path", "status_column": "st"},
                  "st": {"type": "string"}}},
     "status_column references 'st', which must be a column with values"),
    ({"columns": {"order_id": {"type": "string"},
                  "p": {"type": "path", "status_column": "st"},
                  "st": {"type": "string", "values": ["ok", "fail"]}}},
     "mapping each status to `means`"),
    ({"columns": {"order_id": {"type": "string"},
                  "p": {"type": "path", "status_column": "st"},
                  "st": {"type": "string", "values": {"ok": {}}}}},
     "missing `means`"),
    ({"columns": {"order_id": {"type": "string"},
                  "p": {"type": "string", "relative_to": "root"}}},
     "relative_to requires type: path"),
    ({"columns": {"order_id": {"type": "string"},
                  "email": {"type": "string", "pii": "hash"}}},
     "pii must be a boolean"),
    ({"freshness_sla": {"column": "missing_ts", "max_age": "12h"},
      "columns": {"order_id": {"type": "string"}}},
     "freshness_sla.column 'missing_ts' is not in columns"),
])
def test_v2_contract_rejects_invalid_fields(bad_patch, match):
    base = {
        "version": 2,
        "dataset": "orders",
        "grain": "one row per order_id",
        "keys": ["order_id"],
    }
    base.update(bad_patch)
    with pytest.raises(ValueError, match=match):
        C2.validate_contract_v2(base, "contracts/orders.yaml")


# -- 2. Metric v1 validation & exact-type `where` AST allowlist ----------------

@pytest.mark.parametrize("where_str,expected_cols", [
    ("status = 'fetched'", ["status"]),
    ("status = 'a--b'", ["status"]),
    ("status = 'a;b'", ["status"]),
    ("status = '/*not_a_comment*/'", ["status"]),
    ("status <> 'removed_on_purpose'", ["status"]),
    ("status IN ('fetched', 'released') AND size > 0", ["size", "status"]),
    ("delta >= -1 AND delta <= 10", ["delta"]),
    ("attempts IN (-1, 0, 1, 2)", ["attempts"]),
    ("created_at BETWEEN '2026-01-01' AND '2026-01-31'", ["created_at"]),
    ("deleted_at IS NULL AND is_test = FALSE", ["deleted_at", "is_test"]),
    ("is_active IS TRUE AND NOT (is_internal = TRUE)", ["is_active", "is_internal"]),
    ("path LIKE 'logs/%' OR path ILIKE 'archive/%'", ["path"]),
    ("(a = 1 OR b = 2) AND c IS NOT NULL", ["a", "b", "c"]),
])
def test_metric_where_allowlist_accepts_safe_filters(where_str, expected_cols):
    cols = C2.validate_metric_where(
        where_str, "metrics/m.yaml", "numerator",
        allowed_columns=set(expected_cols))
    assert cols == expected_cols


@pytest.mark.parametrize("bad_where,match", [
    ("status = 'fetched'; DROP TABLE fetch_log", "single filter condition"),
    ("read_text('/etc/passwd') IS NOT NULL", "disallowed expression"),
    ("COALESCE(status, 'x') = 'fetched'", "disallowed expression"),
    ("LOWER(status) = 'fetched'", "disallowed expression"),
    ("CAST(size AS INT) > 0", "disallowed expression"),
    ("status IN (SELECT s FROM other)", "not a subquery"),
    ("EXISTS (SELECT 1 FROM other)", "disallowed expression"),
    ("t.status = 'fetched'", "must not be table-qualified"),
    ("db.t.status = 'fetched'", "must not be table-qualified"),
    ("status LIKE other_col", "LIKE pattern must be a string literal"),
    ("'logs/%' LIKE path", "LIKE left side must be a column"),
    ("1 = 1", "must reference a column"),
    ("status = 'fetched' OR 1 = 1", "must reference a column"),
    ("TRUE OR status = 'x'", "boolean literal"),
    ("status = 'x' OR TRUE", "boolean literal"),
    ("TRUE = status", "boolean literal"),
    ("a IN ('x') OR a", "bare column"),
    ("NOT is_test", "bare column"),
    ("status", "bare column"),
    ("status + 1 > 2", "disallowed expression"),
    ("status = 'fetched' -- comment", "must not contain SQL comments"),
    ("status = 'fetched' /* comment */", "must not contain SQL comments"),
    ("-size > 0", "unary '-' is only allowed on numeric literals"),
    ("'a' IN ('a', 'b') AND status = 'fetched'", "IN left side must be a column"),
])
def test_metric_where_allowlist_rejects_unsafe_or_disallowed_nodes(
    bad_where, match
):
    with pytest.raises(ValueError, match=match):
        C2.validate_metric_where(bad_where, "metrics/m.yaml", "numerator")


def test_metric_where_rejects_unknown_column_in_contract():
    with pytest.raises(ValueError, match="references unknown column"):
        C2.validate_metric_where(
            "typo_status = 'fetched'", "metrics/m.yaml", "numerator",
            allowed_columns={"status", "size"})


def test_metric_v1_valid_ratio_and_simple():
    ratio = {
        "version": 1,
        "metric": "collection_rate",
        "meaning": "share of known items collected",
        "kind": "ratio",
        "dataset": "fetch_log",
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
        "grain": "per run_id",
        "unit": "ratio",
        "owner": "data-team",
    }
    out_r = C2.validate_metric(
        ratio, "metrics/collection_rate.yaml",
        allowed_columns={"status", "run_id"})
    assert out_r["metric"] == "collection_rate"

    simple = {
        "metric": "net_revenue",
        "kind": "simple",
        "dataset": "orders",
        "measure": {
            "agg": "sum",
            "column": "amount_cents",
            "where": "status = 'paid' AND is_internal = FALSE",
        },
        "grain": ["order_date"],
        "unit": "minor",
    }
    out_s = C2.validate_metric(
        simple, "metrics/net_revenue.yaml",
        allowed_columns={"amount_cents", "status", "is_internal"})
    assert out_s["version"] == 1


@pytest.mark.parametrize("bad_metric,match", [
    ({"metric": "m", "kind": "derived", "dataset": "d"},
     "kind must be one of \\['simple', 'ratio'\\]"),
    ({"metric": "m", "kind": "ratio", "dataset": "d",
      "numerator": {"agg": "count", "column": "*"}},
     "requires both numerator and denominator"),
    ({"metric": "m", "kind": "ratio", "dataset": "d",
      "numerator": {"agg": "count", "column": "*"},
      "denominator": {"agg": "count", "column": "*"},
      "measure": {"agg": "count", "column": "*"}},
     "not measure"),
    ({"metric": "m", "kind": "simple", "dataset": "d",
      "numerator": {"agg": "count", "column": "*"}},
     "not numerator/denominator"),
    ({"metric": "m", "kind": "simple", "dataset": "d",
      "measure": {"agg": "sum", "column": "*"}},
     "column '\\*' is only valid when agg is 'count'"),
    ({"metric": "m", "kind": "simple", "dataset": "d",
      "measure": {"agg": "sum", "column": "x) FROM read_text('/etc/passwd') --"}},
     "must be a plain column identifier"),
    ({"metric": "m", "kind": "simple", "dataset": "d",
      "measure": {"agg": "sum", "column": "price*qty"}},
     "must be a plain column identifier"),
    ({"metric": "m", "kind": "simple", "dataset": "d; DROP TABLE x",
      "measure": {"agg": "count", "column": "*"}},
     "dataset .* must be a plain table"),
    ({"metric": "m", "kind": "simple", "dataset": "d",
      "measure": {"agg": "median", "column": "x"}},
     "agg must be one of"),
])
def test_metric_v1_rejects_invalid_definitions(bad_metric, match):
    with pytest.raises(ValueError, match=match):
        C2.validate_metric(bad_metric, "metrics/bad.yaml")


# -- 3. Cross-file validation, duplicates & `truequery contract validate` -----

def test_duplicate_contracts_and_metrics_are_rejected(tmp_path):
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    mdir = tmp_path / "metrics"
    mdir.mkdir()
    (cdir / "orders_a.yaml").write_text(
        "version: 2\ndataset: orders\ngrain: one row per id\nkeys: [id]\n",
        encoding="utf-8",
    )
    (cdir / "orders_b.yaml").write_text(
        "version: 2\ndataset: orders\ngrain: one row per id\nkeys: [id]\n",
        encoding="utf-8",
    )
    (cdir / "items.yaml").write_text(
        "version: 2\ndataset: items\ngrain: one row per id\nkeys: [id]\n",
        encoding="utf-8",
    )
    (mdir / "rev_1.yaml").write_text(
        "version: 1\nmetric: rev\nkind: simple\ndataset: items\n"
        "measure: {agg: count, column: '*'}\n",
        encoding="utf-8",
    )
    (mdir / "rev_2.yaml").write_text(
        "version: 1\nmetric: rev\nkind: simple\ndataset: items\n"
        "measure: {agg: count, column: '*'}\n",
        encoding="utf-8",
    )
    code, out, _ = run_cli("contract", "validate", cwd=tmp_path)
    assert code == 1
    assert "contracts/orders_b.yaml: [error] contract: duplicate contract for dataset 'orders' (already defined in contracts/orders_a.yaml)" in out
    assert "metrics/rev_2.yaml: [error] metric: duplicate metric 'rev' (already defined in metrics/rev_1.yaml)" in out


def test_first_contract_wins_across_context_validate_and_metrics(tmp_path):
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    mdir = tmp_path / "metrics"
    mdir.mkdir()
    (cdir / "a.yaml").write_text(
        "version: 2\ndataset: orders\ngrain: one row per order\n"
        "keys: [order_id]\ncolumns:\n  order_id: {type: string}\n",
        encoding="utf-8",
    )
    (cdir / "b.yaml").write_text(
        "version: 2\ndataset: orders\ngrain: one row per customer\n"
        "keys: [customer_id]\ncolumns:\n  customer_id: {type: string}\n",
        encoding="utf-8",
    )
    (mdir / "orders_cnt.yaml").write_text(
        "version: 1\nmetric: orders_cnt\nkind: simple\ndataset: orders\n"
        "measure: {agg: count, column: order_id}\n",
        encoding="utf-8",
    )
    query = tmp_path / "q.sql"
    query.write_text(
        "SELECT o.region, COUNT(*) FROM customers c "
        "LEFT JOIN orders o ON c.id = o.customer_id GROUP BY 1;\n",
        encoding="utf-8",
    )

    # 1. `context` uses a.yaml (keys=order_id) and marks only customers missing
    code, out, err = run_cli("context", "--target", "q.sql", cwd=tmp_path)
    assert code == 0, err
    assert "contract orders: grain=one row per order keys=order_id" in out
    assert "keys=customer_id" not in out
    assert "CONTRACT_MISSING: customers" in out
    assert "CONTRACT_MISSING: orders" not in out
    assert "metric orders_cnt:" in out

    # 2. `contract validate` on the first contract passes; on the second fails
    code_a, out_a, _ = run_cli(
        "contract", "validate", "contracts/a.yaml", cwd=tmp_path)
    assert code_a == 0
    assert out_a.strip() == "clean: 1 contract validated, no errors"

    code_b, out_b, _ = run_cli(
        "contract", "validate", "contracts/b.yaml", cwd=tmp_path)
    assert code_b == 1
    assert (
        "contracts/b.yaml: [error] contract: duplicate contract for dataset "
        "'orders' (already defined in contracts/a.yaml)"
    ) in out_b

    # 3. Whole-project `contract validate` keeps a.yaml for metric validation
    (mdir / "bad_col.yaml").write_text(
        "version: 1\nmetric: bad_col\nkind: simple\ndataset: orders\n"
        "measure: {agg: count, column: customer_id}\n",
        encoding="utf-8",
    )
    code_all, out_all, _ = run_cli("contract", "validate", cwd=tmp_path)
    assert code_all == 1
    assert "contracts/b.yaml: [error] contract: duplicate contract" in out_all
    assert "contracts/a.yaml:" not in out_all
    assert "metrics/orders_cnt.yaml:" not in out_all
    assert "metrics/bad_col.yaml: [error] metric:" in out_all


def test_cross_file_validation_and_cli_contract_validate(tmp_path):
    (tmp_path / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\n"
        "version: 1\nstack: plain\ndialect: null\nmodes: []\n"
        "bases:\n  data_root: ./data\n```\n",
        encoding="utf-8",
    )
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    mdir = tmp_path / "metrics"
    mdir.mkdir()

    (cdir / "fetch_log.yaml").write_text(
        "version: 2\n"
        "dataset: fetch_log\n"
        "kind: files\n"
        "location: {path: 'logs/*.csv', base: data_root, format: csv}\n"
        "grain: one row per (run_id, url)\n"
        "keys: [[run_id, url]]\n"
        "columns:\n"
        "  run_id: {type: string}\n"
        "  url: {type: string}\n"
        "  path: {type: path, relative_to: data_root, status_column: status}\n"
        "  status:\n"
        "    type: string\n"
        "    values:\n"
        "      fetched: {means: present}\n"
        "      removed_on_purpose: {means: absent_expected}\n",
        encoding="utf-8",
    )
    (mdir / "collection_rate.yaml").write_text(
        "version: 1\n"
        "metric: collection_rate\n"
        "kind: ratio\n"
        "dataset: fetch_log\n"
        "numerator: {agg: count, column: '*', where: \"status = 'fetched'\"}\n"
        "denominator: {agg: count, column: '*', "
        "where: \"status <> 'removed_on_purpose'\"}\n",
        encoding="utf-8",
    )

    code, out, err = run_cli("contract", "validate", cwd=tmp_path)
    assert code == 0, err
    assert out.strip() == "clean: 1 contract and 1 metric validated, no errors"

    code, jout, _ = run_cli("contract", "validate", "--json", cwd=tmp_path)
    assert code == 0
    doc = json.loads(jout)
    assert doc["valid"] is True
    assert doc["contracts_checked"] == ["contracts/fetch_log.yaml"]
    assert doc["metrics_checked"] == ["metrics/collection_rate.yaml"]
    assert doc["bases"] == ["data_root"]
    assert doc["problems"] == []

    # Introduce cross-file errors: undeclared base + bad metric column
    (cdir / "bad_base.yaml").write_text(
        "version: 2\n"
        "dataset: bad_base\n"
        "grain: one row per id\n"
        "keys: [id]\n"
        "columns:\n"
        "  id: {type: string}\n"
        "  p: {type: path, relative_to: missing_base}\n",
        encoding="utf-8",
    )
    (mdir / "bad_metric.yaml").write_text(
        "version: 1\n"
        "metric: bad_metric\n"
        "kind: simple\n"
        "dataset: fetch_log\n"
        "measure: {agg: count, column: '*', where: \"no_such_col = 'x'\"}\n",
        encoding="utf-8",
    )

    code, out, _ = run_cli("contract", "validate", cwd=tmp_path)
    assert code == 1
    assert "contracts/bad_base.yaml: [error] contract:" in out
    assert "undeclared base 'missing_base'" in out
    assert "metrics/bad_metric.yaml: [error] metric:" in out
    assert "unknown column(s) ['no_such_col']" in out
    assert "2 errors in 2 contracts and 2 metrics checked" in out

    # Review JSON receipt separates invalid_contracts and invalid_metrics
    (tmp_path / "q.sql").write_text(
        "SELECT run_id FROM fetch_log;\n", encoding="utf-8")
    code, rout, _ = run_cli("review", "q.sql", "--json", cwd=tmp_path)
    assert code == 0
    r_nc = json.loads(rout)["receipt"]["not_checked"]
    assert len(r_nc["invalid_contracts"]) == 1
    assert r_nc["invalid_contracts"][0].startswith("contracts/bad_base.yaml:")
    assert len(r_nc["invalid_metrics"]) == 1
    assert r_nc["invalid_metrics"][0].startswith("metrics/bad_metric.yaml:")

    # SARIF output on errors
    code, sout, _ = run_cli("contract", "validate", "--sarif", cwd=tmp_path)
    assert code == 1
    sarif = json.loads(sout)
    assert sarif["version"] == "2.1.0"
    results = sarif["runs"][0]["results"]
    assert len(results) == 2
    assert {r["ruleId"] for r in results} == {"contract-schema", "metric-schema"}

    # Missing explicit path returns exit code 2
    code, _, err = run_cli("contract", "validate", "no_such.yaml", cwd=tmp_path)
    assert code == 2
    assert "no such file or directory" in err


def test_context_renders_v2_facts_draft_directive_and_metrics(tmp_path):
    (tmp_path / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\n"
        "version: 1\nstack: plain\ndialect: null\nmodes: []\n"
        "bases:\n  data_root: ./data\n```\n",
        encoding="utf-8",
    )
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    mdir = tmp_path / "metrics"
    mdir.mkdir()
    (cdir / "fetch_log.yaml").write_text(
        "version: 2\n"
        "dataset: fetch_log\n"
        "status: draft\n"
        "inferred_from: 100-row sample of fetch.csv\n"
        "kind: files\n"
        "location: {path: 'fetch.csv', base: data_root, format: csv}\n"
        "freshness_sla: {column: fetched_at, max_age: 24h}\n"
        "grain: one row per (run_id, url)\n"
        "keys: [[run_id, url]]\n"
        "columns:\n"
        "  run_id: {type: string, nullable: false}\n"
        "  url: {type: string, nullable: false}\n"
        "  path: {type: path, relative_to: data_root, status_column: status}\n"
        "  size: {type: number, unit: GiB, size_of: path}\n"
        "  fetched_at: {type: timestamp, time_zone: UTC}\n"
        "  status:\n"
        "    type: string\n"
        "    nullable: false\n"
        "    values:\n"
        "      fetched: {means: present}\n"
        "      removed_on_purpose: {means: absent_expected}\n"
        "  email: {type: string, pii: true}\n",
        encoding="utf-8",
    )
    (mdir / "collection_rate.yaml").write_text(
        "version: 1\n"
        "metric: collection_rate\n"
        "meaning: share of items collected\n"
        "kind: ratio\n"
        "dataset: fetch_log\n"
        "numerator: {agg: count, column: '*', where: \"status = 'fetched'\"}\n"
        "denominator: {agg: count, column: '*', "
        "where: \"status <> 'removed_on_purpose'\"}\n"
        "grain: per run_id\n"
        "unit: ratio\n",
        encoding="utf-8",
    )
    (tmp_path / "q.sql").write_text(
        "SELECT status, COUNT(*) FROM fetch_log GROUP BY 1;\n",
        encoding="utf-8",
    )

    code, out, err = run_cli("context", "--target", "q.sql", cwd=tmp_path)
    assert code == 0, err
    assert (
        "contract fetch_log: grain=one row per (run_id, url) "
        "keys=(run_id,url) kind=files status=draft "
        "location=(fetch.csv, format=csv, base=data_root) "
        "freshness_sla=fetched_at<=24h"
    ) in out
    assert "path(path, base=data_root, status_column=status)" in out
    assert "size(number, unit=GiB, size_of=path)" in out
    assert "status(string, nullable=false, values=[fetched:present,removed_on_purpose:absent_expected])" in out
    assert "email(string, pii=true)" in out
    assert (
        "metric collection_rate: kind=ratio dataset=fetch_log "
        "meaning='share of items collected' "
        "numerator=count(*) where status = 'fetched' "
        "denominator=count(*) where status <> 'removed_on_purpose' "
        "grain=per run_id unit=ratio"
    ) in out
    assert "CONTRACT_DRAFT: fetch_log (100-row sample of fetch.csv)" in out


# -- 4. Real JSON Schema (Draft 2020-12) parity test --------------------------

# Cross-field/cross-column/AST rules that JSON Schema Draft 2020-12 cannot
# express because they require comparing two dynamic property values/keys or
# parsing SQL into an AST:
NON_JSON_SCHEMA_CONTRACT_CASES = {
    "dataset_table_mismatch": (
        {"version": 2, "dataset": "a", "table": "b", "grain": "one row",
         "keys": ["id"]},
        "dataset .* and table .* must match",
    ),
    "key_not_in_columns": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["run_idd"], "columns": {"run_id": {"type": "string"}}},
        "keys\\[0\\] \\('run_idd'\\) is not in columns",
    ),
    "partition_key_not_in_columns": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["a"], "partition_key": "b",
         "columns": {"a": {"type": "string"}}},
        "partition_key \\('b'\\) is not in columns",
    ),
    "freshness_col_not_timestamp_or_date": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["id"], "freshness_sla": {"column": "id", "max_age": "12h"},
         "columns": {"id": {"type": "string"}}},
        "freshness_sla.column 'id' must have type 'timestamp' or 'date'",
    ),
    "currency_column_missing_target": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["id"],
         "columns": {"id": {"type": "string"},
                     "amt": {"type": "integer", "unit": "minor",
                             "currency_column": "missing_col"}}},
        "currency_column references unknown column",
    ),
    "size_of_target_not_path": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["id"],
         "columns": {"id": {"type": "string"},
                     "sz": {"type": "number", "unit": "GiB", "size_of": "p"},
                     "p": {"type": "string"}}},
        "size_of references 'p', which must be a column with type: path",
    ),
    "status_column_target_without_means": (
        {"version": 2, "dataset": "orders", "grain": "one row",
         "keys": ["id"],
         "columns": {"id": {"type": "string"},
                     "p": {"type": "path", "status_column": "st"},
                     "st": {"type": "string", "values": ["ok", "fail"]}}},
        "mapping each status to `means`",
    ),
}

NON_JSON_SCHEMA_CONTRACT_CASES["draft_column_in_confirmed_contract"] = (
    {"version": 2, "dataset": "orders", "grain": "one row", "keys": ["id"],
     "columns": {"id": {"type": "string", "status": "draft"}}},
    "columns.id.status is draft, so the contract is a draft too",
)


def test_v2_column_status_and_csv_delimiter_are_accepted():
    doc = {"version": 2, "dataset": "x", "kind": "files", "status": "draft",
           "inferred_from": "a sample", "grain": "TODO: per id",
           "keys": ["id"],
           "location": {"path": "a.csv", "format": "csv", "delimiter": ";"},
           "columns": {"id": {"type": "string", "status": "draft"},
                       "v": {"type": "string", "status": "confirmed"}}}
    assert C2.validate_contract_v2(doc, "x.yaml")["columns"]["id"][
        "status"] == "draft"
    for delim in C2.CSV_DELIMITERS:
        doc["location"]["delimiter"] = delim
        C2.validate_contract_v2(doc, "x.yaml")
    doc["location"] = {"path": "a.parquet", "format": "parquet",
                       "delimiter": ","}
    with pytest.raises(ValueError, match="delimiter is only for format: csv"):
        C2.validate_contract_v2(doc, "x.yaml")


NON_JSON_SCHEMA_METRIC_CASES = {
    "where_sql_ast_function": (
        {"version": 1, "metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "count", "column": "*",
                     "where": "read_text('/etc/passwd') IS NOT NULL"}},
        "disallowed expression",
    ),
    "where_sql_ast_tautology": (
        {"version": 1, "metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "count", "column": "*",
                     "where": "TRUE OR status = 'x'"}},
        "boolean literal",
    ),
}


def test_published_json_schemas_with_real_jsonschema_validator():
    contract_schema_path = os.path.join(SCHEMAS_DIR, "contract-v2.schema.json")
    metric_schema_path = os.path.join(SCHEMAS_DIR, "metric-v1.schema.json")
    with open(contract_schema_path, encoding="utf-8") as handle:
        c_schema = json.load(handle)
    with open(metric_schema_path, encoding="utf-8") as handle:
        m_schema = json.load(handle)

    jsonschema.Draft202012Validator.check_schema(c_schema)
    jsonschema.Draft202012Validator.check_schema(m_schema)
    c_validator = jsonschema.Draft202012Validator(c_schema)
    m_validator = jsonschema.Draft202012Validator(m_schema)

    assert c_schema["$id"] == (
        "https://raw.githubusercontent.com/Analytically-ai/truequery/main/"
        "plugin/engine/src/truequery/schemas/contract-v2.schema.json"
    )
    assert m_schema["$id"] == (
        "https://raw.githubusercontent.com/Analytically-ai/truequery/main/"
        "plugin/engine/src/truequery/schemas/metric-v1.schema.json"
    )
    assert tuple(c_schema["$defs"]["unitEnum"]["enum"]) == C2.ALL_UNITS

    valid_contracts = [
        {
            "version": 2,
            "dataset": "fetch_log",
            "kind": "files",
            "location": {"path": "logs/*.csv", "format": "csv"},
            "grain": "one row per (run_id, url)",
            "keys": [["run_id", "url"]],
            "columns": {
                "run_id": {"type": "string"},
                "url": {"type": "string"},
                "path": {"type": "path", "relative_to": "root",
                         "status_column": "status"},
                "size": {"type": "number", "unit": "GiB", "size_of": "path"},
                "fetched_at": {"type": "timestamp", "time_zone": "UTC"},
                "status": {
                    "type": "string",
                    "values": {
                        "fetched": {"means": "present"},
                        "removed_on_purpose": {"means": "absent_expected"},
                    },
                },
            },
        },
        {
            "version": 2,
            "table": "orders",
            "grain": "one row per order_id",
            "keys": ["order_id"],
            "freshness_sla": {"column": "created_at", "max_age": "30min"},
            "columns": {
                "order_id": {"type": "string"},
                "created_at": {"type": "timestamp",
                               "time_zone": "local:America/New_York"},
                "amount_cents": {"type": "integer", "unit": "minor",
                                 "currency": "USD"},
                "email": {"type": "string", "pii": True},
            },
        },
        {
            "version": 2,
            "dataset": "my-project.ds.Order Items",
            "location": {"relation": "my-project.raw.Order Items"},
            "grain": "unknown users per day",
            "keys": ["order-id", ["file size", "número", "nº_pedido", "latency_µs", "$amount"]],
        },
        {
            "version": 2,
            "dataset": "pagos",
            "grain": "una fila por método de pago",
            "keys": ["id"],
        },
    ]
    for doc in valid_contracts:
        c_validator.validate(doc)
        C2.validate_contract_v2(doc, "test.yaml")

    schema_rejected_contracts = [
        {"version": 1, "dataset": "x", "grain": "g", "keys": ["id"]},
        {"version": True, "dataset": "x", "grain": "g", "keys": ["id"]},
        {"version": 2, "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x; DROP TABLE y", "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "`my-project.ds.orders`", "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "orders\n", "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "   ", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "TODO", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "unknown", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "unknown  # TODO", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["   "]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["a*b"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["price＊qty"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ['"Order ID"']},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "partition_key": "bad;col"},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "extra_unknown": 1},
        {"version": 2, "dataset": "x", "status": "draft", "grain": "g",
         "keys": ["id"]},
        {"version": 2, "dataset": "x", "status": "confirmed", "grain": "g",
         "keys": ["id"],
         "columns": {"id": {"type": "string"},
                     "s": {"values": {"ok": {"means": "TODO"}}}}},
        {"version": 2, "dataset": "x", "kind": "files", "grain": "g",
         "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "files",
         "location": {"path": "../outside.csv", "format": "csv"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "files",
         "location": {"path": "/abs/outside.csv", "format": "csv"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "files",
         "location": {"path": "~/.ssh/*", "format": "csv"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "table",
         "location": {"path": "a.csv", "format": "csv"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "table",
         "location": {"relation": "x) FROM y --"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "freshness_sla": {"column": "ts", "max_age": "10m"}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "unknown_type"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "timestamp", "time_zone": "PST"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "integer", "time_zone": "UTC"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "string", "unit": "GiB"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "number", "size_of": "p"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "number", "unit": "ms", "size_of": "p"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "string", "status_column": "st"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"pii": "mask"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "integer", "unit": "minor"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "integer", "currency": "USD"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "integer", "unit": "minor",
                            "currency": "USD", "currency_column": "c"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"relative_to": "root"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"id": {"type": "string", "status": "maybe"}}},
        {"version": 2, "dataset": "x", "kind": "files",
         "location": {"path": "a.csv", "format": "csv", "delimiter": ":"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "files",
         "location": {"path": "a.jsonl", "format": "jsonl",
                      "delimiter": ";"},
         "grain": "g", "keys": ["id"]},
    ]
    for bad in schema_rejected_contracts:
        assert not c_validator.is_valid(bad), f"expected schema to reject: {bad}"
        with pytest.raises(ValueError):
            C2.validate_contract_v2(bad, "test.yaml")

    # Verify the exact set of cross-field/cross-column contract rules that
    # JSON Schema cannot express (passes JSON Schema, caught by contract_v2.py):
    for name, (doc, match) in NON_JSON_SCHEMA_CONTRACT_CASES.items():
        assert c_validator.is_valid(doc), name
        with pytest.raises(ValueError, match=match):
            C2.validate_contract_v2(doc, "test.yaml")

    valid_metrics = [
        {
            "version": 1,
            "metric": "collection_rate",
            "kind": "ratio",
            "dataset": "fetch_log",
            "numerator": {"agg": "count", "column": "*",
                          "where": "status = 'fetched'"},
            "denominator": {"agg": "count", "column": "*",
                            "where": "status <> 'removed_on_purpose'"},
            "unit": "ratio",
        },
        {
            "metric": "total_bytes",
            "kind": "simple",
            "dataset": "fetch_log",
            "measure": {"agg": "sum", "column": "size"},
            "unit": "GiB",
        },
        {
            "metric": "total-file size$",
            "kind": "simple",
            "dataset": "my-project.ds.Order Items",
            "measure": {"agg": "sum", "column": "file size"},
            "grain": ["order-id", "número"],
            "unit": "bytes",
        },
    ]
    for mdoc in valid_metrics:
        m_validator.validate(mdoc)
        C2.validate_metric(mdoc, "metric.yaml")

    schema_rejected_metrics = [
        {"metric": "m", "kind": "derived", "dataset": "d"},
        {"metric": "m", "kind": "ratio", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "median", "column": "x"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "sum", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "sum", "column": "x) FROM read_text('/etc/passwd') --"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "sum", "column": "price*qty"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "sum", "column": "`file size`"}},
        {"metric": "m", "kind": "simple", "dataset": "d; DROP TABLE x",
         "measure": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "`my-project.ds.orders`",
         "measure": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "ratio", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"},
         "denominator": {"agg": "count", "column": "*"},
         "measure": None},
    ]
    for bad_m in schema_rejected_metrics:
        assert not m_validator.is_valid(bad_m), bad_m
        with pytest.raises(ValueError):
            C2.validate_metric(bad_m, "metric.yaml")

    for name, (mdoc, match) in NON_JSON_SCHEMA_METRIC_CASES.items():
        assert m_validator.is_valid(mdoc), name
        with pytest.raises(ValueError, match=match):
            C2.validate_metric(mdoc, "metric.yaml")


def test_cli_edge_cases_project_dir_and_explicit_metric_order(tmp_path):
    (tmp_path / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\n"
        "version: 1\nstack: dbt\ndialect: null\nmodes: []\n"
        "bases:\n  data_root: ./data\n```\n",
        encoding="utf-8",
    )
    # Non-contract YAML at root must not be swept by `contract validate .`
    (tmp_path / "dbt_project.yml").write_text(
        "name: demo\nversion: 1.0.0\n", encoding="utf-8")
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    mdir = tmp_path / "metrics"
    mdir.mkdir()
    (cdir / "z_orders.yaml").write_text(
        "version: 2\ndataset: orders\ngrain: one row per id\nkeys: [id]\n"
        "columns:\n  id: {type: string}\n  amt: {type: integer}\n",
        encoding="utf-8",
    )
    (mdir / "a_rev.yaml").write_text(
        "version: 1\nmetric: rev\nkind: simple\ndataset: orders\n"
        "measure: {agg: sum, column: amt}\n",
        encoding="utf-8",
    )

    # 1. `truequery contract validate .` ignores dbt_project.yml
    code, out, err = run_cli("contract", "validate", ".", cwd=tmp_path)
    assert code == 0, err
    assert out.strip() == "clean: 1 contract and 1 metric validated, no errors"

    # 2. Validating only a metric file prints `1 metric validated` (not `0 contracts`)
    code, out, _ = run_cli(
        "contract", "validate", "metrics/a_rev.yaml", cwd=tmp_path)
    assert code == 0
    assert out.strip() == "clean: 1 metric validated, no errors"

    # 3. Passing metric before contract in standalone folder still validates contract first
    standalone = tmp_path / "standalone"
    standalone.mkdir()
    (standalone / "a_metric.yaml").write_text(
        "version: 1\nmetric: rev\nkind: simple\ndataset: custom_ds\n"
        "measure: {agg: sum, column: amt}\n",
        encoding="utf-8",
    )
    (standalone / "z_contract.yaml").write_text(
        "version: 2\ndataset: custom_ds\ngrain: one row per id\nkeys: [id]\n"
        "columns:\n  id: {type: string}\n  amt: {type: integer}\n",
        encoding="utf-8",
    )
    code, out, _ = run_cli(
        "contract", "validate",
        "standalone/a_metric.yaml", "standalone/z_contract.yaml",
        cwd=tmp_path,
    )
    assert code == 0
    assert out.strip() == "clean: 1 contract and 1 metric validated, no errors"
