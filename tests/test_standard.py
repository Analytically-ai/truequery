"""M1 gates: the standard loads clean, ids match paths, the generated
registry is fresh and deterministic, and every implemented detector has
a check function wired in checks/sql_checks.py.
"""
import os

import pytest

from truequery import schema

SRC = os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src", "truequery")


def test_all_records_load():
    records = schema.load_all()
    assert len(records) == 220


def test_ids_match_paths():
    records = schema.load_all()
    for slug, record in records.items():
        domain, name = slug.split("/", 1)
        path = os.path.join(SRC, "standard", domain, name + ".yaml")
        assert os.path.exists(path), slug
        assert record.domain == domain


def test_strict_keys_reject_unknown():
    import copy
    records = schema.load_all()
    sample = records["sql/not-in-nullable-subquery"]
    bad = copy.deepcopy(sample.raw)
    bad[" BogusKey"] = 1
    import tempfile
    import yaml
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as fh:
        yaml.safe_dump(bad, fh)
        tmp = fh.name
    with pytest.raises(schema.SchemaError):
        schema.load_record(tmp, {})
    os.unlink(tmp)


def test_registry_generation_is_deterministic_and_fresh():
    import subprocess
    import sys
    proc = subprocess.run(
        [sys.executable, "tools/generate_registry.py", "--check"],
        capture_output=True, text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."))
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_implemented_detectors_have_checks():
    from truequery import std_registry
    from truequery.analyzers import project_checks
    from truequery.checks import sql as sql_checks
    for slug in std_registry.IMPLEMENTED:
        record = std_registry.RECORDS[slug]
        assert record["detector"] == slug, f"{slug} detector id must be its slug"
        assert (slug in sql_checks.CHECKS
                or slug in project_checks.PREVIEW_CHECKS), (
            f"{slug} missing in checks/sql.py or project_checks")


def test_heuristic_five_off_by_default():
    from truequery import std_registry
    for slug in ("sql/average-of-ratios", "sql/distinct-dedup-bandaid",
                 "sql/limit-requires-order-by", "sql/keyset-pagination",
                 "sql/union-all-default", "sql/negative-filter-drops-nulls"):
        assert std_registry.RECORDS[slug]["implemented"] is True
        assert std_registry.RECORDS[slug]["enabled"] is False


def test_backtick_receipt_guidance():
    # Backtick identifiers without a dialect stay UNVERIFIED; the
    # diagnostic tells the user to set dialect in config (one clean
    # line; the path is attached by the caller, covered in
    # test_cli.test_parse_failure_is_diagnostic_not_exit_2).
    from truequery.analyzers import sql as analyzer
    res = analyzer.check_text("select `a` from t", dialect=None, path="q.sql")
    assert res["verified"] is False
    assert any("dialect" in d["message"] for d in res["diagnostics"])


def test_readme_rule_table_matches_registry():
    import os
    from truequery import std_registry
    root = os.path.join(os.path.dirname(__file__), "..")
    with open(os.path.join(root, "README.md"), encoding="utf-8") as handle:
        readme = handle.read()
    for slug, rec in std_registry.RECORDS.items():
        if not rec["implemented"]:
            continue
        status = "on" if rec["enabled"] else "off"
        assert f"`{slug}` | {status} ({rec['tier']})" in readme, slug
