# Consume domain: Analysis and consumption

Scope: everything downstream of curated tables. BI dashboards and reports,
ad-hoc analysis queries, reverse-ETL deliveries into SaaS tools,
bulk exports and extracts, and read-pattern cost
hygiene of consumer queries (LIMIT discipline, no deep OFFSET, aggregation
before transfer).

Out of scope: production pipeline logic (see `model/`), metric definitions
themselves (see `sem/`), and production monitoring, alerting, and cost
governance of pipelines (see `ops/`). This pack asks "is the consumption
safe, bounded, and traceable", never "is the transform correct".

## Top 5 to enforce first, and why

1. **reverse-etl-pii-consent** (contract, error) — PII copied to SaaS
   escapes every warehouse control permanently; the harm (regulatory
   breach, irreversible spread) is the costliest failure in the pack.
2. **parameterized-dashboard-filters** (detector, error) — string-built
   filter SQL is an injection hole; concatenation of user input is visible
   in the template code.
3. **consumer-reads-marts** (contract, warning) — one contract check
   decouples all consumers from pipeline refactors at once.
4. **consumer-cost-guard** (runtime, warning) — the only pre-execution
   defense against runaway bills; BigQuery's free dry run makes it nearly
   free to enforce.
5. **deterministic-export-order** (contract, warning) — resumed or diffed
   exports need a total order, which needs the declared unique key.

Moved out of this pack (F3 merges): `SELECT *` in consumer queries is
covered by `model/explicit-column-lists`; exposure registration by
`gov/exposures-registered`; governed-metric reuse by
`sem/single-governed-metric-definition`. The `explore-*` records are the
explore-mode floor for ad-hoc analysis (added in the F5 pass).

## Existing tools covering parts, and what they cover

- **dbt exposures + `dbt ls --select ... --output json`** cover
  `gov/exposures-registered` structurally (dependency graph) but do not
  enforce that every production dashboard is registered; that completeness
  check is the contract gap.
- **dbt Semantic Layer / MetricFlow** covers
  `sem/single-governed-metric-definition` mechanically (one metric
  definition, many consumers) but cannot stop a BI author from bypassing it
  with custom SQL.
- **SQLFluff and generic SQL linters** cover fragments of
  `model/explicit-column-lists`, `adhoc-limit-required`, and `no-deep-offset`
  as style rules, but without the consumer-artifact scoping (persisted
  dashboard/export/extract queries vs throwaway exploration).
- **Warehouse native controls** (BigQuery dry run and `maximumBytesBilled`,
  Snowflake resource monitors and Dynamic Data Masking, Databricks SQL
  warehouse query limits) are the runtime primitives behind
  `consumer-cost-guard`, `dashboard-query-budget`, and
  `reverse-etl-pii-consent`, but each is per-query or per-object, not
  per-dashboard or per-sync policy.
- **BI tools' own features** (scheduled refresh, caching, parameter binding,
  data-freshness indicators in Looker/Tableau/Power BI/Databricks AI/BI)
  implement the mechanisms for `dashboard-refresh-schedule`,
  `parameterized-dashboard-filters`, and `freshness-displayed`, but defaults
  are permissive; these practices demand the safe configuration be declared
  and checked.

All sources live-fetched 2026-10-12; see per-file `sources` entries.
Paraphrased throughout; disagreements recorded in CONFLICTS_FRAG.md.

## Files

27 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| adhoc-limit-required | detector | warning |  |
| aggregate-before-export | judgment | advisory |  |
| consumer-cost-guard | runtime | warning |  |
| consumer-reads-marts | contract | warning | no |
| dashboard-query-budget | runtime | warning |  |
| dashboard-refresh-schedule | contract | warning | no |
| deterministic-export-order | contract | warning | no |
| explicit-timezone-consumers | contract | warning | no |
| explore-answer-carries-reproducible-query | judgment | warning |  |
| explore-caveats-and-unchecked-stated | judgment | advisory |  |
| explore-day-boundaries-and-partial-periods | judgment | warning |  |
| explore-experiment-readout-follows-plan | judgment | warning |  |
| explore-freshness-and-coverage-checked | judgment | warning |  |
| explore-inspect-distributions-and-examples | judgment | advisory |  |
| explore-join-row-count-checks | judgment | warning |  |
| explore-null-aware-negative-filters | detector | advisory |  |
| explore-question-metric-grain-stated | judgment | warning |  |
| explore-rates-not-counts-across-segments | judgment | warning |  |
| explore-reconcile-to-known-total | judgment | warning |  |
| explore-reuse-governed-metric | judgment | warning |  |
| export-row-cap | contract | warning | no |
| freshness-displayed | contract | warning | no |
| no-deep-offset | detector | warning |  |
| parameterized-dashboard-filters | detector | error |  |
| reverse-etl-idempotent-keys | contract | warning | no |
| reverse-etl-pii-consent | contract | error | no |
| reverse-etl-schema-contract | contract | warning | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 4 |
| contract | 9 |
| runtime | 2 |
| judgment | 12 |
| **total** | **27** |

Severity: 2 error, 21 warning, 4 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

