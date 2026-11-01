"""0.1.2: the SQL inside Python files is checked, not just listed.

Extraction (statuses, parameter markers, positions), the dialect table,
results/receipt/JSON/exit codes, ignores, and the hook.
"""

import io
import json
import os
import subprocess
import sys
import tempfile

import pytest

import truequery.hook as hook
from truequery import engine as E
from truequery.extractors import python_sql as X

CLI = [sys.executable, "-m", "truequery.cli"]
_HOME = tempfile.mkdtemp(prefix="tq-test-home-")
ENV = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME,
           PYTHONPATH=os.path.join(
               os.path.dirname(__file__), "..", "plugin", "engine", "src"))


def cli(*argv, cwd):
    proc = subprocess.run(CLI + list(argv), cwd=cwd, capture_output=True,
                          text=True, env=ENV, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def checked(src, dialect=None):
    """(findings, fragments) for a Python source, as the engine sees it."""
    findings, fragments, _ = E.check_python_source(src, "m.py", dialect)
    return findings, fragments


def statuses(src):
    return [f.status for f in checked(src)[1]]


def where(src, rule="sql/null-equality-comparison"):
    return [(f.line, f.col) for f in checked(src)[0] if f.rule == rule]


# -- statuses -------------------------------------------------------------------
@pytest.mark.parametrize("src", [
    'con.execute("SELECT a FROM t")\n',
    'con.execute("SELECT a "\n            "FROM t")\n',
    'con.execute("SELECT a " + "FROM t")\n',
    'Q = "SELECT a FROM t"\ncon.execute(Q)\n',
    'Q: str = """\nSELECT a FROM t\n"""\ncon.execute(Q.strip())\n',
    'from textwrap import dedent\ncon.execute(dedent("""\n    SELECT a FROM t\n"""))\n',
])
def test_static(src):
    assert statuses(src) == ["static"]


@pytest.mark.parametrize("src", [
    'con.execute(f"SELECT a FROM {table}")\n',
    'con.execute("SELECT a FROM {}".format(table))\n',
    'con.execute("SELECT a FROM {t} WHERE b = {v!r}".format(t=t, v=v))\n',
    'con.execute("SELECT a FROM %s" % table)\n',
    'con.execute("SELECT a FROM %(t)s" % {"t": table})\n',
    'Q = "SELECT a FROM {}"\ncon.execute(Q.format(t))\n',
    'con.execute("SELECT a FROM t WHERE id = " + str(i))\n',
])
def test_templated(src):
    findings, frags = checked(src)
    assert [f.status for f in frags] == ["templated"]
    assert X.PLACEHOLDER + "1" in frags[0].sql


def test_templated_placeholders_are_neutral_identifiers():
    _, frags = checked('con.execute(f"SELECT {a}, {b} FROM {c}")\n')
    assert frags[0].sql == "SELECT __tq_p1, __tq_p2 FROM __tq_p3"
    assert frags[0].placeholders == {"__tq_p1": "{a}", "__tq_p2": "{b}",
                                     "__tq_p3": "{c}"}


@pytest.mark.parametrize("src", [
    # built in a loop
    'import sqlite3\nq = "SELECT a FROM t"\nfor c in cols:\n'
    '    q += " AND " + c\ncon.execute(q)\n',
    # returned by a function
    'con.execute(build_query(x))\n',
    # imported from another module
    'from app.queries import ORDERS_SQL\ncon.execute(ORDERS_SQL)\n',
    # a name with no assignment in this file (a parameter)
    'import sqlite3\ndef run(con, q):\n    return con.execute(q)\n',
    'def run(cur, query):\n    cur.execute(query)\n',
])
def test_dynamic(src):
    findings, frags = checked(src)
    assert [f.status for f in frags] == ["dynamic"]
    assert frags[0].reason == "built at run time"
    assert findings == []


def test_a_loop_is_never_checked_as_its_first_string():
    # The text that runs is "DELETE FROM t WHERE ...": checking the
    # first string alone would be a false alarm.
    src = ('import sqlite3\nq = "DELETE FROM t"\nif x:\n'
           '    q += " WHERE id = 1"\ncon.execute(q)\n')
    findings, frags = checked(src)
    assert findings == [] and [f.status for f in frags] == ["dynamic"]


@pytest.mark.parametrize("src", [
    'con.execute("SELECT FROM WHERE (")\n',
    'from sqlalchemy import text\nlabel = text("Select an option")\n',
])
def test_unparsed(src):
    findings, frags = checked(src)
    assert [f.status for f in frags] == ["unparsed"]
    assert frags[0].reason == "doesn't parse as SQL"
    assert findings == []


@pytest.mark.parametrize("src", [
    'x = 1\n',
    '"""Select the best row."""\n',
    'print("SELECT a FROM t")\n',               # not a SQL call site
    'ax.text(0.5, 0.5, "Select a region")\n',   # not the SQL argument
    'from sqlalchemy import select\n'
    'session.execute(select(User).where(User.id == 1))\n',  # a query object
    'def run(self, task):\n    self.execute(task)\n',  # no sign of SQL
    'pd.read_sql_table(name, con)\n',            # a table name, not SQL
    'import sqlite3\nchart.text(label)\n',       # not SQLAlchemy's text
])
def test_not_sql(src):
    assert statuses(src) == []


def test_one_name_several_strings_extracts_each():
    src = ('if a:\n    q = "SELECT * FROM t WHERE a = NULL"\nelse:\n'
           '    q = "DELETE FROM t"\ncon.execute(q)\n')
    findings, frags = checked(src)
    assert [(f.line, f.status) for f in frags] == [(2, "static"),
                                                   (4, "static")]
    assert sorted((f.rule, f.line) for f in findings) == [
        ("sql/null-equality-comparison", 2),
        ("sql/unguarded-update-delete", 4)]


def test_a_name_with_one_static_and_one_dynamic_value():
    src = ('import sqlite3\nif a:\n    q = "SELECT a FROM t"\nelse:\n'
           '    q = make(x)\ncon.execute(q)\n')
    assert statuses(src) == ["static", "dynamic"]


def test_fragments_are_sorted_by_position():
    src = ('def f():\n    con.execute("SELECT b FROM u")\n'
           'Q = "SELECT a FROM t"\ncon.execute(Q)\n'
           'con.execute(f"SELECT c FROM {x}")\n')
    _, frags = checked(src)
    assert [(f.line, f.col) for f in frags] == [(2, 17), (3, 5), (5, 13)]
    assert [f.status for f in frags] == ["static", "static", "templated"]
    assert checked(src)[1] == frags  # deterministic


# -- parameter markers ------------------------------------------------------------
@pytest.mark.parametrize("marker", ["?", ":name", "$1", "%s", "%(name)s"])
def test_parameter_markers_parse(marker):
    src = f'cur.execute("SELECT a FROM t WHERE b = {marker} AND c = NULL", p)\n'
    findings, frags = checked(src)
    assert [f.status for f in frags] == ["static"]
    assert [f.rule for f in findings] == ["sql/null-equality-comparison"]


def test_pyformat_markers_normalized_only_in_their_exact_forms():
    _, frags = checked(
        "cur.execute(\"SELECT a % 2, b %% 3 FROM t WHERE c = %s AND d = "
        "%(d)s AND e LIKE '%sales%'\", p)\n")
    # %s -> ?, %(d)s -> :d, %% -> % (pyformat in use); a bare % (modulo)
    # and '%sales%' inside a SQL string are left alone.
    assert frags[0].sql == ("SELECT a % 2, b % 3 FROM t WHERE c = ? AND d = "
                            ":d AND e LIKE '%sales%'")
    _, frags = checked('cur.execute("SELECT a % 2 FROM t")\n')
    assert frags[0].sql == "SELECT a % 2 FROM t"
    assert frags[0].status == "static"


def test_percent_formatting_turns_python_fields_into_placeholders():
    _, frags = checked(
        'cur.execute("SELECT * FROM %s WHERE a = %%s" % table, (1,))\n')
    assert frags[0].sql == "SELECT * FROM __tq_p1 WHERE a = ?"


# -- location mapping ---------------------------------------------------------------
def test_triple_quoted_maps_to_the_python_line():
    src = ('import sqlite3\n\n\ndef load(con):\n    return con.execute("""\n'
           '        SELECT *\n        FROM orders\n'
           '        WHERE customer_id = NULL\n    """)\n')
    assert where(src) == [(8, 15)]


def test_single_line_string():
    src = 'con.execute("SELECT * FROM t WHERE a = NULL")\n'
    assert where(src) == [(1, 36)]


@pytest.mark.parametrize("prefix", ["r", "R", "u", "f", "rf", "Fr"])
def test_prefixes(prefix):
    src = f'con.execute({prefix}"SELECT * FROM t WHERE a = NULL")\n'
    assert where(src) == [(1, 36 + len(prefix))]


@pytest.mark.parametrize("quote", ["'", '"', "'''", '"""'])
def test_quotes(quote):
    src = f"con.execute({quote}SELECT * FROM t WHERE a = NULL{quote})\n"
    assert where(src) == [(1, 35 + len(quote))]


def test_fstring_placeholder_before_the_finding_keeps_the_column():
    src = 'con.execute(f"SELECT * FROM {schema}.t WHERE a = NULL")\n'
    assert where(src) == [(1, 46)]


def test_concatenation_over_three_lines_maps_each_part():
    src = ('con.execute("SELECT * " +\n'
           '            "FROM t " +\n'
           '            "WHERE a = NULL")\n')
    assert where(src) == [(3, 20)]


def test_implicit_concatenation_with_comments_between():
    src = ('con.execute(\n    "SELECT * "  # all columns\n'
           '    "FROM t "\n    "WHERE a = NULL"\n)\n')
    assert where(src) == [(4, 12)]


def test_name_resolved_string_is_reported_at_its_assignment():
    src = ('QUERY = """\nSELECT *\nFROM orders\nWHERE id = NULL\n"""\n\n'
           'def load(con):\n    return con.execute(QUERY)\n')
    assert where(src) == [(4, 7)]


def test_escapes_map_to_where_they_are_written():
    # \n and \t are two source characters each; the column after them is
    # still exact.
    src = 'con.execute("SELECT *\\n\\tFROM t WHERE a = NULL")\n'
    assert where(src) == [(1, 39)]


def test_second_statement_on_one_line():
    src = 'con.executescript("DELETE FROM a; DELETE FROM b")\n'
    assert where(src, "sql/unguarded-update-delete") == [(1, 20), (1, 35)]


def test_non_ascii_text_before_the_string_counts_characters():
    src = 'é = con.execute("SELECT * FROM t WHERE a = NULL")\n'
    assert where(src) == [(1, 40)]


def test_snippet_is_the_sql_line_with_placeholders_named():
    findings, _ = checked(
        'con.execute(f"""\nSELECT *\nFROM {t}\nWHERE a = NULL\n""")\n')
    assert findings[0].snippet == "WHERE a = NULL"
    findings, _ = checked('con.execute(f"SELECT * FROM {t} WHERE a = NULL")\n')
    assert findings[0].snippet == "SELECT * FROM {t} WHERE a = NULL"


# -- dialect ------------------------------------------------------------------------
@pytest.mark.parametrize("imports,dialect", [
    (["sqlite3"], "sqlite"),
    (["duckdb"], "duckdb"),
    (["psycopg"], "postgres"),
    (["psycopg2.extras"], "postgres"),
    (["asyncpg"], "postgres"),
    (["pymysql"], "mysql"),
    (["mysql.connector"], "mysql"),
    (["MySQLdb"], "mysql"),
    (["snowflake.snowpark"], "snowflake"),
    (["pyspark.sql", "pyspark.sql.SparkSession"], "spark"),
    (["snowflake.connector"], "snowflake"),
    (["google.cloud", "google.cloud.bigquery"], "bigquery"),
    (["sqlite3", "duckdb"], None),     # conflicting: auto
    (["os", "json"], None),
    (["mysqlx"], None),                # a prefix of a name is not a match
])
def test_dialect_table(imports, dialect):
    assert X.dialect_for_imports(imports) == dialect


def test_dialect_inferred_from_import_forms():
    assert X.extract("from google.cloud import bigquery\n"
                     "c.execute('SELECT 1')\n").dialect == "bigquery"
    assert X.extract("import mysql.connector\n"
                     "c.execute('SELECT 1')\n").dialect == "mysql"


def test_dialect_precedence(tmp_path, monkeypatch):
    seen = []
    real = E.analyzer.check_text

    def spy(text, dialect=None, **kw):
        seen.append(dialect)
        return real(text, dialect=dialect, **kw)

    monkeypatch.setattr(E.analyzer, "check_text", spy)
    (tmp_path / "q.py").write_text("import duckdb\n"
                                   "duckdb.sql('SELECT a FROM t')\n",
                                   encoding="utf-8")
    E.run([str(tmp_path)], str(tmp_path), config={})
    assert seen[-1] == "duckdb"                          # imports
    E.run([str(tmp_path)], str(tmp_path), config={"dialect": "postgres"})
    assert seen[-1] == "postgres"                        # DATA.md
    E.run([str(tmp_path)], str(tmp_path), dialect="snowflake",
          config={"dialect": "postgres"})
    assert seen[-1] == "snowflake"                       # --dialect
    (tmp_path / "q.py").write_text("duckdb.sql('SELECT a FROM t')\n",
                                   encoding="utf-8")
    E.run([str(tmp_path)], str(tmp_path), config={})
    assert seen[-1] is None                              # auto


# -- results, receipt, summary, exit code ---------------------------------------------
def test_mixed_checked_and_dynamic_is_counted_and_listed(tmp_path):
    (tmp_path / "app.py").write_text(
        "import sqlite3\n"
        'con.execute("SELECT a FROM t")\n'
        "def search(con, where):\n"
        '    return con.execute("SELECT a FROM t WHERE " + where)\n'
        "def report(con, q):\n"
        "    return con.execute(q)\n", encoding="utf-8")
    code, out, _ = cli("detect", ".", cwd=tmp_path)
    assert code == 0
    assert out.strip() == ("no findings in 1 file checked offline · "
                           "SQL inside 1 Python file not checked")
    assert "clean" not in out
    code, out, _ = cli("review", ".", cwd=tmp_path)
    assert code == 0
    assert "Checked      0 SQL files · SQL in 1 Python file (2 queries)" in out
    assert ("Not checked  SQL built at run time in 1 Python file: app.py"
            in out)
    code, out, _ = cli("review", ".", "--json", cwd=tmp_path)
    doc = json.loads(out)
    assert doc["receipt"]["files_checked"] == 1
    assert doc["receipt"]["not_checked"]["embedded_sql_files"] == ["app.py"]
    assert doc["receipt"]["not_checked"]["embedded_sql_reasons"] == {
        "app.py": ["built at run time"]}
    assert doc["python_sql"]["app.py"] == {
        "checked": 2, "dynamic": 1, "unparsed": 0, "static": 1,
        "templated": 1, "not_checked": [{"line": 6, "col": 24,
                         "reason": "built at run time"}]}


def test_not_sql_is_listed_and_never_changes_the_exit_code(tmp_path):
    (tmp_path / "ui.py").write_text(
        "from sqlalchemy import text\n"
        'prompt = text("Select an option")\n', encoding="utf-8")
    (tmp_path / "q.sql").write_text("select a from t\n", encoding="utf-8")
    code, out, _ = cli("detect", ".", cwd=tmp_path)
    assert code == 0  # never 3: detection is a heuristic
    assert out.strip() == ("no findings in 1 file checked offline · "
                           "SQL inside 1 Python file not checked")
    code, out, _ = cli("review", ".", cwd=tmp_path)
    assert code == 0
    assert ("Not checked  SQL that doesn't parse in 1 Python file: ui.py"
            in out)
    assert "didn't parse" not in out  # not listed as a broken SQL file
    code, out, _ = cli("review", ".", "--json", cwd=tmp_path)
    doc = json.loads(out)
    assert doc["diagnostics"] == []
    assert doc["receipt"]["files_checked"] == 1
    assert doc["receipt"]["not_checked"]["embedded_sql_reasons"] == {
        "ui.py": ["doesn't parse as SQL"]}


def test_all_unchecked_file_is_listed_not_counted(tmp_path):
    (tmp_path / "a.py").write_text(
        "import sqlite3\ndef f(con, q):\n    con.execute(q)\n"
        'con.execute("SELECT FROM WHERE (")\n', encoding="utf-8")
    code, out, _ = cli("review", ".", "--json", cwd=tmp_path)
    doc = json.loads(out)
    assert code == 0
    assert doc["receipt"]["files_checked"] == 0
    assert doc["receipt"]["python_files"] == 0
    assert doc["receipt"]["not_checked"]["embedded_sql_reasons"] == {
        "a.py": ["built at run time", "doesn't parse as SQL"]}
    code, out, _ = cli("review", ".", cwd=tmp_path)
    assert "Not checked  SQL built at run time in 1 Python file: a.py" in out
    assert "Not checked  SQL that doesn't parse in 1 Python file: a.py" in out


def test_receipt_lists_overflow_to_json(tmp_path):
    for i in range(12):
        (tmp_path / f"m{i:02}.py").write_text(
            "import sqlite3\ndef f(con, q):\n    con.execute(q)\n",
            encoding="utf-8")
    _, out, _ = cli("review", ".", cwd=tmp_path)
    assert ("Not checked  SQL built at run time in 12 Python files: m00.py, "
            "m01.py") in out
    assert "and 2 more (--json lists all)" in out


def test_python_sql_feeds_contracts_and_division_limit(tmp_path):
    (tmp_path / "q.py").write_text(
        'con.execute(f"SELECT a / b FROM orders JOIN {other} USING (id)")\n',
        encoding="utf-8")
    _, out, _ = cli("review", ".", cwd=tmp_path)
    assert "No contract  orders" in out          # placeholders aren't tables
    assert "integer division between columns" in out


def test_sarif_points_at_the_python_line(tmp_path):
    (tmp_path / "q.py").write_text(
        'x = 1\ncon.execute("SELECT * FROM t WHERE a = NULL")\n',
        encoding="utf-8")
    _, out, _ = cli("detect", ".", "--sarif", cwd=tmp_path)
    region = json.loads(out)["runs"][0]["results"][0]["locations"][0][
        "physicalLocation"]
    assert region["artifactLocation"]["uri"] == "q.py"
    assert region["region"] == {"startLine": 2, "startColumn": 36}


# -- ignores ------------------------------------------------------------------------
def test_ignore_add_on_a_python_line_survives_lines_above(tmp_path):
    target = tmp_path / "q.py"
    body = ('    return con.execute("""\n'
            "        DELETE FROM sessions\n"
            '    """)\n\n'
            "def other(con):\n"
            '    con.execute("DELETE FROM carts")\n')
    target.write_text("def purge(con):\n" + body, encoding="utf-8")
    code, out, _ = cli("detect", "q.py", cwd=tmp_path)
    assert code == 1 and "q.py:3:9 [error] sql/unguarded-update-delete" in out
    code, _, err = cli("ignore", "add", "sql/unguarded-update-delete",
                       "q.py:3", "--reason", "agent: test fixture", cwd=tmp_path)
    assert code == 0, err
    entry = json.loads((tmp_path / ".truequery" / "ignores.json").read_text(
        encoding="utf-8"))["ignores"][0]
    assert entry["path"] == "q.py" and entry["stmt"] and entry["offset"] == 0
    code, out, _ = cli("detect", "q.py", cwd=tmp_path)
    assert "q.py:3:" not in out and "q.py:7:" in out
    # Lines inserted above the fragment: the ignore moves with it.
    target.write_text("import sqlite3\n\n\ndef purge(con):\n" + body,
                      encoding="utf-8")
    code, out, _ = cli("detect", "q.py", cwd=tmp_path)
    assert "unguarded-update-delete" in out
    assert "q.py:6:" not in out and "q.py:10:" in out


def test_hand_written_line_ignore_on_a_python_file(tmp_path):
    (tmp_path / "q.py").write_text(
        'con.execute("DELETE FROM t")\ncon.execute("DELETE FROM u")\n',
        encoding="utf-8")
    (tmp_path / ".truequery").mkdir()
    (tmp_path / ".truequery" / "ignores.json").write_text(json.dumps(
        {"ignores": [{"rule": "sql/unguarded-update-delete", "path": "q.py",
                      "line": 2, "reason": "human: fixture"}]}),
        encoding="utf-8")
    _, out, _ = cli("detect", ".", cwd=tmp_path)
    assert "q.py:1:" in out and "q.py:2:" not in out
    assert "(1 hidden by ignores)" in out


def test_ignore_add_on_a_dag_line_outside_its_sql(tmp_path):
    (tmp_path / "dag.py").write_text(
        "from airflow import DAG\ndag = DAG('x')\n", encoding="utf-8")
    code, _, err = cli("ignore", "add", "orch/explicit-catchup-policy",
                       "dag.py:2", "--reason", "human: backfill wanted",
                       cwd=tmp_path)
    assert code == 0, err
    code, out, _ = cli("detect", ".", "--enable-rule",
                       "orch/explicit-catchup-policy", "--fail-on", "warning",
                       cwd=tmp_path)
    assert code == 0 and "catchup" not in out.split("·")[0]


# -- hook ---------------------------------------------------------------------------
class Args:
    def __init__(self, harness="auto", event="auto"):
        self.harness = harness
        self.event = event
        self.log_file = None
        self.scope = "project"


def _hook(payload, monkeypatch):
    monkeypatch.setattr(hook, "read_stdin", lambda timeout=5.0: payload)
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)
    assert hook.main(Args()) == 0
    text = out.getvalue()
    return (json.loads(text)["hookSpecificOutput"]["additionalContext"]
            if text else "")


