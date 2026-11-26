"""0.2 polish: the last fixes before 0.2.0.

1. init --from suggests `absent_unexpected` for status values that name a
   failure; `means` stays TODO until a person confirms.
2. sql/group-by-bare-column is quiet when the bare column's table has its
   key in the GROUP BY (a declared key, or a column named as the key when
   none is declared), directly or one join away, never from a side an
   outer join can make NULL.
3. sql/distinct-dedup-bandaid looks only at the query's own level.
4. sql/keyset-pagination is quiet on LIMIT 1 (picking the n-th row).
5. The SQL path check is quiet on an absolute literal against a relative
   base, as the pandas one is.

Many shapes come from the gold queries of the public BIRD dev set."""
import os
import sys

import pytest

from truequery import initfrom as IF
from truequery import keylookup
from truequery.analyzers.sql import check_text
from truequery.checks.sql import _key_forms, _prefix_elsewhere

sys.path.insert(0, os.path.dirname(__file__))
import test_contract_checks as TC  # noqa: E402
import test_pandas_contracts as TP  # noqa: E402

GB = "sql/group-by-bare-column"
DEDUP = "sql/distinct-dedup-bandaid"
KEYSET = "sql/keyset-pagination"
PATH = "sem/relative-path-base-mismatch"


def _rules(sql, dialect="sqlite", **kw):
    res = check_text(sql, dialect=dialect, **kw)
    assert res["verified"], res["diagnostics"]
    return [f["rule"] for f in res["findings"]]


# -- 2. sql/group-by-bare-column -----------------------------------------------

def test_q596_an_arbitrary_badge_per_user_still_fires():
    # BIRD Q596: Name is a badge's; badges has many rows per UserId.
    sql = ("SELECT Name FROM badges AS T1 INNER JOIN comments AS T2 "
           "ON T1.UserId = t2.UserId GROUP BY T2.UserId "
           "ORDER BY COUNT(T2.UserId) DESC LIMIT 1")
    assert GB in _rules(sql)
    # Qualified, it still fires: UserId isn't the badges table's key.
    assert GB in _rules(sql.replace("SELECT Name", "SELECT T1.Name"))


@pytest.mark.parametrize("sql", [
    # BIRD Q1322, Q1323, Q1403: the table's own key, named <table>_id.
    "SELECT T1.event_name, COUNT(*) FROM event AS T1 INNER JOIN attendance "
    "AS T2 ON T1.event_id = T2.link_to_event GROUP BY T1.event_id",
    # Q1040: `id`, with an alias that differs from the table name.
    "SELECT t1.player_name FROM Player AS t1 INNER JOIN Player_Attributes "
    "AS t2 ON t1.player_api_id = t2.player_api_id GROUP BY t1.id "
    "ORDER BY SUM(t2.heading_accuracy) DESC LIMIT 10",
    # Q1496: plural table, singular key, mixed case, no underscore.
    "SELECT T1.Segment FROM customers AS T1 INNER JOIN yearmonth AS T2 "
    "ON T1.CustomerID = T2.CustomerID GROUP BY T1.CustomerID "
    "ORDER BY SUM(T2.Consumption) ASC LIMIT 1",
    # Q163: mixed-case table and key.
    "SELECT T1.A2 FROM District AS T1 INNER JOIN Account AS T2 "
    "ON T1.District_id = T2.District_id GROUP BY T1.District_id "
    "ORDER BY COUNT(T2.Account_id) DESC LIMIT 1",
    # Q1323 corrected: backtick-quoted names.
    "SELECT E.`event_name` FROM `Event` AS E JOIN `Attendance` AS A "
    "ON E.`event_id` = A.`link_to_event` GROUP BY E.`event_id` "
    "HAVING COUNT(*) > 20",
    # One join step (Q892): drivers' key driverId = the grouped r.driverId.
    "SELECT d.forename, d.surname, SUM(r.points) FROM results r "
    "JOIN drivers d ON d.driverId = r.driverId GROUP BY r.driverId",
    # One join step (Q1381): member_id = the grouped link_to_member.
    "SELECT T1.first_name, T1.last_name FROM member AS T1 INNER JOIN "
    "attendance AS T2 ON T1.member_id = T2.link_to_member "
    "GROUP BY T2.link_to_member HAVING COUNT(T2.link_to_event) > 7",
    # Q309: one key among several grouped columns.
    "SELECT T1.atom_id, COUNT(DISTINCT T2.bond_type), T1.molecule_id "
    "FROM atom AS T1 INNER JOIN bond AS T2 ON T1.molecule_id = "
    "T2.molecule_id GROUP BY T1.atom_id, T2.bond_type",
    # A single table, unqualified columns.
    "SELECT name, COUNT(*) FROM users GROUP BY id",
    "SELECT name, COUNT(*) FROM users GROUP BY user_id",
    "SELECT name, COUNT(*) FROM user GROUP BY users_id",
    "SELECT name, COUNT(*) FROM users GROUP BY userid",
    "SELECT name, COUNT(*) FROM countries GROUP BY country_id",
    "SELECT name, COUNT(*) FROM matches GROUP BY match_id",
    "SELECT name, COUNT(*) FROM box GROUP BY boxes_id",
    "SELECT name, COUNT(*) FROM main.users GROUP BY ID",
    'SELECT "name", COUNT(*) FROM "Users" GROUP BY "User_Id"',
])
def test_a_column_of_a_table_whose_key_is_grouped_is_quiet(sql):
    assert GB not in _rules(sql)
    if '"' not in sql:  # MySQL reads "..." as a string
        assert GB not in _rules(sql, dialect="mysql")


