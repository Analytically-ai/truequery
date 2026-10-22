"""Materialize the dbt_repo models into warehouse.duckdb without dbt.

Expands the Jinja the models use -- {{ config(...) }}, {{ ref('x') }},
{{ source('raw','t') }}, and the three normalize_* macros from
macros/money_and_status.sql -- then creates staging views and mart
tables in dependency order. Deterministic: single-threaded, fixed order.

Run after gen_db.py:  python3 build_marts.py
"""
import os
import re

import duckdb

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "warehouse.duckdb")
REPO = os.path.join(HERE, "dbt_repo")

MACROS = {
    "normalize_order_status": (
        "case lower(trim({c})) when 'completed' then 'completed' "
        "when 'complete' then 'completed' when 'done' then 'completed' "
        "when 'canceled' then 'cancelled' else lower(trim({c})) end"),
    "normalize_payment_status": (
        "case lower(trim({c})) when 'success' then 'succeeded' "
        "when 'succeeded' then 'succeeded' else lower(trim({c})) end"),
    "normalize_subscription_status": (
        "case lower(trim({c})) when 'active' then 'active' "
        "when 'canceled' then 'cancelled' else lower(trim({c})) end"),
}

ORDER = [  # (relative sql path, materialization)
    ("models/staging/stg_customers.sql", "view"),
    ("models/staging/stg_orders.sql", "view"),
    ("models/staging/stg_payments.sql", "view"),
    ("models/staging/stg_refunds.sql", "view"),
    ("models/marts/fct_orders.sql", "table"),
    ("models/marts/dim_customers.sql", "table"),
    ("models/marts/fct_revenue_daily.sql", "table"),
    ("models/marts/fct_subscription_mrr.sql", "table"),
]


def expand(sql: str) -> str:
    sql = re.sub(r"\{\{\s*config\([^}]*\)\s*\}\}", "", sql)
    sql = re.sub(r"\{\{\s*ref\('([^']+)'\)\s*\}\}", r"\1", sql)
    sql = re.sub(r"\{\{\s*source\('[^']+',\s*'([^']+)'\)\s*\}\}",
                 r"\1", sql)
    for name, body in MACROS.items():
        sql = re.sub(r"\{\{\s*" + name + r"\('([^']+)'\)\s*\}\}",
                     lambda m: body.replace("{c}", m.group(1)), sql)
    if "{{" in sql or "{%" in sql:
        raise SystemExit("unexpanded Jinja left in model SQL")
    return sql.strip().rstrip(";")


def main():
    con = duckdb.connect(DB)
    con.execute("PRAGMA threads=1")
    # Reporting runs on the finance team's clock.
    con.execute("SET TimeZone='America/Los_Angeles'")
    for rel, kind in ORDER:
        name = os.path.basename(rel)[:-4]
        sql = expand(open(os.path.join(REPO, rel), encoding="utf-8").read())
        for stmt in (f"DROP VIEW IF EXISTS {name} CASCADE",
                     f"DROP TABLE IF EXISTS {name} CASCADE"):
            try:
                con.execute(stmt)
            except duckdb.CatalogException:
                pass  # object exists as the other kind; second drop gets it
        con.execute(f"CREATE {kind.upper()} {name} AS {sql}")
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"{kind:5} {name:24} rows={n}")
    con.close()
    print("marts built into", DB)


if __name__ == "__main__":
    main()
