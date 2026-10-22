# sem — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## sem/aggregation-type-matches-additivity

*Aggregation type must match the measure's additivity*

Fully additive measures (revenue) may use SUM across all dimensions; semi-additive measures (balances, inventory snapshots) must declare the dimensions they cannot sum across (usually time) and expose a valid rollup (e.g. last-value); non-additive measures (ratios, distinct counts) must not be summed at all and are only valid through their defining expression.

Why: Summing a snapshot balance across days fabricates money: twelve month-end balances added together can overstate assets 12x. Additivity is a property of the business concept, and the layer must enforce it, not hope consum…

```sql
-- bad
measures:
  - name: month_end_balance
    sql: balance
    type: sum   # balance is semi-additive; summing across months invents money
```
```sql
-- good
measures:
  - name: month_end_balance
    sql: balance
    type: average
  dimensions:
    - name: month
      sql: month
      type: time
  # documented: valid across products/warehouses, never summed across time;
  # month-end reporting uses last-value of the period
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: Getting started data modeling; LookML terms and concepts

## sem/conversion-metrics-window-bounds

*Conversion metrics declare bounded entity-typed windows*

Every conversion metric declares a base entity, a conversion entity, bounded calculation and window periods, and the entity type used to join them, so the conversion rate is reproducible and its observation window is explicit.

Why: An unbounded "visited then bought" rate silently grows as late conversions trickle in: the same query returns a different number every week and teams argue about whether conversion improved or the window just widened.

```sql
-- bad
- name: signup_to_paid_rate
  type: conversion
  type_params:
    base_measure: signups
    conversion_measure: paid_starts   # no window: rate drifts forever
```
```sql
-- good
- name: signup_to_paid_rate
  type: conversion
  type_params:
    base_measure: signups
    conversion_measure: paid_starts
    entity: user
    calculation: conversions
    window: { count: 30, period: day }
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Creating metrics; About MetricFlow

## sem/count-distinct-uses-entity-key

*Distinct-count metrics aggregate the entity key, not a nullable attribute*

COUNT DISTINCT metrics (users, orders, customers) aggregate the entity's primary key or another non-null unique identifier — never a nullable descriptive attribute — and the key choice is visible in the definition.

Why: Counting distinct emails instead of user IDs silently merges users sharing an address and drops users with null emails: the "user" count becomes a "non-null distinct email" count wearing a user label, wrong on both ends.

```sql
-- bad
measures:
  - name: active_users
    sql: email
    type: count_distinct
```
```sql
-- good
measures:
  - name: active_users
    sql: user_id
    type: count_distinct
    filters:
      - email IS NOT NULL  # or activity-based filter, declared
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Entities; Getting started data modeling

## sem/cumulative-metrics-time-spine

*Cumulative metrics build on a complete time spine*

Every cumulative metric declares its time granularity and grain period, and the project provides a time-spine model covering the full reporting range so cumulative values account for days with zero activity instead of skipping them.

Why: Without a spine, cumulative totals only advance on active days: a quiet holiday week renders as flat-then-jump, and running totals silently exclude zero-activity periods that the business experienced as real zeros.

```sql
-- bad
- name: mtd_signups
  type: cumulative
  type_params:
    measure: signup_count
    # no time spine in project: gaps vanish instead of counting as zero
```
```sql
-- good
- name: mtd_signups
  type: cumulative
  type_params:
    measure: signup_count
    grain: day
    time_window: { count: 30, period: day }
# project includes metricflow_time_spine covering the reporting range
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: MetricFlow time spine; About MetricFlow

## sem/derived-metrics-compose-from-governed

*Derived metrics compose only from governed inputs*

Derived (and chained derived) metrics reference only other governed metrics by name — never raw columns, model SQL, or dashboard calculations — so every input to a derived number is itself versioned, tested, and documented.

Why: A derived metric reaching past the layer into raw columns inherits none of the layer's filters, grain guards, or change review: one upstream rename breaks it silently while its sibling metrics migrate cleanly.

```sql
-- bad
- name: net_revenue
  type: derived
  type_params:
    expr: gross_revenue - SUM(stg_refunds.amount)  # raw column reference
    metrics: [gross_revenue]
```
```sql
-- good
- name: net_revenue
  type: derived
  type_params:
    expr: gross_revenue - refund_total
    metrics: [gross_revenue, refund_total]
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Creating metrics; Build your metrics

## sem/description-and-ownership-per-metric

*Every governed metric has a plain-language definition and an owner*

Each governed metric carries a one-paragraph plain-language definition (what it counts, key inclusions/exclusions, grain) and a named owner or owning team, both stored in the metric definition itself.

Why: An undocumented metric is a rumor with a number: two teams read different meanings into the same name, and when the number moves nobody owns the explanation. The definition plus owner turns disputes into 30-second reads.

```sql
-- bad
- name: activated_account
  type: simple
  type_params: { measure: activations }
  # no description, no owner
