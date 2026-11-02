"""0.1.3: a statement sqlglot reads only as an opaque Command.

sqlglot doesn't raise on syntax it doesn't support: it logs a warning
and returns exp.Command. One that holds a query or a data change is a
parse failure (not checked, never clean); a session or admin statement,
which no check is about, stays as it was. sqlglot's warning never
reaches the user's terminal.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile

import pytest
import sqlglot

from truequery import engine as E
from truequery.analyzers import sql as analyzer

CLI = [sys.executable, "-m", "truequery.cli"]
_HOME = tempfile.mkdtemp(prefix="tq-test-home-")
ENV = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME,
           PYTHONPATH=os.path.join(
               os.path.dirname(__file__), "..", "plugin", "engine", "src"))

REPRO = "CREATE STATISTICS IF NOT EXISTS s ON a, b FROM t;\n"
# sqlglot 30 reads each of these only as a Command.
DATA_COMMAND = "EXPLAIN SELECT a FROM t WHERE a NOT IN (SELECT b FROM u);\n"


def cli(*argv, cwd):
    proc = subprocess.run(CLI + list(argv), cwd=cwd, capture_output=True,
                          text=True, env=ENV, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def write(tmp_path, name, text):
    (tmp_path / name).write_text(text)
    return tmp_path


def command(sql, dialect="postgres"):
    logger = logging.getLogger("sqlglot")
    before = logger.level
    logger.setLevel(logging.ERROR)
    try:
        stmt = sqlglot.parse_one(sql, read=dialect)
    finally:
        logger.setLevel(before)
    assert isinstance(stmt, sqlglot.exp.Command), (sql, type(stmt))
    return stmt


# -- the policy -----------------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "EXPLAIN SELECT 1",
    "EXPLAIN ANALYZE UPDATE t SET a = 1",
    "PREPARE p AS SELECT a FROM t",
    "DECLARE c CURSOR FOR SELECT a FROM t",
    "DO $$ BEGIN DELETE FROM t; END $$",
    "CREATE MATERIALIZED VIEW mv AS SELECT i FROM generate_series(1, 5) i WITH NO DATA",
    "CREATE TABLE t2 OF some_type",
    "CREATE OR REPLACE RULE r AS ON INSERT TO v DO INSTEAD INSERT INTO t SELECT new.*",
    "IF x THEN DELETE FROM t",  # unknown: treated as holding a query
])
def test_command_that_holds_a_query(sql):
    assert analyzer.command_holds_query(command(sql))


@pytest.mark.parametrize("sql", [
    "CREATE STATISTICS IF NOT EXISTS s ON a, b FROM t",
    "CREATE EXTENSION IF NOT EXISTS pg_trgm",
    "CREATE INDEX i ON t (a) WITH (fillfactor = 70) TABLESPACE x",
    "RESET search_path",
    "SHOW search_path",
    "VACUUM ANALYZE t",
    "REINDEX INDEX CONCURRENTLY i",
    "REFRESH MATERIALIZED VIEW mv",
    "CREATE ROLE r NOSUPERUSER",
    "ALTER TABLE t ALTER COLUMN a SET STATISTICS 100",
    "ALTER TYPE e ADD VALUE IF NOT EXISTS 'b'",
    "DROP DOMAIN d CASCADE",
    "CALL p()",
    "FETCH ALL FROM c",
    "LOCK TABLE t",
])
def test_command_no_check_applies_to(sql):
    assert not analyzer.command_holds_query(command(sql))


# -- .sql files -----------------------------------------------------------------
def test_repro_admin_command_stays_clean_and_quiet(tmp_path):
    """The reported repro: no check is about CREATE STATISTICS, so it is
    still checked and clean, and sqlglot's warning is gone."""
    root = write(tmp_path, "q.sql", REPRO)
    code, out, err = cli("check", ".", cwd=root)
    assert (code, err) == (0, "")
    assert out == "clean: 1 file checked offline, no findings\n"
    code, out, err = cli("detect", ".", cwd=root)
    assert (code, err) == (0, "")


def test_data_command_is_not_checked_exit_3(tmp_path):
    root = write(tmp_path, "q.sql", DATA_COMMAND)
    code, out, err = cli("detect", ".", cwd=root)
    assert code == 3
    assert "clean" not in out
    assert "1 not checked (could not parse)" in out
    assert "unsupported syntax" not in err  # TrueQuery's own line only
    assert err == "q.sql: line 1: could not parse (not checked)\n"
    code, _, _ = cli("detect", "--allow-unverified", ".", cwd=root)
    assert code == 0


def test_data_command_is_a_diagnostic_with_detail():
    res = analyzer.check_text(DATA_COMMAND, dialect="postgres")
    assert not res["verified"] and res["findings"] == []
    [diag] = res["diagnostics"]
    assert diag["message"] == "line 1: could not parse (not checked)"
    assert "EXPLAIN statement could be read only as an opaque command" \
        in diag["detail"]


