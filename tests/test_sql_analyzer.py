"""The SQL analyzer: positions, severities and deterministic output.
Proof bad/good pairs and edge-case probes are pinned here.
"""
import pytest

from truequery import schema
from truequery.analyzers import sql as analyzer

IMPL = {slug for slug, r in schema.load_all().items()
        if r.implemented and r.domain == "sql"}


def check(sql, **kwargs):
    kwargs.setdefault("path", "t.sql")
    kwargs.setdefault("include_disabled", True)
    return analyzer.check_text(sql, **kwargs)


def rules(res):
    return sorted(f["rule"] for f in res["findings"])


def proofs():
    recs = schema.load_all()
    return {s: r.proof for s, r in recs.items()
            if r.proof is not None and r.domain == "sql"
            and s != "sql/fanout-join-pre-aggregation"}  # contract: R2


@pytest.mark.parametrize("slug", sorted(proofs()))
def test_proof_pair(slug):
    proof = proofs()[slug]
    kwargs = {"dialect": "mysql"} if slug == "sql/group-by-bare-column" else {}
    bad = check(proof.bad, **kwargs)
    good = check(proof.good, **kwargs)
    assert slug in rules(bad), (slug, bad)
    assert rules(good) == [], (slug, good)


# -- edge cases (nested queries, false positives, tautology) ---------------------
NESTED = [
    ("with a as (select id from users where id not in (select uid from banned)) select * from a",
     "sql/not-in-nullable-subquery"),
    ("with a as (select o.id from orders o, items i) select * from a",
     "sql/accidental-cross-join"),
    ("with a as (select c.id from customers c left join orders o on o.cid = c.id where o.status = 'paid') select * from a",
     "sql/left-join-null-rejecting-where"),
    ("with a as (select * from t where x = null) select * from a",
     "sql/null-equality-comparison"),
    ("select * from (select o.id from orders o, items i) s",
     "sql/accidental-cross-join"),
    ("select * from (select c.id from customers c left join orders o on o.cid = c.id where o.status = 'paid') s",
     "sql/left-join-null-rejecting-where"),
    ("insert into t select id from users where id not in (select uid from banned)",
     "sql/not-in-nullable-subquery"),
    ("create table t as select o.id from orders o, items i",
     "sql/accidental-cross-join"),
]


@pytest.mark.parametrize("sql,rule", NESTED)
def test_nested_queries_fire(sql, rule):
    res = check(sql)
    assert rule in rules(res), (sql, res)


def test_set_null_is_assignment():
    assert rules(check("UPDATE orders SET status = NULL WHERE id = 1")) == []


def test_coalesce_predicate_is_null_tolerant():
    assert rules(check("SELECT c.id FROM customers c LEFT JOIN orders o "
                       "ON o.cid = c.id WHERE COALESCE(o.status, 'none') <> 'void'")) == []


def test_tautology_where_fires():
    res = check("DELETE FROM orders WHERE 1=1")
    assert rules(res) == ["sql/unguarded-update-delete"]


# -- held-out probes -------------------------------------------------------------
def test_using_join_is_clean():
    assert rules(check("select * from a join b using (id)")) == []


def test_merge_update_is_guarded():
    assert rules(check("merge into t using s on t.id = s.id "
                       "when matched then update set x = null")) == []


@pytest.mark.parametrize("sql", [
    "select c.id from c left join o on o.cid = c.id where o.amount between 1 and 10",
    "select c.id from c left join o on o.cid = c.id where o.note like '%x%'",
    "with x as (select * from c left join o on o.cid = c.id where o.flag in ('a','b')) select count(*) from x",
])
def test_null_rejecting_generalizes(sql):
    res = check(sql)
    assert "sql/left-join-null-rejecting-where" in rules(res), (sql, res)


def test_partial_comma_join_fires():
    res = check("select * from a, b, c where a.id = b.id")
    assert "sql/accidental-cross-join" in rules(res), res


def test_is_not_distinct_from_is_tolerant():
    assert rules(check("select * from a left join b on a.id = b.id "
                       "where a.x is not distinct from b.x")) == []


# -- more held-out probes ------------------------------------------------------------
def check_shipped(sql, **kwargs):
    kwargs.setdefault("path", "t.sql")
    return analyzer.check_text(sql, **kwargs)  # default rule set (no include_disabled)


def test_explicit_cross_join_is_clean():
    assert rules(check_shipped(
        "select d.day, s.store from days d cross join stores s")) == []