```
```sql
-- good
- name: activated_account
  type: simple
  type_params: { measure: activations }
  description: 'Workspace completing onboarding checklist within 14 days of signup. Excludes internal/test workspaces.'
  meta: { owner: growth-analytics, tier: 1 }
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Creating metrics; Semantic models

## sem/dimension-naming-conventions

*Shared dimensions use one name and one definition everywhere*

Dimensions representing the same business concept (customer, country, order date) use the identical name, data type, and value set across all semantic models, enforced by a checked naming convention or a shared dimension reference.

Why: `country` in one model and `country_code` in another (one ISO-2, one full name) makes cross-model filtering silently drop rows on join, and every new analyst re-learns the mapping by trial and error.

```sql
-- bad
# orders model: country (full name); spend model: country_code (ISO-2)
dimensions: [{ name: country, ... }]   # vs [{ name: country_code, ... }]
```
```sql
-- good
dimensions:
  - name: country_code   # identical name, type, ISO-2 values in both models
    type: categorical
    expr: country_code
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: Dimensions; LookML terms and concepts

## sem/entity-primary-key-declared

*Each semantic model declares its entities with uniqueness evidence*

Every semantic model declares at least one primary entity (the unique row identifier) with an expression, plus natural entities for joinable concepts, and each primary entity has a passing uniqueness/not-null test on the underlying model.

Why: Entities are the join keys of the whole metric layer; an untested entity that is not actually unique turns every cross-model query into silent fanout. Declaring plus testing closes the loop between assumption and fact.

```sql
-- bad
semantic_models:
  - name: orders
    model: ref('orders')
    entities: []   # no entity; joins against this model are unanchored
```
```sql
-- good
semantic_models:
  - name: orders
    model: ref('orders')
    entities:
      - name: order_id
        type: primary
        expr: order_id
# plus: unique + not_null tests on orders.order_id
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Entities; Semantic models

## sem/join-paths-declared-via-entities

*Cross-model dimensions resolve only through declared entity paths*

Any query combining a measure from one semantic model with dimensions from another travels through a declared entity join path (entity equality across models). Undeclared fan-out joins and multi-hop paths with ambiguous intermediate grains are rejected at validation time.

Why: Ad-hoc cross-model joins reintroduce every fanout and grain bug the semantic layer was built to prevent, but now wearing the governed metric's trusted name. Declared paths keep the trust earned.

```sql
-- bad
SELECT m.revenue, c.attribution
FROM revenue_model m JOIN campaigns c ON c.name = m.campaign_name  -- undeclared, non-unique
```
```sql
-- good
# MetricFlow query: measure revenue + dimension campaign via campaign entity
mf query --metrics revenue --group-by campaign__channel
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Joins; Validations

## sem/metric-filters-declared-not-hidden

*Metric filters live in the metric definition, not in hidden SQL*

Every filter that defines what a metric counts (e.g. only paying users, excluding test orders, refunds handling) is declared as a filter parameter on the measure or metric itself, visible to consumers, rather than buried in the underlying model's WHERE clause or a dashboard filter.

Why: Hidden filters make the "same" metric return different numbers depending on which model or dashboard computed it, and nobody reading the metric name can see the exclusion. Declared filters travel with the metric everywhe…

```sql
-- bad
models:
  - name: orders
    # hidden business filter in model SQL:
    # SELECT * FROM raw_orders WHERE is_test = false AND status != 'refunded'
metrics:
  - name: order_count   # no declared filter; consumers cannot see exclusions
```
```sql
-- good
measures:
  - name: order_count
    expr: order_id
    agg: count_distinct
    filters:
      - is_test = false
      - status != 'refunded'
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Getting started data modeling; Semantic models

## sem/metric-grain-declaration

*Every metric declares the grain it is defined at*

Each metric or measure declares its native grain (the entity it is true at, e.g. per order, per user per day) in its definition, and any query slicing it at a coarser grain uses the documented rollup path rather than re-aggregating raw rows.

Why: An undeclared grain invites averaging averages and summing counts that were already summed: numbers that look precise and are wrong by construction. Declared grain tells every consumer which rollups are valid.

