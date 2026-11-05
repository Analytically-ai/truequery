"""Tests for .pre-commit-hooks.yaml, root pyproject.toml, and action.yml."""

from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import tomllib

import yaml

from truequery import __version__

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
ENGINE_SRC = os.path.join(ROOT, "plugin", "engine", "src")
ENV = {**os.environ, "PYTHONPATH": ENGINE_SRC}


def test_pre_commit_hooks_manifest_is_valid():
    path = os.path.join(ROOT, ".pre-commit-hooks.yaml")
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as handle:
        hooks = yaml.safe_load(handle)
    assert isinstance(hooks, list) and len(hooks) >= 1
    by_id = {h["id"]: h for h in hooks}
    assert "truequery" in by_id
    primary = by_id["truequery"]
    assert primary["entry"] == "truequery detect"
    assert primary["language"] == "python"
    assert primary["types"] == ["sql"]
    assert primary.get("require_serial") is True

    assert "truequery-python" in by_id
    both = by_id["truequery-python"]
    assert both["entry"] == "truequery detect"
    assert both["language"] == "python"
    assert both["types_or"] == ["sql", "python"]


def test_doc_snippets_match_current_version():
    """Every `rev:` and `@v...` in README.md and docs/ci-and-pre-commit.md must match `v` + __version__."""
    expected = f"v{__version__}"
    for rel in ("README.md", os.path.join("docs", "ci-and-pre-commit.md")):
        path = os.path.join(ROOT, rel)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        revs = re.findall(r"\brev:\s*(v[0-9A-Za-z._-]+)", text)
        action_refs = re.findall(r"Analytically-ai/truequery@(v[0-9A-Za-z._-]+)", text)
        assert revs, f"no rev: found in {rel}"
        assert action_refs, f"no Analytically-ai/truequery@v... found in {rel}"
        for rev in revs:
            assert rev == expected, f"{rel} has rev: {rev}, want {expected}"
        for ref in action_refs:
            assert ref == expected, f"{rel} has @{ref}, want @{expected}"


# `rev: v1.2.3`, `rev: "1.2.3"`, and `@v1.2.3` with whatever comes before it.
PIN_REV = re.compile(r"""\brev:\s*["']?(v?\d[0-9A-Za-z._-]*)""")
PIN_AT = re.compile(r"([\w./-]*)@(v\d[0-9A-Za-z._-]*)")


def _version_pins(text):
    """(offset, pin) for each pin of ours. A pin on someone else's action
    (`actions/checkout@v4`) has its own version."""
    pins = [(m.start(1), m.group(1)) for m in PIN_REV.finditer(text)]
    for match in PIN_AT.finditer(text):
        owner = match.group(1)
        if not owner or owner.lower().split("/")[-1] == "truequery":
            pins.append((match.start(2), match.group(2)))
    return pins


def _newest_changelog_section():
    with open(os.path.join(ROOT, "CHANGELOG.md"), encoding="utf-8") as handle:
        text = handle.read()
    # Keeps the newlines before it, so line numbers still hold.
    match = re.search(r"^## .*?(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match, "CHANGELOG.md: no `## ` section found"
    return "\n" * text.count("\n", 0, match.start()) + match.group(0)


def test_version_pins_in_every_user_facing_doc_match_current_version():
    """Any `rev:` or `@v...` of ours, in the README, every doc and the
    newest CHANGELOG section, is `v` + __version__."""
    expected = f"v{__version__}"
    documents = {"CHANGELOG.md (newest section)": _newest_changelog_section()}
    paths = glob.glob(os.path.join(ROOT, "docs", "**", "*.md"), recursive=True)
    paths.append(os.path.join(ROOT, "README.md"))
    for path in paths:
        with open(path, encoding="utf-8") as handle:
            documents[os.path.relpath(path, ROOT)] = handle.read()
    assert len(documents) > 2
    stale = [
        f"{rel}:{text.count(chr(10), 0, offset) + 1}: pins {pin}, "
        f"expected {expected}"
        for rel, text in documents.items()
        for offset, pin in _version_pins(text)
        if pin != expected
    ]
    assert not stale, "\n".join(stale)


def test_version_pin_guard_tells_ours_from_someone_elses():
    pins = [pin for _, pin in _version_pins(
        "rev: v0.0.1\nrev: \"0.0.2\"\n"
        "uses: Analytically-ai/truequery@v0.0.3\n"
        "the @v0.0.4 tag\n"
        "uses: actions/checkout@v4\n"
        "uses: someone/truequery-fork@v9\n"
        "claude plugin install truequery@analytically\n")]
    assert sorted(pins) == ["0.0.2", "v0.0.1", "v0.0.3", "v0.0.4"]


def test_root_pyproject_matches_engine_metadata():
    root_toml = os.path.join(ROOT, "pyproject.toml")
    engine_toml = os.path.join(ROOT, "plugin", "engine", "pyproject.toml")
    assert os.path.isfile(root_toml)
    with open(root_toml, "rb") as handle:
        root_cfg = tomllib.load(handle)
    with open(engine_toml, "rb") as handle:
        engine_cfg = tomllib.load(handle)

    assert root_cfg["project"]["name"] == engine_cfg["project"]["name"] == "truequery"
    assert root_cfg["project"]["dependencies"] == engine_cfg["project"]["dependencies"]
    assert root_cfg["project"]["scripts"] == engine_cfg["project"]["scripts"]
    assert root_cfg["project"]["requires-python"] == engine_cfg["project"]["requires-python"]
    assert "test" in root_cfg["project"]["optional-dependencies"]
    assert root_cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "plugin/engine/src/truequery"
    ]