def _edit(path, event="PostToolUse"):
    return json.dumps({"hook_event_name": event, "tool_name": "Write",
                       "tool_input": {"file_path": str(path)}})


def test_hook_python_edit_reports_a_planted_bug_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "app.py"
    target.write_text("import sqlite3\n\n"
                      "def load(con):\n"
                      '    return con.execute("SELECT * FROM orders '
                      'WHERE customer_id = NULL")\n', encoding="utf-8")
    text = _hook(_edit(target), monkeypatch)
    assert text.startswith("[TrueQuery] 1 new finding(s) in app.py\n"
                           "- app.py:4 [sql/null-equality-comparison]")
    assert ("`truequery ignore add sql/null-equality-comparison app.py:4"
            in text)
    # The same finding is said once: a re-edit only nudges.
    text = _hook(_edit(target), monkeypatch)
    assert text == ("[TrueQuery] still open: sql/null-equality-comparison "
                    "app.py:4 (seen)")


def test_hook_python_edit_without_sql_is_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "util.py"
    target.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    assert _hook(_edit(target), monkeypatch) == ""
    session = tmp_path / ".truequery" / "session.json"
    if session.exists():
        assert json.loads(session.read_text(encoding="utf-8"))[
            "touched"] == []


def test_hook_stop_includes_touched_python_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "report.py"
    # A Stop-tier finding: the edit pass says nothing, the Stop pass does.
    target.write_text('con.execute("SELECT * FROM a, b")\n',
                      encoding="utf-8")
    assert _hook(_edit(target), monkeypatch) == ""
    text = _hook(_edit(target, event="Stop"), monkeypatch)
    assert text.startswith("[TrueQuery] stop pass · 1 file(s) touched")
    assert "- report.py:1 [sql/accidental-cross-join]" in text


