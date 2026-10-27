# consume — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## consume/adhoc-limit-required

*Ad-hoc exploration queries carry a LIMIT unless they aggregate*

Any ad-hoc SELECT returning raw rows (no GROUP BY or aggregate over the full result) carries an explicit LIMIT (or FETCH FIRST) bounding the rows returned to the analyst.

Why: Unbounded exploratory SELECTs return millions of rows to the analyst's client or notebook, exhausting memory and freezing BI tools. LIMIT bounds the rows returned; it is NOT a cost control in columnar warehouses: in BigQ…

```sql
-- bad
SELECT user_id, event FROM raw_events WHERE dt = '2026-10-01'
```
```sql
-- good
SELECT user_id, event FROM raw_events WHERE dt = '2026-10-01' LIMIT 200
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore
- severity: explore=warning
- sources: Optimize query computation (BigQuery best practices); SELECT; Introduction to optimizing query performance

## consume/aggregate-before-export

*BI queries aggregate in the warehouse instead of pulling raw rows*

Dashboard and report queries that visualize an aggregate (totals, series, top-N) perform the aggregation in warehouse SQL and transfer only the grouped result, never raw event rows for client-side rollup.

Why: Pulling millions of raw rows into a BI server or browser to sum them there wastes scan cost, saturates BI memory, and times out dashboards; grouped results are orders of magnitude smaller and render instantly.

```sql
-- bad
SELECT ts, user_id, revenue FROM fact_clicks WHERE dt >= '2026-09-01'
```
```sql
-- good
SELECT DATE(ts) AS day, SUM(revenue) AS revenue FROM fact_clicks WHERE dt >= '2026-09-01' GROUP BY 1
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: Introduction to optimizing query performance

## consume/consumer-cost-guard

*Consumer queries pass a pre-execution cost estimate before running*

Ad-hoc and BI-issued queries above a trivial size pass a pre-execution estimate (dry run bytes, optimizer plan scan, or budget check); queries exceeding the team byte or dollar threshold require explicit confirmation.

Why: One careless cartesian or full-history scan can cost more than a month of normal analysis; a free pre-check catches it before the meter runs.

```sql
-- bad
-- issued directly with no estimate
SELECT * FROM events_2019 CROSS JOIN events_2024
```
```sql
-- good
-- dry run first: estimated 41 TB, exceeds 1 TB threshold -> blocked for review
DRY_RUN SELECT ...  -- confirmed before execution
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: explore,assure
- severity: assure=warning, explore=warning
- sources: Run a query; Introduction to optimizing query performance

## consume/consumer-reads-marts

*BI and extracts read from curated marts, not raw or staging tables*

Persisted consumer queries (dashboards, reports, exports, reverse-ETL) reference only tables in declared serving marts; direct reads of raw, staging, or intermediate tables are rejected.

Why: Consumers wired to staging inherit every pipeline refactor as a breaking change and bypass tested business logic; marts are the versioned contract that lets pipelines evolve safely.

```sql
-- bad
SELECT status, COUNT(*) FROM stg_orders GROUP BY 1
```
```sql
-- good
SELECT status, COUNT(*) FROM mart_orders GROUP BY 1
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: About dbt models

## consume/dashboard-query-budget

*Dashboards carry a per-refresh query and scan budget*

Each production dashboard declares a per-refresh budget (maximum query count and maximum bytes or credits scanned); refreshes breaching the budget alert the owner and freeze non-essential tiles until reviewed.

Why: Tile sprawl turns one dashboard into fifty queries per viewer per refresh; without a budget the cost grows with headcount while nobody owns it.

```sql
-- bad
dashboards:
  - name: everything_overview   # 60 tiles, no budget
```
```sql
-- good
dashboards:
  - name: everything_overview
    per_refresh_budget: {max_queries: 20, max_bytes_scanned: 50000000000}
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Introduction to optimizing query performance

## consume/dashboard-refresh-schedule

*Dashboards declare refresh cadence matched to data latency*

Each scheduled dashboard or cached report declares a refresh cadence no faster than its slowest upstream mart, with concurrent-refresh limits so overlapping refreshes cannot stack.

Why: Dashboards refreshing every 5 minutes on hourly marts burn 12x the compute for identical numbers and stampede the warehouse at the top of each hour; cadence matched to latency removes the waste.

```sql
-- bad
dashboards:
  - name: exec_overview
    refresh: every 5 minutes   # upstream mart builds hourly
