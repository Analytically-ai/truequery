# TrueQuery Standard — Quality and testing (`dq`)

Scope: the fixed six-dimension quality suite (completeness, uniqueness,
validity, consistency including first-class orphan-key checks, timeliness,
accuracy-via-proxy), fill-rate thresholds and missing-value sentinels, unit
tests with fixtures, and blocking-audit wiring. Timeliness is declared in
`ingest/declare-source-freshness`. The design of TrueQuery's own dq report
lives in `product-spec/dq-report.md`, not here. Out of scope:
history-trained anomaly and reasonability detection (needs stored metric
history; enterprise tier), and pipeline scheduling of checks (orchestration
domain).

All sources below were live-fetched on 2026-10-12. Claims rest on official
docs (dbt Labs, Soda, Great Expectations, Tobiko/SQLMesh, Google Cloud,
PostgreSQL, DuckDB, Snowflake, Dagster Labs); where page bodies were
truncated the practice carries confidence medium. Paraphrased throughout.

## Top 5 to enforce first (and why)

1. `dq/completeness-table-not-empty` (runtime, error) — An empty table is the
   cheapest catastrophe: valid schema, zero rows, plausible-looking zeros
   downstream. One `row_count > 0` check per produced table catches it.
2. `dq/uniqueness-key-no-duplicates` (runtime, error) — Duplicate keys
   multiply through every join. Zero-duplicate key checks block
   double-counted revenue before it reaches finance.
3. `dq/consistency-no-orphan-keys` (runtime, error) — Orphans vanish in inner
   joins, so marts undercount with no error anywhere. The anti-join check is
   cheap and exact.
4. `dq/uniqueness-composite-grain` (contract, required) — Declaring the grain
   is one of the three facts in the small required contract set; without it
   uniqueness checks test the wrong columns.
5. `dq/consistency-paired-aggregate-reconciliation` (runtime, error) — A
   row-count or summed-measure pair against the source catches silent loss
   that per-row checks cannot see.

Freshness moved to `ingest/declare-source-freshness`, which is now the one
canonical freshness record. TrueQuery's own report design (failing-rows SQL
and samples, verdict states, threshold defaults, sampled uniqueness, undated
timeliness) moved to `product-spec/dq-report.md`.

## Existing tools covering parts (integration notes)

- **Great Expectations** (GX Core + Expectation Gallery): the fullest
  deterministic suite — null, uniqueness (single and compound), range, set,
  regex, type, and aggregate expectations with per-validation observed
  values. TrueQuery should compile its six dimensions to GX-equivalent
  predicates where teams already run GX, and adopt GX's observed-value and
  violation-vs-error reporting shape.
- **Soda / SodaCL**: metric-plus-threshold checks YAML (row counts, missing
  and duplicate percents, invalid counts, freshness, reference and
  reconciliation checks), failed-rows samples, and pass/fail/warn/error scan
  summaries. Covers thresholds, sentinels, failing-rows SQL, and verdict
  reporting; TrueQuery's contract file should accept SodaCL-adjacent syntax
  where practical.
- **dbt (data tests, unit tests, sources, contracts)**: four built-in tests
  plus severity config, stored failures, fixture-based unit tests, source
  freshness blocks, and enforced model contracts. Covers the build-loop half
  of this domain; TrueQuery's offline checks complement dbt's
  warehouse-backed tests rather than replacing them.
- **SQLMesh (audits + tests)**: blocking audits that halt plans on any
  returned row, plus fixture unit tests per model and CTE. The reference
  implementation of "audits run automatically and block promotion".
- **dbt-project-evaluator**: meta-rules over a dbt project including testing
  best practices (coverage of models by tests). TrueQuery can reuse its
  "every model has tests" posture as the static half of coverage checks,
  while owning the row-level semantics the evaluator does not assert.
- **SQLFluff**: style and layout lint only — no data semantics. Complementary;
  TrueQuery must keep rule IDs unambiguous when co-running (SQLFluff already
  ships TQ01–TQ03 in its TSQL bundle).
- **Ruff**: Python lint/format only. Relevant solely for the Python test
  fixtures and check-runner code hygiene, not for data assertions.
- **Warehouse DDL (Postgres enforced constraints; Snowflake informational
  constraints; DuckDB constraints)**: native CHECK / NOT NULL / UNIQUE /
  PRIMARY KEY / FOREIGN KEY where enforced. TrueQuery should push enforced
  platforms toward DDL constraints and test everything elsewhere (see
  `dq/unenforced-constraints-are-tested`).
- **BigQuery data-quality scans / Dagster asset checks + freshness
  policies**: managed-scan and orchestrator-side surfaces. TrueQuery feeds
  them (failing-rows SQL, SLA declarations) rather than duplicating them.

## Files

18 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| accuracy-proxy-or-unmeasurable | judgment | advisory |  |
| audits-blocking-every-run | runtime | warning |  |
| checks-versioned-with-code | runtime | advisory |  |
| completeness-declare-missing-sentinels | contract | warning | no |
| completeness-fill-rate-thresholds | contract | warning | no |
| completeness-not-null-required-columns | contract | warning | no |
| completeness-table-not-empty | runtime | error |  |
| consistency-cross-column-row-rules | contract | error | no |
| consistency-no-orphan-keys | runtime | error |  |
| consistency-paired-aggregate-reconciliation | runtime | error |  |
| schema-conformance-checked-on-read | runtime | warning |  |
| unenforced-constraints-are-tested | contract | warning | no |
| uniqueness-composite-grain | contract | warning | yes |
| uniqueness-key-no-duplicates | runtime | error |  |
| unit-tests-with-fixtures | contract | warning | no |
| validity-allowed-value-sets | contract | warning | no |
| validity-enforce-model-contracts | contract | warning | no |
| validity-range-format-type-rules | contract | warning | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 0 |
| contract | 10 |
| runtime | 7 |
| judgment | 1 |
| **total** | **18** |

Severity: 5 error, 11 warning, 2 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

