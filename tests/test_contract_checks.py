"""The checks that read contracts/ and metrics/ (dq/*, sem/*), and the
session notice when a contract or metric changes."""
import json
import os

import pytest

from truequery import engine as E
from truequery import std_registry

CONTRACT_RULES = (
    "dq/undeclared-status-value",
    "dq/expected-absence-counted-as-missing",
    "sem/unit-mismatch",
    "sem/timezone-mismatch",
    "sem/relative-path-base-mismatch",
    "sem/metric-drift",
)

FETCH_LOG = """version: 2
dataset: fetch_log
grain: one row per id
keys: [id]
columns:
  id: {type: integer}
  path: {type: path, relative_to: data_root, status_column: status}
  status:
    type: string
    values:
      fetched: {means: present}
      released: {means: absent_expected}
      removed_on_purpose: {means: absent_expected}
      failed: {means: absent_unexpected}
"""

DATA_MD = "# Data\n\n```tq-config\nbases:\n  data_root: ./data\n```\n"


def _on(*slugs):
    return {slug: {"enabled": True,
                   "tier": std_registry.RECORDS[slug]["tier"],
                   "severity": std_registry.RECORDS[slug]["severity"]}
            for slug in slugs}


def _project(tmp_path, files):
    root = tmp_path / "proj"
    for rel, text in {"DATA.md": DATA_MD, **files}.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _rules(tmp_path, sql, files, rules=CONTRACT_RULES, dialect=None):
    root = _project(tmp_path, {"q.sql": sql, **files})
    findings = []
    for result in E.run([str(root / "q.sql")], str(root), dialect=dialect,
                        settings=_on(*rules), config={}):
        findings += result.findings
    return findings


def _fired(findings):
    return sorted({f.rule for f in findings})


# -- dq/undeclared-status-value -------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM fetch_log WHERE status = 'downloaded'",
    "SELECT COUNT(*) FROM fetch_log WHERE status IN ('fetched', 'done')",
    "SELECT CASE status WHEN 'gone' THEN 1 END FROM fetch_log",
    "SELECT COUNT(*) FROM fetch_log f WHERE f.status <> 'removed'",
    "WITH s AS (SELECT status FROM fetch_log) "
    "SELECT COUNT(*) FROM s WHERE status = 'downloaded'",
    # A typo in the WHERE of an UPDATE or DELETE changes nothing.
    "UPDATE fetch_log SET status = 'failed' WHERE status = 'downlaoded'",
    "DELETE FROM fetch_log WHERE status = 'downlaoded'",
    "DELETE FROM fetch_log WHERE id IN (1, 2) AND status IN ('gone')",
    # A contract naming only the table matches it in any schema.
    "SELECT COUNT(*) FROM legacy.fetch_log WHERE status = 'downloaded'",
])
def test_undeclared_value_fires(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/undeclared-status-value" in _fired(findings)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM fetch_log WHERE status = 'fetched'",
    "SELECT COUNT(*) FROM fetch_log WHERE status IN ('released', 'failed')",
    # Another table's status column isn't the contract's.
    "SELECT COUNT(*) FROM other_log WHERE status = 'downloaded'",
    # Unqualified, two sources, one without a contract: can't tell.
    "SELECT COUNT(*) FROM fetch_log JOIN other_log USING (id) "
    "WHERE status = 'downloaded'",
    # A transformed column isn't the declared column.
    "SELECT COUNT(*) FROM fetch_log WHERE UPPER(status) = 'FETCHED'",
    # A CTE named like the table is the CTE, a UNION body too.
    "WITH fetch_log AS (SELECT id, status FROM legacy_fetch UNION ALL "
    "SELECT id, status FROM archive_fetch) "
    "SELECT COUNT(*) FROM fetch_log WHERE status = 'queued'",
    "WITH fetch_log AS (SELECT id, status FROM legacy_fetch) "
    "SELECT COUNT(*) FROM fetch_log WHERE status = 'queued'",
    # An UPDATE's SET writes a value; it doesn't compare one.
    "UPDATE fetch_log SET status = 'fetched' WHERE id = 1",
])
def test_undeclared_value_stays_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/undeclared-status-value" not in _fired(findings)


