# model — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## model/dbt-contract-enforced

*Public marts declare enforced column-name and type contracts*

Every model exposed to other teams or projects (public marts) must declare an enforced contract listing each output column name and data type, so a breaking shape change fails the build before data ships.

Why: An unannounced renamed or retyped column breaks every downstream query and dashboard at read time. Contracts convert that runtime breakage into a build-time failure at the source.

```sql
-- bad
# schema.yml — no contract
models:
  - name: fct_orders
    description: orders mart
```
```sql
-- good
models:
  - name: fct_orders
    config:
      contract: { enforced: true }
    columns:
      - name: order_id
        data_type: int
      - name: total
        data_type: numeric
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Model contracts

## model/delete-handling-incremental

*Incremental models declare how source deletes are handled*

Every incremental model must record its delete policy in its config or contract (ignore with justification, propagate via delete-plus-insert or merge with delete branch, or track via snapshot) and implement exactly that policy; silent default-ignoring of deletes is not allowed.

Why: Most incremental strategies only insert or upsert, so deleted source rows live on forever as ghost records. Undead rows overstate active counts and can leak data that a user asked to have removed.

```sql
-- bad
{{ config(materialized='incremental', incremental_strategy='append') }}
SELECT user_id, email FROM raw_users;  -- deletes never propagate
```
```sql
-- good
{{ config(materialized='incremental', incremental_strategy='delete+insert',
           unique_key='user_id', delete_policy='propagate') }}
SELECT user_id, email FROM raw_users
{% if is_incremental() %}
WHERE updated_at > (SELECT MAX(updated_at) - INTERVAL '3 days' FROM {{ this }})
{% endif %};
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: About incremental strategy; Table deletes, updates, and merges (Delta Lake)

## model/explicit-column-lists

*Persisted SQL lists its columns; SELECT * is for exploration only*

Mode-aware. In explore mode (throwaway ad-hoc queries that nothing else reads) SELECT * is fine and is not flagged. In build and change mode, any persisted SQL must project an explicit column list: model SQL (staging, intermediate, mart), CREATE TABLE AS, CREATE VIEW and materialized views, and consumer queries saved behind dashboards, scheduled reports, exports and reverse-ETL syncs. A bare `*` or `alias.*` as the projection is a finding. Narrowing forms that state intent (BigQuery `SELECT * EXCEPT (...)`, Snowflake and DuckDB `SELECT * EXCLUDE (...)`) are reported at advisory, not warning. Views that deliberately mirror one source must say so in a comment or config to be exempt.

Why: A star binds the output shape to whatever the source holds at run or creation time. Upstream additions, renames, reorders and drops then flow silently into stored tables and consumers instead of showing up as a reviewabl…

```sql
-- bad
-- build mode: model or CTAS
CREATE TABLE snap AS SELECT * FROM src;
SELECT * FROM {{ ref('stg_orders') }};
```
```sql
-- good
CREATE TABLE snap AS SELECT a, b FROM src;
SELECT order_id, status, total, updated_at FROM {{ ref('stg_orders') }};
-- explore mode: fine, not flagged
SELECT * FROM mart_orders LIMIT 20;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore,build,change
- severity: build=warning, change=warning, explore=warning
- sources: SELECT statement (DuckDB); About dbt models; SELECT (PostgreSQL); INSERT (PostgreSQL); SQLMesh Models overview

## model/full-refresh-parity

*Incremental logic reproduces full-refresh results*

The incremental path of a model must return the same rows as a full refresh over the same input: teams verify this by periodically comparing a full refresh against the incremental table and treating divergence as a defect.

Why: Incremental filters, lookbacks, and merge keys drift from the full logic over time (schema changes, late data, strategy edits), so the production table quietly diverges from what the SQL claims. Parity checks catch the d…

```sql
-- bad
SELECT ... FROM raw_events
{% if is_incremental() %}
WHERE event_type != 'refund'  -- logic differs from full refresh
{% endif %};
```
```sql
-- good
SELECT ... FROM raw_events;  -- identical logic both paths
{% if is_incremental() %}   -- only the watermark filter differs
WHERE occurred_at > (SELECT MAX(occurred_at) - INTERVAL '3 days' FROM {{ this }})
{% endif %};
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Configure incremental models; SQLMesh overview

## model/grain-preserved-joins

*Joins preserve the declared grain of the model*

Every multi-table model must declare its grain (the columns that make one row unique) and every join in the model must preserve that grain: each many-side input is pre-aggregated or deduplicated to one row per join key before joining, unless the grain change is intentional and documented.