@pytest.mark.parametrize("sql", [
    # Q994: GROUP BY a name. Unique in today's data, but never declared.
    "SELECT SUM(T1.points), T2.name, T2.nationality FROM constructorResults "
    "AS T1 INNER JOIN constructors AS T2 ON T1.constructorId = "
    "T2.constructorId GROUP BY T2.name",
    # Two tables with an `id`: the GROUP BY is the other table's.
    "SELECT p.title, COUNT(*) FROM posts p JOIN users u "
    "ON p.user_id = u.id GROUP BY u.id",
    # The many side of the join: attendance has many rows per event.
    "SELECT T2.link_to_member, COUNT(*) FROM event AS T1 JOIN attendance "
    "AS T2 ON T1.event_id = T2.link_to_event GROUP BY T1.event_id",
    # Unqualified with two tables: can't tell whose column it is.
    "SELECT event_name, COUNT(*) FROM event e JOIN attendance a "
    "ON e.event_id = a.link_to_event GROUP BY e.event_id",
    # A key-like name of another table: order_items isn't orders.
    "SELECT product, COUNT(*) FROM order_items GROUP BY order_id",
    # `<table>_<word>_id` isn't the naming fallback (Q1026, Q1028).
    "SELECT t.team_long_name, COUNT(*) FROM Match m JOIN Team t "
    "ON m.away_team_api_id = t.team_api_id GROUP BY m.away_team_api_id",
    # A CTE or a derived table has no known key, whatever its name.
    "WITH event AS (SELECT * FROM attendance) "
    "SELECT event_name, COUNT(*) FROM event GROUP BY event_id",
    "SELECT x.event_name, COUNT(*) FROM (SELECT * FROM attendance) x "
    "GROUP BY x.id",
    # The same table twice, no aliases: don't guess which.
    "SELECT event.name, COUNT(*) FROM event JOIN event "
    "ON event.id = event.parent_id GROUP BY event.id",
    # An OR in ON doesn't pin the key to the group.
    "SELECT d.surname, SUM(r.points) FROM results r JOIN drivers d "
    "ON d.driverId = r.driverId OR d.code = r.code GROUP BY r.driverId",
    # The first bare column is fixed by the key, the second isn't.
    "SELECT e.event_name, a.member, COUNT(*) FROM event e JOIN attendance a "
    "ON e.event_id = a.link_to_event GROUP BY e.event_id",
    # The record's own bad example.
    "SELECT dept, emp, COUNT(*) FROM staff GROUP BY dept",
])
def test_a_column_not_fixed_by_a_key_still_fires(sql):
    assert GB in _rules(sql)