def test_comma_join_link_is_ast_based():
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select o.id from orders o, items i where o.price = 1"))
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select a.id from a, b where a.x = 1 and b.flag = true"))
    assert rules(check_shipped(
        "select a.id from a, b where a.id = b.id")) == []


@pytest.mark.parametrize("sql", [
    "select c.id from c left join o on o.cid = c.id where o.status = 'paid' or o.status is null",
    "select c.id from c left join o on o.cid = c.id where o.amt > 5 or o.amt is null",
    "select c.id from c left join o on o.cid = c.id where o.cid is null or o.amount > 5",
])
def test_or_is_null_exempt(sql):
    assert rules(check_shipped(sql)) == [], sql


def test_intdiv_failure_stays_unverified():
    res = check_shipped("select a // 2, b from t where x = null and")
    assert "sql/integer-division-truncation" in rules(res)
    assert res["verified"] is False
    assert res["diagnostics"], "parse failure must be recorded, never silent"


def test_negative_filter_off_by_default():
    assert rules(check_shipped("select * from t where id <> 5")) == []


def test_null_neq_reported_once():
    res = check_shipped("select * from t where x <> NULL")
    assert rules(res) == ["sql/null-equality-comparison"]


def test_row_number_needs_pick_one_filter():
    assert rules(check_shipped(
        "select *, row_number() over (order by id) rn from t")) == []
    assert "sql/row-number-dedupe-tiebreaker" in rules(check_shipped(
        "select * from (select *, row_number() over (order by id) rn from t) where rn = 1"))
    assert "sql/row-number-dedupe-tiebreaker" in rules(check_shipped(
        "select *, row_number() over (order by id) rn from t qualify rn = 1"))


def test_conditional_count_is_clean():
    assert rules(check_shipped(
        "select count(case when kind = 'a' then 1 end) from t")) == []
    assert "sql/case-without-else-in-aggregate" in rules(check_shipped(
        "select sum(case when kind = 'a' then amt end) from t"))


def test_null_guarded_case_and_same_level_count_star():
    """False positives seen in agent-written SQL: conditional SUMs wrapped
    in COALESCE (the NULL is the intended neutral value), and COUNT(*) in
    an unjoined pre-agg CTE with the LEFT JOIN in the outer query."""
    # Shape 1: COALESCE/IFNULL/NVL around the aggregate suppresses.
    assert rules(check_shipped(
        "SELECT COALESCE(SUM(CASE WHEN created_at >= '2024-01-01' "
        "THEN amount END), 0) FROM orders")) == []
    assert "sql/case-without-else-in-aggregate" in rules(check_shipped(
        "SELECT AVG(CASE WHEN c THEN x END) FROM t"))
    assert rules(check_shipped(
        "SELECT COALESCE(AVG(CASE WHEN c THEN x END), 0) FROM t")) == []
    assert rules(check_shipped(
        "SELECT IFNULL(SUM(CASE WHEN c THEN x END), 0) FROM t")) == []
    assert rules(check_shipped(
        "SELECT NVL(SUM(CASE WHEN c THEN x END), 0) FROM t")) == []
    # Unguarded aggregates still fire.
    assert "sql/case-without-else-in-aggregate" in rules(check_shipped(
        "SELECT SUM(CASE WHEN c THEN x END) FROM t"))
    # Shape 2: COUNT(*) in the CTE, LEFT JOIN outside -> clean.
    cte = ("WITH order_rev AS (SELECT customer_id, COUNT(*) AS n "
           "FROM orders GROUP BY customer_id) "
           "SELECT c.customer_id FROM customers c "
           "LEFT JOIN order_rev o ON o.customer_id = c.customer_id")
    assert rules(check_shipped(cte)) == []
    # Same-level COUNT(*) over a LEFT JOIN still fires.
    assert "sql/count-star-after-left-join" in rules(check_shipped(
        "SELECT c.customer_id, COUNT(*) FROM customers c "
        "LEFT JOIN orders o ON o.customer_id = c.customer_id "
        "GROUP BY c.customer_id"))
    # ... as does COUNT(*) over a LEFT JOIN inside a CTE body.
    assert "sql/count-star-after-left-join" in rules(check_shipped(
        "WITH a AS (SELECT c.id, COUNT(*) FROM c "
        "LEFT JOIN o ON o.id = c.id GROUP BY c.id) SELECT * FROM a"))