@pytest.mark.parametrize("marker", ["%s", ":ids", "?", "%(ids)s"])
def test_list_parameters_parse(marker):
    _, frags = checked(f'cur.execute("SELECT a FROM t WHERE id IN {marker}", p)\n')
    assert frags[0].status == "static"
    assert frags[0].sql.endswith("IN (" + {"%s": "?", "%(ids)s": ":ids"}.get(
        marker, marker) + ")")


def test_driver_supplied_rows_parse():
    findings, frags = checked(
        'client.execute("INSERT INTO t (a, b) VALUES", rows)\n')
    assert frags[0].status == "static" and findings == []
    findings, frags = checked('client.execute("INSERT INTO t VALUES", rows)\n')
    assert [f.rule for f in findings] == ["sql/insert-explicit-column-list"]
    assert (findings[0].line, findings[0].col) == (1, 17)


@pytest.mark.parametrize("src", [
    'con.execute("SELECT now() AS t, 1 AS v WHERE 1 = 1")\n',
    'con.execute("SELECT {{ p + 1 }}")\n',
    'con.execute(f"SELECT {cols}")\n',
    'con.execute("SELECT 1")\n',
])
def test_select_without_from_that_is_still_sql(src):
    assert statuses(src)[0] in ("static", "templated")


def test_psycopg_sql_composable_is_templated():
    findings, frags = checked(
        "from psycopg2 import sql\n"
        'cur.execute(sql.SQL("DELETE FROM {}").format(sql.Identifier(t)))\n')
    assert [f.status for f in frags] == ["templated"]
    assert frags[0].sql == "DELETE FROM __tq_p1"
    assert [(f.rule, f.line, f.col) for f in findings] == [
        ("sql/unguarded-update-delete", 2, 22)]


