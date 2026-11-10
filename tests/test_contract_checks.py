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


def _rules(tmp_path, sql, files, rules=CONTRACT_RULES):
    root = _project(tmp_path, {"q.sql": sql, **files})
    findings = []
    for result in E.run([str(root / "q.sql")], str(root),
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
])
def test_undeclared_value_stays_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/undeclared-status-value" not in _fired(findings)


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
])
def test_explicit_absence_handling_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql,
                      {"contracts/fetch_log.yaml": FETCH_LOG})
    assert "dq/expected-absence-counted-as-missing" not in _fired(findings)


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
])
def test_unit_mismatch_fires(tmp_path, sql):
    findings = _rules(tmp_path, sql, {"contracts/invoices.yaml": INVOICES})
    assert _fired(findings) == ["sem/unit-mismatch"]


@pytest.mark.parametrize("sql", [
    "SELECT SUM(amount_cents - fee_cents) FROM invoices",
    "SELECT SUM(amount_cents / 100.0 - discount_usd) FROM invoices",
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


# -- sem/relative-path-base-mismatch --------------------------------------

DISK = ("version: 2\ndataset: disk_files\ngrain: one row per full_path\n"
        "keys: [full_path]\ncolumns:\n  full_path: {type: path}\n")


def test_path_base_mismatch_fires(tmp_path):
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  "ON f.path = d.full_path",
        {"contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    assert "sem/relative-path-base-mismatch" in _fired(findings)


def test_normalized_path_is_quiet(tmp_path):
    findings = _rules(
        tmp_path, "SELECT COUNT(*) FROM fetch_log f JOIN disk_files d "
                  "ON ('/srv/data/' || f.path) = d.full_path",
        {"contracts/fetch_log.yaml": FETCH_LOG, "contracts/disk.yaml": DISK})
    assert "sem/relative-path-base-mismatch" not in _fired(findings)


# -- sem/metric-drift -----------------------------------------------------

RATE = ("metric: collection_rate\nkind: ratio\ndataset: fetch_log\n"
        "numerator: {agg: count, column: '*', where: \"status = 'fetched'\"}\n"
        "denominator: {agg: count, column: '*', "
        "where: \"status <> 'removed_on_purpose'\"}\n")


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
])
def test_metric_as_declared_is_quiet(tmp_path, sql):
    findings = _rules(tmp_path, sql,
                      {"contracts/fetch_log.yaml": FETCH_LOG,
                       "metrics/collection_rate.yaml": RATE},
                      rules=("sem/metric-drift",))
    assert not findings


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
