"""Precision and recall pins from the pre-release code audit.

Each case is a query the checker once got wrong. Outer joins, generators
and NOT IN guards are judged on the AST, never on the query's text.
"""
import pytest

from truequery.analyzers.sql import check_text


def _rules(sql, dialect=None, path="<input>"):
    return [f["rule"] for f in check_text(sql, dialect=dialect,
                                          path=path)["findings"]]


@pytest.mark.parametrize("sql", [
    # An inner join whose ON mentions a column named left_id / right_ref.
    "select count(*) from orders o join items i on i.left_id = o.id;",
    "select count(*) from orders o join items i on i.right_ref = o.id;",
    # LEFT SEMI / ANTI joins add no nullable rows.
    "select count(*) from orders o left semi join items i on i.order_id = o.id;",
])
def test_count_star_ignores_non_outer_joins(sql):
    assert "sql/count-star-after-left-join" not in _rules(sql, "duckdb")


def test_count_star_still_fires_on_real_left_join():
    sql = "select count(*) from orders o left join items i on i.order_id = o.id;"
    assert "sql/count-star-after-left-join" in _rules(sql)


def test_inner_join_filter_with_outer_named_column_is_fine():
    sql = ("select o.id from orders o join customers c on c.id = o.outer_ref "
           "where c.region = 'EU';")
    assert "sql/left-join-null-rejecting-where" not in _rules(sql)


def test_left_join_filter_still_fires():
    sql = ("select o.id from orders o left join customers c on c.id = o.cid "
           "where c.region = 'EU';")
    assert "sql/left-join-null-rejecting-where" in _rules(sql)


def test_table_named_like_a_generator_is_still_a_table():
    # `range` inside price_ranges once skipped the cross-join check.
    sql = "select o.id, pr.label from orders o join price_ranges pr;"
    assert "sql/accidental-cross-join" in _rules(sql)


@pytest.mark.parametrize("sql,dialect", [
    ("select * from a, unnest(a.arr) as u(x);", "duckdb"),
    ("select * from a join generate_series(1, 3) g on true;", "duckdb"),
    ("select * from a, lateral flatten(input => a.v) f;", "snowflake"),
    # BigQuery implicit UNNEST of an array column of the left table.
    ("select c.id, x.url from contracts c, c.attachments as x;", "bigquery"),
    ("select x.url from contracts, contracts.attachments as x;", "bigquery"),
])
def test_generators_are_not_cross_joins(sql, dialect):
    assert "sql/accidental-cross-join" not in _rules(sql, dialect)


def test_not_in_guard_inside_or_is_no_guard():
    sql = ("select id from customers where id not in (select customer_id "
           "from orders where customer_id is not null or status = 'x');")
    assert "sql/not-in-nullable-subquery" in _rules(sql)


def test_not_in_guard_as_conjunct_is_a_guard():
    sql = ("select id from customers where id not in (select customer_id "
           "from orders where customer_id is not null and status = 'x');")
    assert "sql/not-in-nullable-subquery" not in _rules(sql)


@pytest.mark.parametrize("dialect", ["duckdb", "mysql", "bigquery",
                                     "snowflake"])
def test_slash_is_true_division_in_these_dialects(dialect):
    assert "sql/integer-division-truncation" not in _rules(
        "select 1 / 2 as half;", dialect)


@pytest.mark.parametrize("dialect", [None, "postgres", "sqlite", "tsql"])
def test_slash_truncates_in_these_dialects(dialect):
    assert "sql/integer-division-truncation" in _rules(
        "select 1 / 2 as half;", dialect)


def test_explicit_integer_division_operator_fires():
    assert "sql/integer-division-truncation" in _rules(
        "select 5 // 2 as q;", "duckdb")


@pytest.mark.parametrize("sql", [
    "select status, count(*) from orders group by 1;",
    "select o.status as s, count(*) from orders o group by s;",
    "select o.status, count(*) from orders o group by status;",
])
def test_group_by_positions_and_aliases(sql):
    assert "sql/group-by-bare-column" not in _rules(sql, "mysql")


