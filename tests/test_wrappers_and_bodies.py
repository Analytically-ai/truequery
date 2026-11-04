"""0.1.4: the queries inside wrappers and function bodies are checked.

sqlglot reads EXPLAIN, PREPARE … AS, DECLARE … CURSOR FOR and some
CREATE … AS only as an opaque Command, and keeps a function's body as
text. The query inside is found, parsed and checked, with findings at
their true line and column. A procedural body (PL/pgSQL, a BEGIN … END
block) is not checked and says so; a body in another language holds no
SQL and is left as it was.
"""

import json
import os
import subprocess
import sys
import tempfile

import pytest

from truequery import engine as E
from truequery.analyzers import sql as analyzer

CLI = [sys.executable, "-m", "truequery.cli"]
_HOME = tempfile.mkdtemp(prefix="tq-test-home-")
ENV = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME,
           PYTHONPATH=os.path.join(
               os.path.dirname(__file__), "..", "plugin", "engine", "src"))

NULL_EQ = "sql/null-equality-comparison"
NOT_IN = "sql/not-in-nullable-subquery"
UNGUARDED = "sql/unguarded-update-delete"


def cli(*argv, cwd, stdin=None):
    proc = subprocess.run(CLI + list(argv), cwd=cwd, capture_output=True,
                          text=True, env=ENV, timeout=120, input=stdin)
    return proc.returncode, proc.stdout, proc.stderr


def check(sql, dialect="postgres"):
    return analyzer.check_text(sql, dialect=dialect)


def at(sql, needle, nth=0):
    """(line, col) of the nth `needle` in sql, 1-based."""
    i = -1
    for _ in range(nth + 1):
        i = sql.index(needle, i + 1)
    pre = sql[:i]
    return pre.count("\n") + 1, i - (pre.rfind("\n") + 1) + 1


def found(res, rule=None):
    return [(f["line"], f["col"]) for f in res["findings"]
            if rule is None or f["rule"] == rule]


# -- the splitter ---------------------------------------------------------------
def test_dollar_quoted_body_is_one_statement():
    sql = ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $body$\n"
           "  SELECT 1; SELECT 2;\n$body$;\nSELECT 3;\n")
    parts = analyzer.split_statements(sql)
    assert [start for _, start in parts] == [1, 3]
    assert parts[0][0].rstrip().endswith("$body$;")


def test_parameters_and_names_are_not_dollar_quotes():
    sql = "SELECT $1, a$b$ FROM t WHERE x = $2;\nSELECT 2;\n"
    assert len(analyzer.split_statements(sql)) == 2
    # An opening tag that never closes is plain text.
    assert len(analyzer.split_statements("SELECT '$$';\nSELECT 2;\n")) == 2
    assert len(analyzer.split_statements("SELECT 1 $$;\nSELECT 2;\n")) == 2


def test_comments_inside_a_body_are_kept_for_the_body():
    sql = "SELECT $$ -- not a comment; it's text $$;\n"
    assert analyzer.strip_comments(sql, "postgres") == sql


# -- wrappers -------------------------------------------------------------------
@pytest.mark.parametrize("wrapper", [
    "EXPLAIN ",
    "EXPLAIN ANALYZE VERBOSE ",
    "EXPLAIN (ANALYZE, COSTS OFF) ",
    "explain (verbose, costs off, format json)\n",
    "PREPARE p (int, text) AS ",
    "PREPARE p AS\n  ",
    "DECLARE c CURSOR FOR ",
    "DECLARE c NO SCROLL CURSOR WITH HOLD FOR\n",
    "CREATE MATERIALIZED VIEW mv AS ",
    "CREATE TEMP TABLE t2 WITH (fillfactor = 70) AS ",
])
def test_finding_inside_a_wrapper_at_its_line_and_column(wrapper):
    query = "SELECT a FROM t WHERE a = NULL"
    sql = wrapper + query + ";\n"
    res = check(sql)
    assert res["verified"], res["diagnostics"]
    assert found(res, NULL_EQ) == [at(sql, "a = NULL")]
    # The same column the bare query has, shifted by the wrapper.
    bare = check(query + ";\n")
    line, col = found(bare, NULL_EQ)[0]
    last = wrapper.split("\n")[-1]
    assert found(res, NULL_EQ) == [(line + wrapper.count("\n"),
                                    col + len(last))]


