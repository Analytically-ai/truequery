"""A broken config is reported, never silently replaced by defaults.

A typo in DATA.md that quietly re-enabled a disabled rule, or quietly
turned checking off in the hook, would be worse than a crash.
"""
import io
import json
import os
import subprocess
import sys

import truequery.hook as hook

SRC = os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src")
BROKEN_DATA_MD = "# DATA.md\n\n```tq-config\nstack: [plain\n```\n"


def _tq(*argv, cwd, env=None):
    proc = subprocess.run(
        [sys.executable, "-m", "truequery.cli", *argv], cwd=cwd,
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": SRC, **(env or {})})
    return proc.returncode, proc.stdout, proc.stderr


def test_broken_data_md_is_a_usage_error(tmp_path):
    (tmp_path / "DATA.md").write_text(BROKEN_DATA_MD, encoding="utf-8")
    (tmp_path / "a.sql").write_text("select 1;\n", encoding="utf-8")
    code, _, err = _tq("detect", "a.sql", cwd=tmp_path)
    assert code == 2
    assert "tq-config block is not valid YAML" in err
    assert "Traceback" not in err


def test_broken_user_config_is_a_usage_error(tmp_path):
    cfg = tmp_path / "xdg" / "truequery"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("rules: [\n", encoding="utf-8")
    (tmp_path / "a.sql").write_text("select 1;\n", encoding="utf-8")
    code, _, err = _tq("detect", "a.sql", cwd=tmp_path,
                       env={"XDG_CONFIG_HOME": str(tmp_path / "xdg")})
    assert code == 2 and "config.yaml: not valid YAML" in err


def test_invalid_contract_is_named_in_the_receipt(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "orders.yaml").write_text(
        "table: [orders\n", encoding="utf-8")
    (tmp_path / "a.sql").write_text("select id from orders;\n",
                                    encoding="utf-8")
    code, out, err = _tq("review", cwd=tmp_path)
    assert code == 0, err
    assert "invalid contract: contracts/orders.yaml: not valid YAML" in out


class Args:
    harness, event, log_file, scope = "claude", "auto", None, "project"


def test_hook_reports_broken_config_once_and_still_checks(tmp_path,
                                                           monkeypatch):
    (tmp_path / "DATA.md").write_text(BROKEN_DATA_MD, encoding="utf-8")
    bad = tmp_path / "bad.sql"
    bad.write_text("select * from t where x = null;\n", encoding="utf-8")
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write", "cwd": str(tmp_path),
                          "tool_input": {"file_path": str(bad)}})

    def fire():
        monkeypatch.setattr(hook, "read_stdin", lambda timeout=5.0: payload)
        out = io.StringIO()
        monkeypatch.setattr(hook.sys, "stdout", out)
        assert hook.main(Args()) == 0
        return out.getvalue()

    first = fire()
    assert "tq-config block is not valid YAML" in first
    assert "sql/null-equality-comparison" in first  # defaults still ran
    assert "not valid YAML" not in fire()  # said once, not on every edit


def test_unknown_dialect_flag_is_a_usage_error(tmp_path):
    (tmp_path / "a.sql").write_text("select 1;\n", encoding="utf-8")
    code, _, err = _tq("detect", "a.sql", "--dialect", "nosuchsql", cwd=tmp_path)
    assert code == 2 and "unknown dialect" in err


def test_dialect_names_are_normalized(tmp_path):
    (tmp_path / "a.sql").write_text("select top 1 id from t;\n", encoding="utf-8")
    code, out, _ = _tq("detect", "a.sql", "--dialect", "SQLServer", cwd=tmp_path)
    assert code == 0 and out.startswith("clean:")


def test_unknown_dialect_in_data_md_is_reported(tmp_path):
    (tmp_path / "DATA.md").write_text(
        "```tq-config\ndialect: nosuchsql\n```\n", encoding="utf-8")
    (tmp_path / "a.sql").write_text("select 1;\n", encoding="utf-8")
    code, _, err = _tq("detect", "a.sql", cwd=tmp_path)
    assert code == 2 and "unknown dialect" in err


def test_init_uses_the_profile_dbt_project_names(tmp_path):
    (tmp_path / "dbt_project.yml").write_text(
        "name: shop\nprofile: shop_sf\n", encoding="utf-8")
    (tmp_path / "profiles.yml").write_text(
        "other:\n  target: dev\n  outputs: {dev: {type: postgres}}\n"
        "shop_sf:\n  target: prod\n  outputs:\n    dev: {type: duckdb}\n"
        "    prod: {type: snowflake}\n", encoding="utf-8")
    code, out, _ = _tq("init", "--json", cwd=tmp_path)
    assert code == 0 and json.loads(out)["dialect"] == "snowflake"