def test_group_by_bare_column_still_fires():
    sql = "select status, region, count(*) from orders group by status;"
    assert "sql/group-by-bare-column" in _rules(sql, "mysql")


@pytest.mark.parametrize("dialect", [None, "bigquery", "spark", "databricks",
                                     "sqlite", "clickhouse", "snowflake"])
def test_comma_join_without_predicate_fires_in_every_dialect(dialect):
    # Some dialects parse a comma join into the same tree as CROSS JOIN.
    sql = "select a.id from accounts a, orders o;"
    assert "sql/accidental-cross-join" in _rules(sql, dialect)


@pytest.mark.parametrize("dialect", [None, "bigquery", "spark", "sqlite"])
def test_explicit_cross_join_is_intentional(dialect):
    sql = "select a.id from accounts a cross join (select 1 as n) d;"
    assert "sql/accidental-cross-join" not in _rules(sql, dialect)
    sql = "select a.id from accounts a cross join orders o;"
    assert "sql/accidental-cross-join" not in _rules(sql, dialect)


def test_comma_join_with_where_link_is_fine_in_bigquery():
    sql = "select a.id from accounts a, orders o where o.account_id = a.id;"
    assert "sql/accidental-cross-join" not in _rules(sql, "bigquery")


def test_non_utf8_file_is_listed_not_checked(tmp_path):
    from truequery.engine import run
    (tmp_path / "latin1.sql").write_bytes("select 'Mäx' from t;\n".encode("latin-1"))
    (tmp_path / "ok.sql").write_text("select 1;\n", encoding="utf-8")
    results = run([str(tmp_path)], str(tmp_path))
    by_name = {r.rel: r for r in results}
    assert not by_name["latin1.sql"].verified
    assert by_name["ok.sql"].verified



@pytest.mark.parametrize("sql,dialect", [
    ("insert into u (id, x) values (1, null) "
     "on conflict (id) do update set x = null;", "postgres"),
    ("insert into u (id, x) values (1, null) "
     "on duplicate key update x = null;", "mysql"),
])
def test_upsert_set_null_is_an_assignment(sql, dialect):
    assert "sql/null-equality-comparison" not in _rules(sql, dialect)


@pytest.mark.parametrize("sql", [
    "select order_id from orders join customers on customer_id = cust_id;",
    'select c.name from "orders" join customers c on orders.customer_id = c.id;',
    "select order_id from orders, customers where customer_id = cust_id;",
])
def test_unqualified_and_quoted_links_are_links(sql):
    assert "sql/accidental-cross-join" not in _rules(sql)


def test_one_sided_on_still_fires():
    sql = "select * from orders o join customers c on o.flag = 1;"
    assert "sql/accidental-cross-join" in _rules(sql)


def test_update_from_with_unqualified_target_column_is_linked():
    sql = ("update accounts set balance = p.amount from payments p "
           "where p.account_ref = account_no;")
    assert "sql/unguarded-update-delete" not in _rules(sql, "postgres")


def test_update_from_cartesian_names_tables_plainly():
    sql = "update accounts set balance = 0 from payments p where p.ok;"
    [finding] = [f for f in check_text(sql, dialect="postgres")["findings"]
                 if f["rule"] == "sql/unguarded-update-delete"]
    assert "U:" not in finding["why"] and "U:" not in finding["fix"]


@pytest.mark.parametrize("sql", [
    # A correlated subquery is its own scope.
    "select c.id from customers c left join orders o on o.customer_id = c.id "
    "where not exists (select 1 from refunds r where r.order_id = o.id);",
    "select c.id from customers c left join orders o on o.customer_id = c.id "
    "where coalesce(lower(o.status), 'none') = 'none';",
])
def test_left_join_null_tolerant_filters(sql):
    assert "sql/left-join-null-rejecting-where" not in _rules(sql)


def test_not_in_single_source_qualified_guard():
    sql = ("select id from customers where id not in (select o.customer_id "
           "from orders o where customer_id is not null);")
    assert "sql/not-in-nullable-subquery" not in _rules(sql)


