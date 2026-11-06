"""Contract Model v2, DATA.md bases, Metrics v1, JSON Schemas, and
`truequery contract validate` tests (PR A).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

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
        "  max_age: 24h\n"
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


def test_v2_draft_contract_requires_inferred_from_and_allows_todo_means():
    raw_draft = {
        "version": 2,
        "dataset": "events",
        "status": "draft",
        "inferred_from": "2,000-row sample of events.csv",
        "grain": "one row per event_id",
        "keys": ["event_id"],
        "columns": {
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

    # means: TODO on confirmed contract fails
    confirmed_with_todo = dict(raw_draft, status="confirmed")
    with pytest.raises(ValueError, match="only allowed when contract status is 'draft'"):
        C2.validate_contract_v2(confirmed_with_todo, "contracts/events.yaml")


@pytest.mark.parametrize("bad_patch,match", [
    ({"version": 3}, "version must be 2"),
    ({"dataset": "a", "table": "b"}, "dataset .* and table .* must match"),
    ({"dataset": ""}, "dataset .* is required"),
    ({"grain": ""}, "grain must be a non-empty string"),
    ({"keys": "id"}, "keys must be a list"),
    ({"keys": [[]]}, "composite key .* must be a non-empty list"),
    ({"kind": "stream"}, "kind must be one of"),
    ({"kind": "files"}, "location is required"),
    ({"kind": "files", "location": {"path": "../outside.csv", "format": "csv"}},
     "location.path must be a non-empty relative path without '\\.\\.'"),
    ({"kind": "files", "location": {"path": "/abs/file.csv", "format": "csv"}},
     "location.path must be a non-empty relative path"),
    ({"kind": "files", "location": {"path": "a.csv", "format": "xlsx"}},
     "location.format must be one of"),
    ({"write_mode": "truncate"}, "write_mode must be one of"),
    ({"freshness_sla": {"column": "ts", "max_age": "24hours"}},
     "freshness_sla.max_age must match"),
    ({"columns": {"ts": {"type": "timestamp", "time_zone": "EST"}}},
     "time_zone must be 'UTC', 'naive', or 'local:<Area>/<Location>'"),
    ({"columns": {"amt": {"type": "integer", "unit": "minor"}}},
     "requires currency or currency_column"),
    ({"columns": {"amt": {"type": "integer", "currency": "USD"}}},
     "requires unit 'minor' or 'major'"),
    ({"columns": {"amt": {"type": "integer", "unit": "minor", "currency": "us"}}},
     "3-letter uppercase ISO-4217"),
    ({"columns": {"amt": {"type": "integer", "unit": "minor",
                          "currency": "USD", "currency_column": "c"}}},
     "cannot set both currency and currency_column"),
    ({"columns": {"amt": {"type": "integer", "unit": "minor",
                          "currency_column": "missing_col"}}},
     "currency_column references unknown column 'missing_col'"),
    ({"columns": {"sz": {"type": "number", "unit": "GiB", "size_of": "p"},
                  "p": {"type": "string"}}},
     "size_of references 'p', which must be a column with type: path"),
    ({"columns": {"p": {"type": "path", "status_column": "st"},
                  "st": {"type": "string"}}},
     "status_column references 'st', which must be a column with values"),
    ({"columns": {"p": {"type": "string", "relative_to": "root"}}},
     "relative_to requires type: path"),
    ({"columns": {"email": {"type": "string", "pii": "hash"}}},
     "pii must be a boolean"),
    ({"freshness_sla": {"column": "missing_ts", "max_age": "12h"},
      "columns": {"id": {"type": "string"}}},
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
    ("status <> 'removed_on_purpose'", ["status"]),
    ("status IN ('fetched', 'released') AND size > 0", ["size", "status"]),
    ("delta >= -1 AND delta <= 10", ["delta"]),
    ("attempts IN (-1, 0, 1, 2)", ["attempts"]),
    ("created_at BETWEEN '2026-01-01' AND '2026-01-31'", ["created_at"]),
    ("deleted_at IS NULL AND NOT is_test", ["deleted_at", "is_test"]),
    ("path LIKE 'logs/%' OR path ILIKE 'archive/%'", ["path"]),
    ("(a = 1 OR b = 2) AND c IS NOT NULL", ["a", "b", "c"]),
])
def test_metric_where_allowlist_accepts_safe_filters(where_str, expected_cols):
    cols = C2.validate_metric_where(
        where_str, "metrics/m.yaml", "numerator",
        allowed_columns=set(expected_cols))
    assert cols == expected_cols


@pytest.mark.parametrize("bad_where,match", [
    ("status = 'fetched'; DROP TABLE fetch_log", "must not contain ';'"),
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
            "where": "status = 'paid' AND NOT is_internal",
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
      "measure": {"agg": "median", "column": "x"}},
     "agg must be one of"),
])
def test_metric_v1_rejects_invalid_definitions(bad_metric, match):
    with pytest.raises(ValueError, match=match):
        C2.validate_metric(bad_metric, "metrics/bad.yaml")


# -- 3. Cross-file validation & `truequery contract validate` CLI -------------

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
        "grain: one row per (run_id, url)\n"
        "keys: [[run_id, url]]\n"
        "columns:\n"
        "  size: {type: number, unit: GiB}\n"
        "  fetched_at: {type: timestamp, time_zone: UTC}\n"
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
        "where: \"status <> 'removed_on_purpose'\"}\n"
        "unit: ratio\n",
        encoding="utf-8",
    )
    (tmp_path / "q.sql").write_text(
        "SELECT status, COUNT(*) FROM fetch_log GROUP BY 1;\n",
        encoding="utf-8",
    )

    code, out, err = run_cli("context", "--target", "q.sql", cwd=tmp_path)
    assert code == 0, err
    assert "contract fetch_log: grain=one row per (run_id, url) keys=(run_id,url) kind=files status=draft" in out
    assert "columns fetch_log: size(number, unit=GiB); fetched_at(timestamp, tz=UTC); status(string, values=[fetched:present,removed_on_purpose:absent_expected])" in out
    assert "metric collection_rate: kind=ratio dataset=fetch_log unit=ratio" in out
    assert "CONTRACT_DRAFT: fetch_log (100-row sample of fetch.csv)" in out

    code, jout, _ = run_cli("context", "--target", "q.sql", "--json", cwd=tmp_path)
    assert code == 0
    doc = json.loads(jout)
    assert "fetch_log" in doc["contracts"]
    assert "collection_rate" in doc["metrics"]
    assert any(d.startswith("CONTRACT_DRAFT: fetch_log")
               for d in doc["directives"])


# -- 4. JSON Schema parity test -----------------------------------------------

def _mini_jsonschema_validate(instance, schema, root_schema=None):
    """Deterministic subset validator for Draft 2020-12 JSON Schemas used in
    contract-v2.schema.json and metric-v1.schema.json, so parity tests run
    offline without requiring third-party jsonschema packages."""
    import re as _re
    if root_schema is None:
        root_schema = schema
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/$defs/"), f"unsupported ref {ref}"
        def_name = ref.split("/")[-1]
        return _mini_jsonschema_validate(
            instance, root_schema["$defs"][def_name], root_schema)

    if "const" in schema:
        if instance != schema["const"]:
            return False

    if "enum" in schema:
        if instance not in schema["enum"]:
            return False

    if "type" in schema:
        t = schema["type"]
        types = t if isinstance(t, list) else [t]
        matched_type = False
        for expected in types:
            if expected == "null" and instance is None:
                matched_type = True
            elif expected == "boolean" and isinstance(instance, bool):
                matched_type = True
            elif (expected == "integer"
                  and isinstance(instance, int)
                  and not isinstance(instance, bool)):
                matched_type = True
            elif (expected == "number"
                  and isinstance(instance, (int, float))
                  and not isinstance(instance, bool)):
                matched_type = True
            elif expected == "string" and isinstance(instance, str):
                matched_type = True
            elif expected == "array" and isinstance(instance, list):
                matched_type = True
            elif expected == "object" and isinstance(instance, dict):
                matched_type = True
        if not matched_type:
            return False

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            return False
        if "pattern" in schema and not _re.search(schema["pattern"], instance):
            return False

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            return False
        if "items" in schema:
            for item in instance:
                if not _mini_jsonschema_validate(
                        item, schema["items"], root_schema):
                    return False

    if isinstance(instance, dict):
        if "minProperties" in schema and len(instance) < schema["minProperties"]:
            return False
        for req in schema.get("required", []):
            if req not in instance:
                return False
        props = schema.get("properties", {})
        addl = schema.get("additionalProperties", True)
        for k, v in instance.items():
            if k in props:
                if not _mini_jsonschema_validate(v, props[k], root_schema):
                    return False
            else:
                if addl is False:
                    return False
                if isinstance(addl, dict):
                    if not _mini_jsonschema_validate(v, addl, root_schema):
                        return False

    if "anyOf" in schema:
        if not any(_mini_jsonschema_validate(instance, sub, root_schema)
                   for sub in schema["anyOf"]):
            return False

    if "oneOf" in schema:
        if sum(1 for sub in schema["oneOf"]
               if _mini_jsonschema_validate(instance, sub, root_schema)) != 1:
            return False

    if "not" in schema:
        if _mini_jsonschema_validate(instance, schema["not"], root_schema):
            return False

    if "if" in schema:
        if _mini_jsonschema_validate(instance, schema["if"], root_schema):
            if not _mini_jsonschema_validate(
                    instance, schema["then"], root_schema):
                return False

    if "allOf" in schema:
        for sub in schema["allOf"]:
            if not _mini_jsonschema_validate(instance, sub, root_schema):
                return False

    return True


def test_published_json_schemas_exist_and_match_contract_v2_and_metric_v1():
    contract_schema_path = os.path.join(SCHEMAS_DIR, "contract-v2.schema.json")
    metric_schema_path = os.path.join(SCHEMAS_DIR, "metric-v1.schema.json")
    assert os.path.isfile(contract_schema_path)
    assert os.path.isfile(metric_schema_path)

    with open(contract_schema_path, encoding="utf-8") as handle:
        c_schema = json.load(handle)
    with open(metric_schema_path, encoding="utf-8") as handle:
        m_schema = json.load(handle)

    assert c_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert m_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"

    # Parity on unit vocabulary
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
            "freshness_sla": {"column": "created_at", "max_age": "24h"},
            "columns": {
                "created_at": {"type": "timestamp",
                               "time_zone": "local:America/New_York"},
                "amount_cents": {"type": "integer", "unit": "minor",
                                 "currency": "USD"},
                "email": {"type": "string", "pii": True},
            },
        },
    ]
    for doc in valid_contracts:
        assert _mini_jsonschema_validate(doc, c_schema), doc
        C2.validate_contract_v2(doc, "test.yaml")

    invalid_schema_contracts = [
        {"version": 1, "dataset": "x", "grain": "g", "keys": ["id"]},
        {"version": 2, "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "extra_unknown": 1},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"c": {"type": "unknown_type"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"c": {"time_zone": "PST"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"c": {"pii": "mask"}}},
        {"version": 2, "dataset": "x", "status": "draft", "grain": "g",
         "keys": ["id"]},
        {"version": 2, "dataset": "x", "status": "confirmed", "grain": "g",
         "keys": ["id"],
         "columns": {"s": {"values": {"ok": {"means": "TODO"}}}}},
        {"version": 2, "dataset": "x", "kind": "files", "grain": "g",
         "keys": ["id"]},
        {"version": 2, "dataset": "x", "kind": "table",
         "location": {"path": "a.csv", "format": "csv"},
         "grain": "g", "keys": ["id"]},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"amt": {"type": "integer", "unit": "minor"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"amt": {"type": "integer", "currency": "USD"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"amt": {"type": "integer", "unit": "minor",
                             "currency": "USD", "currency_column": "c"}}},
        {"version": 2, "dataset": "x", "grain": "g", "keys": ["id"],
         "columns": {"p": {"relative_to": "root"}}},
    ]
    for bad in invalid_schema_contracts:
        assert not _mini_jsonschema_validate(bad, c_schema), bad
        with pytest.raises(ValueError):
            C2.validate_contract_v2(bad, "test.yaml")

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
    ]
    for mdoc in valid_metrics:
        assert _mini_jsonschema_validate(mdoc, m_schema), mdoc
        C2.validate_metric(mdoc, "metric.yaml")

    invalid_schema_metrics = [
        {"metric": "m", "kind": "derived", "dataset": "d"},
        {"metric": "m", "kind": "ratio", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "median", "column": "x"}},
        {"metric": "m", "kind": "simple", "dataset": "d",
         "measure": {"agg": "sum", "column": "*"}},
        {"metric": "m", "kind": "ratio", "dataset": "d",
         "numerator": {"agg": "count", "column": "*"},
         "denominator": {"agg": "count", "column": "*"},
         "measure": None},
    ]
    for bad_m in invalid_schema_metrics:
        assert not _mini_jsonschema_validate(bad_m, m_schema), bad_m
        with pytest.raises(ValueError):
            C2.validate_metric(bad_m, "metric.yaml")


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

