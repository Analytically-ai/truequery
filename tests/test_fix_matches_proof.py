"""Fix-text audit: every concrete rewrite a detector's
`fix` suggests, applied to proof data, returns the proof's `good_result`.

For all 21 implemented rules the canonical row runs the proof's `good`
query (the endorsed application of the fix) and asserts `good_result`.
Extra rows pin alternatives the fix text offers (endorsed) and the
alternatives it must NOT offer (wrong_answer: asserted to differ, with
the reason — e.g. OR-on-a-filtered-column, ELSE 0 under AVG). If a fix
string or a good query changes, these rows force a re-audit.
"""
import ast

import duckdb

from truequery import schema
from truequery.analyzers import sql as analyzer
from truequery.checks import sql as checks


def norm(value):
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    return value


def run(setup, query):
    con = duckdb.connect()
    try:
        con.execute(setup)
        return con.execute(query).fetchall()
    finally:
        con.close()


def records():
    return schema.load_all()


def test_canonical_fix_returns_good_result():
    """Every implemented rule: proof.good (the fix, applied) == good_result."""
    recs = records()
    assert set(checks.CHECKS) <= set(recs), "registry covers every check"
    for slug in sorted(checks.CHECKS):
        proof = recs[slug].proof
        assert proof is not None, f"{slug} has no proof"
        got = run(proof.setup, proof.good)
        assert norm(got) == norm(proof.good_result), (
            f"{slug}: endorsed fix returned {got!r}, proof says "
            f"{proof.good_result!r}")


def test_endorsed_alternatives():
    """Second rewrites the fix text offers also return good_result."""
    rows = [
        # not-in: excluding NULLs from the subquery is the documented
        # alternative to NOT EXISTS.
        ('sql/not-in-nullable-subquery',
         'CREATE TABLE users(id INT); INSERT INTO users VALUES (1), (2), (3);'
         'CREATE TABLE banned(user_id INT);'
         'INSERT INTO banned VALUES (2), (NULL);',
         'SELECT id FROM users WHERE id NOT IN '
         '(SELECT user_id FROM banned WHERE user_id IS NOT NULL) ORDER BY id',
         [(1,), (3,)]),
        # distinct bandaid: pre-aggregating before the join (the fix text)
        # matches the semi-join good_result on the proof data.
        ('sql/distinct-dedup-bandaid',
         'CREATE TABLE orders(order_id INT, cid INT, amt INT);'
         'INSERT INTO orders VALUES (1, 1, 10), (2, 1, 10), (3, 2, 30);'
         'CREATE TABLE tags(cid INT, tag VARCHAR);'
         'INSERT INTO tags VALUES (1, \'vip\'), (1, \'eu\'), (2, \'eu\');',
         'SELECT SUM(a) FROM (SELECT cid, SUM(amt) AS a FROM orders '
         'GROUP BY cid) o WHERE EXISTS '
         '(SELECT 1 FROM tags t WHERE t.cid = o.cid)',
         [(50,)]),
        # null-equality CASE branch: the searched-CASE rewrite.
        ('sql/null-equality-comparison',
         'CREATE TABLE customers(email VARCHAR);'
         'INSERT INTO customers VALUES (\'a@x\'), (NULL);',
         'SELECT COUNT(*) FROM customers WHERE CASE WHEN email IS NULL '
         'THEN 1 ELSE 0 END = 1',
         [(1,)]),
        # negative-filter (off by default): OR col IS NULL keeps NULLs.
        ('sql/negative-filter-drops-nulls',
         'CREATE TABLE orders(status VARCHAR);'
         'INSERT INTO orders VALUES (\'done\'), (\'cancelled\'), (NULL);',
         'SELECT COUNT(*) FROM orders WHERE status <> \'cancelled\' '
         'OR status IS NULL',
         [(2,)]),
    ]
    for slug, setup, query, expected in rows:
        assert norm(run(setup, query)) == norm(expected), (slug, query)