def test_finding_on_a_later_line_of_a_wrapped_query():
    sql = ("EXPLAIN (ANALYZE, COSTS OFF)\n"
           "SELECT a\n"
           "  FROM t\n"
           " WHERE a NOT IN (SELECT b FROM u)\n"
           "   AND c = NULL;\n")
    res = check(sql)
    assert res["verified"]
    # The rule points at the predicate's start, as in a bare query.
    assert found(res, NOT_IN) == [at(sql, "a NOT IN")]
    assert found(res, NULL_EQ) == [at(sql, "c = NULL")]
    assert found(res, NOT_IN)[0][0] == 4


def test_create_as_with_no_data():
    sql = ("CREATE MATERIALIZED VIEW mv AS\n"
           "SELECT a FROM t WHERE a = NULL\nWITH NO DATA;\n")
    res = check(sql)
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]


def test_wrapped_data_change():
    sql = "EXPLAIN ANALYZE DELETE FROM t;\n"
    res = check(sql)
    assert res["verified"] and found(res, UNGUARDED) == [at(sql, "DELETE")]


def test_wrapper_of_a_clean_query_is_checked_and_clean():
    for sql in ("EXPLAIN SELECT a FROM t WHERE a IS NULL;\n",
                "EXPLAIN EXECUTE p(1);\n",
                "PREPARE p AS SELECT a FROM t WHERE a = $1;\n"):
        res = check(sql)
        assert res["verified"] and res["findings"] == [], sql


@pytest.mark.parametrize("sql", [
    "EXPLAIN (FORMAT JSON) SELECT a FROM t WHERE a = = 1;\n",  # doesn't parse
    "EXPLAIN SOMETHING NEW SELECT 1;\n",           # an option we don't know
    "DECLARE c INT;\n",                            # not a cursor
    "PREPARE p FROM 'SELECT 1';\n",                # MySQL: a query in a string
    "CREATE TABLE t2 OF some_type;\n",             # no query to unwrap
    "CREATE TABLE t2 AS (x);\n",                   # brackets, but no query
])
def test_a_wrapper_that_cant_be_unwrapped_stays_not_checked(sql):
    res = check(sql)
    assert not res["verified"]
    [diag] = res["diagnostics"]
    assert diag["message"] == "line 1: could not parse (not checked)"
    if "AS (x)" not in sql:  # that one is a parse error, not a Command
        assert "opaque command" in diag["detail"]


@pytest.mark.parametrize("dialect", [None, "duckdb", "snowflake"])
def test_wrapper_the_parser_rejects_outright(dialect):
    """Under these dialects sqlglot raises on DECLARE … CURSOR instead of
    keeping a Command: the query inside is still found and checked."""
    sql = ("DECLARE c CURSOR WITH HOLD FOR\n"
           "  SELECT a FROM t WHERE a = NULL;\n")
    res = check(sql, dialect)
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]
    res = check("DECLARE c CURSOR FOR SELECT FROM WHERE;\n", dialect)
    assert [d["message"] for d in res["diagnostics"]] == [
        "line 1: could not parse (not checked)"]


def test_wrapper_in_a_shared_chunk_and_after_other_statements():
    sql = ('SELECT 1 AS "it\'s";\nEXPLAIN SELECT a FROM t WHERE a = NULL;\n'
           'SELECT 2 AS "x\'";\n')
    assert len(analyzer.split_statements(sql)) == 1
    res = check(sql)
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]


# -- SQL function and procedure bodies ------------------------------------------
@pytest.mark.parametrize("open_, close", [("$$", "$$"), ("$body$", "$body$"),
                                          ("$f_1$", "$f_1$")])