```
```sql
-- good
dashboards:
  - name: exec_overview
    refresh: hourly at :15
    max_concurrent_refreshes: 1
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Databricks documentation; Working with Materialized Views

## consume/deterministic-export-order

*Exports and paginated extracts specify a total ORDER BY*

Every bulk export and every paginated extract query ends in an ORDER BY over a unique key combination, so repeated runs and resumed pages produce a deterministic row sequence.

Why: Without a total order, warehouse parallelism returns rows in arbitrary sequence: diffing two extracts flags phantom changes, resumed exports duplicate or skip rows, and auditors cannot reproduce a delivered file.

```sql
-- bad
SELECT order_id, amount FROM mart_orders WHERE dt = '2026-10-01'
```
```sql
-- good
SELECT order_id, amount FROM mart_orders WHERE dt = '2026-10-01' ORDER BY order_id
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SELECT

## consume/explicit-timezone-consumers

*Consumer datetime logic states its time zone explicitly*

Consumer queries that filter, bucket, or display datetimes state the time zone in SQL (AT TIME ZONE, CONVERT_TZ, or timestamp-with-zone literals) instead of relying on session or server defaults.

Why: Session time zones differ between the analyst laptop, the BI server, and the warehouse default; implicit zones shift day boundaries and silently move revenue between reporting periods.

```sql
-- bad
SELECT DATE(event_ts) AS day, SUM(amount) FROM mart_orders GROUP BY 1
```
```sql
-- good
SELECT DATE(event_ts AT TIME ZONE 'America/Chicago') AS day, SUM(amount) FROM mart_orders GROUP BY 1
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SELECT Statement

## consume/explore-answer-carries-reproducible-query

*The final answer carries the exact queries that reproduce every number in it*

The shared answer is kept separate from the exploration that led to it, and every number it reports can be traced to a query or script attached to or linked from the answer (saved query, committed file, or notebook revision). Each such query runs top to bottom without manual edits, has its date window and parameters written as literals or named parameters (not relative expressions such as CURRENT_DATE left unrecorded), and produces the reported value. No reported number depends on a hand-edited spreadsheet step or an unrecorded scratch cell.

Why: Exploration produces many dead-end queries; if the conclusion is not tied to the one that matters, nobody (including the author a month later) can check it, rerun it on new data, or find the bug when the number is challe…

```sql
-- bad
Answer: "September churn was 3.4%."  (from cell 37 of an unsaved notebook)
```
```sql
-- good
Answer: "September churn was 3.4%."
Query: analyses/2026-10-churn/churn_sept.sql @ commit 4f2c1a9
  -- params: month_start = '2026-09-01', month_end = '2026-10-01'
  -- run at 2026-10-11 09:12 UTC; fct_subscriptions max(_loaded_at) 2026-10-11 06:00 UTC
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Ten Simple Rules for Reproducible Computational Research; The AQuA Book; Good Data Analysis

## consume/explore-caveats-and-unchecked-stated

*The answer states its filters, assumptions, caveats and what was not checked*

The shared answer contains a section that lists: (1) every exclusion filter applied (test accounts, internal users, outliers, date limits) with the number or share of rows each removed; (2) the assumptions the result depends on; (3) known limitations, including whether the result is descriptive or supports a causal claim; and (4) checks that were not performed. If the analysis supports no causal claim, the answer does not use causal language ("drove", "caused", "because of") about its findings.

Why: Readers cannot weigh a number whose filters and assumptions are hidden, and silent filtering is a common way for an analysis to answer a narrower question than the one asked. Saying what was not checked turns a confident…

```sql
-- bad
Notifications drive retention: 61% vs 30% D30 retention.
```
```sql
-- good
D30 retention: 61% (notifications on) vs 30% (off). Descriptive only.
Filters: excluded test accounts (1,204 rows, 0.3%); users with <3 sessions
  (38% of sign-ups), so this covers engaged users only.
Assumptions: notification flag as of day 7 reflects the user's setting.
Not checked: self-selection (engaged users opt in); iOS vs Android split.
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=advisory
- sources: Good Data Analysis; The AQuA Book

## consume/explore-day-boundaries-and-partial-periods

*Day-level answers state their time zone and exclude or label partial periods*