def test_a_contract_naming_its_schema_skips_other_schemas(tmp_path):
    contract = FETCH_LOG.replace("dataset: fetch_log",
                                 "dataset: analytics.fetch_log")
    files = {"contracts/fetch_log.yaml": contract}
    quiet = _rules(tmp_path / "a", "SELECT COUNT(*) FROM legacy.fetch_log "
                                   "WHERE status = 'downloaded'", files)
    fires = _rules(tmp_path / "b", "SELECT COUNT(*) FROM analytics.fetch_log "
                                   "WHERE status = 'downloaded'", files)
    assert "dq/undeclared-status-value" not in _fired(quiet)
    assert "dq/undeclared-status-value" in _fired(fires)


def test_v1_allowed_values_feed_the_check(tmp_path):
    contract = ("table: orders\ngrain: one row per order\nkeys: [id]\n"
                "allowed_values:\n  status: [paid, refunded]\n")
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM orders WHERE status = 'payed'",
                      {"contracts/orders.yaml": contract})
    assert _fired(findings) == ["dq/undeclared-status-value"]


def test_no_contract_no_finding(tmp_path):
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM fetch_log WHERE status = 'x'", {})
    assert not findings


def test_a_finding_from_a_draft_contract_says_so(tmp_path):
    draft = FETCH_LOG.replace(
        "keys: [id]\n",
        "keys: [id]\nstatus: draft\ninferred_from: 200-row sample\n")
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM fetch_log WHERE status = 'x'",
                      {"contracts/fetch_log.yaml": draft})
    [finding] = [f for f in findings
                 if f.rule == "dq/undeclared-status-value"]
    assert "from draft contract fetch_log, inferred from 200-row sample" \
        in finding.why


def test_an_invalid_contract_feeds_no_check(tmp_path):
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM fetch_log WHERE status = 'x'",
                      {"contracts/fetch_log.yaml": FETCH_LOG + "bogus: 1\n"})
    assert not findings


# -- dq/expected-absence-counted-as-missing -------------------------------

def test_negating_present_sweeps_in_expected_absences(tmp_path):
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log WHERE status <> 'fetched'",
        {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/expected-absence-counted-as-missing" in _fired(findings)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM fetch_log WHERE status = 'failed'",
    "SELECT COUNT(*) FROM fetch_log WHERE status <> 'fetched' "
    "AND status NOT IN ('released', 'removed_on_purpose')",
    "SELECT COUNT(*) FROM fetch_log WHERE status <> 'failed'",
    # Excluded upstream, in a CTE or a subquery.
    "WITH live AS (SELECT * FROM fetch_log WHERE status NOT IN "
    "('released', 'removed_on_purpose')) "
    "SELECT COUNT(*) FROM live WHERE status <> 'fetched'",
    "SELECT COUNT(*) FROM (SELECT status FROM fetch_log WHERE status <> "
    "'released' AND status <> 'removed_on_purpose') t "
    "WHERE t.status <> 'fetched'",
    # Excluded in an inner join's ON clause.
    "SELECT COUNT(*) FROM runs r JOIN fetch_log f ON f.run_id = r.id "
    "AND f.status NOT IN ('released', 'removed_on_purpose') "
    "WHERE f.status <> 'fetched'",
    # Excluded alongside OR ... IS NULL.
    "SELECT COUNT(*) FROM fetch_log WHERE (status <> 'fetched' OR status "
    "IS NULL) AND status NOT IN ('released', 'removed_on_purpose')",
    # Restricted to the values that mean missing.
    "SELECT COUNT(*) FROM fetch_log WHERE status <> 'fetched' "
    "AND status IN ('failed')",
    # Counts only what is present.
    "SELECT COUNT(*) - COUNT(CASE WHEN status = 'failed' THEN 1 END) "
    "FROM fetch_log",
])
def test_explicit_absence_handling_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql,
                      {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/expected-absence-counted-as-missing" not in _fired(findings)


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM fetch_log WHERE NOT (status = 'fetched')",
    "SELECT SUM(CASE WHEN status = 'fetched' THEN 0 ELSE 1 END) "
    "FROM fetch_log",
    "SELECT COUNT(CASE WHEN status = 'fetched' THEN NULL ELSE 1 END) "
    "FROM fetch_log",
    "SELECT COUNT(*) - COUNT(CASE WHEN status = 'fetched' THEN 1 END) "
    "FROM fetch_log",
    # Excluded only on a LEFT JOIN's ON: the rows are still there.
    "SELECT COUNT(*) FROM fetch_log f LEFT JOIN runs r ON r.id = f.run_id "
    "AND f.status NOT IN ('released', 'removed_on_purpose') "
    "WHERE f.status <> 'fetched'",
])
def test_counting_expected_absences_fires(tmp_path, sql):
    findings = _rules(tmp_path, sql,
                      {"contracts/fetch_log.yaml": FETCH_LOG})
    [finding] = [f for f in findings
                 if f.rule == "dq/expected-absence-counted-as-missing"]
    assert finding.why.startswith(
        "released and removed_on_purpose in fetch_log.status are absent on "
        "purpose")