def test_the_finding_lands_on_the_column_that_is_not_fixed():
    sql = ("SELECT e.event_name, a.member, COUNT(*) FROM event e JOIN "
           "attendance a ON e.event_id = a.link_to_event GROUP BY e.event_id")
    [finding] = [f for f in check_text(sql, dialect="sqlite")["findings"]
                 if f["rule"] == GB]
    assert finding["col"] == sql.index("a.member") + 1
    assert finding["why"] == ("bare column not in GROUP BY returns an "
                              "arbitrary value (MySQL/SQLite)")
    assert finding["fix"] == "add it to GROUP BY or aggregate it"


@pytest.mark.parametrize("sql", [
    # The grouped column is on the side an outer join fills with NULL:
    # every user without an order lands in one NULL group, and u.name is
    # any one of them (on SQLite, one user is silently gone).
    "SELECT u.name, COUNT(o.id) FROM users u LEFT JOIN orders o "
    "ON o.user_id = u.id GROUP BY o.user_id",
    "SELECT u.name, COUNT(o.id) FROM orders o RIGHT JOIN users u "
    "ON u.id = o.user_id GROUP BY o.user_id",
    "SELECT u.name, COUNT(o.id) FROM users u FULL JOIN orders o "
    "ON u.id = o.user_id GROUP BY o.user_id",
])
def test_the_join_step_never_starts_on_a_nullable_side(sql):
    assert GB in _rules(sql)


@pytest.mark.parametrize("sql", [
    # The key grouped directly, outer join or not.
    "SELECT u.name, COUNT(o.id) FROM users u LEFT JOIN orders o "
    "ON o.user_id = u.id GROUP BY u.id",
    # The grouped column is on the preserved side: an unmatched order
    # gives one NULL user, never a mix of users.
    "SELECT u.name, COUNT(o.id) FROM orders o LEFT JOIN users u "
    "ON u.id = o.user_id GROUP BY o.user_id",
])
def test_an_outer_join_from_the_preserved_side_is_quiet(sql):
    assert GB not in _rules(sql)


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", None])
def test_dialects_that_reject_the_query_are_unchanged(dialect):
    for sql in ("SELECT name, COUNT(*) FROM users GROUP BY id",
                "SELECT dept, emp, COUNT(*) FROM staff GROUP BY dept"):
        assert GB not in _rules(sql, dialect=dialect)


TEAM = """version: 2
dataset: team
kind: table
grain: one row per team
keys: [team_api_id]
columns:
  team_api_id: {type: integer}
  team_long_name: {type: string}
"""

EVENT_BY_CODE = """version: 2
dataset: event
kind: table
grain: one row per event code
keys: [event_code]
columns:
  event_code: {type: string}
  event_id: {type: integer}
  event_name: {type: string}
"""


def _in_project(tmp_path, contracts):
    keylookup.clear_cache()
    cdir = tmp_path / "contracts"
    cdir.mkdir(exist_ok=True)
    for name, text in contracts.items():
        (cdir / f"{name}.yaml").write_text(text, encoding="utf-8")
    return str(tmp_path / "q.sql")


Q1028 = ("SELECT teamInfo.team_long_name FROM League AS leagueData INNER JOIN "
         "Match AS matchData ON leagueData.id = matchData.league_id INNER JOIN "
         "Team AS teamInfo ON matchData.away_team_api_id = teamInfo.team_api_id "
         "GROUP BY matchData.away_team_api_id ORDER BY COUNT(*) DESC LIMIT 1")


def test_a_contract_key_one_join_away_quiets_q1028(tmp_path):
    assert GB in _rules(Q1028)  # no contract: team_api_id isn't named as a key
    path = _in_project(tmp_path, {"team": TEAM})
    res = check_text(Q1028, dialect="sqlite", path=path)
    assert GB not in [f["rule"] for f in res["findings"]]
    assert GB in res["key_lookups"]  # the receipt says the keys were used