def test_alias_identity_uses_names_not_printed_sql():
    # g(d) and v(x) print with their column-alias lists; the link must
    # resolve the bare alias name.
    assert rules(check_shipped(
        "select d from generate_series(1,3) as g(d), t where t.k = g.d")) == []
    assert rules(check_shipped(
        "select * from (values (1),(2)) v(x), t where t.id = v.x")) == []
    # Quoted identifiers compare exactly.
    assert rules(check_shipped(
        'select "O".id from orders "O", items "I" where "I".oid = "O".id')) == []


def test_join_on_tautology_by_meaning():
    for on in ("true", "1=1", "a.id = a.id"):
        res = check_shipped(f"select * from a join b on {on}")
        assert "sql/accidental-cross-join" in rules(res), on
    assert rules(check_shipped(
        "select * from a join b on a.id = b.id")) == []


def test_lateral_correlation_is_a_link():
    assert rules(check_shipped(
        "select a.id, l.x from a left join lateral "
        "(select b.x from b where b.aid = a.id order by b.ts desc limit 1) "
        "l on true")) == []
    assert rules(check_shipped(
        "select a.id from a, unnest(a.tags) u")) == []  # comma form too
    # uncorrelated ON TRUE still fires
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select * from a join b on true"))


def test_cartesian_dml_fires():
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "delete from t using s where s.flag = 1"))
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "update t set x = s.x from s where s.ok"))
    assert rules(check_shipped(
        "delete from t using s where t.id = s.id")) == []
    assert rules(check_shipped(
        "update t set x = s.x from s where t.id = s.id")) == []


def test_m6_precision_fixes():
    """Real-project verdicts pinned. ON that references the joined table
    links even when the other side is unqualified (cal-itp, mattermost
    matview backfills); ON referencing only the far side still fires."""
    assert rules(check_shipped(
        "SELECT a.x, d.pt FROM trips_counted a "
        "LEFT JOIN dim_shapes_arrays d ON shape_array_key = d.key")) == []
    assert rules(check_shipped(
        "SELECT n FROM jsonb_array_elements_text(v) AS oid "
        "JOIN jsonb_to_recordset(o) AS names(id text, name text) "
        "ON oid = names.id")) == []
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select * from a join b on a.flag = 1"))
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select * from a join b on b.x = 5"))
    # dbt macro expansion is unknowable: no finding without evidence.
    assert rules(check_shipped(
        "select dep.value as p from latest_graph g, __tq_jinja")) == []
    # CASE scope is the record statement: SUM/AVG fire, MAX/MIN are the
    # conditional-extremum idiom (dbt-artifacts dim_dbt__current_models).
    assert "sql/case-without-else-in-aggregate" in rules(check_shipped(
        "SELECT SUM(CASE WHEN c THEN x END) FROM t"))
    assert "sql/case-without-else-in-aggregate" in rules(check_shipped(
        "SELECT AVG(CASE WHEN c THEN x END) FROM t"))
    assert rules(check_shipped(
        "SELECT MAX(CASE WHEN c THEN x END) FROM t")) == []
    assert rules(check_shipped(
        "SELECT MIN(CASE WHEN c THEN x END) FROM t")) == []
    # set-op flags projection stars only; COUNT(*) branches are clean.
    # (Rule off by default per D11, so these use check() = all implemented.)
    assert rules(check(
        "SELECT a, COUNT(*) FROM t GROUP BY a UNION ALL "
        "SELECT a, COUNT(*) FROM s GROUP BY a")) == []
    assert "sql/set-operation-column-position" in rules(check(
        "SELECT * FROM t UNION ALL SELECT * FROM s"))
    assert "sql/set-operation-column-position" in rules(check(
        "SELECT t.* FROM t UNION ALL SELECT s.* FROM s"))


def test_not_in_never_null_inference():
    """A subquery column provably non-null in the file never
    fires — inner-join equality (the 3 Spider has_pet shapes), WHERE
    equality, COUNT(*). OR-scope, LEFT-join and bare-nullable shapes
    still fire."""
    has_pet = ("SELECT major, age FROM student WHERE stuid NOT IN "
               "(SELECT T1.stuid FROM student AS T1 JOIN has_pet AS T2 "
               "ON T1.stuid = T2.stuid JOIN pets AS T3 ON T3.petid = T2.petid)")
    assert rules(check_shipped(has_pet)) == []
    assert rules(check_shipped(
        "SELECT id FROM t WHERE id NOT IN "
        "(SELECT id FROM s WHERE id = 5)")) == []
    assert rules(check_shipped(
        "SELECT id FROM t WHERE id NOT IN (SELECT count(*) FROM s)")) == []
    assert "sql/not-in-nullable-subquery" in rules(check_shipped(
        "SELECT id FROM users WHERE id NOT IN (SELECT uid FROM banned)"))
    assert "sql/not-in-nullable-subquery" in rules(check_shipped(
        "SELECT id FROM t WHERE id NOT IN "
        "(SELECT id FROM s WHERE id = 5 OR x = 1)"))
    assert "sql/not-in-nullable-subquery" in rules(check_shipped(
        "SELECT id FROM t WHERE id NOT IN "
        "(SELECT a.id FROM a LEFT JOIN b ON a.id = b.id)"))