Why: A join against an un-deduplicated many-side input silently multiplies rows (fanout), inflating measures such as revenue or counts. Wrong numbers from fanout are among the costliest warehouse failures because downstream c…

```sql
-- bad
SELECT o.order_id, o.total, l.sku
FROM orders o JOIN order_lines l ON l.order_id = o.order_id;
-- grain was one row per order; now one row per line, total double-counts
```
```sql
-- good
WITH lines AS (
  SELECT order_id, COUNT(*) AS line_cnt, SUM(amount) AS lines_total
  FROM order_lines GROUP BY order_id
)
SELECT o.order_id, o.total, lines.line_cnt
FROM orders o LEFT JOIN lines ON lines.order_id = o.order_id;
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: How we structure our dbt projects; Design Tip 81 Fact Table Surrogate Key

## model/idempotent-reexec

*Models are idempotent under re-execution*

Re-running any model over the same input time range must produce the same output rows: no append-without-dedupe, no non-deterministic functions in keys or filters, and no reliance on run time as business time.

Why: Non-idempotent models turn every retry, backfill, or failed run into data duplication or loss. Pipeline retries are routine, so re-execution safety prevents broken pipelines from becoming wrong numbers.

```sql
-- bad
{{ config(materialized='incremental', incremental_strategy='append') }}
SELECT *, CURRENT_TIMESTAMP AS loaded_at FROM raw_events;
-- rerun duplicates every row
```
```sql
-- good
{{ config(materialized='incremental', incremental_strategy='merge', unique_key='event_id') }}
SELECT event_id, occurred_at, payload FROM raw_events
{% if is_incremental() %}
WHERE occurred_at > (SELECT MAX(occurred_at) - INTERVAL '3 days' FROM {{ this }})
{% endif %};
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Idempotence in dbt; About incremental models

## model/incremental-lookback-window

*Incremental watermark filters include a lookback window for late data*

Incremental models whose sources can arrive late or be updated after first arrival must subtract a documented lookback window (e.g. 3 days) from the watermark maximum, combined with a merge/upsert strategy, so late rows are picked up on subsequent runs.

Why: A strict greater-than-max filter permanently drops late-arriving and restated rows, producing missing or understated facts that are hard to notice. Lookback windows are the standard guard against this silent loss.

```sql
-- bad
{% if is_incremental() %}
WHERE occurred_at > (SELECT MAX(occurred_at) FROM {{ this }})
{% endif %}
```
```sql
-- good
{% if is_incremental() %}
WHERE occurred_at > (SELECT MAX(occurred_at) - INTERVAL '3 days' FROM {{ this }})
{% endif %}
-- with unique_key + merge strategy so reprocessed rows upsert cleanly
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Configure incremental models; About microbatch incremental models

## model/incremental-watermark-filter

*Incremental models filter source rows on a monotonic watermark*

Every incremental model must declare a monotonic watermark column (event or updated timestamp) and, when running incrementally, filter source rows to watermark values newer than the current table maximum, using the incremental-run guard of the framework.

Why: Without a watermark filter each incremental run rescans and reprocesses the full source history, causing runaway cost and run times; worse, a missing filter can silently reprocess nothing or everything depending on strat…

```sql
-- bad
{{ config(materialized='incremental', unique_key='event_id') }}
SELECT event_id, occurred_at, payload FROM raw_events;
```
```sql
-- good
{{ config(materialized='incremental', unique_key='event_id') }}
SELECT event_id, occurred_at, payload FROM raw_events
{% if is_incremental() %}
WHERE occurred_at > (SELECT MAX(occurred_at) FROM {{ this }})
{% endif %};
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Configure incremental models; About incremental models

## model/jinja-macro-determinism

*Macros are deterministic and free of hidden runtime calls*

Macros that generate model logic must be deterministic for a given input: no unseeded randomness, no wall-clock calls inside compiled predicates or keys, and no queries that change result between compile and run, unless the non-determinism is the documented purpose (e.g. sampling).

Why: Hidden volatility in macros makes identical runs produce different data or different SQL per environment, breaking idempotency and making failures unreproducible across dev and production.