# -- sem/unit-mismatch ----------------------------------------------------

INVOICES = """version: 2
dataset: invoices
grain: one row per id
keys: [id]
columns:
  id: {type: integer}
  amount_cents: {type: integer, unit: minor, currency: USD}
  fee_cents: {type: integer, unit: minor, currency: USD}
  discount_usd: {type: number, unit: major, currency: USD}
  amount_eur_cents: {type: integer, unit: minor, currency: EUR}
"""


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount_cents - discount_usd) FROM invoices",
    "SELECT * FROM invoices WHERE amount_cents > amount_eur_cents",
    # Chains of three or more.
    "SELECT fee_cents + amount_cents + discount_usd FROM invoices",
    "SELECT (amount_cents + fee_cents) - discount_usd FROM invoices",
    "SELECT discount_usd - fee_cents - amount_cents FROM invoices",
])
def test_unit_mismatch_fires(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/invoices.yaml": INVOICES})
    assert _fired(findings) == ["sem/unit-mismatch"]


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount_cents - fee_cents) FROM invoices",
    "SELECT SUM(amount_cents / 100.0 - discount_usd) FROM invoices",
    "SELECT amount_cents + fee_cents - discount_usd * 100 FROM invoices",
    "SELECT amount_cents * 0.01 + discount_usd FROM invoices",
])
def test_unit_match_or_conversion_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/invoices.yaml": INVOICES})
    assert not findings


# -- sem/timezone-mismatch ------------------------------------------------

EVENTS = ("version: 2\ndataset: events\ngrain: one row per id\nkeys: [id]\n"
          "columns:\n  id: {type: integer}\n"
          "  occurred_at: {type: timestamp, time_zone: UTC}\n"
          "  logged_at: {type: timestamp, time_zone: UTC}\n")
SHIFTS = ("version: 2\ndataset: shifts\ngrain: one row per day\n"
          "keys: [day]\ncolumns:\n  day: {type: date}\n"
          "  starts_at: {type: timestamp, time_zone: 'local:Europe/Prague'}\n")


def test_timezone_mismatch_fires(tmp_path):
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM events e JOIN shifts s "
                  "ON e.occurred_at >= s.starts_at",
        {"contracts/events.yaml": EVENTS, "contracts/shifts.yaml": SHIFTS})
    assert _fired(findings) == ["sem/timezone-mismatch"]


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FROM events WHERE occurred_at >= logged_at",
    "SELECT COUNT(*) FROM events e JOIN shifts s ON "
    "(e.occurred_at AT TIME ZONE 'Europe/Prague') >= s.starts_at",
])
def test_same_zone_or_converted_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/events.yaml": EVENTS,
                                      "contracts/shifts.yaml": SHIFTS})
    assert not findings