def test_pre_commit_entry_behaves_on_staged_filenames(tmp_path):
    """Simulate pre-commit passing staged filenames to `truequery detect`."""
    good = tmp_path / "good.sql"
    bad = tmp_path / "bad.sql"
    broken = tmp_path / "broken.sql"
    good.write_text("SELECT id, customer_id FROM orders WHERE id = 1;\n", encoding="utf-8")
    bad.write_text("DELETE FROM orders;\n", encoding="utf-8")
    broken.write_text("SELECT * FROM (((;\n", encoding="utf-8")

    proc_clean = subprocess.run(
        [sys.executable, "-m", "truequery.cli", "detect", str(good)],
        cwd=tmp_path,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc_clean.returncode == 0, proc_clean.stderr

    proc_bad = subprocess.run(
        [sys.executable, "-m", "truequery.cli", "detect", str(good), str(bad)],
        cwd=tmp_path,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc_bad.returncode == 1
    assert "sql/unguarded-update-delete" in proc_bad.stdout

    proc_unv = subprocess.run(
        [sys.executable, "-m", "truequery.cli", "detect", str(broken)],
        cwd=tmp_path,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc_unv.returncode == 3

    proc_allow = subprocess.run(
        [
            sys.executable,
            "-m",
            "truequery.cli",
            "detect",
            "--allow-unverified",
            str(broken),
        ],
        cwd=tmp_path,
        env=ENV,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc_allow.returncode == 0


def _extract_action_python() -> str:
    action_path = os.path.join(ROOT, "action.yml")
    with open(action_path, encoding="utf-8") as handle:
        action = yaml.safe_load(handle)
    run_step = [s for s in action["runs"]["steps"] if s.get("id") == "run"][0]
    script = run_step["run"]
    marker = '"${TQ_VENV_PYTHON}" - <<\'PY\'\n'
    py_start = script.index(marker) + len(marker)
    py_end = script.rindex("\nPY")
    return script[py_start:py_end]


def _make_fake_venv(tmp_path):
    """Create a fake venv layout with bin/truequery (POSIX) and Scripts/truequery.exe / .cmd."""
    venv_dir = tmp_path / "truequery-venv"
    bindir = venv_dir / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    tq_sh = bindir / "truequery"
    tq_sh.write_text(
        f'#!/bin/sh\nPYTHONPATH="{ENGINE_SRC}" exec "{sys.executable}" -m truequery.cli "$@"\n',
        encoding="utf-8",
    )
    tq_sh.chmod(0o755)

    scripts_dir = venv_dir / "Scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    tq_cmd = scripts_dir / "truequery.cmd"
    tq_cmd.write_text(
        f'@echo off\r\nset "PYTHONPATH={ENGINE_SRC}"\r\n"{sys.executable}" -m truequery.cli %*\r\n',
        encoding="utf-8",
    )
    return venv_dir


def test_action_setup_python_does_not_mutate_caller_environment():
    action_path = os.path.join(ROOT, "action.yml")
    with open(action_path, encoding="utf-8") as handle:
        action = yaml.safe_load(handle)
    setup_steps = [
        s for s in action["runs"]["steps"]
        if str(s.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert setup_steps, "no actions/setup-python step found in action.yml"
    for step in setup_steps:
        assert step.get("id"), "setup-python step must have an id"
        with_cfg = step.get("with") or {}
        assert with_cfg.get("update-environment") is False, (
            "setup-python must set update-environment: false so caller Python is not mutated"
        )


def test_github_action_manifest_and_runner(tmp_path):
    action_path = os.path.join(ROOT, "action.yml")
    assert os.path.isfile(action_path)
    with open(action_path, encoding="utf-8") as handle:
        action = yaml.safe_load(handle)

    assert action["name"] == "TrueQuery"
    assert action["runs"]["using"] == "composite"
    inputs = action["inputs"]
    for key in (
        "path",
        "command",
        "fail-on",
        "dialect",
        "mode",
        "allow-unverified",
        "sarif-file",
        "summary",
    ):
        assert key in inputs, key

    py_code = _extract_action_python()
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "bad.sql").write_text("SELECT * FROM a WHERE x = NULL;\n", encoding="utf-8")
    summary_file = tmp_path / "summary.md"
    output_file = tmp_path / "output.txt"
    sarif_file = tmp_path / "out" / "report.sarif"
    venv_dir = _make_fake_venv(tmp_path)

    # On Windows, subprocess without shell looks for .exe unless PATHEXT / .cmd is resolved;
    # set TQ_VENV_DIR on POSIX to test full-path resolution without PATH, and put Scripts on PATH on Windows.
    env = {
        **os.environ,
        "TQ_VENV_DIR": "" if sys.platform == "win32" else str(venv_dir),
        "PATH": (
            f"{venv_dir / 'Scripts'}{os.pathsep}{os.environ.get('PATH', '')}"
            if sys.platform == "win32"
            else "/usr/bin:/bin"
        ),
        "TQ_PATH": str(proj),
        "TQ_COMMAND": "review",
        "TQ_FAIL_ON": "error",
        "TQ_DIALECT": "",
        "TQ_MODE": "",
        "TQ_ALLOW_UNVERIFIED": "false",
        "TQ_SARIF_FILE": str(sarif_file),
        "TQ_SUMMARY": "true",
        "GITHUB_STEP_SUMMARY": str(summary_file),
        "GITHUB_OUTPUT": str(output_file),
    }
    proc = subprocess.run(
        [sys.executable, "-c", py_code],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 1
    assert "sql/null-equality-comparison" in proc.stdout
    assert "TrueQuery receipt" in proc.stdout

    summary_text = summary_file.read_text(encoding="utf-8")
    assert "### TrueQuery" in summary_text
    assert "````text" in summary_text
    assert "TrueQuery receipt" in summary_text

    output_text = output_file.read_text(encoding="utf-8")
    assert "exit_code=1" in output_text
    assert f"sarif_file={sarif_file}" in output_text

    sarif_doc = json.loads(sarif_file.read_text(encoding="utf-8"))
    assert sarif_doc["version"] == "2.1.0"
    results = sarif_doc["runs"][0]["results"]
    assert any(r["ruleId"] == "sql/null-equality-comparison" for r in results)


def test_github_action_error_edges(tmp_path):
    """Invalid command or config error writes exit_code=2, warns on stderr, and leaves sarif_file empty."""
    py_code = _extract_action_python()
    proj = tmp_path / "proj"
    proj.mkdir()
    venv_dir = _make_fake_venv(tmp_path)
    base_env = {
        **os.environ,
        "TQ_VENV_DIR": "" if sys.platform == "win32" else str(venv_dir),
        "PATH": (
            f"{venv_dir / 'Scripts'}{os.pathsep}{os.environ.get('PATH', '')}"
            if sys.platform == "win32"
            else "/usr/bin:/bin"
        ),
    }

    # 1. Unsupported command
    out1 = tmp_path / "out1.txt"
    sarif1 = tmp_path / "sarif1.sarif"
    env1 = {
        **base_env,
        "TQ_PATH": str(proj),
        "TQ_COMMAND": "invalid-cmd",
        "TQ_SARIF_FILE": str(sarif1),
        "GITHUB_OUTPUT": str(out1),
    }
    proc1 = subprocess.run(
        [sys.executable, "-c", py_code],
        cwd=proj,
        env=env1,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc1.returncode == 2
    assert "exit_code=2" in out1.read_text(encoding="utf-8")
    assert not sarif1.exists()

    # 2. Invalid dialect (CLI exits 2 with empty stdout -> warns to stderr that SARIF was empty)
    out2 = tmp_path / "out2.txt"
    sarif2 = tmp_path / "sarif2.sarif"
    env2 = {
        **base_env,
        "TQ_PATH": str(proj),
        "TQ_COMMAND": "review",
        "TQ_DIALECT": "not-a-real-dialect",
        "TQ_SARIF_FILE": str(sarif2),
        "GITHUB_OUTPUT": str(out2),
    }
    proc2 = subprocess.run(
        [sys.executable, "-c", py_code],
        cwd=proj,
        env=env2,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc2.returncode == 2
    assert "exit_code=2" in out2.read_text(encoding="utf-8")
    assert "sarif_file=\n" in out2.read_text(encoding="utf-8")
    assert "SARIF output was empty" in proc2.stderr
    assert not sarif2.exists()