```sql
-- bad
{% macro recent_filter(col) %}
WHERE {{ col }} > CURRENT_TIMESTAMP - INTERVAL '1 day'  -- shifts every run
{% endmacro %}
```
```sql
-- good
{% macro recent_filter(col, cutoff) %}
WHERE {{ col }} > '{{ cutoff }}'  -- caller passes an explicit boundary
{% endmacro %}
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Jinja and macros; Idempotence in dbt

## model/jinja-no-nested-curlies

*Jinja logic lives in macros, never in nested curlies*

Model SQL must not nest Jinja delimiters inside one another; repeated or conditional SQL fragments must be extracted into tested macros instead of inline nested expressions.

Why: Nested curlies fail in confusing ways at compile time and hide logic that reviewers cannot trace, so model behavior diverges from what the SQL appears to say. Extracted macros are reviewable and testable in one place.

```sql
-- bad
SELECT * FROM {{ ref({{ var('mart_name') }}) }};
```
```sql
-- good
-- macros/resolve_mart.sql
{% macro resolve_mart(name) %}{{ ref(name) }}{% endmacro %}
SELECT order_id FROM {{ resolve_mart(var('mart_name')) }};
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Don't nest your curlies; Jinja and macros

## model/materialization-choice

*Materialization fits the model layer and refresh need*

Staging models default to views, intermediate models to views or ephemeral where the warehouse supports them, and marts to tables or incremental models; any deviation (e.g. a view mart, a table staging) must carry a one-line justification, and ephemeral models must not be referenced by snapshots or exposed outputs.

Why: Wrong materializations waste money (tables rebuilt for throwaway staging) or break consumers (views recomputed on every dashboard load, ephemeral models invisible to snapshots). Layer-appropriate defaults prevent both.

```sql
-- bad
-- models/staging/stg_orders.sql
{{ config(materialized='table') }}
SELECT id AS order_id FROM raw_orders;
```
```sql
-- good
-- models/staging/stg_orders.sql
{{ config(materialized='view') }}
SELECT id AS order_id FROM raw_orders;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore,build
- severity: build=advisory, explore=advisory
- sources: Materializations; Best practice guides

## model/merge-source-deduped

*Merge sources are deduplicated to one row per target key*

Before any MERGE or merge-strategy incremental run, the source query must be reduced to at most one row per target merge key (dedupe with explicit pick-the-winner logic), because engines apply only one match per target row.

Why: Duplicate source rows per target key make the merge outcome engine- and order-dependent: one duplicate wins silently and the loser (often the newer record) is lost. The target then holds stale values with no error raised…

```sql
-- bad
MERGE INTO customers t USING raw_customers s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET balance = s.balance;
-- raw_customers may hold several rows per customer_id
```
```sql
-- good
WITH ranked AS (
  SELECT s.*, ROW_NUMBER() OVER (
    PARTITION BY customer_id ORDER BY updated_at DESC) AS rn
  FROM raw_customers s
)
MERGE INTO customers t USING (SELECT * FROM ranked WHERE rn = 1) s
ON t.customer_id = s.customer_id
WHEN MATCHED THEN UPDATE SET balance = s.balance;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: MERGE (Snowflake); MERGE (Postgres); MERGE statement (BigQuery DML)

## model/microbatch-event-time

*Microbatch models partition on event time with explicit batch size*

Microbatch incremental models must declare an event-time column and an explicit batch size, process only closed batches, and reject or quarantine rows stamped in the future relative to run time.

Why: Batching on processing time instead of event time reprocesses open, still-arriving intervals on every run (cost, duplicates), while future timestamps poison watermarks so subsequent batches silently skip real data.

```sql
-- bad
{{ config(materialized='incremental', incremental_strategy='microbatch') }}
-- no event_time, framework falls back to run-time batching
```
```sql
-- good
{{ config(materialized='incremental', incremental_strategy='microbatch',
           event_time='occurred_at', batch_size='day', begin_date='2024-01-01') }}
SELECT * FROM raw_events WHERE occurred_at < CURRENT_TIMESTAMP;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: About microbatch incremental models

## model/null-join-keys-excluded

*Null join keys are handled explicitly, never silently dropped*

Every join on a nullable key must state its null policy in the model: filter nulls before the join, coalesce to an explicit default, or route null-key rows to a quarantine path. Inner joins that silently drop nulls are not acceptable without that statement.

Why: Equality joins never match nulls, so rows with null keys vanish without an error. The missing rows understate facts and the loss is invisible in ordinary row-count monitoring.

```sql
-- bad
SELECT o.order_id, p.promo_name
FROM orders o JOIN promotions p ON p.code = o.promo_code;
-- null promo_code rows vanish
```
```sql
-- good
SELECT o.order_id, COALESCE(p.promo_name, 'no-promo') AS promo_name
FROM orders o LEFT JOIN promotions p ON p.code = o.promo_code;
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SELECT statement (DuckDB)

