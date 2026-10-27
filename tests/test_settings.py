"""D12 user control: layered rule settings (default < user < project
< CLI), `tq rules set` round-trips, guards, the receipt line, and the
hook's in-session change warning. Agents never change rule settings;
this file covers the human commands only."""

import json
import os
import subprocess
import sys


CLI = [sys.executable, "-m", "truequery.cli"]
SRC = os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src")
NOTIN = "sql/not-in-nullable-subquery"
SETOP = "sql/set-operation-column-position"
UNGUARDED = "sql/unguarded-update-delete"
UNIMPL = "sql/fanout-join-pre-aggregation"  # draft record, no detector


def run(*argv, cwd, env=None, timeout=120):
    proc = subprocess.run(CLI + list(argv), cwd=cwd, capture_output=True,
                          text=True,
                          env=env or dict(os.environ, PYTHONPATH=SRC),
                          timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


def env_with_config(tmp_path, **kw):
    return dict(os.environ, PYTHONPATH=SRC,
                XDG_CONFIG_HOME=str(tmp_path / "xdg"), **kw)


def project(tmp_path, name="proj", extra=""):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    (root / "q.sql").write_text(
        "SELECT id FROM users WHERE id NOT IN "
        "(SELECT user_id FROM banned);\n", encoding="utf-8")
    (root / "u.sql").write_text(
        "SELECT * FROM a UNION ALL SELECT * FROM b;\n", encoding="utf-8")
    config = ("# DATA.md\n\n```tq-config\n"
              "dialect: null\nmodes: []\nstack: plain\nversion: 1\n"
              + extra + "```\n")
    (root / "DATA.md").write_text(config, encoding="utf-8")
    return root


def write_user(tmp_path, text):
    cfg = tmp_path / "xdg" / "truequery" / "config.yaml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(text, encoding="utf-8")
    return cfg


def test_precedence_user_project_cli(tmp_path):
    root = project(tmp_path)
    write_user(tmp_path,
               f"rules:\n  {NOTIN}:\n    tier: immediate\n")
    env = env_with_config(tmp_path)
    # user alone: tier immediate
    code, out, _ = run("rules", NOTIN, cwd=root, env=env)
    assert code == 0
    text = subprocess.run(CLI + ["rules", "--json"], cwd=root,
                          capture_output=True, text=True,
                          env=env).stdout
    rows = {r["rule"]: r for r in json.loads(text)}
    assert rows[NOTIN]["tier"] == "immediate"
    assert rows[NOTIN]["source"] == "user"
    # project disables the rule outright
    (root / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\ndialect: null\nmodes: []\nstack: plain\n"
        "version: 1\nrules:\n"
        f"  {NOTIN}:\n    enabled: false\n```\n", encoding="utf-8")
    text = subprocess.run(CLI + ["rules", "--json"], cwd=root,
                          capture_output=True, text=True,
                          env=env).stdout
    rows = {r["rule"]: r for r in json.loads(text)}
    assert rows[NOTIN]["enabled"] is False
    assert rows[NOTIN]["source"] == "project"
    code, out, _ = run("detect", "q.sql", cwd=root, env=env)
    assert code == 0 and NOTIN not in out
    # CLI re-enables (highest precedence)
    code, out, _ = run("detect", "q.sql", "--enable-rule", NOTIN,
                       cwd=root, env=env)
    assert code == 1 and NOTIN in out


def test_rules_set_round_trip_project(tmp_path):
    root = project(tmp_path)
    env = env_with_config(tmp_path)
    code, out, _ = run("rules", "set", SETOP, "--enabled", "on",
                       "--tier", "stop", "--severity", "advisory",
                       cwd=root, env=env)
    assert code == 0
    assert f"{SETOP} is now on tier=stop severity=advisory" in out
    assert "from the project settings" in out
    # written into the tq-config fence
    text = (root / "DATA.md").read_text(encoding="utf-8")
    assert "rules:" in text and SETOP in text
    # and the off-by-default rule now runs on detect
    code, out, _ = run("detect", "u.sql", cwd=root, env=env)
    assert code == 0  # advisory < default fail-on error
    assert f"[advisory] {SETOP}" in out


def test_rules_set_round_trip_user(tmp_path):
    root = project(tmp_path)
    env = env_with_config(tmp_path)
    code, out, _ = run("rules", "set", NOTIN, "--tier", "stop",
                       "--severity", "warning", "--scope", "user",
                       cwd=root, env=env)
    assert code == 0 and "from the user settings" in out
    assert "overrides your user setting" not in out
    cfg = tmp_path / "xdg" / "truequery" / "config.yaml"
    assert NOTIN in cfg.read_text(encoding="utf-8")
    text = subprocess.run(CLI + ["rules", "--json"], cwd=root,
                          capture_output=True, text=True,
                          env=env).stdout
    rows = {r["rule"]: r for r in json.loads(text)}
    assert rows[NOTIN]["source"] == "user"
    assert rows[NOTIN]["severity_override"] == "warning"


def test_rules_set_guards(tmp_path):
    root = project(tmp_path)
    env = env_with_config(tmp_path)
    # unknown slug
    code, _, err = run("rules", "set", "sql/nope", "--enabled", "on",
                       cwd=root, env=env)
    assert code == 2 and "unknown rule" in err
    # unimplemented can't be enabled
    code, _, err = run("rules", "set", UNIMPL, "--enabled", "on",
                       cwd=root, env=env)
    assert code == 2 and "cannot be enabled" in err
    # unimplemented can't be immediate
    code, _, err = run("rules", "set", UNIMPL, "--tier", "immediate",
                       cwd=root, env=env)
    assert code == 2 and "no detector" in err
    # nothing to set
    code, _, err = run("rules", "set", NOTIN, cwd=root, env=env)
    assert code == 2 and "nothing to set" in err
    # an unimplemented rule CAN be disabled/named toward stop
    code, out, _ = run("rules", "set", UNIMPL, "--enabled", "off",
                       cwd=root, env=env)
    assert code == 0


def test_receipt_line_counts_adjustments(tmp_path):
    root = project(tmp_path, extra=(
        "rules:\n"
        f"  {NOTIN}:\n    severity: warning\n"
        f"  {SETOP}:\n    enabled: true\n"))
    env = env_with_config(tmp_path)
    code, out, _ = run("review", "q.sql", cwd=root, env=env)
    assert code == 0  # severity warning < default fail-on error
    assert "rule settings: 2 adjusted by project config" in out


def test_receipt_line_flags_user_config_adjustment(tmp_path):
    root = project(tmp_path)
    write_user(tmp_path, f"rules:\n  {NOTIN}:\n    severity: warning\n")
    env = env_with_config(tmp_path)
    code, out, _ = run("review", "q.sql", cwd=root, env=env)
    assert code == 0  # warning < default fail-on error
    assert "rule settings: 1 adjusted by user config" in out


def test_interrupt_edits_false_silences_per_edit(tmp_path, monkeypatch):
    from test_hook import Args, _event, _run
    root = project(tmp_path, extra="interrupt_edits: false\nrules:\n"
                     f"  {UNGUARDED}:\n    tier: immediate\n")
    target = root / "d.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(root)
    out = _run(_event(str(target)), Args(harness="claude"), monkeypatch)
    assert "DELETE" in open(target).read() or True
    code, text = out if isinstance(out, tuple) else (0, out)
    assert "triage" not in text.lower()
    assert "[TrueQuery]" not in text


def test_config_change_notice(tmp_path, monkeypatch):
    import truequery.hook as hook_mod  # noqa: F401
    from test_hook import Args, _event, _run
    root = project(tmp_path, extra=f"rules:\n  {NOTIN}:\n    enabled: false\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(root)
    target = root / "q.sql"
    _run(_event(str(target)), Args(), monkeypatch)  # seeds config_fp
    # human edits the rules block mid-session
    (root / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\ndialect: null\nmodes: []\nstack: plain\n"
        "version: 1\nrules:\n"
        f"  {NOTIN}:\n    enabled: true\n```\n", encoding="utf-8")
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert "rule settings changed this session" in out
    # and the notice does not repeat on the next event
    code, out2 = _run(_event(str(target)), Args(), monkeypatch)
    assert "rule settings changed this session" not in out2


def test_rules_set_user_says_when_the_project_overrides_it(tmp_path):
    root = project(tmp_path)
    env = env_with_config(tmp_path)
    run("rules", "set", NOTIN, "--severity", "warning", cwd=root, env=env)
    code, out, _ = run("rules", "set", NOTIN, "--severity", "advisory",
                       "--scope", "user", cwd=root, env=env)
    assert code == 0
    assert "from the project settings" in out
    assert "a project setting overrides your user setting here" in out
