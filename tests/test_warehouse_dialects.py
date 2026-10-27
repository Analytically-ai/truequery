"""Synthetic warehouse workloads exercise explicit CLI dialect selection.

Each clean/bug pair changes a predicate while retaining dialect-specific
syntax, so neither a parse failure nor a silently skipped file can pass.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


FIXTURES = Path(__file__).parent / "fixtures" / "dialects"
ENGINE = Path(__file__).resolve().parents[1] / "plugin" / "engine" / "src"
CASES = [
    ("snowflake", "qualify", "sql/null-equality-comparison"),
    ("snowflake", "flatten", "sql/null-equality-comparison"),
    ("snowflake", "iff", "sql/null-equality-comparison"),
    ("redshift", "qualify", "sql/null-equality-comparison"),
    ("redshift", "distkey_sortkey", "sql/not-in-nullable-subquery"),
    ("redshift", "delete_using", "sql/unguarded-update-delete"),
]


@pytest.mark.parametrize("dialect,case,rule", CASES)
@pytest.mark.parametrize("variant", ["clean", "bug"])
def test_warehouse_dialect_cli(dialect, case, rule, variant, tmp_path, isolated_home):
    name = f"{case}.{variant}.sql"
    shutil.copyfile(FIXTURES / dialect / name, tmp_path / name)
    proc = subprocess.run(
        [sys.executable, "-m", "truequery.cli", "detect", name,
         "--dialect", dialect, "--json", "--fail-on", "warning"],
        cwd=tmp_path, capture_output=True, text=True, timeout=120,
        env=dict(os.environ, PYTHONPATH=str(ENGINE)),
    )
    assert proc.returncode == (0 if variant == "clean" else 1), proc.stdout + proc.stderr
    assert proc.stderr == ""
    doc = json.loads(proc.stdout)
    assert doc["offline"] is True
    assert doc["files"] == [name]
    assert doc["diagnostics"] == []
    assert [finding["rule"] for finding in doc["findings"]] == (
        [] if variant == "clean" else [rule]
    )
    for finding in doc["findings"]:
        assert finding["location"]["file"] == name
        assert 1 <= finding["location"]["line"] <= len((tmp_path / name).read_text().splitlines())