def test_a_contract_key_on_a_nullable_side_still_fires(tmp_path):
    path = _in_project(tmp_path, {"team": TEAM})
    sql = ("SELECT t.team_long_name, COUNT(m.id) FROM Team t LEFT JOIN "
           "Match m ON m.away_team_api_id = t.team_api_id "
           "GROUP BY m.away_team_api_id")
    assert GB in _rules(sql, path=path)
    assert GB not in _rules(sql.replace("LEFT JOIN", "JOIN"), path=path)


def test_a_contract_key_grouped_directly_is_quiet(tmp_path):
    path = _in_project(tmp_path, {"team": TEAM})
    sql = "SELECT team_long_name, COUNT(*) FROM team GROUP BY team_api_id"
    assert GB not in _rules(sql, path=path)


def test_a_declared_key_wins_over_the_naming_fallback(tmp_path):
    # The contract says event_code is the key, so event_id isn't trusted.
    sql = "SELECT event_name, COUNT(*) FROM event GROUP BY event_id"
    assert GB not in _rules(sql)
    path = _in_project(tmp_path, {"event": EVENT_BY_CODE})
    assert GB in _rules(sql, path=path)
    assert GB not in _rules(sql.replace("event_id", "event_code"), path=path)


def test_a_draft_contract_key_quiets_nothing(tmp_path):
    path = _in_project(tmp_path, {"team": TEAM.replace(
        "version: 2\n", "version: 2\nstatus: draft\n")})
    assert GB in _rules(Q1028, path=path)


def test_a_primary_key_in_create_table_is_not_read():
    # Each statement is checked on its own: a key declared in another
    # statement isn't known (only contracts and dbt tests are).
    sql = ("CREATE TABLE team (team_api_id INTEGER PRIMARY KEY, "
           "team_long_name TEXT);\n"
           "SELECT team_long_name, COUNT(*) FROM team GROUP BY team_api_id;")
    assert GB in _rules(sql)


def test_key_forms():
    assert {"id", "user_id", "userid", "users_id", "usersid"} <= \
        _key_forms("users")
    assert {"country_id", "countries_id"} <= _key_forms("countries")
    assert "order_id" not in _key_forms("order_items")


# -- 3. sql/distinct-dedup-bandaid ---------------------------------------------

def _all(sql, dialect="sqlite"):
    return _rules(sql, dialect=dialect, include_disabled=True)


@pytest.mark.parametrize("sql", [
    # BIRD Q152, Q215, Q260, Q263 (corrected): DISTINCT inside IN (...).
    "SELECT COUNT(*) FROM atom WHERE molecule_id IN "
    "(SELECT DISTINCT molecule_id FROM bond WHERE bond_type = '#')",
    "SELECT COUNT(*) FROM t WHERE EXISTS "
    "(SELECT DISTINCT 1 FROM u WHERE u.id = t.id)",
    # Q145, Q1036, Q1457: the only aggregate is in a WHERE subquery.
    "SELECT DISTINCT client_id FROM loan WHERE amount > "
    "(SELECT AVG(amount) FROM loan)",
    # A DISTINCT scalar subquery next to an outer aggregate.
    "SELECT (SELECT DISTINCT name FROM u WHERE u.id = 1), COUNT(*) FROM t",
    # A DISTINCT CTE: not a derived table directly under the aggregate.
    "WITH ids AS (SELECT DISTINCT ID FROM Patient) SELECT COUNT(*) FROM ids",
    # Q349, Q84: the aggregates are in a joined subquery, not over DISTINCT.
    "SELECT DISTINCT c.name FROM cards c JOIN (SELECT uuid, COUNT(*) AS n "
    "FROM rulings GROUP BY uuid) r ON r.uuid = c.uuid",
    # A derived table whose outer SELECT aggregates only in a subquery.
    "SELECT s.x FROM (SELECT DISTINCT x FROM t) s "
    "WHERE s.x > (SELECT AVG(y) FROM u)",
])
def test_distinct_outside_the_aggregates_level_is_quiet(sql):
    assert DEDUP not in _all(sql)