## model/snapshot-scd2-validity

*SCD2 snapshots keep exactly one open record per key with valid windows*

Every snapshot or hand-built slowly-changing-dimension type 2 table must guarantee per natural key: exactly one open (current) record, validity windows that do not overlap, and contiguous coverage (each new start equals the prior end) unless gaps are documented.

Why: Overlapping or doubled SCD2 windows join facts to two dimension versions at once (fanout, wrong numbers); gaps drop facts from point-in-time joins (missing rows). Both corrupt every historical report built on the dimensi…

```sql
-- bad
-- two open rows for customer 7:
-- (7, '2024-01-01', NULL), (7, '2024-06-01', NULL)
```
```sql
-- good
-- (7, '2024-01-01', '2024-06-01'), (7, '2024-06-01', NULL)
-- enforced by snapshot unique_key + dbt_valid_from/dbt_valid_to logic
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Add snapshots to your DAG; Slowly changing data (SCD) Type 2 operation into Delta tables

## model/snapshot-strategy-choice

*Snapshot strategy matches source change-tracking capability*

Snapshot models must use timestamp strategy when the source provides a reliable updated-at column, and check strategy over an explicit column list only when it does not; the choice and, for check strategy, the tracked columns must be recorded on the snapshot.

Why: A check strategy over all columns fires on irrelevant changes and balloons history; a timestamp strategy on an unreliable column misses real changes. Either way history silently diverges from the source and audits fail.

```sql
-- bad
{% snapshot prod_snapshot %}
{{ config(strategy='check', check_cols='all', unique_key='sku') }}
SELECT * FROM raw_products;
{% endsnapshot %}
```
```sql
-- good
{% snapshot prod_snapshot %}
{{ config(strategy='timestamp', updated_at='updated_at', unique_key='sku') }}
SELECT sku, name, price, updated_at FROM raw_products;
{% endsnapshot %}
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore,build
- severity: build=warning, explore=warning
- sources: Add snapshots to your DAG

## model/staging-no-business-logic

*Staging models mirror sources with renames and casts only*

Staging models must map one-to-one to a single source with only light preparation (renaming, type casting, basic cleanup); joins, aggregations, and business rules belong in intermediate or mart layers.

Why: Business logic buried in staging is reused blindly by every downstream model, so one wrong filter or join corrupts the whole project at once. Thin staging keeps the blast radius of a fix small and lineage readable.

```sql
-- bad
-- stg_orders.sql
SELECT o.order_id, SUM(l.amount) AS total   -- aggregation in staging
FROM raw_orders o JOIN raw_lines l ON l.order_id = o.order_id
WHERE o.status = 'complete' GROUP BY o.order_id;
```
```sql
-- good
-- stg_orders.sql
SELECT id AS order_id, status, total_cents / 100.0 AS total
FROM raw_orders;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=advisory, change=advisory
- sources: How we structure our dbt projects

## model/surrogate-keys-stable

*Surrogate keys are deterministic hashes of natural keys*

Surrogate keys in dimensions and merge keys must be computed as deterministic hashes of the natural key (e.g. md5 of the key columns), not as row numbers, sequences, or random values that change between runs.

Why: Positional keys shift on every rebuild, breaking merge matching, SCD2 continuity, and fact-to-dimension joins after any full refresh. Hashed keys survive rebuilds and integrate disparate sources on the same natural key.

```sql
-- bad
SELECT ROW_NUMBER() OVER (ORDER BY email) AS customer_key, email FROM stg;
```
```sql
-- good
SELECT MD5(LOWER(TRIM(email))) AS customer_key, email FROM stg;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: Design Tip 81 Fact Table Surrogate Key

## model/unique-key-merge-key

*Incremental merge models declare a non-null unique merge key*

Every model using a merge or delete-plus-insert incremental strategy must declare its merge key (unique_key) and every key column must be NOT NULL with one row per key value in each processed batch.

Why: Null or duplicated merge keys make upserts nondeterministic: matched rows update the wrong target, or one source row silently wins among duplicates. The result is duplicated or missing rows in production tables.

```sql
-- bad
{{ config(materialized='incremental', incremental_strategy='merge') }}
SELECT email, name FROM raw_customers;  -- no unique_key, nulls allowed
```
```sql
-- good
{{ config(materialized='incremental', incremental_strategy='merge', unique_key='customer_id') }}
SELECT customer_id, email, name FROM raw_customers WHERE customer_id IS NOT NULL;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: About incremental strategy; MERGE statement (BigQuery DML); MERGE (Postgres)