@pytest.mark.parametrize("src", [
    "import pandas as pd\nnode.execute(context)\n",
    "import pandas as pd\ndf = pd.DataFrame()\naction.execute(df)\n",
    "import sqlite3\nself.tool.execute(Args(q))\n",
])
def test_execute_on_something_that_is_not_a_database(src):
    assert statuses(src) == []


@pytest.mark.parametrize("src", [
    "import pandas as pd\ncursor.execute(make(x))\n",
    "import sqlite3\nself._conn.cursor().execute(stmt)\n",
    "import pandas as pd\npd.read_sql(q, con)\n",
    "op.execute(build_sql())\n",
])
def test_execute_on_a_database_handle(src):
    assert statuses(src) == ["dynamic"]


@pytest.mark.parametrize("src", [
    'cur.execute(f"DELETE FROM {s} USING {f} AS f WHERE {cond} AND (x = 1)")\n',
    'cur.execute(f"SELECT * FROM a, b WHERE {link}")\n',
    'cur.execute(f"UPDATE s SET a = f.a FROM {f} AS f WHERE ({cond})")\n',
])
def test_placeholder_condition_is_unknown_not_missing(src):
    assert checked(src)[0] == []


def test_placeholder_elsewhere_still_reports_a_missing_condition():
    findings, _ = checked('cur.execute(f"DELETE FROM {table}")\n')
    assert [f.rule for f in findings] == ["sql/unguarded-update-delete"]
    findings, _ = checked('cur.execute(f"SELECT * FROM {schema}.a, b")\n')
    assert [f.rule for f in findings] == ["sql/accidental-cross-join"]


def test_placeholder_window_order_is_unknown_not_missing():
    src = ('cur.execute(f"SELECT * FROM (SELECT a, ROW_NUMBER() OVER '
           '(PARTITION BY k ORDER BY {order}) AS rn FROM t) x WHERE rn = 1")\n')
    assert checked(src)[0] == []
    src = src.replace("{order}", "ts")
    assert [f.rule for f in checked(src)[0]] == [
        "sql/row-number-dedupe-tiebreaker"]