BEN_SETUP = (
    'CREATE TABLE customers(customer_id INT);'
    'INSERT INTO customers VALUES (1), (2);'
    'CREATE TABLE orders(order_id INT, customer_id INT, status VARCHAR,'
    ' amount INT);'
    'INSERT INTO orders VALUES (10, 1, \'completed\', 80), (11, 1, NULL, 20);'
)
BEN_RIGHT = [(1, 80), (2, 0)]


def test_left_join_or_on_filtered_column_is_still_wrong():
    """Owner dataset: Ben has an 80 completed order and a 20 NULL-status
    order. OR-on-the-filtered-column counts the NULL row (100, wrong);
    the ON-move and the join-key OR return 80 with no customer dropped."""
    bad = ("select c.customer_id, coalesce(sum(o.amount), 0) from customers c "
           "left join orders o on o.customer_id = c.customer_id "
           "where o.status = 'completed' group by 1 order by 1")
    assert norm(run(BEN_SETUP, bad)) == [(1, 80)]  # customer 2 vanishes
    on_move = ("select c.customer_id, coalesce(sum(o.amount), 0) from customers c "
               "left join orders o on o.customer_id = c.customer_id "
               "and o.status = 'completed' group by 1 order by 1")
    assert norm(run(BEN_SETUP, on_move)) == norm(BEN_RIGHT)
    or_key = ("select c.customer_id, coalesce(sum(o.amount), 0) from customers c "
              "left join orders o on o.customer_id = c.customer_id "
              "where o.status = 'completed' or o.customer_id is null "
              "group by 1 order by 1")
    assert norm(run(BEN_SETUP, or_key)) == norm(BEN_RIGHT)
    or_filtered = ("select c.customer_id, coalesce(sum(o.amount), 0) from customers c "
                   "left join orders o on o.customer_id = c.customer_id "
                   "where o.status = 'completed' or o.status is null "
                   "group by 1 order by 1")
    assert norm(run(BEN_SETUP, or_filtered)) == [(1, 100), (2, 0)]


def test_avg_else_zero_is_still_wrong():
    """ELSE 0 under AVG drags the mean; ELSE NULL preserves the subset
    mean. The SUM/AVG fix strings must name the right neutral value."""
    setup = ('CREATE TABLE tx(type VARCHAR, amt INT);'
             "INSERT INTO tx VALUES ('sale', 100), ('sale', 200), ('refund', 10);")
    else_zero = 'SELECT AVG(CASE WHEN type = \'sale\' THEN amt ELSE 0 END) FROM tx'
    else_null = 'SELECT AVG(CASE WHEN type = \'sale\' THEN amt ELSE NULL END) FROM tx'
    assert norm(run(setup, else_zero)) == [(100.0,)]
    assert norm(run(setup, else_null)) == [(150.0,)]


def _fix_for(sql):
    res = analyzer.check_text(sql, path='t.sql')
    return [f for f in res['findings']]


def test_fix_strings_teach_the_audited_rewrite():
    bad_join = ("select c.id from c left join o on o.cid = c.id "
                "where o.status = 'paid'")
    fixes = [f['fix'] for f in _fix_for(bad_join)
             if f['rule'] == 'sql/left-join-null-rejecting-where']
    assert len(fixes) == 1
    assert 'ON' in fixes[0]
    assert 'OR col IS NULL' not in fixes[0]
    assert 'right.key IS NULL' in fixes[0]
    sum_fixes = [f['fix'] for f in _fix_for(
        'SELECT SUM(CASE WHEN c THEN x END) FROM t')
        if f['rule'] == 'sql/case-without-else-in-aggregate']
    assert sum_fixes and 'ELSE 0' in sum_fixes[0]
    avg_fixes = [f['fix'] for f in _fix_for(
        'SELECT AVG(CASE WHEN c THEN x END) FROM t')
        if f['rule'] == 'sql/case-without-else-in-aggregate']
    assert avg_fixes and 'ELSE NULL' in avg_fixes[0]