An answer reported at day, week, or month grain states the time zone used to assign timestamps to periods, and that zone matches the one the question's audience reports in (or the difference is called out). The query converts timestamps to that zone explicitly before truncating. Any period that is not complete at query time (including the current day, week, or month) is excluded with a closed upper bound, or is shown and labelled as partial.

Why: Warehouses default to UTC or to the session's time zone, so the same query can assign late-evening events to different days depending on where it runs; a peak day can split across two dates. Including the current, incomp…

```sql
-- bad
SELECT DATE(created_at) AS day, COUNT(*) FROM orders
WHERE created_at >= CURRENT_DATE - 14
GROUP BY 1
```
```sql
-- good
-- Days in America/New_York; last complete day is 2026-10-10
SELECT DATE(created_at, 'America/New_York') AS day, COUNT(*) FROM orders
WHERE created_at >= TIMESTAMP('2026-09-27', 'America/New_York')
  AND created_at <  TIMESTAMP('2026-10-11', 'America/New_York')   -- closed: today excluded
GROUP BY 1
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Date functions (GoogleSQL); Date/Time Types

## consume/explore-experiment-readout-follows-plan

*Experiment readouts follow the pre-registered metrics and analysis plan*

An A/B test readout reports the success, guardrail, and decision criteria that were recorded before the experiment started, over the pre-specified analysis window and population, and states the decision those criteria give. Before reporting effects, it reports the sample ratio mismatch (SRM) check; if the check fails, effects are not presented as a result. Any metric, segment, or window not in the plan is labelled exploratory and does not change the decision. If no plan was recorded, the readout says so and labels all findings exploratory.

Why: Choosing metrics, segments, or windows after seeing the data lets the analyst gravitate to whichever looks best, inflating false positives (the "garden of forking paths"). A sample ratio mismatch signals a data-quality p…

```sql
-- bad
Result: treatment wins! +4.2% add-to-cart on mobile in week 2 (p=0.03).
```
```sql
-- good
Plan (registered 2026-09-01): success = checkout conversion; guardrails =
  page latency p95, refund rate; window 14 days; ship if success
  significant and guardrails non-inferior.
SRM: 50.1/49.9, chi-square p=0.41, passed.
Success: +0.3% (95% CI -0.9% to +1.5%), not significant. Guardrails passed.
Decision per plan: do not ship.
Exploratory (not decision-relevant): add-to-cart mobile week 2 +4.2%, 1 of 40 slices.
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Risk-Aware Product Decisions in A/B Tests with Multiple Metrics; What is an Experiment Design?; Diagnosing Sample Ratio Mismatch in A/B Testing; Trustworthy Online Controlled Experiments: A Practical Guide to A/B Testing

## consume/explore-freshness-and-coverage-checked

*Check data freshness and coverage of the window before concluding*