def test_unguarded_skips_down_migrations():
    """Full-table resets in down/rollback migrations are the
    intent. Up-migrations and plain files still fire; `markdown/` is
    not a down migration."""
    assert rules(check_shipped(
        "UPDATE roles SET schemeid = NULL;",
        path="000157_backfill_roles_schemeid.down.sql")) == []
    assert rules(check_shipped(
        "DELETE FROM t;", path="migrations/down/003.sql")) == []
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "UPDATE roles SET schemeid = NULL;", path="000158_x.up.sql"))
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "DELETE FROM orders"))
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "DELETE FROM orders", path="docs/markdown/guide.sql"))


def test_on_subquery_never_links_outer_table():
    """`ON x.id NOT IN (SELECT ...)` references
    only the joined table — the outer table is unlinked, so it fires."""
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "SELECT x FROM users u JOIN x ON x.id NOT IN (SELECT uid FROM banned)"))


def test_positions_point_at_the_node():
    """Every finding points at the offending node, never at 1:1 by
    default. (line, col) pinned per rule; unimplemented records
    (fanout, sargable, random-sampling) have no detector to pin."""
    cases = [
        ("sql/not-in-nullable-subquery", {},
         "select id\nfrom a\nwhere id not in (select a_id from b)", 3, 7),
        ("sql/unguarded-update-delete", {},
         "select 1\nfrom t;\ndelete from t", 3, 1),
        ("sql/accidental-cross-join", {},
         "select *\nfrom a, b\nwhere a.x = 1", 2, 7),
        ("sql/left-join-null-rejecting-where", {},
         "select c.id\nfrom c left join o on o.cid = c.id\n"
         "where o.status = 'paid'", 3, 7),
        ("sql/null-equality-comparison", {},
         "select *\nfrom t\nwhere x = null", 3, 7),
        ("sql/count-star-after-left-join", {},
         "select a.id\nfrom a left join b on a.id = b.id\n"
         "where count(*) > 1", 3, 7),
        ("sql/nondeterministic-merge-keys", {},
         "merge into t\nusing s\non t.id = s.id and s.u = uuid()\n"
         "when matched then update set x = s.x", 3, 26),
        ("sql/set-operation-column-position", {},
         "select *\nfrom t1\nunion\nselect * from t2", 3, 1),
        ("sql/average-of-ratios", {},
         "select\navg(a / b)\nfrom t", 2, 1),
        ("sql/between-date-on-timestamp", {},
         "select *\nfrom t\nwhere created_at between '2024-01-01' and '2024-01-31'",
         3, 7),
        ("sql/distinct-dedup-bandaid", {},
         "select sum(x)\nfrom (select distinct y from t) s", 2, 23),
        ("sql/insert-explicit-column-list", {},
         "-- comment\ninsert into t\nselect 1, 2", 2, 1),
        ("sql/integer-division-truncation", {},
         "select 1\nfrom t\nwhere x > 1/2", 3, 11),
        ("sql/keyset-pagination", {},
         "select *\nfrom t\norder by id limit 10 offset 20", 3, 22),
        ("sql/limit-requires-order-by", {},
         "select *\nfrom t\nlimit 10", 3, 1),
        ("sql/natural-join-explicit-keys", {},
         "select *\nfrom a natural join b", 2, 21),
        ("sql/negative-filter-drops-nulls", {},
         "select *\nfrom t\nwhere id <> 5", 3, 7),
        ("sql/row-number-dedupe-tiebreaker", {},
         "select * from (\n"
         "select *, row_number() over (order by id) rn from t\n"
         ") where rn = 1", 2, 11),
        ("sql/case-without-else-in-aggregate", {},
         "select\nsum(case when k = 'a' then amt end)\nfrom t", 2, 15),
        ("sql/group-by-bare-column", {"dialect": "mysql"},
         "select a, b, count(*)\nfrom t\ngroup by a", 3, 1),
        ("sql/union-all-default", {},
         "select a\nfrom t1\nunion\nselect a from t2", 3, 1),
    ]
    for slug, kwargs, sql, line, col in cases:
        res = check(sql, include_disabled=True, **kwargs)
        got = [(f["line"], f["col"]) for f in res["findings"]
               if f["rule"] == slug]
        assert got == [(line, col)], (slug, got)