def test_data_command_in_review_receipt(tmp_path):
    root = write(tmp_path, "q.sql", DATA_COMMAND)
    code, out, err = cli("review", ".", cwd=root)
    assert code == 3
    assert "Not checked  1 file that didn't parse: q.sql" in out
    assert "unsupported syntax" not in out + err


def test_session_and_grant_file_stays_clean(tmp_path):
    root = write(tmp_path, "q.sql",
                 "SET search_path = app;\nRESET work_mem;\n"
                 "GRANT SELECT ON t TO reader;\nVACUUM t;\n")
    code, out, err = cli("detect", ".", cwd=root)
    assert (code, err) == (0, "")
    assert out == "clean: 1 file checked offline, no findings\n"


def test_other_statements_of_the_file_still_report(tmp_path):
    """Not checked in full, but what did parse is still real."""
    root = write(tmp_path, "q.sql", DATA_COMMAND + "DELETE FROM t;\n")
    code, out, _ = cli("detect", ".", cwd=root)
    assert code == 1  # a finding outranks "not checked"
    assert "q.sql:2:1 [error] sql/unguarded-update-delete" in out
    assert "1 not checked" in out


def test_only_the_command_is_unchecked_in_a_shared_chunk():
    """A quote inside a quoted identifier keeps several statements in one
    chunk (pg-regress horology.sql). Only the Command is not checked."""
    sql = ('SELECT 1 AS "it\'s";\nEXPLAIN SELECT 2;\nDELETE FROM t;\n'
           'SELECT 2 AS "x\'";\n')
    assert len(analyzer.split_statements(sql)) == 1
    res = analyzer.check_text(sql, dialect="postgres")
    assert [d["message"] for d in res["diagnostics"]] == [
        "line 2: could not parse (not checked)"]
    assert [(f["line"], f["rule"]) for f in res["findings"]] == [
        (3, "sql/unguarded-update-delete")]


# -- Python ---------------------------------------------------------------------
# A query sqlglot reads only as a Command (`WITH NO DATA`), with a
# finding inside it that no check could see.
PY_DATA = ('import psycopg2\n'
           'cur.execute("CREATE MATERIALIZED VIEW mv AS '
           'SELECT a FROM t WHERE a = NULL WITH NO DATA")\n')


def test_python_data_command_is_unparsed():
    findings, frags, _ = E.check_python_source(PY_DATA, "m.py")
    assert [f.status for f in frags] == ["unparsed"]
    assert frags[0].reason == "doesn't parse as SQL"
    assert findings == []


def test_python_admin_command_is_checked():
    src = ('import psycopg2\n'
           'cur.execute("CREATE STATISTICS IF NOT EXISTS s ON a, b FROM t")\n')
    findings, frags, _ = E.check_python_source(src, "m.py")
    assert [f.status for f in frags] == ["static"]
    assert findings == []


def test_python_data_command_is_listed_not_exit_3(tmp_path):
    root = write(tmp_path, "m.py", PY_DATA)
    code, out, err = cli("detect", ".", cwd=root)
    assert (code, err) == (0, "")
    assert "clean" not in out
    code, out, err = cli("review", ".", cwd=root)
    assert (code, err) == (0, "")
    assert "SQL that doesn't parse in 1 Python file: m.py" in out


# -- sqlglot's log --------------------------------------------------------------
def hook(root, name):
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write",
                          "tool_input": {"file_path": name}})
    proc = subprocess.run(CLI + ["hook", "--harness", "claude"], cwd=root,
                          input=payload, capture_output=True, text=True,
                          env=ENV, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def test_hook_writes_nothing_from_sqlglot(tmp_path):
    (tmp_path / "q.sql").write_text(REPRO)
    code, out, err = hook(tmp_path, "q.sql")
    assert (code, err) == (0, "")
    assert "unsupported syntax" not in out


def test_hook_says_a_data_command_is_unverified(tmp_path):
    (tmp_path / "q.sql").write_text(DATA_COMMAND)
    code, out, err = hook(tmp_path, "q.sql")
    assert (code, err) == (0, "")
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[TrueQuery] UNVERIFIED" in body
    assert "unsupported syntax" not in body


def test_host_logging_is_left_as_it_was():
    """Quiet only while checking: a host application's sqlglot logger
    keeps its level, and a host that already silenced it stays silent."""
    logger = logging.getLogger("sqlglot")
    before = logger.level
    try:
        for level in (logging.NOTSET, logging.DEBUG, logging.CRITICAL):
            logger.setLevel(level)
            analyzer.check_text(REPRO, dialect="postgres")
            E.check_python_source(PY_DATA, "m.py")
            assert logger.level == level
    finally:
        logger.setLevel(before)


def test_no_warning_while_checking(caplog):
    with caplog.at_level(logging.WARNING, logger="sqlglot"):
        analyzer.check_text(REPRO, dialect="postgres")
        E.check_python_source(PY_DATA, "m.py")
    assert not [r for r in caplog.records if r.name.startswith("sqlglot")]