Before drawing a conclusion about a time window, the analysis checks and records, for each source table used: the maximum event or load timestamp (freshness), the minimum and maximum dates actually present inside the window, and the row count per day (or per period of the answer's grain) across the window. Periods with missing data, a visible drop-off at the end of the window, or counts outside the normal range are either excluded and reported as excluded, or named as a caveat in the answer.

Why: Late-arriving and partially loaded data make the most recent days look like a decline, and a gap in a source looks like a change in behaviour. Checking freshness and per-day volume is quick and separates "the business ch…

```sql
-- bad
SELECT COUNT(*) FROM signups
WHERE created_at >= CURRENT_DATE - 7
```
```sql
-- good
SELECT MAX(created_at), MAX(_loaded_at) FROM signups;           -- freshness
SELECT CAST(created_at AS DATE) d, COUNT(*) FROM signups
WHERE created_at >= DATE '2026-10-01' AND created_at < DATE '2026-10-08'
GROUP BY 1 ORDER BY 1;                                          -- coverage per day
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Source freshness; About microbatch incremental models; Good Data Analysis

## consume/explore-inspect-distributions-and-examples

*Inspect distributions and raw example rows, not only summary statistics*

For each measure the answer summarizes with a mean, sum, or rate, the analysis looks at its distribution (histogram, quantiles including min, p50, p99, and max, or a top-N of values) and at a sample of raw rows, including the largest values. Values that dominate the summary (for example, a few rows contributing more than a stated share of a sum) are either explained or reported separately, and the answer says so.

Why: Very different data can share the same mean and variance; a single outlier, a sentinel value such as 999999, or a duplicated test account can drive a summary statistic. Reading real rows is also how most logic bugs in ne…

```sql
-- bad
SELECT AVG(order_value) FROM orders WHERE order_week = '2026-10-05'
```
```sql
-- good
SELECT MIN(order_value), APPROX_QUANTILES(order_value, 100)[OFFSET(50)] AS p50,
       APPROX_QUANTILES(order_value, 100)[OFFSET(99)] AS p99, MAX(order_value)
FROM orders WHERE order_week = '2026-10-05';
SELECT order_id, customer_id, order_value FROM orders
WHERE order_week = '2026-10-05' ORDER BY order_value DESC LIMIT 20;
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=advisory
- sources: Good Data Analysis; Anscombe's quartet

## consume/explore-join-row-count-checks

*Check row counts and key uniqueness at each join before aggregating*

For every join in a query whose result feeds a SUM, COUNT, or AVG, the analysis checks, before trusting the aggregate, that the row count after the join equals the row count of the table at the intended grain (or differs by an explained amount), or that the join key is unique on the side that should be "one". The counts or the uniqueness check are kept in the notebook or answer. A join that changes the row count unexpectedly is resolved (pre-aggregate, deduplicate on a stated key, or change the join) before the result is reported.

Why: A one-to-many join silently repeats the "one" side's measures, so sums and counts come out inflated with no error or warning. A before-and-after count is the cheapest test that catches it, and it also catches the opposit…

```sql
-- bad
SELECT SUM(o.total)
FROM orders o JOIN order_items i ON i.order_id = o.order_id
WHERE i.category = 'books'
```
```sql
-- good
-- check: orders = 120,431 rows; after join = 120,431? -> no, 301,877 (fanout)
SELECT SUM(o.total)
FROM orders o
WHERE EXISTS (SELECT 1 FROM order_items i
              WHERE i.order_id = o.order_id AND i.category = 'books')
-- check after rewrite: 48,210 orders, matches COUNT(DISTINCT order_id)
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Understanding symmetric aggregates; Good Data Analysis

## consume/explore-null-aware-negative-filters

*Negative filters and NULL comparisons state how NULLs are treated*

In an ad-hoc query, a predicate of the form col <> value, col != value, col NOT IN (literal list), or col NOT LIKE pattern is accompanied, in the same query, by an explicit NULL decision for that column: an "OR col IS NULL", an "AND col IS NOT NULL", a COALESCE/IFNULL on the column, or IS DISTINCT FROM in place of <>. Comparisons written as "= NULL" or "<> NULL" never appear.

Why: Ordinary comparisons with NULL yield unknown, so WHERE status <> 'refunded' silently drops every row whose status is NULL, and "= NULL" matches nothing. Aggregates skip NULLs too (COUNT(col) is not COUNT(*), AVG ignores …

```sql
-- bad
SELECT COUNT(*) FROM sessions
WHERE account_type <> 'internal'
```
```sql
-- good
SELECT COUNT(*) FROM sessions
WHERE account_type IS DISTINCT FROM 'internal'   -- NULL (anonymous) kept on purpose
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=advisory
- sources: Comparison Functions and Operators; Aggregate Functions

## consume/explore-question-metric-grain-stated

*State the question, metric definition and grain before querying*

Before the first result-producing query, the analysis records in writing (notebook header, ticket, or answer draft): (1) the question as one sentence that a number or comparison can answer; (2) each metric used, with its numerator, its denominator if it is a ratio, and the inclusion and exclusion rules for both; (3) the grain of the answer (one row per what, e.g. per customer per week); and (4) the population and time window in scope. An analysis follows this practice only if all four items are present and the reported result matches the stated grain and window.

Why: Analysis that starts from the data rather than a question tends to wander, and ratios with unstated numerators and denominators are a main source of two analysts reporting different numbers for the same words. Writing th…

```sql
-- bad
-- notebook cell 1
SELECT * FROM mart_orders LIMIT 100
-- ...forty cells later: "conversion is 4.1%"
```
```sql
-- good
-- Question: did September conversion fall versus August?
-- Metric: conversion = distinct customers with >=1 paid order
--         / distinct customers who signed up, same calendar month
--         (excludes test accounts and internal employees)
-- Grain: one row per signup month; Window: 2026-08-01 to 2026-09-30
-- Population: self-serve signups, all regions
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Good Data Analysis; The AQuA Book

## consume/explore-rates-not-counts-across-segments

*Compare segments of different sizes with rates, and check for mix shift*

When an answer compares segments (regions, cohorts, plans, variants) or periods whose base populations differ by more than a factor of two, the comparison is made on a normalized measure (rate, per-unit value, or share) with its denominator stated, and each segment's base size is shown next to the rate. When an overall rate is compared across periods or groups, the answer also shows the rate within the main sub-segments, so a change in segment mix (Simpson's paradox) is visible rather than read as a change in behaviour.

Why: Raw counts mostly measure segment size, so the biggest segment "wins" every comparison. Pooled rates can move, or even reverse, purely because the mix of segments changed, while every segment's own rate stays the same or…

```sql
-- bad
SELECT country, COUNT(*) AS tickets FROM tickets GROUP BY 1 ORDER BY 2 DESC
```
```sql
-- good
SELECT t.country,
       COUNT(*)                                 AS tickets,
       a.active_accounts,
       1000.0 * COUNT(*) / a.active_accounts    AS tickets_per_1k_accounts
FROM tickets t JOIN active_accounts_by_country a USING (country)
GROUP BY t.country, a.active_accounts
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Good Data Analysis; Simpson's paradox

## consume/explore-reconcile-to-known-total

*Reconcile the headline number against a known total before reporting it*

Before an ad-hoc answer is shared, its headline number (or the total its breakdown sums to) is compared with at least one independent reference: a governed dashboard or mart figure for the same window, the same quantity computed by a different route (another table or method), or the value for a previous period. The answer records the reference used, both values, and the difference. A difference larger than the tolerance stated in the answer is explained or the result is not reported as final.

Why: Most surprising numbers turn out to be errors rather than discoveries. Measuring the same thing two ways, and checking it against past values, catches broken joins, wrong filters and partial loads that look perfectly pla…

```sql
-- bad
-- revenue by channel, Q3: shared directly from the notebook
SELECT channel, SUM(amount) FROM stg_payments
WHERE paid_at >= '2026-07-01' AND paid_at < '2026-10-01' GROUP BY 1
```
```sql
-- good
-- same query, plus a reconciliation cell kept in the answer:
-- sum over channels = 11,904,220
-- fct_revenue Q3 (governed) = 11,911,005; diff -0.06% (tolerance 0.5%)
-- Q2 by same query = 11,236,410; Q3/Q2 = +5.9%, in line with plan
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Good Data Analysis; The AQuA Book

## consume/explore-reuse-governed-metric

*Ad-hoc analysis reuses governed metric definitions instead of re-deriving them*

When a metric named in the question has a governed definition (semantic layer metric, certified mart column, or registered KPI), the analysis computes it from that definition or reports the governed value alongside its own. If the analysis deliberately uses a different formula, the answer names the governed metric, states how the custom formula differs, and labels the result with a name that is not the governed metric's name.

Why: Re-deriving a known metric in a notebook is the quickest way to produce a number that disagrees with the dashboard the stakeholder already trusts. Starting from the standard metric also catches problems early: if the sta…

```sql
-- bad
-- "active users" re-derived from raw events
SELECT COUNT(DISTINCT user_id) FROM raw_events
WHERE event_ts >= CURRENT_DATE - 30
```
```sql
-- good
-- governed metric via the semantic layer
-- dbt sl query --metrics active_users --group-by metric_time__month
-- custom variant, labelled as such:
-- "active_users_incl_sync (differs from governed active_users:
--  includes background sync events)"
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=warning
- sources: Build your metrics; Good Data Analysis