def test_not_in_every_union_branch_must_be_guarded():
    sql = ("select id from customers where id not in ("
           "select customer_id from orders where customer_id is not null "
           "union all select customer_id from returns);")
    assert "sql/not-in-nullable-subquery" in _rules(sql)


def test_row_number_without_order_by_fires():
    sql = ("select * from (select *, row_number() over (partition by "
           "customer_id) as rn from orders) x where rn = 1;")
    assert "sql/row-number-dedupe-tiebreaker" in _rules(sql)


def test_group_by_qualified_key_matches_bare_projection():
    sql = "select customer_id, count(*) from orders o group by o.customer_id;"
    assert "sql/group-by-bare-column" not in _rules(sql, "mysql")


def test_schema_named_like_alias_is_not_an_unnest_outside_bigquery():
    sql = "select s.id, c.name from sales.orders sales, sales.customers c;"
    assert "sql/accidental-cross-join" in _rules(sql, "postgres")


@pytest.mark.parametrize("path,fires", [
    ("reports/drill-down.sql", True),
    ("migrations/000157_drop_cache.down.sql", False),
    ("db/rollback/cache.sql", False),
])
def test_down_migration_paths(path, fires):
    assert ("sql/unguarded-update-delete"
            in _rules("delete from report_cache;", path=path)) is fires


@pytest.mark.parametrize("sql,fires", [
    ("select * from orders where ordered_at between '2025-03-01' and '2025-03-31';", True),
    ("select * from orders where created_ts between date '2025-03-01' and date '2025-03-31';", True),
    ("select * from orders where order_date between '2025-03-01' and '2025-03-31';", False),
])
def test_between_fires_on_timestamp_columns_only(sql, fires):
    assert ("sql/between-date-on-timestamp" in _rules(sql, "duckdb")) is fires


def test_cte_lookup_resolves_the_table_not_its_alias(tmp_path):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "orders.yaml").write_text(
        "table: orders\ngrain: one row per order\nkeys: [id]\n",
        encoding="utf-8")
    path = str(tmp_path / "q.sql")
    one_to_one = ("with o1 as (select id, total from orders o where total > 0) "
                  "select c.id, count(*) from customers c "
                  "left join o1 on o1.id = c.last_order_id group by c.id;")
    fan_out = ("with o2 as (select orders.id from orders join order_items i "
               "on i.order_id = orders.id) select c.id, count(*) from customers c "
               "left join o2 on o2.id = c.last_order_id group by c.id;")
    assert "sql/count-star-after-left-join" not in _rules(one_to_one, path=path)
    assert "sql/count-star-after-left-join" in _rules(fan_out, path=path)


def test_keys_are_not_borrowed_from_unrelated_folders(tmp_path):
    from truequery import keylookup
    keylookup._CACHE.clear()
    other = tmp_path / "old_projects" / "acme" / "models"
    other.mkdir(parents=True)
    (other / "schema.yml").write_text(
        "models:\n  - name: orders\n    columns:\n      - name: customer_id\n"
        "        tests: [unique]\n", encoding="utf-8")
    sql = ("select c.id, count(*) from customers c left join orders o "
           "on o.customer_id = c.id group by c.id;")
    assert "sql/count-star-after-left-join" in _rules(
        sql, path=str(tmp_path / "q.sql"))



def test_jinja_inside_identifiers_and_strings_still_parses():
    sql = ("select case when model_type = '{{ t }}' then 1 end as is_{{ t }}_model,\n"
           "  count(*) as n\nfrom {{ ref('models') }} m\n"
           "left join {{ ref('conv') }} c on c.id = m.id\n")
    res = check_text(sql)
    assert res["verified"], res["diagnostics"]


def test_nested_jinja_in_config_string_is_one_tag():
    sql = ("{{\n  config(\n    post_hook=\"{{ insert(this, kind='nodes') }}\"\n  )\n}}\n"
           "select 1 as x\n")
    assert check_text(sql)["verified"]