def test_recall_backlog():
    # ON referencing one side only is a cartesian, not a join
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select * from a join b on a.flag = 1"))
    assert rules(check_shipped(
        "select * from a join b on a.id = b.id and a.flag = 1")) == []
    # tautology by meaning through OR
    assert "sql/unguarded-update-delete" in rules(check_shipped(
        "delete from t where x = 1 or true"))
    assert "sql/accidental-cross-join" in rules(check_shipped(
        "select * from a join b on a.id = b.id or true"))
    # simple CASE WHEN NULL never matches
    assert "sql/null-equality-comparison" in rules(check_shipped(
        "select case x when null then 1 else 0 end from t"))
    assert rules(check_shipped(
        "select case when x is null then 1 else 0 end from t")) == []
    # IS NOT DISTINCT FROM a literal rejects NULLs like =
    assert "sql/left-join-null-rejecting-where" in rules(check_shipped(
        "select c.id from c left join o on o.cid = c.id "
        "where o.status is not distinct from 'x'"))
    assert rules(check_shipped(
        "select * from a left join b on a.id = b.id "
        "where a.x is not distinct from b.x")) == []
    # NOT IN over non-null literals cannot be poisoned
    assert rules(check_shipped(
        "select * from t where id not in (select 1 union all select 2)")) == []
    assert "sql/not-in-nullable-subquery" in rules(check_shipped(
        "select * from t where id not in (select a_id from b)"))


# -- positions, severity, determinism --------------------------------------------------
def test_findings_carry_positions_and_severity():
    res = check("select id from a where id not in (select a_id from b)")
    assert len(res["findings"]) == 1
    finding = res["findings"][0]
    assert finding["line"] == 1 and finding["col"] >= 1
    assert finding["severity"] == "error" and finding["mode"] == "build"
    assert finding["evidence"] == "file"


def test_explore_downgrades_error_to_warning():
    res = check("select id from a where id not in (select a_id from b)",
                mode="explore")
    assert res["findings"][0]["severity"] == "warning"


def test_output_is_deterministic():
    sql = open("tests/fixtures/dbt-orders/models/marts/orders.sql").read()
    first = check(sql)
    second = check(sql)
    assert first == second


def test_backtick_names_file_and_dialect():
    res = check("select `a` from t", path="q.sql")
    assert res["verified"] is False
    assert any("dialect" in d["message"] for d in res["diagnostics"])
    assert all("\x1b[" not in d["message"] for d in res["diagnostics"])


def _lines_for(sql, rule):
    res = analyzer.check_text(sql, path="q.sql", include_disabled=True)
    return sorted(f["line"] for f in res["findings"] if f["rule"] == rule)


def test_locator_two_occurrences_clean_first():
    # Fix 3a: the finding must point at the firing node, not at an
    # earlier clean occurrence of the same text.
    cases = [
        # (rule, sql, expected firing lines)
        ("sql/count-star-after-left-join",
         "with a as (\n    select k, count(*) as n from t group by 1\n),\n"
         "b as (\n    select o.k, count(*) as m\n"
         "    from o left join fx on fx.c = o.c\n    group by 1\n)\n"
         "select * from a join b using (k)",
         [5]),
        ("sql/left-join-null-rejecting-where",
         "select a.id\nfrom a\nleft join b on b.id = a.id\n"
         "left join c on c.id = a.id\nwhere c.x = 1",
         [5]),
        ("sql/null-equality-comparison",
         "select *\nfrom t\nwhere a is null\nand b = null",
         [4]),
        ("sql/not-in-nullable-subquery",
         "select *\nfrom t\n"
         "where a not in (select 1 union all select 2)\n"
         "and b not in (select c from u)",
         [4]),
        ("sql/unguarded-update-delete",
         "select * from t;\ndelete from t2",
         [2]),
        ("sql/case-without-else-in-aggregate",
         "select count(case when k = 'a' then 1 end) as n,\n"
         "sum(case when k = 'a' then x end) as s\nfrom t",
         [2]),
    ]
    for rule, sql, expected in cases:
        assert _lines_for(sql, rule) == expected, rule