## consume/export-row-cap

*Every bulk export declares a maximum row bound*

Each scheduled extract or bulk export declares a maximum row count; an export whose result exceeds the bound fails loudly instead of delivering a truncated or ballooning file.

Why: Unbounded exports grow silently with the business until a 50 GB CSV breaks the SFTP drop, the vendor parser, or the egress bill; an explicit cap converts silent breakage into an actionable alert.

```sql
-- bad
exports:
  - name: nightly_customer_list
    query: ./customer_list.sql   # no bound declared
```
```sql
-- good
exports:
  - name: nightly_customer_list
    query: ./customer_list.sql
    max_rows: 5000000
    on_overflow: fail
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Run a query

## consume/freshness-displayed

*Dashboards and extracts surface data freshness to viewers*

Each production dashboard and scheduled extract displays the maximum data timestamp (or pipeline run time) of its inputs, and tiles backed by stale inputs beyond the declared SLA show a stale badge instead of silent numbers.

Why: Numbers without a timestamp read as current; decisions get made on yesterday's partial load presented as this morning's truth.

```sql
-- bad
dashboards:
  - name: demand_overview
    tiles: [revenue, orders]   # no freshness tile
```
```sql
-- good
dashboards:
  - name: demand_overview
    tiles: [revenue, orders, data_as_of]
    freshness_sla_hours: 26
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Add data tests to your DAG