def test_sql_body_in_dollar_quotes(open_, close):
    sql = (f"CREATE OR REPLACE FUNCTION f(x int) RETURNS int AS {open_}\n"
           "  SELECT a FROM t;\n"
           "  DELETE FROM t;\n"
           "  SELECT a FROM t\n"
           "   WHERE a = NULL;\n"
           f"{close} LANGUAGE sql STABLE;\n")
    res = check(sql)
    assert res["verified"], res["diagnostics"]
    assert found(res, UNGUARDED) == [at(sql, "DELETE")]
    assert found(res, NULL_EQ) == [at(sql, "a = NULL")]


def test_sql_body_in_single_quotes_maps_doubled_quotes():
    """Inside a single-quoted body, '' is one character: the column of a
    finding after one is the column in the file."""
    sql = ("CREATE FUNCTION f() RETURNS text AS '\n"
           "  SELECT ''it''''s'' AS s, a FROM t WHERE a = NULL\n"
           "' LANGUAGE sql;\n")
    res = check(sql)
    assert res["verified"]
    assert found(res, NULL_EQ) == [at(sql, "a = NULL")]


def test_sql_body_single_line_and_procedure():
    sql = ("CREATE PROCEDURE p() LANGUAGE sql AS $$ DELETE FROM t $$;\n"
           "CREATE FUNCTION g() RETURNS int LANGUAGE sql\n"
           "  AS 'SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)';\n")
    res = check(sql)
    assert res["verified"]
    assert found(res, UNGUARDED) == [at(sql, "DELETE")]
    assert found(res, NOT_IN) == [at(sql, "a NOT IN")]


def test_sql_body_that_doesnt_parse_is_not_checked_at_its_line():
    sql = ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\n"
           "  SELECT 1;\n"
           "  SELECT FROM WHERE = ;\n"
           "$$;\n")
    res = check(sql)
    assert not res["verified"]
    assert [d["message"] for d in res["diagnostics"]] == [
        "line 3: could not parse (not checked)"]


def test_sql_function_was_clean_and_now_reports():
    """The false clean 0.1.4 removes: one statement, no `;`, parsed as a
    function whose body nothing looked inside."""
    sql = ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\n"
           "SELECT a FROM t WHERE a = NULL\n$$;\n")
    res = check(sql)
    assert found(res) == [at(sql, "a = NULL")]


def test_sql_bodies_that_are_not_procedural():
    """Empty bodies, a SQL-standard BEGIN ATOMIC body, and a SQL body
    that opens with DECLARE … CURSOR are plain SQL (pg-regress)."""
    for sql in ("CREATE FUNCTION f(int) RETURNS int LANGUAGE SQL AS '';\n",
                "CREATE PROCEDURE p(x text)\nBEGIN ATOMIC\nEND;\n"):
        res = check(sql)
        assert res["verified"] and res["findings"] == [], sql
    sql = ("CREATE FUNCTION f(text) RETURNS void\n"
           " AS 'DECLARE c CURSOR FOR SELECT a FROM t WHERE a = NULL;'\n"
           " LANGUAGE SQL;\n")
    res = check(sql)
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]


def test_snowflake_sql_function_body():
    sql = "CREATE FUNCTION f() RETURNS INT AS 'SELECT a FROM t WHERE a = NULL';\n"
    res = check(sql, "snowflake")
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]


def test_bigquery_body_is_part_of_the_tree():
    sql = ("CREATE TEMP FUNCTION f(x INT64) AS (\n"
           "  (SELECT a FROM t WHERE a = NULL)\n);\n")
    res = check(sql, "bigquery")
    assert res["verified"] and found(res) == [at(sql, "a = NULL")]


def test_body_in_another_language_is_left_as_it_was():
    for sql in ("CREATE FUNCTION f() RETURNS int AS $$ return 1 $$ "
                "LANGUAGE plpython3u;\n",
                "CREATE FUNCTION f() RETURNS INT LANGUAGE JAVASCRIPT "
                "AS 'return a = NULL';\n"):
        res = check(sql, "snowflake" if "JAVASCRIPT" in sql else "postgres")
        assert res["verified"] and res["findings"] == [], sql


# -- procedural bodies ----------------------------------------------------------
PLPGSQL = ("CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$\n"
           "BEGIN\n"
           "  NEW.updated_at := now();\n"
           "  RETURN NEW;\n"
           "END\n"
           "$$;\n")