```sql
-- bad
- name: avg_order_value
  type: simple
  type_params:
    expr: AVG(order_total)   # grain undeclared; consumers re-average freely
```
```sql
-- good
- name: avg_order_value
  type: ratio
  type_params:
    numerator: order_revenue
    denominator: order_count
  description: 'Grain: order. Never average across days; re-query at day grain.'
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Semantic models; Ratio metrics

## sem/metric-smoke-test-on-change

*Changed metrics carry a before/after smoke comparison*

Every change to a governed metric's logic ships with a before/after query over a fixed comparison window (same grain, same filters), reviewed in the PR, so the magnitude and direction of the restatement are known before merge — not discovered by consumers afterwards.

Why: Even approved redefinitions blindside consumers when the size of the jump is unknown: a "small filter fix" that moves revenue 6% breaks forecasts, targets, and trust simultaneously. The smoke comparison prices the change…

```sql
-- bad
# PR changes revenue filter with no comparison evidence
-    - status NOT IN ('refunded')
+    - status NOT IN ('refunded', 'chargeback')
```
```sql
-- good
# PR includes: old vs new revenue over trailing 90d at month grain,
# max monthly delta 0.8%, direction uniform — attached to the change record
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Validations; Saved queries

## sem/no-fanout-double-counting

*Multi-entity joins in the semantic layer must not fan out measures*

In dbt Semantic Layer joins, a measure may only be queried with dimensions from another semantic model through a declared entity path where the join is many-to-one from the measure side. Joins that fan out (one-to-many from the measure side) are rejected or require an explicit deduplication rule.

Why: Fanout silently multiplies measures: one order joined to three line items triples revenue. It is the single most expensive wrong-number bug in metric layers because every downstream number inflates without any error rais…

```sql
-- bad
# orders joined to line_items, SUM(order_total) grouped by line category
SELECT c.category, SUM(o.order_total) FROM orders o
JOIN line_items l ON l.order_id = o.id GROUP BY 1  -- fanned out
```
```sql
-- good
entities:
  - name: order_id
    type: primary
    expr: order_id
# MetricFlow joins line-item grain up to order via the primary entity only
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Joins; Entities

## sem/no-metric-defined-off-staging

*Governed metrics build on tested marts, not staging tables*

Semantic models sit on top of cleaned, tested mart or intermediate models — never directly on raw staging tables — so every governed metric inherits the data-quality tests and documented contracts of the layer below.

Why: A metric wired to staging inherits every upstream schema wobble and null surprise with no test to catch it: the metric becomes the first alert for breakages that mart tests were built to contain.

```sql
-- bad
semantic_models:
  - name: churn_inputs
    model: ref('stg_events')   # raw staging, untested
```
```sql
-- good
semantic_models:
  - name: churn_inputs
    model: ref('mart_customer_activity')  # tested, contracted
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: Semantic models; Validations

## sem/null-handling-in-aggregations

*Measures declare null and missing-value handling explicitly*

Every measure definition states how nulls and missing values are treated (ignored, counted as zero, or excluded with a companion validity metric), and COUNT DISTINCT measures never silently mix nulls into the distinct set.

Why: Databases disagree on null semantics (COUNT(x) skips nulls, COUNT(*) does not), so an undeclared choice flips between warehouses and migrations: a backfill of null emails quietly drops 200K users from an engagement metri…

```sql
-- bad
measures:
  - name: engaged_users
    sql: email
    type: count_distinct   # nullable column; null treatment undeclared
```
```sql
-- good
measures:
  - name: engaged_users
    sql: email
    type: count_distinct
    filters:
      - email IS NOT NULL
    description: 'Null emails excluded; see dq validity check on users.email.'
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Getting started data modeling; Creating metrics

## sem/percentile-metrics-use-approximate

*Percentile metrics use warehouse approximate aggregates explicitly*

Median, percentile, and distribution metrics use the warehouse's approximate aggregate (or a declared exact method with an acknowledged cost), and the approximation method is recorded in the metric definition — never an exact percentile computed by pulling raw rows client-side.

Why: Exact percentiles over billions of rows are a runaway-cost query wearing a dashboard's refresh schedule: one p99 tile can scan terabytes hourly, while approximate variants return within 1-2% accuracy at a fraction of the…

```sql
-- bad
measures:
  - name: p99_latency
    sql: percentile_cont(latency, 0.99)  # exact over full event stream
    type: number
```
```sql
-- good
measures:
  - name: p99_latency
    sql: approx_quantile(latency, 0.99)
    type: number
    description: 'Approximate p99 (±1%). Exact available on request for audits.'
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build
- severity: build=warning
- sources: Getting started data modeling; LookML terms and concepts

## sem/ratio-metrics-divide-after-aggregation

*Ratios divide aggregates, never average precomputed ratios*

Any metric that is a ratio, rate, or percentage is defined as numerator metric divided by denominator metric (ratio/derived type), so the layer aggregates first and divides second. Defining a ratio as an average of row-level or day-level ratios is prohibited.

Why: Averaging ratios weights tiny and huge denominators equally: a 100% conversion day with 2 visitors outweighs a 1% day with 20,000 visitors. Divide-after-aggregation is the only weighting-correct order of operations.