@pytest.mark.parametrize("dialect, sql", [
    ("bigquery", "SELECT COUNT(*) FROM events e JOIN shifts s "
                 "ON e.occurred_at >= TIMESTAMP(s.starts_at, 'Europe/Prague')"),
    ("trino", "SELECT COUNT(*) FROM events e JOIN shifts s "
              "ON e.occurred_at >= with_timezone(s.starts_at, 'Europe/Prague')"),
    ("trino", "SELECT COUNT(*) FROM events e JOIN shifts s "
              "ON at_timezone(e.occurred_at, 'Europe/Prague') >= s.starts_at"),
    ("snowflake", "SELECT COUNT(*) FROM events e JOIN shifts s ON "
                  "CONVERT_TIMEZONE('UTC', 'Europe/Prague', e.occurred_at) "
                  ">= s.starts_at"),
    ("bigquery", "SELECT COUNT(*) FROM events e JOIN shifts s "
                 "ON DATETIME(e.occurred_at, 'Europe/Prague') >= s.starts_at"),
    ("spark", "SELECT COUNT(*) FROM events e JOIN shifts s ON "
              "from_utc_timestamp(e.occurred_at, 'Europe/Prague') >= s.starts_at"),
    ("duckdb", "SELECT COUNT(*) FROM events e JOIN shifts s ON "
               "timezone('Europe/Prague', e.occurred_at) >= s.starts_at"),
    ("mysql", "SELECT COUNT(*) FROM events e JOIN shifts s ON "
              "CONVERT_TZ(e.occurred_at, 'UTC', 'Europe/Prague') >= s.starts_at"),
])
def test_each_dialects_time_zone_conversion_is_quiet(tmp_path, dialect, sql):
    findings = _rules(tmp_path, sql, {"contracts/events.yaml": EVENTS,
                                      "contracts/shifts.yaml": SHIFTS},
                      dialect=dialect)
    assert "sem/timezone-mismatch" not in _fired(findings)


# -- sem/relative-path-base-mismatch --------------------------------------

DISK = ("version: 2\ndataset: disk_files\ngrain: one row per full_path\n"
        "keys: [full_path]\ncolumns:\n  full_path: {type: path}\n")