@pytest.mark.parametrize("sql, dialect, language", [
    (PLPGSQL, "postgres", "PL/pgSQL"),
    ("CREATE FUNCTION f() RETURNS int AS 'BEGIN RETURN 1; END' "
     "LANGUAGE 'plpgsql';\n", "postgres", "PL/pgSQL"),
    ("DO $$ BEGIN PERFORM 1; END $$;\n", "postgres", "PL/pgSQL"),
    ("CREATE OR REPLACE PROCEDURE p() RETURNS VARCHAR LANGUAGE SQL AS $$\n"
     "BEGIN\n  RETURN 'x';\nEND\n$$;\n", "snowflake", "Snowflake Scripting"),
    ("CREATE PROCEDURE p() RETURNS INT LANGUAGE SQL AS "
     "'DECLARE n INT; BEGIN RETURN n; END';\n", "snowflake",
     "Snowflake Scripting"),
    ("CREATE PROCEDURE p AS BEGIN SELECT 1 END;\n", "tsql", "T-SQL"),
])
def test_procedural_body_is_not_checked_with_a_reason(sql, dialect, language):
    res = check(sql, dialect)
    assert not res["verified"]
    [diag] = res["diagnostics"]
    assert diag["message"] == \
        f"line 1: procedural code in {language} (not checked)"
    assert "doesn't look inside procedural code yet" in diag["detail"]


def test_procedural_body_line_is_the_create():
    res = check("SELECT 1;\n\n" + PLPGSQL)
    assert [d["message"] for d in res["diagnostics"]] == [
        "line 3: procedural code in PL/pgSQL (not checked)"]


def test_plain_statements_inside_procedural_code_still_report():
    """As before 0.1.4: a statement inside a procedural body that parses
    on its own as SQL is checked; the body is still not checked."""
    sql = ("DO $$\n"
           "BEGIN\n"
           "  IF NOT done THEN\n"
           "    ALTER TABLE t ADD COLUMN c int;\n"
           "    SELECT a FROM t WHERE a = NULL;\n"
           "  END IF;\n"
           "END\n"
           "$$;\n")
    res = check(sql)
    assert not res["verified"]
    assert [d["message"] for d in res["diagnostics"]] == [
        "line 1: procedural code in PL/pgSQL (not checked)"]
    assert found(res, NULL_EQ) == [at(sql, "a = NULL")]


def test_procedural_file_exit_3_and_allow_unverified(tmp_path):
    (tmp_path / "f.sql").write_text(PLPGSQL)
    code, out, err = cli("detect", ".", cwd=tmp_path)
    assert code == 3
    assert "clean" not in out
    assert "1 not checked (procedural code)" in out
    assert err == "f.sql: line 1: procedural code in PL/pgSQL (not checked)\n"
    code, out, _ = cli("detect", "--allow-unverified", ".", cwd=tmp_path)
    assert code == 0 and "clean" not in out


def test_procedural_file_in_the_receipt(tmp_path):
    (tmp_path / "f.sql").write_text(PLPGSQL)
    (tmp_path / "g.sql").write_text("SELECT FROM WHERE = ;\n")
    code, out, _ = cli("review", "--json", ".", cwd=tmp_path)
    assert code == 3
    nc = json.loads(out)["receipt"]["not_checked"]
    assert nc["unverified_files"] == ["f.sql", "g.sql"]
    assert nc["procedural_files"] == ["f.sql"]
    assert nc["unparsed_files"] == ["g.sql"]
    code, out, _ = cli("review", ".", cwd=tmp_path)
    assert "Not checked  1 file that didn't parse: g.sql" in out
    assert ("Not checked  1 file with procedural code (PL/pgSQL, BEGIN … "
            "END), not looked inside yet: f.sql") in out


def test_hook_says_procedural_code_is_unverified(tmp_path):
    (tmp_path / "f.sql").write_text(PLPGSQL)
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write",
                          "tool_input": {"file_path": "f.sql"}})
    code, out, err = cli("hook", "--harness", "claude", cwd=tmp_path,
                         stdin=payload)
    assert (code, err) == (0, "")
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[TrueQuery] UNVERIFIED — procedural code, not checked:" in body
    assert "procedural code in PL/pgSQL" in body