```sql
-- bad
SELECT AVG(daily_conversion_rate) AS weekly_conversion FROM daily_kpis
```
```sql
-- good
- name: conversion_rate
  type: ratio
  type_params:
    numerator: orders_count
    denominator: visitors_count
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: Ratio metrics; Creating metrics

## sem/saved-query-canonical-slices

*Top metrics ship canonical saved queries for standard slices*

Each tier-1 metric (the handful the business actually runs on) has at least one saved query pinning its standard slice — default dimensions, standard time grain, and common filters — so consumers start from the blessed formulation instead of rebuilding it per dashboard.

Why: Without canonical slices, ten dashboards slice "revenue by month" ten slightly different ways (different status filters, different month definitions) and the reconciliation tax is permanent. Saved queries make the standa…

```sql
-- bad
metrics: [{ name: revenue, ... }]
# no saved query: every dashboard hand-rolls "revenue by month"
```
```sql
-- good
saved_queries:
  - name: revenue_by_month
    query_params:
      metrics: [revenue]
      group_by: [TimeDimension('ordered_on', 'month')]
      where: "{{ Dimension('order_status') }} != 'cancelled'"
    description: Canonical monthly revenue; start here.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,explore
- severity: build=advisory, explore=advisory
- sources: Saved queries; Build your metrics

## sem/semantic-model-validates

*Semantic layer definitions pass static validation in CI*

The project's semantic models, metrics, and saved queries pass static validation (`dbt parse` plus MetricFlow validation) in CI on every change, with zero errors: dangling refs, unresolvable entities, and invalid dimensions fail the build before merge.

Why: Metric YAML is code with no compiler in the editor: a renamed entity or a deleted dimension breaks every query touching it, and without CI validation the breakage surfaces at 9am when the dashboards refresh.

```sql
-- bad
# CI runs only `dbt build`; metric YAML errors surface at query time
steps: [dbt build --select marts]
```
```sql
-- good
steps:
  - dbt parse --no-partial-parse
  - mf validate --skip-dw  # static validation, no warehouse needed
  - dbt build --select marts
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Validations; About MetricFlow

## sem/single-governed-metric-definition

*Each business metric has exactly one governed definition*

Every metric name used in reporting or decisions (e.g. revenue, active user, churn) resolves to exactly one governed definition in the semantic layer. No two metric definitions with different logic share the same business name. Dashboards, BI calculated fields, exports, notebooks and syncs that report a governed metric reference that definition (by metric name, saved query or semantic-layer API) instead of re-implementing its formula in their own SQL.

Why: Duplicate metric logic is the classic "two dashboards disagree" failure: each copy drifts independently, executives act on whichever number they saw first, and reconciliation becomes a monthly fire drill. A single govern…

```sql
-- bad
# metrics.yml defines revenue; dashboard D7 also defines its own:
SELECT SUM(amount) AS revenue FROM orders  -- ignores refunds, unlike governed metric
```
```sql
-- good
metrics:
  - name: revenue
    type: simple
    type_params:
      measure: order_revenue
    description: Gross order revenue net of refunds
# dashboards query the metric by name via the semantic layer API
# or bind a saved query such as revenue_by_week
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Build your metrics; Creating metrics

## sem/time-dimension-standardized

*Time dimensions use one canonical date column and timezone per grain*

Each semantic model exposes a declared primary time dimension per usable grain (day, week, month), all derived from one canonical event timestamp in one declared timezone, and metrics aggregate over these dimensions rather than ad-hoc date casts.

Why: Mixed timezones and competing date columns split single days across two reporting days: UTC-midnight vs Pacific-midnight cutoffs move up to 8% of events between days and make day-over-day deltas meaningless.

```sql
-- bad
SELECT DATE(created_at) AS day, SUM(total) FROM orders GROUP BY 1        -- UTC
SELECT DATE(created_at_pt) AS day, SUM(spend) FROM ad_spend GROUP BY 1  -- Pacific
```
```sql
-- good
dimensions:
  - name: ordered_on
    type: time
    type_params: { time_granularity: day }
    expr: CONVERT_TZ(created_at_utc, 'America/Los_Angeles')
    # declared primary time dimension; all daily metrics use it
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Dimensions; MetricFlow time spine

## sem/unit-consistency

*Measures declare units and currency; mixed units never aggregate*

Every monetary or unit-bearing measure declares its currency or unit, values are converted to a common unit before aggregation, and raw multi-currency or mixed-unit columns are never summed without conversion.

Why: Summing dollars, euros, and yen as bare numbers produces a total in no currency at all; with typical mixes the reported figure can be off by multiples while looking exact to the cent.

```sql
-- bad
measures:
  - name: global_revenue
    sql: amount
    type: sum   # amount mixes USD, EUR, JPY; no currency declared
```
```sql
-- good
measures:
  - name: global_revenue_usd
    sql: amount_usd_converted
    type: sum
    description: 'Currency: USD, converted at daily rate. Grain: order.'
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: Getting started data modeling; LookML terms and concepts
