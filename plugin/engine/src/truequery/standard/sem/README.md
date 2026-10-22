# Semantic layer and metrics (`sem`) — TrueQuery standard pack (draft)

Scope: metric definitions, grains, aggregations, dimensions, filters,
semantic models, saved queries, and governed metric APIs. Metric change review lives in
`ops/model-versioning-breaking-changes`. Out of scope: dashboard layout/visualization and raw model/transform
logic (those belong to `consume` and `model`).

All sources below were fetched on 2026-10-12 and are paraphrased, not
quoted.

## Top 5 to enforce first (and why)

1. **sem/no-fanout-double-counting** (detector, error) — silent revenue
   multiplication, the most expensive single wrong-number bug in metric
   layers.
2. **sem/ratio-metrics-divide-after-aggregation** (detector, error) —
   averaging ratios is the most common "looks right, always wrong"
   aggregation error.
3. **sem/aggregation-type-matches-additivity** (contract, error) — summing a
   balance across time is a wrong answer; once the additivity tag is
   declared the check is exact.
4. **sem/single-governed-metric-definition** (detector, warning) — duplicate
   metric definitions are the root of "two dashboards disagree". Only the
   duplicate-definition part is checkable from code; spotting a metric
   re-implemented in dashboard SQL is judgment.
5. **sem/semantic-model-validates** (detector, warning) — cheapest gate in
   the pack: run the tool's own validator in CI and whole classes of
   breakage never merge.

Metric change review moved to `ops/model-versioning-breaking-changes`;
exposure registration moved to `gov/exposures-registered`.
`saved-query-canonical-slices` and `no-metric-defined-off-staging` are dbt
conventions and are advisory only.

## Existing tools covering parts (integration notes)

- **dbt + MetricFlow validation** (`dbt parse`, `mf validate`): covers
  semantic-model-validates, join-path resolution, entity wiring, and metric
  type checking. TrueQuery should invoke these validators, not reimplement
  them; add the grain/additivity/filter rules they do not check.
- **SQLFluff**: layout/style only — no semantic-layer awareness. Complementary;
  TrueQuery owns metric semantics.
- **dbt-project-evaluator**: covers project-structure hygiene and some
  documentation gaps; overlaps description-and-ownership-per-metric (presence
  checks). TrueQuery adds metric-specific semantics (grain, additivity,
  fanout, ratio order-of-operations).
- **Great Expectations / Soda**: row- and distribution-level data tests on
  underlying models (feed no-metric-defined-off-staging's assumption that
  marts are tested). They do not check metric-layer semantics.
- **Ruff**: Python hygiene for MetricFlow custom work; unrelated to metric
  semantics.

## Conflicts

See CONFLICTS_FRAG.md (3 entries).

## Files

22 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| aggregation-type-matches-additivity | contract | error | no |
| conversion-metrics-window-bounds | detector | warning |  |
| count-distinct-uses-entity-key | detector | warning |  |
| cumulative-metrics-time-spine | detector | warning |  |
| derived-metrics-compose-from-governed | detector | warning |  |
| description-and-ownership-per-metric | detector | warning |  |
| dimension-naming-conventions | contract | warning | no |
| entity-primary-key-declared | detector | warning |  |
| join-paths-declared-via-entities | detector | warning |  |
| metric-filters-declared-not-hidden | detector | warning |  |
| metric-grain-declaration | detector | warning |  |
| metric-smoke-test-on-change | judgment | warning |  |
| no-fanout-double-counting | detector | error |  |
| no-metric-defined-off-staging | detector | advisory |  |
| null-handling-in-aggregations | contract | warning | no |
| percentile-metrics-use-approximate | detector | warning |  |
| ratio-metrics-divide-after-aggregation | detector | error |  |
| saved-query-canonical-slices | contract | advisory | no |
| semantic-model-validates | detector | warning |  |
| single-governed-metric-definition | detector | warning |  |
| time-dimension-standardized | detector | warning |  |
| unit-consistency | contract | error | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 16 |
| contract | 5 |
| runtime | 0 |
| judgment | 1 |
| **total** | **22** |

Severity: 4 error, 16 warning, 2 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