# -- Python ---------------------------------------------------------------------
def test_python_wrapper_finding_at_the_python_line_and_column():
    """A wrapper sqlglot reads only as a Command, inside Python. (The
    Python extractor takes strings that start with a SQL keyword such as
    CREATE or SELECT; one that starts with EXPLAIN isn't taken as SQL.)"""
    src = ('import psycopg2\n'
           'cur.execute("CREATE MATERIALIZED VIEW mv AS "\n'
           '            "SELECT a FROM t WHERE a = NULL WITH NO DATA")\n')
    findings, frags, _ = E.check_python_source(src, "m.py")
    assert [f.status for f in frags] == ["static"]
    assert [(f.rule, f.line, f.col) for f in findings] == [
        (NULL_EQ, *at(src, "a = NULL"))]


def test_python_sql_function_body():
    src = ('import psycopg2\n'
           'cur.execute("""\n'
           'CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\n'
           '  SELECT a FROM t;\n'
           '  SELECT a FROM t WHERE a = NULL;\n'
           '$$""")\n')
    findings, frags, _ = E.check_python_source(src, "m.py")
    assert [f.status for f in frags] == ["static"]
    assert [(f.rule, f.line, f.col) for f in findings] == [
        (NULL_EQ, *at(src, "a = NULL"))]


def test_python_query_that_is_all_placeholder_is_not_checked():
    """`CREATE TABLE {t} AS ({query})`: the query is a placeholder, so
    nothing in it can be checked."""
    src = ('import psycopg\n'
           'cur.execute(f"CREATE TABLE {name} AS ({entity_df})")\n')
    findings, frags, _ = E.check_python_source(src, "m.py")
    assert [f.status for f in frags] == ["unparsed"] and findings == []


PY_PLPGSQL = ('import psycopg2\n'
              'cur.execute("""\n'
              'CREATE FUNCTION touch() RETURNS trigger LANGUAGE plpgsql AS $$\n'
              'BEGIN\n  NEW.updated_at := now();\n  RETURN NEW;\nEND\n'
              '$$""")\n')


def test_python_procedural_body_is_unparsed_never_exit_3(tmp_path):
    findings, frags, _ = E.check_python_source(PY_PLPGSQL, "m.py")
    assert [f.status for f in frags] == ["unparsed"] and findings == []
    (tmp_path / "m.py").write_text(PY_PLPGSQL)
    code, out, err = cli("detect", ".", cwd=tmp_path)
    assert (code, err) == (0, "")
    assert "clean" not in out


# -- ignores inside a function body ---------------------------------------------
def test_ignore_inside_a_body_holds_when_lines_move(tmp_path):
    body = ("CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$\n"
            "  SELECT a FROM t WHERE a = NULL;\n"
            "  SELECT b FROM t WHERE b = NULL;\n"
            "$$;\n")
    (tmp_path / "f.sql").write_text(body)
    code, out, _ = cli("detect", "f.sql", cwd=tmp_path)
    assert code == 1 and "f.sql:2:" in out and "f.sql:3:" in out
    code, _, err = cli("ignore", "add", NULL_EQ, "./f.sql:2",
                       "--reason", "agent: legacy", cwd=tmp_path)
    assert code == 0, err
    code, out, _ = cli("detect", "f.sql", cwd=tmp_path)
    assert code == 1 and "f.sql:2:" not in out and "f.sql:3:" in out
    (tmp_path / "f.sql").write_text("-- moved\nSELECT 1;\n\n" + body)
    code, out, _ = cli("detect", "f.sql", cwd=tmp_path)
    assert code == 1 and "f.sql:5:" not in out and "f.sql:6:" in out


# -- the bare command ------------------------------------------------------------
def test_bare_command_hint_says_truequery(tmp_path):
    code, out, _ = cli(cwd=tmp_path)
    assert code == 2
    assert "Try: truequery detect <file.sql>" in out
    assert "tq " not in out.split("Try:")[1]