@pytest.mark.parametrize("sql", [
    # A derived table feeding SUM in the SELECT directly above.
    "SELECT SUM(amount) FROM (SELECT DISTINCT o.id, o.amount FROM orders o "
    "JOIN items i ON i.order_id = o.id) d",
    "SELECT COUNT(*) FROM a JOIN (SELECT DISTINCT a_id FROM b) s "
    "ON s.a_id = a.id",
    # COUNT in the DISTINCT's own projection.
    "SELECT DISTINCT c.id, COUNT(o.id) FROM customers c JOIN orders o "
    "ON o.customer_id = c.id GROUP BY c.id",
    "SELECT DISTINCT SUM(amount) OVER (PARTITION BY customer_id) FROM orders",
    # A UNION branch of the derived table still feeds the SUM.
    "SELECT SUM(x) FROM (SELECT DISTINCT x FROM t UNION ALL "
    "SELECT x FROM u) s",
])
def test_distinct_over_its_own_aggregate_still_fires(sql):
    assert DEDUP in _all(sql)


def test_distinct_dedup_stays_off_by_default():
    sql = "SELECT SUM(x) FROM (SELECT DISTINCT x FROM t) d"
    assert DEDUP not in _rules(sql)


# -- 4. sql/keyset-pagination --------------------------------------------------

@pytest.mark.parametrize("sql,dialect", [
    ("SELECT name FROM t ORDER BY score DESC LIMIT 1 OFFSET 5", "sqlite"),
    ("SELECT name FROM t ORDER BY score DESC LIMIT 5, 1", "sqlite"),
    ("SELECT name FROM t ORDER BY score DESC LIMIT 5, 1", "mysql"),
    ("SELECT name FROM t ORDER BY score DESC OFFSET 5 ROWS "
     "FETCH FIRST 1 ROW ONLY", "postgres"),
])
def test_picking_the_nth_row_is_not_pagination(sql, dialect):
    assert KEYSET not in _all(sql, dialect=dialect)


@pytest.mark.parametrize("sql,dialect", [
    ("SELECT name FROM t ORDER BY id LIMIT 20 OFFSET 40", "sqlite"),
    ("SELECT name FROM t ORDER BY id LIMIT 40, 20", "mysql"),
    # BIRD Q31 picks two rows; only the literal 1 is quiet.
    ("SELECT name FROM t ORDER BY id LIMIT 9, 2", "sqlite"),
    # Placeholders: the page size is unknown, and a parameterised
    # LIMIT/OFFSET is the usual paging query, so it fires.
    ("SELECT name FROM t ORDER BY id LIMIT ? OFFSET ?", "sqlite"),
    ("SELECT name FROM t ORDER BY id OFFSET 40 ROWS "
     "FETCH NEXT 20 ROWS ONLY", "postgres"),
])
def test_page_sized_limits_still_fire(sql, dialect):
    assert KEYSET in _all(sql, dialect=dialect)


# -- 5. the SQL path check agrees with the pandas one ---------------------------

@pytest.mark.parametrize("literal,fires", [
    ("other/", True),
    ("./archive/", True),
    ("/mnt/other/", False),     # absolute vs a relative base: may be the base
    ("/srv/data/", False),
    ("~/data/", False),
    ("C:/data/", False),
    ("s3://bucket/raw/", False),  # a URL: another place
    ("data/", False),           # the declared base
    ("./data/", False),
])
def test_sql_and_pandas_agree_on_a_literal_in_front_of_a_path(
        tmp_path, literal, fires):
    sql = TC._rules(
        tmp_path / "sql", f"SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                          f"ON ('{literal}' || f.path) = d.full_path",
        {"contracts/fetch_log.yaml": TC.FETCH_LOG,
         "contracts/disk.yaml": TC.DISK})
    sql_fired = PATH in {f.rule for f in sql}
    pandas_fired = PATH in TP.fired(
        tmp_path / "py", TP.BOTH + f"p = '{literal}' + log['local_path']\n")
    assert sql_fired == pandas_fired == fires