def test_path_base_mismatch_fires(tmp_path):
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  "ON f.path = d.full_path",
        {"contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    assert "sem/relative-path-base-mismatch" in _fired(findings)


@pytest.mark.parametrize("on", [
    "('./data/' || f.path) = d.full_path",
    "CONCAT('./data/', f.path) = d.full_path",
    "'data' || f.path = d.full_path",
    # Not a plain prefix: can't tell, so quiet.
    "REPLACE(d.full_path, '/srv/data/', '') = f.path",
])
def test_path_with_the_declared_base_is_quiet(tmp_path, on):
    findings = _rules(
        tmp_path, f"SELECT COUNT(*) FROM fetch_log f JOIN disk_files d ON {on}",
        {"contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    assert "sem/relative-path-base-mismatch" not in _fired(findings)


@pytest.mark.parametrize("prefix", ["/mnt/other/", "/srv/data/"])
def test_path_with_another_literal_base_fires(tmp_path, prefix):
    findings = _rules(
        tmp_path, f"SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  f"ON ('{prefix}' || f.path) = d.full_path",
        {"contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    [finding] = [f for f in findings
                 if f.rule == "sem/relative-path-base-mismatch"]
    assert finding.why.startswith(f"the query puts '{prefix}' in front of "
                                  "fetch_log.path, but fetch_log.path is "
                                  "relative to data_root (./data)")


def test_a_base_read_from_the_environment_accepts_any_prefix(tmp_path):
    # Its real value is only known when the query runs.
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  "ON ('/srv/data/' || f.path) = d.full_path",
        {"DATA.md": "# Data\n\n```tq-config\nbases:\n  data_root: "
                    "{env: DATA_ROOT, default: ./data}\n```\n",
         "contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    assert "sem/relative-path-base-mismatch" not in _fired(findings)


# -- sem/metric-drift -----------------------------------------------------

RATE = ("metric: collection_rate\nkind: ratio\ndataset: fetch_log\n"
        "numerator: {agg: count, column: '*', where: \"status = 'fetched'\"}\n"
        "denominator: {agg: count, column: '*', "
        "where: \"status <> 'removed_on_purpose'\"}\n")


_NUM = "COUNT(*) FILTER (WHERE status = 'fetched')"
_DEN = "COUNT(*) FILTER (WHERE status <> 'removed_on_purpose')"


def test_metric_drift_names_the_missing_filter(tmp_path):
    findings = _rules(
        tmp_path,
        "SELECT COUNT(*) FILTER (WHERE status = 'fetched') * 1.0 / COUNT(*) "
        "AS collection_rate FROM fetch_log",
        {"contracts/fetch_log.yaml": FETCH_LOG,
         "metrics/collection_rate.yaml": RATE},
        rules=("sem/metric-drift",))
    [finding] = findings
    assert "denominator omits declared filter" in finding.why


@pytest.mark.parametrize("sql", [
    "SELECT COUNT(*) FILTER (WHERE status = 'fetched') * 1.0 / "
    "COUNT(*) FILTER (WHERE status <> 'removed_on_purpose') "
    "AS collection_rate FROM fetch_log",
    "SELECT SUM(CASE WHEN status = 'fetched' THEN 1 ELSE 0 END) * 1.0 / "
    "COUNT(CASE WHEN status <> 'removed_on_purpose' THEN 1 END) "
    "AS collection_rate FROM fetch_log",
    # Another name: not a claim to compute the metric.
    "SELECT COUNT(*) AS n FROM fetch_log",
    # A slice of the metric: a run, a date window, a joined dimension.
    f"SELECT {_NUM} * 1.0 / {_DEN} AS collection_rate FROM fetch_log "
    "WHERE run_id = 'r1'",
    f"SELECT {_NUM} * 1.0 / {_DEN} AS collection_rate FROM fetch_log "
    "WHERE fetched_at >= DATE '2026-01-01'",
    f"SELECT {_NUM.replace('status', 'f.status')} * 1.0 / "
    f"{_DEN.replace('status', 'f.status')} AS collection_rate "
    "FROM fetch_log f JOIN runs r ON r.id = f.run_id WHERE r.env = 'prod'",
    # The denominator's filter in the WHERE, applied to both parts.
    "SELECT COUNT(*) FILTER (WHERE status = 'fetched') * 1.0 / COUNT(*) "
    "AS collection_rate FROM fetch_log WHERE status <> 'removed_on_purpose'",
    "SELECT AVG(CASE WHEN status = 'fetched' THEN 1.0 ELSE 0 END) "
    "AS collection_rate FROM fetch_log WHERE status <> 'removed_on_purpose'",
    # NOT IN for <>, NULLIF and ROUND around the parts.
    "SELECT ROUND(COUNT(*) FILTER (WHERE status = 'fetched') * 1.0 / NULLIF("
    "COUNT(*) FILTER (WHERE status NOT IN ('removed_on_purpose')), 0), 3) "
    "AS collection_rate FROM fetch_log",
    # The name reused for something that isn't a ratio.
    "WITH x AS (SELECT run_id, COUNT(*) AS collection_rate FROM fetch_log "
    "GROUP BY run_id) SELECT * FROM x",
    # Parts built in a CTE: nothing to compare here.
    f"WITH n AS (SELECT {_NUM} AS a, {_DEN} AS b FROM fetch_log) "
    "SELECT a * 1.0 / b AS collection_rate FROM n",
])
def test_metric_as_declared_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql,
                      {"contracts/fetch_log.yaml": FETCH_LOG,
                       "metrics/collection_rate.yaml": RATE},
                      rules=("sem/metric-drift",))
    assert not findings


@pytest.mark.parametrize("dialect, sql", [
    ("bigquery", "SELECT COUNTIF(status = 'fetched') / COUNTIF(status <> "
                 "'removed_on_purpose') AS collection_rate FROM fetch_log"),
    ("bigquery", "SELECT SAFE_DIVIDE(COUNTIF(status = 'fetched'), COUNTIF("
                 "status <> 'removed_on_purpose')) AS collection_rate "
                 "FROM fetch_log"),
    ("snowflake", "SELECT DIV0(COUNT_IF(status = 'fetched'), COUNT_IF(status "
                  "<> 'removed_on_purpose')) AS collection_rate FROM fetch_log"),
    ("snowflake", "SELECT COUNT_IF(STATUS = 'fetched') / COUNT(*) AS "
                  "COLLECTION_RATE FROM FETCH_LOG "
                  "WHERE STATUS <> 'removed_on_purpose'"),
])
def test_dialect_forms_of_the_metric(tmp_path, dialect, sql):
    files = {"contracts/fetch_log.yaml": FETCH_LOG,
             "metrics/collection_rate.yaml": RATE}
    assert not _rules(tmp_path / "ok", sql, files,
                      rules=("sem/metric-drift",), dialect=dialect)
    # The same form with the denominator's filter dropped is drift.
    drifted = (sql.replace("COUNTIF(status <> 'removed_on_purpose')",
                           "COUNT(*)")
               .replace("COUNT_IF(status <> 'removed_on_purpose')", "COUNT(*)")
               .replace(" WHERE STATUS <> 'removed_on_purpose'", ""))
    [finding] = _rules(tmp_path / "drift", drifted, files,
                       rules=("sem/metric-drift",), dialect=dialect)
    assert "the denominator omits declared filter" in finding.why


RATE_IN = ("metric: collection_rate\nkind: ratio\ndataset: fetch_log\n"
           "numerator: {agg: count, column: '*', "
           "where: \"status IN ('fetched', 'released')\"}\n"
           "denominator: {agg: count, column: '*'}\n")


@pytest.mark.parametrize("numerator, drift", [
    ("status = 'fetched' OR status = 'released'", False),
    ("status IN ('released', 'fetched')", False),
    ("NOT status NOT IN ('fetched', 'released')", False),
    ("status = 'fetched'", True),
])
def test_equivalent_filters_compare_equal(tmp_path, numerator, drift):
    findings = _rules(
        tmp_path, f"SELECT COUNT(*) FILTER (WHERE {numerator}) * 1.0 / "
                  "COUNT(*) AS collection_rate FROM fetch_log",
        {"contracts/fetch_log.yaml": FETCH_LOG,
         "metrics/collection_rate.yaml": RATE_IN},
        rules=("sem/metric-drift",))
    assert bool(findings) is drift


def test_messages_use_plain_words_not_contract_keys(tmp_path):
    sql = ("SELECT COUNT(*) FROM fetch_log WHERE status = 'downloaded';\n"
           "SELECT COUNT(*) FROM fetch_log WHERE status <> 'fetched';\n"
           "SELECT SUM(amount_cents - discount_usd) FROM invoices;\n"
           "SELECT COUNT(*) FROM events e JOIN shifts s "
           "ON e.occurred_at >= s.starts_at;\n"
           "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
           "ON f.path = d.full_path;\n"
           "SELECT COUNT(*) FILTER (WHERE status = 'fetched') * 1.0 / COUNT(*) "
           "AS collection_rate FROM fetch_log;\n")
    findings = _rules(tmp_path, sql, {
        "contracts/fetch_log.yaml": FETCH_LOG,
        "contracts/invoices.yaml": INVOICES, "contracts/events.yaml": EVENTS,
        "contracts/shifts.yaml": SHIFTS, "contracts/disk.yaml": DISK,
        "metrics/collection_rate.yaml": RATE})
    assert _fired(findings) == sorted(CONTRACT_RULES)
    for finding in findings:
        text = finding.why + " " + finding.fix
        for key in ("absent_expected", "relative_to", "unanchored",
                    "time_zone:", "unit:", "currency:", "means:"):
            assert key not in text, (finding.rule, key, text)


def test_contract_rules_are_off_by_default():
    for slug in CONTRACT_RULES:
        assert std_registry.RECORDS[slug]["enabled"] is False, slug


# -- contracts/metrics changed during a session ---------------------------

def test_hook_flags_a_contract_edit_and_the_receipt_names_it(
        tmp_path, monkeypatch):
    from test_hook import Args, _event, _run
    from truequery import cli
    root = _project(tmp_path, {
        "q.sql": "SELECT COUNT(*) FROM fetch_log\n",
        "contracts/fetch_log.yaml": FETCH_LOG})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(root)
    _run(_event(str(root / "q.sql")), Args(), monkeypatch)  # seeds state
    contract = root / "contracts" / "fetch_log.yaml"
    contract.write_text(FETCH_LOG.replace(
        "      failed:", "      downloaded: {means: present}\n      failed:"),
        encoding="utf-8")
    _, out = _run(_event(str(contract)), Args(), monkeypatch)
    assert "[TrueQuery] contracts/metrics changed this session" in out
    assert "contracts/fetch_log.yaml" in out
    _, again = _run(_event(str(root / "q.sql")), Args(), monkeypatch)
    assert "contracts/metrics changed this session" not in again
    capture = []
    monkeypatch.setattr("builtins.print",
                        lambda *a, **k: capture.append(" ".join(map(str, a))))
    cli.main(["review", "--json", "q.sql"])
    receipt = json.loads(capture[0])["receipt"]
    assert receipt["contracts_changed"] == ["contracts/fetch_log.yaml"]


def test_no_notice_without_a_contract_change(tmp_path, monkeypatch):
    from test_hook import Args, _event, _run
    root = _project(tmp_path, {
        "q.sql": "SELECT COUNT(*) FROM fetch_log\n",
        "contracts/fetch_log.yaml": FETCH_LOG})
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(root)
    for _ in range(2):
        _, out = _run(_event(str(root / "q.sql")), Args(), monkeypatch)
        assert "contracts/metrics changed" not in out
    assert os.path.isdir(root / ".truequery")


# -- review findings (independent review of C1) ---------------------------

def test_advice_never_says_edit_the_contract(tmp_path):
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM fetch_log WHERE status = 'x'",
                      {"contracts/fetch_log.yaml": FETCH_LOG})
    [finding] = [f for f in findings
                 if f.rule == "dq/undeclared-status-value"]
    assert "update the contract" not in finding.fix
    assert "ask the user whether the contract should list 'x'" in finding.fix


def test_a_seeded_contract_is_named_as_a_draft(tmp_path):
    seeded = ("table: orders\ngrain: unknown  # TODO: one row per <what>\n"
              "keys: []\nallowed_values:\n  status: [placed, shipped]\n")
    findings = _rules(tmp_path,
                      "SELECT COUNT(*) FROM orders WHERE status = 'returned'",
                      {"contracts/orders.yaml": seeded})
    [finding] = findings
    assert "from draft contract orders: its grain is unconfirmed" \
        in finding.why


def test_a_metrics_folder_doesnt_move_the_project_root(tmp_path):
    # No contracts anywhere; a dbt metrics/ folder next to the model must not
    # hide the schema YAML that declares the join key (review finding 2).
    root = tmp_path / "proj"
    files = {
        "dbt_project.yml": "name: demo\n",
        "models/staging/schema.yml":
            "models:\n  - name: fx_rates\n    tests:\n"
            "      - dbt_utils.unique_combination_of_columns:\n"
            "          combination_of_columns: [currency, rate_date]\n",
        "models/marts/metrics/revenue.yml": "metrics: []\n",
        "models/marts/q.sql":
            "select o.day_utc, count(*) as n\nfrom orders o\n"
            "left join {{ ref('fx_rates') }} fx on fx.currency = o.currency "
            "and fx.rate_date = o.day_utc\ngroup by 1\n",
    }
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    findings = [f for r in E.run([str(root / "models/marts/q.sql")],
                                 str(root), config={}) for f in r.findings]
    assert not findings


def test_contract_checks_off_read_no_contract(tmp_path, monkeypatch):
    from truequery import keylookup
    calls = []
    monkeypatch.setattr(keylookup, "declared_context_for",
                        lambda *a, **k: calls.append(a) or
                        {"contracts": {}, "metrics": {}, "bases": {}})
    _rules(tmp_path, "SELECT 1 FROM fetch_log",
           {"contracts/fetch_log.yaml": FETCH_LOG},
           rules=("sql/not-in-nullable-subquery",))
    assert calls == []


def _session(tmp_path, monkeypatch, files):
    from test_hook import Args, _event, _run
    root = _project(tmp_path, files)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nope"))
    monkeypatch.chdir(root)

    def edit(rel, text=None):
        if text is not None:
            (root / rel).write_text(text, encoding="utf-8")
        return _run(_event(str(root / rel)), Args(), monkeypatch)[1]
    return root, edit


def test_notice_once_per_change(tmp_path, monkeypatch):
    root, edit = _session(tmp_path, monkeypatch, {
        "q.sql": "SELECT 1\n", "contracts/fetch_log.yaml": FETCH_LOG})
    edit("q.sql")
    changed = FETCH_LOG.replace("failed:", "lost:")
    assert "contracts/metrics changed" in edit("contracts/fetch_log.yaml",
                                                changed)
    # Written again with the same content: nothing new to say.
    assert "contracts/metrics changed" not in edit("contracts/fetch_log.yaml",
                                                    changed)


def test_notice_watches_a_nested_contracts_folder(tmp_path, monkeypatch):
    root, edit = _session(tmp_path, monkeypatch, {
        "shop/q.sql": "SELECT 1\n",
        "shop/contracts/fetch_log.yaml": FETCH_LOG})
    edit("shop/q.sql")
    out = edit("shop/contracts/fetch_log.yaml",
               FETCH_LOG.replace("failed:", "lost:"))
    assert "shop/contracts/fetch_log.yaml" in out


def _stop(monkeypatch):
    from test_hook import Args, _run
    return _run(json.dumps({"hook_event_name": "Stop",
                            "stop_hook_active": False}), Args(), monkeypatch)[1]


def test_notice_names_a_first_contract_in_a_new_folder(tmp_path,
                                                       monkeypatch):
    root, edit = _session(tmp_path, monkeypatch, {"q.sql": "SELECT 1\n"})
    edit("q.sql")
    (root / "contracts").mkdir()
    assert "contracts/fetch_log.yaml" in edit("contracts/fetch_log.yaml",
                                              FETCH_LOG)
    assert "contracts/metrics changed" not in _stop(monkeypatch)


def test_notice_names_contracts_a_shell_command_created(tmp_path,
                                                        monkeypatch):
    # `truequery init` run from the shell: no edit event names the files.
    root, edit = _session(tmp_path, monkeypatch, {"q.sql": "SELECT 1\n"})
    edit("q.sql")
    (root / "contracts").mkdir()
    (root / "contracts/fetch_log.yaml").write_text(FETCH_LOG,
                                                   encoding="utf-8")
    assert "contracts/fetch_log.yaml" in edit("q.sql")


def test_notice_names_a_nested_contract_edited_before_its_sql(
        tmp_path, monkeypatch):
    root, edit = _session(tmp_path, monkeypatch, {
        "q.sql": "SELECT 1\n",
        "shop/contracts/fetch_log.yaml": FETCH_LOG})
    edit("q.sql")
    out = edit("shop/contracts/fetch_log.yaml",
               FETCH_LOG.replace("failed:", "lost:"))
    assert "shop/contracts/fetch_log.yaml" in out
    # Said once: the end of the turn doesn't take it for a deleted file.
    assert "contracts/metrics changed" not in _stop(monkeypatch)


def test_shell_change_caught_at_the_next_event(tmp_path, monkeypatch):
    root, edit = _session(tmp_path, monkeypatch, {
        "q.sql": "SELECT 1\n", "contracts/fetch_log.yaml": FETCH_LOG})
    edit("q.sql")
    # Changed without an edit event (a shell write the hook didn't see).
    (root / "contracts/fetch_log.yaml").write_text(
        FETCH_LOG.replace("failed:", "lost:"), encoding="utf-8")
    assert "contracts/fetch_log.yaml" in edit("q.sql")


def test_later_hand_edits_arent_credited_to_the_session(tmp_path,
                                                        monkeypatch):
    from truequery import hook
    root, edit = _session(tmp_path, monkeypatch, {
        "q.sql": "SELECT 1\n", "contracts/fetch_log.yaml": FETCH_LOG})
    edit("q.sql")
    (root / "contracts/fetch_log.yaml").write_text(
        FETCH_LOG.replace("failed:", "lost:"), encoding="utf-8")
    assert hook.session_contracts_changed(str(root)) == []
