"""pandas checks that read contracts (0.2 part C2): the link from a
DataFrame to a contract, the three new py/ checks, and C1's dq/ and sem/
checks on pandas code. A finding where the code contradicts the contract;
none where it's fine, where the link isn't certain, or where there's no
contract."""
import pytest

from truequery import engine as E
from truequery import std_registry
from truequery.analyzers import pandas_checks as pc
from truequery.analyzers import pandas_contracts as PC

DATA_MD = ("# Data\n\n```tq-config\nbases:\n  data_root: ./data\n"
           "  remote: {env: DATA_ROOT, default: /srv/data}\n```\n")

ORDERS = """version: 2
dataset: orders
kind: files
location: {path: data/orders.csv, format: csv}
grain: one row per order
keys: [order_id]
columns:
  order_id: {type: string, nullable: false}
  amount_cents: {type: integer, unit: minor, currency: USD}
  fee_cents: {type: integer, unit: minor, currency: USD, nullable: false}
  discount_usd: {type: number, unit: major, currency: USD}
  qty: {type: integer}
  status:
    type: string
    values: [paid, refunded]
"""

FETCH_LOG = """version: 2
dataset: fetch_log
kind: files
location: {path: logs/fetch/*.csv, format: csv}
grain: one row per fetch
keys: [[run_id, url]]
columns:
  run_id: {type: string}
  url: {type: string}
  fetched_at: {type: timestamp, time_zone: UTC}
  status:
    type: string
    values:
      fetched: {means: present}
      released: {means: absent_expected}
      removed_on_purpose: {means: absent_expected}
      failed: {means: absent_unexpected}
  local_path: {type: path, relative_to: data_root, status_column: status}
"""

DISK = """version: 2
dataset: disk_files
kind: files
location: {path: logs/disk.csv, format: csv}
grain: one row per file
keys: [full_path]
columns:
  full_path: {type: path}
  seen_at: {type: timestamp, time_zone: "local:Europe/Prague"}
"""

SHARDS = """version: 2
dataset: shards
kind: files
location: {path: shards.parquet, base: data_root, format: parquet}
grain: one row per shard
keys: [shard_id]
columns:
  shard_id: {type: string}
  remote_path: {type: path, relative_to: remote}
"""

EVENTS = """version: 2
dataset: events
kind: table
grain: one row per event
keys: [event_id]
write_mode: append
columns:
  event_id: {type: string}
"""

LATEST = """version: 2
dataset: latest
kind: table
grain: one row per customer
keys: [customer_id]
write_mode: merge_on_key
columns:
  customer_id: {type: string}
"""

DAILY = """version: 2
dataset: daily
kind: files
location: {path: out/daily.csv, format: csv}
grain: one row per day
keys: [day]
write_mode: overwrite_partition
columns:
  day: {type: date}
"""

APPENDLOG = """version: 2
dataset: applog
kind: files
location: {path: out/applog.csv, format: csv}
grain: one row per line
keys: [line_id]
write_mode: append
columns:
  line_id: {type: string}
"""

APPENDDIR = """version: 2
dataset: batches
kind: files
location: {path: out/batches/*.csv, format: csv}
grain: one row per item
keys: [item_id]
write_mode: append
columns:
  item_id: {type: string}
"""

V1 = """table: legacy
grain: one row per thing
keys: [thing_id]
allowed_values:
  state: [open, closed]
"""

FILES = {
    "DATA.md": DATA_MD,
    "contracts/orders.yaml": ORDERS,
    "contracts/fetch_log.yaml": FETCH_LOG,
    "contracts/disk.yaml": DISK,
    "contracts/shards.yaml": SHARDS,
    "contracts/events.yaml": EVENTS,
    "contracts/latest.yaml": LATEST,
    "contracts/daily.yaml": DAILY,
    "contracts/applog.yaml": APPENDLOG,
    "contracts/batches.yaml": APPENDDIR,
    "contracts/legacy.yaml": V1,
}

HEAD = "import os\nimport sqlite3\nfrom pathlib import Path\n\nimport pandas as pd\n\n"


def _on(*slugs):
    return {slug: {"enabled": True,
                   "tier": std_registry.RECORDS[slug]["tier"],
                   "severity": std_registry.RECORDS[slug]["severity"]}
            for slug in slugs}