def test_an_absolute_literal_against_an_absolute_base_still_fires(tmp_path):
    findings = TC._rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  "ON ('/mnt/other/' || f.path) = d.full_path",
        {"DATA.md": "# Data\n\n```tq-config\nbases:\n  data_root: /srv/data\n"
                    "```\n",
         "contracts/fetch_log.yaml": TC.FETCH_LOG,
         "contracts/disk.yaml": TC.DISK})
    [finding] = [f for f in findings if f.rule == PATH]
    assert finding.why.startswith("the query puts '/mnt/other/' in front of "
                                  "fetch_log.path")


def test_prefix_elsewhere():
    assert _prefix_elsewhere("/mnt/x/", "./data")
    assert _prefix_elsewhere("https://cdn.example.org/", "/srv/data")
    assert not _prefix_elsewhere("/mnt/x/", "/srv/data")
    assert not _prefix_elsewhere("other/", "./data")


# -- 1. init --from: a failure-named status suggests absent_unexpected ---------

LOG = ("url,status,local_path\n"
       "https://a.example/1,fetched,files/1.html\n"
       "https://a.example/2,failed,\n"
       "https://a.example/3,timeout,\n"
       "https://a.example/4,released,\n"
       "https://a.example/5,removed,\n"
       "https://a.example/6,fetched,files/6.html\n"
       "https://a.example/7,failed,\n"
       "https://a.example/8,fetched,files/8.html\n"
       "https://a.example/9,released,\n"
       "https://a.example/10,removed,\n")


def _plan(tmp_path, text):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "fetch.csv").write_text(text, encoding="utf-8")
    [draft] = IF.plan(str(tmp_path), [str(logs)])["drafts"]
    return draft


def test_failure_named_values_get_a_suggestion_not_an_answer(tmp_path):
    draft = _plan(tmp_path, LOG)
    status = draft["contract"]["columns"]["status"]
    assert status["values"] == {v: {"means": "TODO"} for v in (
        "failed", "fetched", "released", "removed", "timeout")}
    assert status["status"] == "draft"
    assert draft["contract"]["status"] == "draft"
    [question] = [q for q in draft["questions"] if q.startswith("status:")]
    assert "Suggested for failed, timeout: absent_unexpected" in question
    assert "nobody wants it" in question
    assert "we wanted it and didn't get it" in question
    comment = draft["comments"][("status", "values")]
    assert "suggested for failed, timeout: absent_unexpected" in comment


def test_init_from_writes_means_todo_in_the_file(tmp_path, capsys):
    from truequery import cli
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "fetch.csv").write_text(LOG, encoding="utf-8")
    assert cli.main(["init", str(tmp_path), "--from",
                     str(tmp_path / "logs")]) == 0
    text = (tmp_path / "contracts" / "fetch.yaml").read_text(encoding="utf-8")
    assert "absent_unexpected" not in text.replace(
        "suggested for failed, timeout: absent_unexpected", "")
    assert "means: TODO" in text
    out = capsys.readouterr().out
    assert "Suggested for failed, timeout: absent_unexpected" in out


def test_no_failure_value_no_suggestion(tmp_path):
    draft = _plan(tmp_path, LOG.replace("failed", "skipped")
                  .replace("timeout", "queued"))
    [question] = [q for q in draft["questions"] if q.startswith("status:")]
    assert "Suggested" not in question
    assert "suggested" not in draft["comments"][("status", "values")]


@pytest.mark.parametrize("value,named", [
    ("failed", True), ("FAILED", True), ("fetchFailed", True),
    ("http_error", True), ("errored", True), ("timed_out", True),
    ("TimedOut", True), ("fetch-timeout", True), ("lost", True),
    ("rejected", True), ("aborted", True),
    ("no_error", False), ("error_free", False), ("not_failed", False),
    ("fetched", False), ("released", False), ("removed", False),
    ("removed_on_purpose", False), ("skipped", False), ("", False),
])
def test_names_failure(value, named):
    assert IF.names_failure(value) is named