## consume/no-deep-offset

*Consumer pagination uses keyset seeks, not deep OFFSET*

Persisted consumer queries (dashboard pagination, extract loops, API backing queries) paginate with keyset conditions on an ordered unique key rather than OFFSET values beyond a small declared bound (default 1000 rows); OFFSET past that bound is rejected.

Why: OFFSET forces the engine to produce and discard every skipped row, so page latency and scanned bytes grow linearly with depth; keyset pagination keeps every page constant-cost and stable under concurrent inserts.

```sql
-- bad
SELECT order_id, amount FROM mart_orders ORDER BY ordered_at LIMIT 10000 OFFSET 1500000
```
```sql
-- good
SELECT order_id, amount FROM mart_orders WHERE (ordered_at, order_id) > ('2026-09-01', 0) ORDER BY ordered_at, order_id LIMIT 10000
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SELECT

## consume/parameterized-dashboard-filters

*Dashboard filters are bound parameters, not concatenated SQL text*

Dashboard and report filter values reach the warehouse as bound parameters or typed filter bindings; no consumer query builds SQL by concatenating user-supplied text.

Why: Concatenated filters invite SQL injection from viewer inputs and break on ordinary values containing quotes; parameter binding removes both failure modes at once.

```sql
-- bad
query = "SELECT * FROM m WHERE store = '" + user_input + "'"
```
```sql
-- good
SELECT store, revenue FROM m WHERE store = {{ bound_parameter('store') }}
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: SELECT Statement

## consume/reverse-etl-idempotent-keys

*Reverse-ETL syncs declare idempotent merge keys*

Each reverse-ETL sync into a SaaS destination declares the destination merge key and runs as an idempotent upsert, so retries and overlapping runs never create duplicate records downstream.

Why: At-least-once delivery plus blind inserts duplicates CRM contacts, double -counts audiences, and triggers repeated customer emails; keyed upserts make every retry safe.

```sql
-- bad
syncs:
  - name: crm_contacts
    operation: insert   # retries duplicate
```
```sql
-- good
syncs:
  - name: crm_contacts
    operation: upsert
    merge_key: external_user_id
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: What is Airflow

## consume/reverse-etl-pii-consent

*Reverse-ETL payloads exclude non-consented PII by policy*

Every reverse-ETL sync declares which personal-data fields it sends and the consent basis for each; fields without a recorded basis are dropped or masked before the payload leaves the warehouse.

Why: SaaS copies of warehouse PII escape warehouse access controls and masking policies; sending emails or phone numbers without consent basis risks regulatory violations and irreversible third-party spread.

```sql
-- bad
syncs:
  - name: ad_audience
    fields: [email, phone, full_name]   # no basis recorded
```
```sql
-- good
syncs:
  - name: ad_audience
    fields: [email_sha256]
    consent_basis: marketing_opt_in_v2
    pii_policy: mask-except-hashed
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: Understanding Dynamic Data Masking; About Secure Data Sharing

## consume/reverse-etl-schema-contract

*Reverse-ETL syncs pin an explicit field mapping contract*

Each reverse-ETL sync declares an explicit source-column to destination -field mapping with types; upstream renames or type changes that break the mapping fail the sync at validation time instead of shipping misaligned data.

Why: Positional or name-matched syncs silently misalign when upstream schemas evolve: amounts land in text notes fields, nulls overwrite good CRM data, and nobody notices until customers complain.

```sql
-- bad
syncs:
  - name: crm_accounts
    source: mart_accounts   # implicit name matching, no mapping
```
```sql
-- good
syncs:
  - name: crm_accounts
    mapping:
      - {from: account_id, to: External_Id__c, type: string}
      - {from: plan_tier, to: Plan__c, type: picklist}
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Add data tests to your DAG