def _project(tmp_path, files):
    root = tmp_path / "proj"
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def run(tmp_path, code, files=None, rules=PC.RULES, path="job.py"):
    """Findings (rule, line, why) for one .py file in a project."""
    root = _project(tmp_path, {**(FILES if files is None else files),
                               path: HEAD + code})
    out = []
    for result in E.run([str(root / path)], str(root), settings=_on(*rules),
                        config={}):
        out += [(f.rule, f.line - HEAD.count("\n"), f.why)
                for f in result.findings if f.rule in rules]
    return sorted(out)


def fired(tmp_path, code, **kw):
    return sorted({r for r, _, _ in run(tmp_path, code, **kw)})


# -- every new and extended check is off by default --------------------------

def test_all_ship_off_by_default():
    for slug in PC.RULES:
        assert std_registry.RECORDS[slug]["enabled"] is False, slug
        assert std_registry.RECORDS[slug]["evidence"] == "contract", slug


def test_default_settings_read_no_contract(tmp_path, monkeypatch):
    """With the rules off (the default), contracts aren't even loaded."""
    calls = []
    from truequery import keylookup
    real = keylookup.declared_context_for
    monkeypatch.setattr(keylookup, "declared_context_for",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    root = _project(tmp_path, {**FILES, "job.py": HEAD +
                               "o = pd.read_csv('data/orders.csv')\n"})
    E.run([str(root / "job.py")], str(root), config={})
    assert calls == []


# -- the link -------------------------------------------------------------------

@pytest.mark.parametrize("read", [
    "pd.read_csv('data/orders.csv')",
    "pd.read_csv('./data/orders.csv')",
    "pd.read_csv(ORDERS)",
    "pd.read_csv(Path('data') / 'orders.csv')",
    "pd.read_csv(Path('data/orders.csv'))",
    "pd.read_csv(os.path.join('data', 'orders.csv'))",
    "pd.read_csv(filepath_or_buffer='data/orders.csv')",
])
def test_literal_paths_link(tmp_path, read):
    code = f"ORDERS = 'data/orders.csv'\no = {read}\nx = o[o.status == 'payed']\n"
    assert fired(tmp_path, code) == sorted([PC.READ_DTYPE, PC.UNDECLARED])


@pytest.mark.parametrize("read", [
    "pd.read_csv(path)",                       # a parameter
    "pd.read_csv(f'data/{name}.csv')",         # an f-string
    "pd.read_csv(os.path.join(DIR, name))",    # a join with a variable
    "pd.read_csv('data/other.csv')",           # no contract
    "pd.read_csv(TWICE)",                      # not a constant
    "pd.read_csv('data/orders.csv', **opts)",  # can't tell
    "pd.read_csv('data/orders.csv', names=['a', 'status'])",
])
def test_no_link_no_finding(tmp_path, read):
    code = ("DIR = 'data'\nTWICE = 'data/orders.csv'\nTWICE = 'x.csv'\n"
            "def f(path, name, opts):\n"
            f"    o = {read}\n    return o[o.status == 'payed']\n")
    assert fired(tmp_path, code) == []


def test_a_glob_location_and_a_base_location_link(tmp_path):
    code = ("log = pd.read_csv('logs/fetch/2026-10-01.csv', dtype=str)\n"
            "x = log[log.status == 'downloaded']\n"
            "sh = pd.read_parquet('data/shards.parquet')\n"
            "y = sh[sh.shard_id == 's1']\n")
    found = run(tmp_path, code)
    assert [(r, line) for r, line, _ in found] == [(PC.UNDECLARED, 2)]


def test_link_follows_row_filters_and_stops_at_renames(tmp_path):
    keep = ("o = pd.read_csv('data/orders.csv', dtype=str)\n"
            "p = o[o.qty > 1].sort_values('qty').head(10).copy()\n"
            "q = p.query('qty > 2').reset_index(drop=True)\n"
            "x = q[q['status'] == 'payed']\n")
    assert fired(tmp_path, keep) == [PC.UNDECLARED]
    for step in ("o.rename(columns={'state': 'status'})",
                 "o.assign(status=o.qty)",
                 "o.fillna('unknown')",
                 "o.merge(other, on='order_id')",
                 "pd.concat([o, o])"):
        code = ("o = pd.read_csv('data/orders.csv', dtype=str)\n"
                f"p = {step}\nx = p[p['status'] == 'payed']\n")
        assert fired(tmp_path, code) == [], step


def test_a_replaced_column_is_no_longer_the_contracts(tmp_path):
    for write in ("o['status'] = o['status'].str.lower().map(codes)",
                  "o.loc[o.qty > 1, 'status'] = 'payed'",
                  "o.status = 'x'",
                  "o.columns = ['a', 'status']",
                  "o.rename(columns={'s': 'status'}, inplace=True)"):
        code = ("o = pd.read_csv('data/orders.csv', dtype=str)\n"
                f"{write}\nx = o[o['status'] == 'payed']\n")
        assert fired(tmp_path, code) == [], write


def test_read_sql_of_one_contracted_table_links(tmp_path):
    files = {**FILES, "contracts/fetch_log.yaml": FETCH_LOG.replace(
        "kind: files\nlocation: {path: logs/fetch/*.csv, format: csv}\n", "")}
    code = ("con = sqlite3.connect('x.db')\n"
            "a = pd.read_sql('SELECT * FROM fetch_log', con)\n"
            "x = a[a.status == 'downloaded']\n"
            "b = pd.read_sql_table('fetch_log', con)\n"
            "y = b[b.status == 'downloaded']\n"
            # a join, or a column renamed in the SELECT: no link
            "c = pd.read_sql('SELECT * FROM fetch_log f JOIN runs r ON 1=1', con)\n"
            "z = c[c.status == 'downloaded']\n"
            "d = pd.read_sql('SELECT kind AS status FROM fetch_log', con)\n"
            "w = d[d.status == 'downloaded']\n")
    found = run(tmp_path, code, files=files)
    assert [(r, line) for r, line, _ in found] == [(PC.UNDECLARED, 3),
                                                  (PC.UNDECLARED, 5)]


def test_no_contracts_no_change(tmp_path):
    code = ("o = pd.read_csv('data/orders.csv')\n"
            "x = o[o.status == 'payed']\n"
            "o.to_sql('events', con, if_exists='append')\n")
    assert fired(tmp_path, code, files={"DATA.md": DATA_MD}) == []
    assert fired(tmp_path, code, files={}) == []


def test_notebook_style_and_unparseable_files(tmp_path):
    flat = "o = pd.read_csv('data/orders.csv')\nprint(o[o.status == 'payed'])\n"
    assert fired(tmp_path, flat) == sorted([PC.READ_DTYPE, PC.UNDECLARED])
    assert fired(tmp_path, "o = pd.read_csv('data/orders.csv'\n") == []


def test_v1_contract_values_feed_the_value_check_only(tmp_path):
    code = ("l = pd.read_sql('SELECT * FROM legacy', con)\n"
            "x = l[l.state == 'opened']\n"
            "l.to_sql('legacy', con, if_exists='replace')\n")
    assert fired(tmp_path, code) == [PC.UNDECLARED]


def test_a_draft_contract_says_so(tmp_path):
    files = {**FILES, "contracts/orders.yaml": ORDERS.replace(
        "grain: one row per order\n",
        "grain: one row per order\nstatus: draft\n"
        "inferred_from: 2,000-row sample\n")}
    code = "o = pd.read_csv('data/orders.csv')\nx = o[o.status == 'payed']\n"
    found = run(tmp_path, code, files=files)
    assert found and all(
        "(from draft contract orders, inferred from 2,000-row sample)" in why
        for _, _, why in found)


def test_messages_use_plain_words_not_contract_keys(tmp_path):
    code = ("o = pd.read_csv('data/orders.csv')\n"
            "x = o[o.status == 'payed']\n"
            "m = o['amount_cents'].fillna(0).mean()\n"
            "n = o['amount_cents'] + o['discount_usd']\n"
            "log = pd.read_csv('logs/fetch/a.csv', dtype=str)\n"
            "disk = pd.read_csv('logs/disk.csv', dtype=str)\n"
            "k = len(log[log.status != 'fetched'])\n"
            "t = log['fetched_at'] < disk['seen_at']\n"
            "p = 'other/' + log['local_path']\n"
            "e.to_sql('events', con, if_exists='replace')\n")
    found = run(tmp_path, code)
    assert {r for r, _, _ in found} == set(PC.RULES)
    for _, _, why in found:
        for key in ("absent_expected", "relative_to", "time_zone:", "unit:",
                    "currency:", "means:", "write_mode", "tq "):
            assert key not in why, (key, why)


# -- py/pandas-read-csv-contract-dtype -------------------------------------------

@pytest.mark.parametrize("args", [
    "",
    ", dtype={'qty': int}",
    ", usecols=['order_id', 'qty']",
    ", parse_dates=['x']",
])
def test_read_dtype_fires(tmp_path, args):
    code = f"o = pd.read_csv('data/orders.csv'{args})\n"
    assert fired(tmp_path, code) == [PC.READ_DTYPE]


@pytest.mark.parametrize("args", [
    ", dtype=str",
    ", dtype='string'",
    ", dtype=object",
    ", dtype=pd.StringDtype()",
    ", dtype={'order_id': 'string'}",
    ", dtype={'ORDER_ID': str}",
    ", dtype=types",                  # a dtype it can't read
    ", dtype={**types}",
    ", converters={'order_id': str}",
    ", usecols=['qty', 'status']",     # the key isn't read
    ", usecols=lambda c: c != 'x'",    # can't tell
    ", header=None",                   # columns are numbers
    ", **kwargs",
])
def test_read_dtype_quiet(tmp_path, args):
    code = (f"def f(types, kwargs):\n"
            f"    return pd.read_csv('data/orders.csv'{args})\n")
    assert fired(tmp_path, code) == []


def test_read_dtype_needs_a_string_key(tmp_path):
    # composite key, both strings: both named; a parquet read keeps types
    found = run(tmp_path, "a = pd.read_csv('logs/fetch/a.csv')\n"
                          "b = pd.read_parquet('data/shards.parquet')\n")
    assert [(r, line) for r, line, _ in found] == [(PC.READ_DTYPE, 1)]
    assert "'run_id' and 'url', the string keys" in found[0][2]
    # names= renames the columns: the key isn't among them
    assert fired(tmp_path, "o = pd.read_csv('data/orders.csv', "
                           "names=['a', 'b'])\n") == []


# -- py/pandas-fillna-zero-on-money ----------------------------------------------

@pytest.mark.parametrize("expr", [
    "o['amount_cents'].fillna(0)",
    "o.amount_cents.fillna(0.0).mean()",
    "o['amount_cents'].fillna(value=0)",
    "o.fillna(0)",
    "o.fillna({'amount_cents': 0, 'qty': 1})",
    "o[['amount_cents', 'qty']].fillna(0)",
])
def test_fillna_money_fires(tmp_path, expr):
    code = f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n"
    assert fired(tmp_path, code) == [PC.FILLNA_MONEY]


@pytest.mark.parametrize("expr", [
    "o['amount_cents'].fillna(0).sum()",   # sum is unchanged
    "o['fee_cents'].fillna(0)",            # nullable: false
    "o['qty'].fillna(0)",                  # not money
    "o['amount_cents'].fillna(-1)",        # not 0
    "o['amount_cents'].fillna(o['fee_cents'])",
    "o[['qty']].fillna(0)",
    "o.fillna({'qty': 0})",
    "other['amount_cents'].fillna(0)",     # not linked
])
def test_fillna_money_quiet(tmp_path, expr):
    code = f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n"
    assert fired(tmp_path, code) == []


def test_fillna_on_a_replaced_column_is_quiet(tmp_path):
    code = ("o = pd.read_csv('data/orders.csv', dtype=str)\n"
            "o['amount_cents'] = o['amount_cents'].astype(float) * 100\n"
            "x = o['amount_cents'].fillna(0)\n")
    assert fired(tmp_path, code) == []


# -- py/pandas-write-mode-violation ----------------------------------------------

@pytest.mark.parametrize("write", [
    "d.to_sql('events', con, if_exists='replace')",
    "d.to_sql(name='events', con=con, if_exists='replace', index=False)",
    "d.to_sql('latest', con, if_exists='append')",
    "d.to_csv('out/applog.csv')",                    # mode 'w' by default
    "d.to_csv('out/applog.csv', mode='w')",
    "d.to_parquet('out/applog.csv')",
    "d.to_csv('out/daily.csv', index=False)",
])
def test_write_mode_fires(tmp_path, write):
    assert fired(tmp_path, f"{write}\n") == [PC.WRITE_MODE]


@pytest.mark.parametrize("write", [
    "d.to_sql('events', con, if_exists='append')",
    "d.to_sql('events', con)",                       # fail: no overwrite
    "d.to_sql('latest_stage', con, if_exists='replace')",
    "d.to_sql('latest', con, if_exists='replace')",  # not an append
    "d.to_sql('events', con, if_exists=how)",        # can't tell
    "d.to_sql('events', con, **kw)",
    "d.to_csv('out/applog.csv', mode='a', header=False)",
    "d.to_csv('out/batches/2026-10-01.csv')",        # a new file per load
    "d.to_csv(path)",
    "d.to_parquet('out/daily.csv', partition_cols=['day'])",
    "d.to_sql('other', con, if_exists='replace')",   # no contract
])
def test_write_mode_quiet(tmp_path, write):
    code = f"def f(d, con, how, kw, path):\n    {write}\n"
    assert fired(tmp_path, code) == []


def test_write_mode_v1_and_no_write_mode_are_quiet(tmp_path):
    code = ("d.to_sql('orders', con, if_exists='replace')\n"
            "d.to_sql('legacy', con, if_exists='replace')\n")
    assert fired(tmp_path, code) == []


# -- dq/undeclared-status-value ----------------------------------------------------

@pytest.mark.parametrize("expr", [
    "o[o['status'] == 'payed']",
    "o[o.status != 'payed']",
    "o[o.status.eq('payed')]",
    "o[o['status'].isin(['paid', 'payed'])]",
    "o[(o.status == 'paid') | (o.status == 'refnded')]",
    "o.query(\"status == 'payed'\")",
    "o.query(\"status in ['paid', 'payed']\")",
    "o.loc[o.status == 'payed', 'qty']",
])
def test_undeclared_value_fires(tmp_path, expr):
    code = f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n"
    assert fired(tmp_path, code) == [PC.UNDECLARED]


@pytest.mark.parametrize("expr", [
    "o[o['status'] == 'paid']",
    "o[o.status.isin(['paid', 'refunded'])]",
    "o[o.status.str.lower() == 'payed']",     # transformed: not the column
    "o.query('status == @wanted')",
    "o.query('`status` == \"payed\"')",       # backticks: not parsed
    "o[o.qty == 'payed']",                    # no declared values
    "o[o.status == wanted]",
])
def test_undeclared_value_quiet(tmp_path, expr):
    code = (f"wanted = input()\n"
            f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n")
    assert fired(tmp_path, code) == []


def test_not_equal_to_an_undeclared_value_is_true_for_every_row(tmp_path):
    code = ("o = pd.read_csv('data/orders.csv', dtype=str)\n"
            "x = o[o.status != 'payed']\n")
    (_, _, why), = run(tmp_path, code)
    assert why.endswith("so the comparison is true for every row")


# -- dq/expected-absence-counted-as-missing ----------------------------------------

LOG = "log = pd.read_csv('logs/fetch/a.csv', dtype=str)\n"


@pytest.mark.parametrize("code", [
    "n = len(log[log.status != 'fetched'])",
    "n = log[log['status'] != 'fetched'].shape[0]",
    "n = log[log.status.ne('fetched')]['url'].count()",
    "n = (log.status != 'fetched').sum()",
    "n = (~(log.status == 'fetched')).sum()",
    "n = (~log.status.isin(['fetched'])).mean()",
    "missing = log[log.status != 'fetched']\nn = len(missing)",
    "m = log.status != 'fetched'\nn = m.sum()",
    "n = len(log.query(\"status != 'fetched'\"))",
    # one absent value left out, the other not
    "n = len(log[(log.status != 'fetched') & (log.status != 'released')])",
])
def test_counting_expected_absences_fires(tmp_path, code):
    assert fired(tmp_path, LOG + code + "\n") == [PC.ABSENCE]


@pytest.mark.parametrize("code", [
    "n = len(log[log.status == 'failed'])",
    "n = (log.status == 'failed').sum()",
    "n = len(log[(log.status != 'fetched') & "
    "~log.status.isin(['released', 'removed_on_purpose'])])",
    "n = len(log[(log.status != 'fetched') & log.status.isin(['failed'])])",
    "live = log[~log.status.isin(['released', 'removed_on_purpose'])]\n"
    "n = len(live[live.status != 'fetched'])",
    "live = log.query(\"status not in ['released', 'removed_on_purpose']\")\n"
    "n = (live.status != 'fetched').sum()",
    "x = log[log.status != 'fetched']",           # not counted
    "n = len(log)",
    "n = (log.url != 'x').sum()",                 # not a status column
])
def test_explicit_absence_handling_is_quiet(tmp_path, code):
    assert fired(tmp_path, LOG + code + "\n") == []


def test_exclusion_in_read_sql_where_is_kept(tmp_path):
    files = {**FILES, "contracts/fetch_log.yaml": FETCH_LOG.replace(
        "kind: files\nlocation: {path: logs/fetch/*.csv, format: csv}\n", "")}
    quiet = ("a = pd.read_sql(\"SELECT * FROM fetch_log WHERE status NOT IN "
             "('released', 'removed_on_purpose')\", con)\n"
             "n = (a.status != 'fetched').sum()\n")
    assert fired(tmp_path, quiet, files=files) == []
    loud = quiet.replace(" WHERE status NOT IN ('released', "
                         "'removed_on_purpose')", "")
    assert fired(tmp_path, loud, files=files) == [PC.ABSENCE]


# -- sem/unit-mismatch -------------------------------------------------------------

@pytest.mark.parametrize("expr", [
    "o['amount_cents'] + o['discount_usd']",
    "o.amount_cents - o.discount_usd",
    "o.fee_cents + o.amount_cents + o.discount_usd",
    "o['amount_cents'].sum() - o['discount_usd'].sum()",
    "o.amount_cents > o.discount_usd",
    "o.amount_cents.add(o.discount_usd)",
    "o.amount_cents.lt(o.discount_usd)",
])
def test_unit_mismatch_fires(tmp_path, expr):
    code = f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n"
    assert fired(tmp_path, code) == [PC.UNIT]


@pytest.mark.parametrize("expr", [
    "o['amount_cents'] + o['fee_cents']",
    "o['amount_cents'] / 100 - o['discount_usd']",
    "o['amount_cents'] - o['discount_usd'] * 100",
    "o['amount_cents'] + o['qty']",
    "o2 = o.assign(discount_usd=o.discount_usd * 100)\n"
    "x = o2.amount_cents - o2.discount_usd",
])
def test_unit_match_or_conversion_is_quiet(tmp_path, expr):
    code = f"o = pd.read_csv('data/orders.csv', dtype=str)\nx = {expr}\n"
    assert fired(tmp_path, code) == []


# -- sem/timezone-mismatch ---------------------------------------------------------

BOTH = ("log = pd.read_csv('logs/fetch/a.csv', dtype=str)\n"
        "disk = pd.read_csv('logs/disk.csv', dtype=str)\n")


def test_timezone_mismatch_fires_on_text(tmp_path):
    code = BOTH + "x = log[log['fetched_at'] < disk['seen_at']]\n"
    assert fired(tmp_path, code) == [PC.TZ]


@pytest.mark.parametrize("code", [
    # parsed: pandas compares instants (or raises), so not flagged
    "x = pd.to_datetime(log.fetched_at) < pd.to_datetime(disk.seen_at)",
    "x = log.fetched_at < log.fetched_at",
    "x = log.fetched_at < '2026-01-01'",
    "x = log.fetched_at < pd.Timestamp.now()",
])
def test_timezone_parsed_or_same_zone_is_quiet(tmp_path, code):
    assert fired(tmp_path, BOTH + code + "\n") == []


def test_parse_dates_makes_it_parsed(tmp_path):
    code = ("log = pd.read_csv('logs/fetch/a.csv', dtype=str, "
            "parse_dates=['fetched_at'])\n"
            "disk = pd.read_csv('logs/disk.csv', dtype=str, "
            "parse_dates=['seen_at'])\n"
            "x = log.fetched_at < disk.seen_at\n")
    assert fired(tmp_path, code) == []


# -- sem/relative-path-base-mismatch -------------------------------------------------

@pytest.mark.parametrize("code", [
    "p = 'other/' + log['local_path']",
    "p = 'other' + '/' + log['local_path']",
    "p = log['local_path'].apply(lambda x: os.path.join('other', x))",
    "p = log.local_path.map(lambda x: 'other/' + x)",
    "p = log.local_path.apply(lambda x: Path('other') / x)",
    "x = log[log.local_path == disk.full_path]",
    "x = log.merge(disk, left_on='local_path', right_on='full_path')",
])
def test_path_base_mismatch_fires(tmp_path, code):
    assert fired(tmp_path, BOTH + code + "\n") == [PC.PATH]


@pytest.mark.parametrize("code", [
    "p = 'data/' + log['local_path']",
    "p = './data' + '/' + log['local_path']",
    "p = log['local_path'].apply(lambda x: os.path.join('data', x))",
    "p = '/mnt/other/' + log['local_path']",   # absolute vs relative base
    "p = base + log['local_path']",            # not a literal
    "x = log[('data/' + log.local_path) == disk.full_path]",
    "p = log.local_path + '.bak'",
])
def test_path_with_the_declared_base_is_quiet(tmp_path, code):
    assert fired(tmp_path, "base = input()\n" + BOTH + code + "\n") == []


def test_a_base_read_from_the_environment_accepts_any_prefix(tmp_path):
    code = ("s = pd.read_parquet('data/shards.parquet')\n"
            "p = 'anything/' + s['remote_path']\n")
    assert fired(tmp_path, code) == []


# -- engine wiring ----------------------------------------------------------------------

def test_pandas_rules_include_the_contract_checks():
    assert set(PC.NEW_RULES) <= set(pc.RULES)
    assert set(PC.EXTENDED_RULES) <= set(pc.ALL_RULES)
    assert set(PC.EXTENDED_RULES).isdisjoint(pc.RULES)  # they have SQL proofs


# -- review of C2: a parameter is never a constant --------------------------------

@pytest.mark.parametrize("code", [
    "path = 'data/orders.csv'\ndef load(path):\n    return pd.read_csv(path)\n",
    "path = 'data/orders.csv'\ndef load(*, path):\n    return pd.read_csv(path)\n",
    "path = 'data/orders.csv'\ndef load(path, /):\n    return pd.read_csv(path)\n",
    "path = 'data/orders.csv'\ndef load(*path):\n    return pd.read_csv(path)\n",
    "path = 'data/orders.csv'\ndef load(**path):\n    return pd.read_csv(path)\n",
    "p = 'data/orders.csv'\nframes = list(map(lambda p: pd.read_csv(p), files))\n",
    "path = 'data/orders.csv'\ndef load(path=None):\n"
    "    if path is None:\n        path = 'data/orders.csv'\n"
    "    return pd.read_csv(path)\n",
    "table = 'events'\ndef save(d, con, table):\n"
    "    d.to_sql(table, con, if_exists='replace')\n",
    "VALID = ['payed']\ndef pick(VALID):\n"
    "    o = pd.read_csv('data/orders.csv', dtype=str)\n"
    "    return o[o.status.isin(VALID)]\n",
])
def test_a_parameter_named_like_a_constant_is_not_linked(tmp_path, code):
    assert fired(tmp_path, code) == []


def test_the_constant_still_links_where_no_parameter_shadows_it(tmp_path):
    code = ("path = 'data/orders.csv'\ndef load():\n"
            "    return pd.read_csv(path)\n")
    assert fired(tmp_path, code) == [PC.READ_DTYPE]


@pytest.mark.parametrize("prefix", ["s3://bucket/raw/", "gs://b/", "https://cdn.example.org/",
                                    "file:///srv/"])
def test_a_url_in_front_of_a_path_is_another_place(tmp_path, prefix):
    code = BOTH + f"p = '{prefix}' + log['local_path']\n"
    assert fired(tmp_path, code) == []
