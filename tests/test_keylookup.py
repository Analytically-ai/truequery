"""Lookup-join suppression for sql/count-star-after-left-join (Fix 3b).

A LEFT JOIN whose ON covers a declared UNIQUE key of the nullable side
matches at most one row, so COUNT(*) stays correct and the rule stays
silent. Partial coverage or no declared key still fires.
"""
import os

from truequery.analyzers.sql import check_text

FX_CONTRACT = """\
table: fx_rates
grain: one row per currency and day
keys:
  - [currency, rate_date]
columns:
  currency: {type: text}
  rate_date: {type: date}
  rate_to_usd: {type: numeric}
"""

# CTE over the contracted table, joined on the full key.
SUPPRESSED_Q = """\
with fx as (
    select rate_date, currency, rate_to_usd from raw.fx_rates
)
select o.day_utc, count(*) as n
from orders o
left join fx on fx.currency = o.currency and fx.rate_date = o.day_utc
group by 1;
"""

# Same shape, but the ON covers only half the key: still fires.
PARTIAL_Q = """\
with fx as (
    select rate_date, currency, rate_to_usd from raw.fx_rates
)
select o.day_utc, count(*) as n
from orders o
left join fx on fx.currency = o.currency
group by 1;
"""

# No contract at all: unchanged behaviour, fires.
NOKEY_Q = """\
select o.day_utc, count(*) as n
from orders o
left join rates r on r.currency = o.currency and r.day = o.day_utc
group by 1;
"""


def _layout(tmp_path):
    cdir = tmp_path / "contracts"
    cdir.mkdir()
    (cdir / "fx_rates.yaml").write_text(FX_CONTRACT, encoding="utf-8")
    (tmp_path / "dbt_project.yml").write_text("name: demo\n", encoding="utf-8")
    return str(tmp_path / "models" / "m.sql")


def _rules(res):
    return [f["rule"] for f in res["findings"]]


def test_full_key_lookup_suppressed(tmp_path):
    res = check_text(SUPPRESSED_Q, path=_layout(tmp_path))
    assert "sql/count-star-after-left-join" not in _rules(res)


def test_partial_key_coverage_still_fires(tmp_path):
    res = check_text(PARTIAL_Q, path=_layout(tmp_path))
    assert "sql/count-star-after-left-join" in _rules(res)


def test_no_declared_key_still_fires(tmp_path):
    res = check_text(NOKEY_Q, path=_layout(tmp_path))
    assert "sql/count-star-after-left-join" in _rules(res)


OR_Q = """\
with fx as (
    select rate_date, currency, rate_to_usd from raw.fx_rates
)
select o.day_utc, count(*) as n
from orders o
left join fx on fx.currency = o.currency or fx.rate_date = o.day_utc
group by 1;
"""

AND_EXTRA_Q = """\
with fx as (
    select rate_date, currency, rate_to_usd from raw.fx_rates
)
select o.day_utc, count(*) as n
from orders o
left join fx on fx.currency = o.currency
    and fx.rate_date = o.day_utc
    and fx.rate_to_usd > 0
group by 1;
"""


def test_or_predicate_still_fires(tmp_path):
    """`fx.c = o.c OR fx.d = o.d` can match several rows even when the
    key [currency, rate_date] is named — only AND-conjunct equalities
    count toward coverage."""
    res = check_text(OR_Q, path=_layout(tmp_path))
    assert "sql/count-star-after-left-join" in _rules(res)


def test_and_with_extra_predicate_suppressed(tmp_path):
    """Full-key AND coverage plus a non-equality conjunct still matches
    at most one row."""
    res = check_text(AND_EXTRA_Q, path=_layout(tmp_path))
    assert "sql/count-star-after-left-join" not in _rules(res)


def test_seed_sql_is_clean(tmp_path):
    """The demo seed's fx CTE resolves through the shipped contract."""
    from truequery import keylookup
    keylookup._CACHE.clear()
    path = os.path.join("examples", "shop", "dbt_repo",
                         "models", "marts", "fct_orders.sql")
    keys = keylookup.declared_keys_for(path)
    assert keys.get("fx_rates") == [frozenset({"currency", "rate_date"})]


ALT_KEYS_CONTRACT = """\
table: customers
grain: one row per customer
keys: [customer_id, email]
"""

ALT_Q = """\
select o.order_id, count(*) as n
from orders o
left join customers c on c.customer_id = o.customer_id
group by 1;
"""


def test_any_one_alternative_key_is_a_lookup(tmp_path):
    """`keys: [customer_id, email]` declares two keys, each unique on its
    own. Joining on either one matches at most one row."""
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "customers.yaml").write_text(
        ALT_KEYS_CONTRACT, encoding="utf-8")
    res = check_text(ALT_Q, path=str(tmp_path / "q.sql"))
    assert "sql/count-star-after-left-join" not in _rules(res)
