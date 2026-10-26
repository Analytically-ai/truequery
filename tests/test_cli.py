"""M3 CLI goldens: exact stdout/JSON/SARIF per verb on fixtures.

Fixtures are copied to tmp_path so init/DATA.md never touch the repo.
Subprocess runs double as cold-start evidence (see test_latency.py).
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile


FIX = os.path.join(os.path.dirname(__file__), "fixtures")
CLI = [sys.executable, "-m", "truequery.cli"]
# A throwaway home: the developer's own ~/.claude and ~/.config/truequery
# must never change what a test sees.
_HOME = tempfile.mkdtemp(prefix="tq-test-home-")
ENV = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME,
           PYTHONPATH=os.path.join(
               os.path.dirname(__file__), "..", "plugin", "engine", "src"))

DASH = " — "


def run(*argv, cwd):
    proc = subprocess.run(CLI + list(argv), cwd=cwd, capture_output=True,
                          text=True, env=ENV, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def fresh(name, tmp_path):
    dest = tmp_path / name
    shutil.copytree(os.path.join(FIX, name), dest)
    return tmp_path


def test_detect_text_golden(tmp_path):
    root = fresh("plain", tmp_path)
    code, out, err = run("detect", "plain", cwd=root)
    assert code == 1
    assert out == (
        "plain/bad.sql:1:1 [error] sql/unguarded-update-delete"
        f"{DASH}no WHERE: every row is affected\n"
        "    fix: add a WHERE predicate, or use TRUNCATE if you mean the whole table\n"
        "1 error, 0 warnings in 2 files checked offline\n")
    assert err == ""


def test_detect_clean_file(tmp_path):
    root = fresh("plain", tmp_path)
    code, out, _ = run("detect", "plain/ok.sql", cwd=root)
    assert code == 0
    assert out == "clean: 1 file checked offline, no findings\n"


def test_detect_json_shape(tmp_path):
    root = fresh("plain", tmp_path)
    code, out, _ = run("detect", "plain", "--json", cwd=root)
    assert code == 1
    doc = json.loads(out)
    assert doc["schema_version"] == 1
    assert doc["standard"] == "0.1" and doc["offline"] is True
    assert doc["modes"] == ["build"]
    assert doc["files"] == ["plain/bad.sql", "plain/ok.sql"]
    assert doc["diagnostics"] == []
    assert len(doc["findings"]) == 1
    finding = doc["findings"][0]
    assert finding == {
        "rule": "sql/unguarded-update-delete",
        "severity": "error",
        "mode": "build",
        "evidence_level": "file",
        "location": {"file": "plain/bad.sql", "line": 1, "col": 1},
        "snippet": "DELETE FROM orders;",
        "why": "no WHERE: every row is affected",
        "fix": "add a WHERE predicate, or use TRUNCATE if you mean the whole table",
        "standard_ref": "sql/unguarded-update-delete (standard-0.1)",
        "tool_version": "0.1.0",
    }


def test_detect_sarif_shape(tmp_path):
    root = fresh("plain", tmp_path)
    code, out, _ = run("detect", "plain", "--sarif", cwd=root)
    assert code == 1
    doc = json.loads(out)
    assert doc["version"] == "2.1.0"
    assert doc["$schema"].endswith("sarif-2.1.0.json")
    driver = doc["runs"][0]["tool"]["driver"]
    assert driver["name"] == "tq" and driver["version"] == "0.1.0"
    ids = [r["id"] for r in driver["rules"]]
    assert ids == sorted(ids) and "parse-error" in ids
    assert len(doc["runs"][0]["results"]) == 1
    result = doc["runs"][0]["results"][0]
    assert result == {
        "ruleId": "sql/unguarded-update-delete",
        "level": "error",
        "message": {"text": "no WHERE: every row is affected Fix: add a "
                            "WHERE predicate, or use TRUNCATE if you mean "
                            "the whole table"},
        "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "plain/bad.sql"},
            "region": {"startLine": 1, "startColumn": 1}}}],
        "properties": {"mode": "build", "evidence_level": "file"},
    }


def test_detect_skips_build_dirs(tmp_path):
    root = fresh("plain", tmp_path)
    for skip in ("target", "dbt_packages", "node_modules", ".venv",
                 ".git"):
        d = root / skip
        d.mkdir()
        (d / "bad.sql").write_text("DELETE FROM t;\n", encoding="utf-8")
    code, out, _ = run("detect", ".", cwd=root)
    assert code == 1
    assert "target/bad.sql" not in out and "dbt_packages" not in out
    assert "in 2 files" in out


def test_detect_exit_codes_and_fail_on(tmp_path):
    root = tmp_path
    (root / "warn.sql").write_text(
        "SELECT c.id FROM c LEFT JOIN o ON o.cid = c.id "
        "WHERE o.status = 'paid';\n", encoding="utf-8")
    code, _, _ = run("detect", "warn.sql", cwd=root)
    assert code == 0  # warning passes the default error bar
    code, _, _ = run("detect", "warn.sql", "--fail-on", "warning",
                     cwd=root)
    assert code == 1
    code, _, _ = run("detect", "nope.sql", cwd=root)
    assert code == 2  # usage error only


def test_parse_failure_is_diagnostic_not_exit_2(tmp_path):
    root = tmp_path
    (root / "broken.sql").write_text("select * from (((;\n",
                                     encoding="utf-8")
    code, out, err = run("detect", "broken.sql", cwd=root)
    assert code == 3  # not everything was checked: CI must not pass
    assert run("detect", "broken.sql", "--allow-unverified", cwd=root)[0] == 0
    assert out == ("no findings in 0 files checked · "
                   "1 not checked (could not parse)\n")
    assert "broken.sql" in err and "could not parse" in err
    assert "\x1b[" not in err  # no ANSI in agent-facing diagnostics
    code, _, err = run("detect", "broken.sql", "--verbose", cwd=root)
    assert code == 3 and "detail:" in err


def test_detect_reports_file_over_size_limit(tmp_path):
    root = tmp_path
    with open(root / "big.sql", "w", encoding="utf-8") as handle:
        chunk = "SELECT 1;\n" * 100000  # 1 MB per write, never parsed
        for _ in range(11):
            handle.write(chunk)
    code, out, err = run("detect", "big.sql", cwd=root)
    assert code == 3  # not everything was checked: CI must not pass
    assert out == ("no findings in 0 files checked · "
                   "1 not checked (over the size limit)\n")
    assert "big.sql" in err and "size limit" in err
    code, out, _ = run("review", "big.sql", cwd=root)
    assert "Not checked  1 file over the 10 MB limit: big.sql" in out


def test_detect_is_deterministic(tmp_path):
    root = fresh("plain", tmp_path)
    _, first, _ = run("detect", "plain", cwd=root)
    _, second, _ = run("detect", "plain", cwd=root)
    assert first == second
    _, reversed_out, _ = run("detect", "plain/ok.sql", "plain/bad.sql",
                             cwd=root)
    _, ordered_out, _ = run("detect", "plain/bad.sql", "plain/ok.sql",
                            cwd=root)
    assert reversed_out == ordered_out


def test_modes_from_data_md(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(os.path.join(FIX, "minidbt"), root)
    code, _, _ = run("init", cwd=root)
    assert code == 0
    code, out, _ = run("detect", ".", cwd=root)
    assert code == 1
    assert "analyses/explore.sql:1:24 [warning]" in out  # explore downgrade
    assert "models/bad.sql:1:24 [error]" in out


def test_review_receipt_golden(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(os.path.join(FIX, "minidbt"), root)
    code, out, _ = run("review", ".", "--mode", "build", cwd=root)
    assert code == 1
    assert out == (
        "analyses/explore.sql:1:24 [error] "
        "sql/not-in-nullable-subquery"
        f"{DASH}a NULL in the subquery makes NOT IN return zero rows\n"
        "    fix: rewrite as NOT EXISTS, or add WHERE col IS NOT NULL\n"
        "models/bad.sql:1:24 [error] sql/not-in-nullable-subquery"
        f"{DASH}a NULL in the subquery makes NOT IN return zero rows\n"
        "    fix: rewrite as NOT EXISTS, or add WHERE col IS NOT NULL\n"
        "\n"
        "TrueQuery receipt\n"
        "Checked      3 SQL files · 2 project files · "
        "contracts for 0 of 3 tables\n"
        "Found        2 errors\n"
        "Not checked  your data: TrueQuery reads files and never connects "
        "to your database\n"
        "Not checked  dbt, incremental-model and Airflow rules beyond the 4 "
        "project checks\n"
        "No contract  a, b, t\n")


def test_receipt_names_only_the_limits_that_apply(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text(
        "-- a / in a comment is not division\nSELECT 'a/b' AS s FROM t;\n",
        encoding="utf-8")
    _, out, _ = run("review", ".", cwd=root)
    assert "Found        no problems" in out
    assert "integer division" not in out
    assert "Airflow" not in out
    assert "your data: TrueQuery reads files" in out  # always said
    (root / "q.sql").write_text("SELECT a / b AS r FROM t;\n",
                                encoding="utf-8")
    _, out, _ = run("review", ".", cwd=root)
    assert ("Not checked  integer division between columns: column types "
            "aren't known from files alone") in out


def test_review_json_carries_receipt(tmp_path):
    root = fresh("plain", tmp_path)
    code, out, _ = run("review", "plain", "--json", cwd=root)
    assert code == 1
    doc = json.loads(out)
    receipt = doc["receipt"]
    assert receipt["files_checked"] == 2
    assert receipt["evidence"]["metadata"] == "no adapter"
    # no file divides and there are no project files: no limit applies
    assert receipt["not_checked"]["recall_limits"] == []
    # judgment scope respects the mode: build sees none of the
    # explore-only sql practices, explore sees both
    assert receipt["judgment_in_scope"] == []
    assert receipt["tier_overrides"] == {}
    code, out, _ = run("review", "plain", "--json", "--mode", "explore",
                       cwd=root)
    assert code == 0  # error downgrades to warning in explore mode
    scoped = json.loads(out)["receipt"]["judgment_in_scope"]
    assert [e["rule"] for e in scoped] == [
        "sql/explore-limit-is-not-a-sample",
        "sql/explore-profile-filter-columns"]


def test_tier_overrides_are_testing_only(tmp_path):
    """D12: the legacy testing knob is honored as project settings, and
    the receipt reports the real settings line instead."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text("DELETE FROM t;\n", encoding="utf-8")
    (root / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\n"
        "dialect: null\nmodes: []\nstack: plain\n"
        "tier_overrides: {sql/unguarded-update-delete: stop}\n"
        "version: 1\n```\n",
        encoding="utf-8")
    code, out, _ = run("review", "q.sql", cwd=root)
    assert code == 1
    assert "tier overrides active (testing" not in out
    assert "rule settings: 1 adjusted by project config" in out


