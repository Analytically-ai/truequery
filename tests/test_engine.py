"""Engine facade: one code path for the CLI and (in M4) the hook."""

import os

from truequery import engine as E
from truequery import project as P

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "plain")


def test_run_returns_sorted_rel_paths_and_modes(tmp_path):
    import shutil
    shutil.copy(os.path.join(FIX, "bad.sql"), tmp_path / "b.sql")
    shutil.copy(os.path.join(FIX, "ok.sql"), tmp_path / "a.sql")
    results = E.run([str(tmp_path)], str(tmp_path))
    assert [r.rel for r in results] == ["a.sql", "b.sql"]
    assert all(r.mode == "build" for r in results)
    by_rel = {r.rel: r for r in results}
    assert [f.rule for f in by_rel["b.sql"].findings] == [
        "sql/unguarded-update-delete"]
    assert by_rel["b.sql"].verified
    assert by_rel["a.sql"].findings == []


def test_run_applies_mode_override_and_data_md(tmp_path):
    import shutil
    dest = tmp_path / "proj"
    shutil.copytree(os.path.join(os.path.dirname(__file__), "fixtures",
                                 "minidbt"), dest)
    before = E.run([str(dest)], str(dest))
    assert {f.severity for r in before for f in r.findings} == {"error"}
    (dest / "DATA.md").write_text(
        P.render_data_md(P.default_config("dbt", None)),
        encoding="utf-8")
    after = E.run([str(dest)], str(dest))
    severities = {(r.rel, f.severity) for r in after for f in r.findings}
    assert ("analyses/explore.sql", "warning") in severities
    assert ("models/bad.sql", "error") in severities
    forced = E.run([str(dest)], str(dest), mode="explore")
    assert {f.severity for r in forced for f in r.findings} == {"warning"}


def _hash_of(statement: str) -> str:
    import hashlib
    import re
    norm = re.sub(r"\s+", " ", statement).strip().rstrip(";")
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]


def test_stmt_hash_maps_to_containing_statement(tmp_path):
    # Property: every finding's hash equals the hash of the statement
    # containing its line — across gaps, comments and reorderings.
    bodies = [
        "select id from users where id not in (select uid from banned);",
        "select * from t where x = null;",
        "delete from sessions;",
    ]
    text = "\n\n-- a comment gap\n\n".join(bodies) + "\n"
    target = tmp_path / "q.sql"
    target.write_text(text, encoding="utf-8")
    results = E.run([str(target)], str(tmp_path))
    assert len(results) == 1
    # statement i lives on line 4*i - 3 (single line + 3 gap lines)
    for finding in results[0].findings:
        body = bodies[(finding.line + 3) // 4 - 1] \
            if finding.line in (1, 5, 9) else None
        assert body is not None, finding
        assert finding._stmt == _hash_of(body), (finding, body)
    hashes = {f._stmt for f in results[0].findings}
    assert hashes == {_hash_of(b) for b in bodies}


def test_stmt_hash_stable_under_other_statement_edits(tmp_path):
    # Editing statement 2 (a comment) must not move statement 1's hash.
    stmt1 = "delete from sessions;"
    stmt2 = "select * from t where x = null;"
    target = tmp_path / "q.sql"
    target.write_text(stmt1 + "\n" + stmt2 + "\n", encoding="utf-8")
    before = {f.rule: f._stmt
              for f in E.run([str(target)], str(tmp_path))[0].findings}
    target.write_text(stmt1 + "\n-- touched\n" + stmt2 + "\n",
                      encoding="utf-8")
    after = {f.rule: f._stmt
             for f in E.run([str(target)], str(tmp_path))[0].findings}
    assert before["sql/unguarded-update-delete"] == \
        after["sql/unguarded-update-delete"]
    # comments are stripped before hashing: statement 2 keeps its hash
    # too, even though its line shifted
    assert before["sql/null-equality-comparison"] == \
        after["sql/null-equality-comparison"]


def test_check_file_shape_and_missing_path(tmp_path):
    target = tmp_path / "q.sql"
    target.write_text("select * from (((;\n", encoding="utf-8")
    findings, diagnostics, _lookups = E.check_file(str(target))
    assert findings == []
    assert len(diagnostics) == 1
    assert "could not parse" in diagnostics[0].message


def test_run_skips_files_over_size_limit(tmp_path, monkeypatch):
    """Files over MAX_FILE_BYTES are not checked (verified False) with
    the reason. run() is the shared path for detect and the hook."""
    monkeypatch.setattr(E, "MAX_FILE_BYTES", 64)
    big = tmp_path / "big.sql"
    big.write_text("DELETE FROM t;\n" * 16, encoding="utf-8")  # 240 bytes
    small = tmp_path / "small.sql"
    small.write_text("DELETE FROM t;\n", encoding="utf-8")
    by_rel = {r.rel: r for r in E.run([str(tmp_path)], str(tmp_path))}
    assert by_rel["big.sql"].verified is False
    assert by_rel["big.sql"].findings == []  # never parsed, never checked
    assert [d.message for d in by_rel["big.sql"].diagnostics] == [E.TOO_LARGE]
    assert "size limit" in by_rel["big.sql"].diagnostics[0].message
    assert by_rel["small.sql"].verified
    assert [f.rule for f in by_rel["small.sql"].findings] == [
        "sql/unguarded-update-delete"]


def test_receipt_never_reads_a_file_the_checker_skipped(tmp_path):
    # The size limit is a safety boundary: the receipt's limit lines must
    # not tokenize a file the checker refused (it timed out on Windows).
    from truequery import report as RP
    from truequery.engine import FileResult, TOO_LARGE
    from truequery.models import Diagnostic
    missing = str(tmp_path / "never-opened.sql")  # opening it would raise
    skipped = FileResult(path=missing, rel="big.sql", mode="build",
                         diagnostics=[Diagnostic("big.sql", TOO_LARGE)])
    assert RP.recall_limits([skipped]) == []


def test_macro_definition_files_are_recognised():
    from truequery.engine import is_macro_file
    only_macros = ("{# money #}\n{% macro cents(c) %}\n  ({{ c }} / 100.0)\n"
                   "{% endmacro %}\n-- note\n{%- test positive(model) -%}\n"
                   "select * from {{ model }} where x < 0\n{%- endtest -%}\n")
    assert is_macro_file(only_macros)
    # a real query beside a macro is still a query: checked as usual
    assert not is_macro_file("{% macro m() %}1{% endmacro %}\n"
                             "select a from t where b = null\n")
    assert not is_macro_file("select * from {{ ref('orders') }}\n")