def test_init_writes_and_keeps(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(os.path.join(FIX, "minidbt"), root)
    code, out, _ = run("init", cwd=root)
    assert code == 0
    assert "stack=dbt" in out
    data_md = root / "DATA.md"
    assert "```tq-config" in data_md.read_text(encoding="utf-8")
    seeded = root / "contracts" / "bad.yaml"
    assert seeded.exists()
    text = seeded.read_text(encoding="utf-8")
    assert "grain: one row per id" in text  # from unique + not_null
    assert "keys: [id]" in text
    assert "paid" in text  # from accepted_values
    combo = (root / "contracts" / "combo.yaml").read_text(encoding="utf-8")
    assert "grain: one row per (a, b)" in combo
    legacy = (root / "contracts" / "legacy.yaml").read_text(encoding="utf-8")
    assert "grain: one row per id" in legacy  # dbt >= 1.8 data_tests:
    from truequery import project as P
    assert P.load_contracts(str(root))["bad"]["grain"] == "one row per id"
    code, out2, _ = run("init", cwd=root)
    assert code == 0 and "kept existing" in out2
    (root / "contracts" / "bad.yaml").write_text("user: edits\n",
                                                 encoding="utf-8")
    code, _, _ = run("init", cwd=root)  # no force: contracts kept
    assert code == 0
    assert (root / "contracts" / "bad.yaml").read_text(
        encoding="utf-8") == "user: edits\n"
    data_md.write_text("user edits\n", encoding="utf-8")
    code, _, _ = run("init", "--force", "--json", cwd=root)
    assert code == 0
    assert data_md.read_text(encoding="utf-8") != "user edits\n"


def test_context_directives(tmp_path):
    root = tmp_path / "proj"
    shutil.copytree(os.path.join(FIX, "minidbt"), root)
    code, out, _ = run("context", "--target", "models/bad.sql",
                       cwd=root)
    assert code == 0
    assert "MODE: build" in out and "STANDARD: 0.1" in out
    assert "EVIDENCE_AVAILABLE: file" in out
    assert "CONTEXT_STALE" in out  # no DATA.md yet
    assert "CONTRACT_MISSING: a" in out
    code, _, _ = run("init", cwd=root)
    assert code == 0
    code, out, _ = run("context", "--target", "models/bad.sql",
                       "--json", cwd=root)
    assert code == 0
    doc = json.loads(out)
    assert doc["stale"] is False
    assert doc["tables_referenced"] == ["a", "b"]
    assert "CONTRACT_MISSING: a" in doc["directives"]


def test_detect_outside_root_prints_absolute(tmp_path):
    target = tmp_path / "elsewhere" / "q.sql"
    target.parent.mkdir()
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    code, out, _ = run("detect", str(target), cwd=work)
    assert code == 1
    assert out.startswith(str(target) + ":1:1")


def test_package_data_ships_the_standard():
    # `tq rules <slug>` reads standard/*.yaml + registry_impl.json at
    # runtime; a wheel without them breaks installed runs. hatchling
    # ships the whole package directory, so guard that config (CI also
    # builds the wheel and checks its contents).
    import tomllib
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "plugin", "engine", "pyproject.toml"), "rb") as handle:
        wheel = tomllib.load(handle)["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["src/truequery"]
    assert all("yaml" not in p and "json" not in p
               for p in wheel.get("exclude", []))


def test_hook_posttooluse_end_to_end(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text("select * from (((;\n", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write",
                          "tool_input": {"file_path": "q.sql"}})
    proc = subprocess.run(CLI + ["hook", "--harness", "claude"], cwd=root,
                          input=payload, capture_output=True, text=True,
                          env=ENV, timeout=120)
    assert proc.returncode == 0
    body = json.loads(proc.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "PostToolUse"
    assert "[TrueQuery] UNVERIFIED" in body["additionalContext"]
    assert (root / ".truequery" / "session.json").exists()


def test_hook_stop_end_to_end(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text("SELECT * FROM a, b;\n", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write",
                          "tool_input": {"file_path": "q.sql"}})
    subprocess.run(CLI + ["hook", "--harness", "claude"], cwd=root,
                   input=payload, capture_output=True, text=True,
                   env=ENV, timeout=120)
    stop = json.dumps({"hook_event_name": "Stop"})
    proc = subprocess.run(CLI + ["hook", "--harness", "claude"], cwd=root,
                          input=stop, capture_output=True, text=True,
                          env=ENV, timeout=120)
    assert proc.returncode == 0
    body = json.loads(proc.stdout)["hookSpecificOutput"]
    assert body["hookEventName"] == "Stop"
    assert "stop pass ·" in body["additionalContext"]
    # Fix 1: fresh findings only, never the receipt
    assert "TrueQuery receipt" not in body["additionalContext"]
    assert "accidental-cross-join" in body["additionalContext"]


def test_install_end_to_end(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    code, out, _ = run("install", "--harness", "claude", cwd=root)
    assert code == 0
    settings = json.loads(
        (root / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert any("hook --harness" in json.dumps(h)
               for h in settings["hooks"]["PostToolUse"])
    assert any("hook --harness" in json.dumps(h)
               for h in settings["hooks"]["Stop"])
    code, out2, _ = run("install", "--harness", "claude", cwd=root)
    assert code == 0  # idempotent


def test_ignore_add_narrowest_only(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text("DELETE FROM t;\n", encoding="utf-8")
    code, _, err = run("ignore", "add", "sql/unguarded-update-delete",
                       "q.sql", "--reason", "x", cwd=root)
    assert code == 2 and "narrowest scope only" in err
    code, _, err = run("ignore", "add", "sql/unguarded-update-delete",
                       "q.sql:1", cwd=root)
    assert code == 2 and "--reason is required" in err
    code, _, err = run("ignore", "add", "sql/nope", "q.sql:1",
                       "--reason", "x", cwd=root)
    assert code == 2 and "unknown rule" in err
    code, out, _ = run("ignore", "add", "sql/unguarded-update-delete",
                       "q.sql:1", "--reason", "agent: one-off cleanup",
                       cwd=root)
    assert code == 0 and "ignore added" in out
    code, out, _ = run("detect", "q.sql", cwd=root)
    assert code == 0 and out == "no findings in 1 file checked offline (1 hidden by ignores)\n"
    code, out, _ = run("review", "q.sql", "--json", cwd=root)
    assert code == 0
    assert json.loads(out)["receipt"]["ignores"] == {"narrow": 1,
                                                     "broad": 0}


def test_context_resolves_refs_and_skips_ctes(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.sql").write_text(
        "with orders as (select * from {{ ref('stg_orders') }}),\n"
        "order_items as (select * from {{ ref('order_items') }})\n"
        "select * from orders join order_items on "
        "orders.id = order_items.id;\n",
        encoding="utf-8")
    code, out, _ = run("context", "--target", "m.sql", cwd=root)
    assert code == 0
    assert "CONTRACT_MISSING: stg_orders" in out
    assert "CONTRACT_MISSING: order_items" in out
    assert "CONTRACT_MISSING: orders\n" not in out
    (root / "s.sql").write_text(
        "select * from {{ source('shop', 'orders') }} o;\n",
        encoding="utf-8")
    code, out, _ = run("context", "--target", "s.sql", "--json",
                       cwd=root)
    assert code == 0
    assert json.loads(out)["tables_referenced"] == ["shop.orders"]


_DBT_ORDERS = os.path.join(os.path.dirname(__file__), "fixtures", "dbt-orders")


def test_init_seeds_contracts_from_data_tests(tmp_path):
    """dbt >= 1.8 `data_tests:` (unique + not_null) seed the contract key."""
    import shutil
    root = tmp_path / "dbt-orders"
    shutil.copytree(_DBT_ORDERS, root)
    code, out, _ = run("init", cwd=root)
    assert code == 0
    orders = (root / "contracts" / "orders.yaml").read_text(
        encoding="utf-8")
    assert "keys: [order_id]" in orders  # unique + not_null data_tests


def test_context_matches_qualified_and_source_names(tmp_path):
    """A `fx_rates` contract covers `raw.fx_rates` and
    `{{ source('raw','fx_rates') }}` — no CONTRACT_MISSING for either."""
    root = tmp_path / "proj"
    root.mkdir()
    cdir = root / "contracts"
    cdir.mkdir()
    (cdir / "fx_rates.yaml").write_text(
        "table: fx_rates\ngrain: one row per currency and day\n"
        "keys:\n  - [currency, rate_date]\n",
        encoding="utf-8")
    (root / "m.sql").write_text(
        "select * from raw.fx_rates;\n", encoding="utf-8")
    code, out, _ = run("context", "--target", "m.sql", cwd=root)
    assert code == 0
    assert "CONTRACT_MISSING" not in out
    assert "contract raw.fx_rates" in out
    (root / "s.sql").write_text(
        "select * from {{ source('raw', 'fx_rates') }};\n",
        encoding="utf-8")
    code, out, _ = run("context", "--target", "s.sql", "--json",
                       cwd=root)
    assert code == 0
    doc = json.loads(out)
    assert doc["directives"] == [d for d in doc["directives"]
                                 if not d.startswith("CONTRACT_MISSING")]
    assert "raw.fx_rates" in doc["contracts"]


def test_shadowing_cte_still_lists_real_upstreams():
    from truequery import project as P
    text = open(os.path.join(_DBT_ORDERS, "models", "marts", "orders.sql"),
                encoding="utf-8").read()
    assert sorted(P.referenced_tables(text)) == ["order_items",
                                                 "stg_orders"]


def test_ignore_scoped_to_its_statement(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "q.sql"
    target.write_text("delete from sessions;\n"
                      "select * from t where x = null;\n",
                      encoding="utf-8")
    code, _, _ = run("ignore", "add", "sql/unguarded-update-delete",
                     "q.sql:1", "--reason", "agent: one-off cleanup",
                     cwd=root)
    assert code == 0
    # editing statement 2 keeps statement 1 suppressed, and statement
    # 2's own (changed) finding still reports
    target.write_text("delete from sessions;\n"
                      "select * from t where y = null;\n",
                      encoding="utf-8")
    code, out, _ = run("detect", "q.sql", cwd=root)
    assert code == 1
    assert "null-equality-comparison" in out
    assert "unguarded-update-delete" not in out
    # a new bug in statement 1 is NOT silenced by the old ignore
    target.write_text(
        "delete from sessions where id not in (select uid from banned);\n"
        "select * from t where y = null;\n",
        encoding="utf-8")
    code, out, _ = run("detect", "q.sql", cwd=root)
    assert code == 1
    assert "not-in-nullable-subquery" in out


def test_rules_list_and_detail(tmp_path):
    code, out, _ = run("rules", cwd=tmp_path)
    assert code == 0
    assert "sql/not-in-nullable-subquery [on stop]" in out
    assert "source: default" in out
    assert "unimplemented records hidden" in out
    code, full, _ = run("rules", "--all", cwd=tmp_path)
    assert code == 0 and len(full) > len(out)
    code, out, _ = run("rules", "sql/not-in-nullable-subquery",
                       cwd=tmp_path)
    assert code == 0
    assert "statement:" in out and "proof bad:" in out
    code, _, err = run("rules", "sql/does-not-exist", cwd=tmp_path)
    assert code == 2 and "unknown rule" in err


def test_prog_follows_entry_point(monkeypatch):
    from truequery import cli as CLI
    import sys
    monkeypatch.setattr(sys, "argv", ["truequery", "--help"])
    assert CLI.build_parser().prog == "truequery"
    monkeypatch.setattr(sys, "argv", ["/x/bin/tq", "--help"])
    assert CLI.build_parser().prog == "tq"



def test_ignore_covers_one_line_not_the_whole_statement(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "q.sql").write_text(
        "select *\nfrom t\nwhere a = null\n  and b = 1\n  or c = null;\n",
        encoding="utf-8")
    code, _, _ = run("ignore", "add", "sql/null-equality-comparison",
                     "./q.sql:3", "--reason", "agent: legacy", cwd=root)
    assert code == 0
    code, out, _ = run("detect", "q.sql", cwd=root)
    assert code == 1 and "q.sql:5:" in out and "q.sql:3:" not in out


def test_init_force_never_overwrites_contracts(tmp_path):
    root = fresh("minidbt", tmp_path)
    assert run("init", cwd=root)[0] == 0
    contract = next((root / "contracts").glob("*.yaml"))
    contract.write_text("table: x\ngrain: confirmed by finance\nkeys: [[a, b]]\n",
                        encoding="utf-8")
    assert run("init", "--force", cwd=root)[0] == 0
    assert "confirmed by finance" in contract.read_text(encoding="utf-8")


def test_named_python_file_is_checked_once(tmp_path):
    (tmp_path / "dag.py").write_text(
        "from airflow import DAG\nd = DAG('x')\n", encoding="utf-8")
    code, out, _ = run("detect", "dag.py", "--enable-rule",
                       "orch/explicit-catchup-policy", "--fail-on", "warning",
                       cwd=tmp_path)
    assert out.count("[warning] orch/explicit-catchup-policy") == 1
