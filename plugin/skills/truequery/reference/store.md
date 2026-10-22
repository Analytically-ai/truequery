# store — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## store/cluster-on-frequent-filters-joins

*Co-locate data with clustering on frequent filter and join columns*

Tables above ~10 GB (or with recurring filtered queries) must declare a clustering layout — BigQuery CLUSTER BY, Snowflake cluster key, or Delta liquid clustering — over the 1–4 columns most used in filters and joins, with the highest-selectivity filter column first; clustering columns must be low-moderate cardinality, non-repeated top-level columns.

Why: Clustering sorts data within (or across) partitions into blocks so engines skip blocks that cannot match; without it, selective filters still scan everything inside each partition. Wrong or missing clustering is pure was…

```sql
-- bad
CREATE TABLE orders (...) PARTITION BY DATE(order_ts); -- no clustering, queries filter customer_id
```
```sql
-- good
CREATE TABLE orders (...) PARTITION BY DATE(order_ts) CLUSTER BY customer_id, status;
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: BigQuery — Introduction to clustered tables; Snowflake — Clustering Keys and Clustered Tables

## store/compact-small-files-optimize

*Schedule compaction for continuously written lake tables*

Any Delta or Iceberg table fed by streaming or small-batch writes must have a scheduled compaction (Delta OPTIMIZE / auto-compact, Iceberg rewrite-data-files procedure) at least weekly, so average data-file size stays near the engine target (~128 MB–1 GB) instead of accumulating kilobyte files.

Why: Continuous small writes accumulate thousands of tiny files; every read then pays open/list/deserialize overhead per file, query latency collapses, and the table can become effectively unreadable long before the data volu…

```sql
-- bad
-- streaming write, no maintenance job anywhere: df.writeStream.format('delta').start('/events')
```
```sql
-- good
OPTIMIZE events; -- weekly job, plus autoCompact enabled on the table
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Delta Lake — Best practices (compact files); Iceberg — Maintenance (compact data files)

## store/declare-not-null-check-constraints

*Declare NOT NULL and CHECK constraints where the format supports them*

Stored tables must declare NOT NULL on columns that are never legitimately null (especially keys) and CHECK constraints for invariants the engine can enforce (e.g. amount >= 0), on every platform/format that supports constraints (Delta, Postgres-family, BigQuery PK/FK/NN); absence on a supported platform is a violation.

Why: Constraints reject corrupt rows at write time instead of letting them poison joins and aggregates; NOT NULL also documents the physical contract that downstream join and deduplication logic relies on.

```sql
-- bad
CREATE TABLE orders (order_id BIGINT, amount DECIMAL(18,2));
```
```sql
-- good
CREATE TABLE orders (order_id BIGINT NOT NULL, amount DECIMAL(18,2) CHECK (amount >= 0));
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Delta Lake — Constraints (NOT NULL and CHECK)

## store/declare-sort-order-for-range-queries

*Declare sort order for tables serving range and merge workloads*

Tables whose recurring workload is range scans, windowed processing, or MERGE-matching on a sort key must declare that sort order (Iceberg sort order, Snowflake cluster key on the range column, Delta clustering on the range column); evolving the sort order must use the engine's evolution mechanism, not a silent rewrite.

Why: Declared sort order lets engines use min/max file statistics and merge optimizations; without it, range queries and merges scan and shuffle far more than necessary, and ad-hoc "ORDER BY at read time" cannot recover the l…

```sql
-- bad
CREATE TABLE metrics (ts TIMESTAMP, host STRING, v DOUBLE); -- range queries on ts, no order declared
```
```sql
-- good
CREATE TABLE metrics (ts TIMESTAMP, host STRING, v DOUBLE) WRITE ORDERED BY (ts);
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Iceberg — Evolution (sort order evolution); Databricks — Optimization recommendations

## store/enable-change-feed-for-incremental-consumers

*Enable change data feed where downstream reads are incremental*

Delta tables consumed incrementally (streaming reads, CDC outbox, audit consumers) must enable the change data feed (delta.enableChangeDataFeed) rather than forcing consumers to diff full snapshots.

Why: Without CDF, every incremental consumer re-reads whole partitions and hand-rolls change detection — slower, more expensive, and prone to missed deletes; CDF provides row-level change semantics the engine maintains.

```sql
-- bad
CREATE TABLE outbox (id BIGINT, op STRING) USING DELTA; -- consumed by streaming job, no CDF
```
```sql
-- good
CREATE TABLE outbox (id BIGINT, op STRING) USING DELTA TBLPROPERTIES (delta.enableChangeDataFeed = true);
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build
- severity: build=advisory
- sources: Delta Lake — Table utility commands (change feed and history); Databricks — Optimization recommendations

## store/evolve-partitions-by-metadata-not-rewrite

*Change Iceberg partition layouts by metadata evolution, not rewrites*

Changes to an Iceberg table's partition spec must use partition evolution (ADD/REPLACE PARTITION FIELD as a metadata operation) so old and new layouts coexist; rewriting all data files into a new Hive-style directory tree is a violation.

Why: Partition evolution is designed as a metadata-only change: existing files stay valid and old partitions remain queryable, while a full rewrite burns compute, doubles storage transiently, and risks data loss on failure mi…

```sql
-- bad
CREATE TABLE events_new PARTITIONED BY (day(ts)); INSERT OVERWRITE events_new SELECT * FROM events; -- full rewrite for layout change
```
```sql
-- good
ALTER TABLE events REPLACE PARTITION FIELD month(ts) WITH day(ts); -- metadata-only evolution
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: change
- severity: change=advisory
- sources: Iceberg — Evolution (partition evolution); Iceberg — Partitioning (hidden partitioning)

## store/explicit-physical-types-no-implicit-casts

*Stored tables declare explicit physical column types*

DDL for a stored table (or a model with an enforced contract) must declare an explicit physical type for every column; types must not be left to inference from a query, and the final SELECT should cast to the declared types.

Why: Inferred types drift with engine upgrades, source changes, or dialect transpilation (e.g. integer width, timestamp precision, string length), causing silent truncation, failed merges, or cross-engine incompatibility in I…

```sql
-- bad
CREATE TABLE t (id, amount, ts);
```
```sql
-- good
CREATE TABLE t (id BIGINT NOT NULL, amount DECIMAL(18,2), ts TIMESTAMPTZ);
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SQLMesh Models overview — explicit types; dbt — Configure incremental models

## store/full-refresh-by-replace-not-delete-insert

*Full refreshes replace table content atomically, never DELETE plus INSERT*

A full-refresh rebuild of a table must use an atomic replace (CREATE OR REPLACE, Delta REPLACE WHERE / CLONE-replace, Iceberg snapshot swap), never a DELETE FROM followed by INSERT: at no point may readers see an empty or half-loaded table.

Why: DELETE-then-INSERT exposes every concurrent reader to a window with missing data and turns a failed reload into total data loss (old rows deleted, new rows never written); atomic replace keeps the old snapshot live until…

```sql
-- bad
DELETE FROM marts.orders; INSERT INTO marts.orders SELECT ...;
```
```sql
-- good
CREATE OR REPLACE TABLE marts.orders AS SELECT ...;
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Delta Lake — Best practices (replace the content or schema of a table); Delta Lake — Table utility commands (clone and restore)

## store/incremental-watermark-partition-alignment

*Align incremental watermarks with the physical partition grain*

Incremental models must filter source reads on the same grain as the target's partition key (event date for daily partitions, hour for hourly), using a declared watermark column and lookback; an incremental filter on a different grain than the partition key is a violation.

Why: Misaligned watermarks either rescan everything (watermark coarser than partitions never prunes) or silently miss late data (watermark finer than the overwrite grain drops rows that arrive after the partition was written)…

```sql
-- bad
-- partitioned by event_date, but incremental filter uses a different key:
SELECT * FROM src WHERE _ingested_at > (SELECT MAX(_ingested_at) FROM {{ this }})
```
```sql
-- good
-- same grain + lookback:
SELECT * FROM src WHERE event_date >= DATE_SUB(CURRENT_DATE, INTERVAL 3 DAY)
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: dbt — Configure incremental models

## store/no-high-cardinality-partition-column

*Never partition on a high-cardinality or unique column*

A partition key must not be a high-cardinality column (order/user/session id or raw timestamp): partition cardinality must stay in the low thousands per table, and no single partition should be expected below ~1 GB in Delta-family tables.

Why: Partitioning by unique IDs explodes into millions of tiny directories/files: listing overhead dominates, pruning fails, compactions never catch up, and small-file reads cost more than the data. This is the classic costly…

```sql
-- bad
PARTITIONED BY (user_id)
```
```sql
-- good
PARTITIONED BY (days(event_ts))
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Delta Lake — Best practices (choose the right partition column); Iceberg — Partitioning (hidden partitioning)

## store/partition-expiration-retention-policy

*Bound partition lifetime with expiration or lifecycle policy*

Append-only event or log tables must declare a bounded lifetime — BigQuery partition_expiration_days, a Snowflake storage lifecycle policy, or a scheduled DELETE/VACUUM of aged partitions — consistent with the privacy and cost policy; unbounded perpetual retention of raw events is a violation.

Why: Raw events accumulate forever by default: storage cost grows linearly, stale PII outlives its legal basis, and every future scan pays for data nobody queries. An explicit expiry turns retention into a reviewed decision.

```sql
-- bad
CREATE TABLE events (...) PARTITION BY DATE(ts); -- no expiry, retained forever
```
```sql
-- good
CREATE TABLE events (...) PARTITION BY DATE(ts) OPTIONS (partition_expiration_days = 400);
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: BigQuery — Manage partitioned tables; Snowflake — Temporary and Transient Tables (table-type retention choices)

## store/partition-on-filter-column

*Large tables are partitioned on a column queries actually filter*

Any table expected to exceed ~1 GB per natural time grain (or already above ~10 GB) must declare a partition spec on a column that recurring queries filter or prune on — typically a date/timestamp truncated to day or coarser — rather than remaining unpartitioned.

Why: Without a prunable partition layout every query scans the full table: linear cost growth, blown SLOs, and runaway BigQuery billed bytes. Partitioning on the filtered grain lets engines skip whole partitions (file pruning…

```sql
-- bad
CREATE TABLE events (event_ts TIMESTAMP, user_id BIGINT, payload STRING);
```
```sql
-- good
CREATE TABLE events (event_ts TIMESTAMP, user_id BIGINT, payload STRING) PARTITION BY DATE(event_ts);
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: BigQuery — Introduction to partitioned tables; Delta Lake — Best practices (choose the right partition column)

## store/partition-predicate-required

*Queries against partitioned tables must constrain the partition column*

Every recurring or reviewed SELECT against a partitioned table must include a filter on (a transform of) the partition column, or an explicit full-scan justification recorded in the review; ad-hoc one-off exploration is exempt.

Why: Partitioning only saves money when queries prune: one unfiltered scheduled query per day against a multi-TB table can dominate the entire warehouse bill and starve concurrency.

```sql
-- bad
SELECT user_id, COUNT(*) FROM events GROUP BY 1; -- events partitioned by event_date, no filter
```
```sql
-- good
SELECT user_id, COUNT(*) FROM events WHERE event_date >= DATE '2026-09-01' GROUP BY 1;
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: explore,assure
- severity: assure=warning, explore=warning
- sources: BigQuery — Introduction to partitioned tables; Databricks — Optimization recommendations (dynamic file pruning)

## store/prefer-auto-liquid-clustering-over-manual-tune

*Prefer automatic or liquid clustering over hand-tuned sort keys*

On platforms that offer it (BigQuery automatic re-clustering, Snowflake automatic clustering, Delta liquid clustering), teams must use the automatic mechanism rather than hand-maintained sort keys, Z-ORDER schedules, or periodic manual rewrites — unless a benchmark in the repo justifies manual control.

Why: Hand-tuned sort layouts rot as data distribution shifts and every manual rewrite is a full-scan maintenance bill; automatic clustering adapts incrementally and removes a recurring human tuning loop that is usually stale.

```sql
-- bad
OPTIMIZE events ZORDER BY (user_id); -- weekly cron, no benchmark on file
```
```sql
-- good
ALTER TABLE events CLUSTER BY AUTO (user_id); -- platform maintains layout incrementally
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Delta Lake — Use liquid clustering for Delta tables; Snowflake — Clustering Keys and Clustered Tables

## store/rewrite-manifests-and-stats-periodically

*Rewrite manifests and refresh statistics on churned Iceberg tables*

Iceberg tables with frequent writes, deletes, or partition evolution must run scheduled manifest rewrites (rewrite_manifests) and statistics refreshes (compute table/partition statistics) so planning stays metadata-only.

Why: Each commit adds manifest entries; without rewrites, query planning lists thousands of tiny manifests and file-level stats go stale, so pruning degrades into full scans even though the data layout is fine.

```sql
-- bad
-- 2 years of MERGE workload, zero maintenance procedures scheduled
```
```sql
-- good
CALL catalog.system.rewrite_manifests('db.events'); -- weekly job
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: assure
- severity: assure=warning
- sources: Iceberg — Maintenance (rewrite manifests, compute statistics)

## store/single-open-format-per-medallion-layer

*Standardize one open table format per medallion layer*

Each medallion layer (bronze/raw, silver, gold) in a lakehouse must use a single declared open table format (all-Delta or all-Iceberg per layer) with cross-format reads restricted to explicitly listed migration tables; a layer mixing formats by accident is a violation.

Why: Mixed formats per layer double the maintenance surface (two VACUUM/OPTIMIZE regimes, two time-travel semantics), break uniform governance and cloning, and make every reader negotiate format per table instead of per layer…

```sql
-- bad
-- silver layer: 40 Delta tables + 12 Iceberg tables, no migration list
```
```sql
-- good
-- silver layer: all Iceberg; bronze→silver migration allowlist in contract tracks 2 remaining Delta tables
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build
- severity: build=advisory
- sources: Delta Lake — Best practices (overview); Iceberg — Maintenance (overview)

## store/time-travel-retention-explicit

*Declare an explicit time-travel and retention window per table class*

Every production table (or its database/schema default) must declare an explicit historical window — Snowflake DATA_RETENTION_TIME_IN_DAYS, BigQuery table history expectations, Delta log/VACUUM retention, Iceberg snapshot-retention settings — matched to its recovery class (e.g. finance ≥ 30 days where edition allows); relying on silent platform defaults is a violation.

Why: Defaults are short (Snowflake standard 1 day, Delta VACUUM default 7 days, BigQuery travel window 7 days) and differ per platform; a team that assumes "we can always go back a month" discovers after an incident that hist…

```sql
-- bad
CREATE TABLE finance.ledger (...); -- no retention declared, default applies
```
```sql
-- good
CREATE TABLE finance.ledger (...) DATA_RETENTION_TIME_IN_DAYS = 30;
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Snowflake — Understanding and using Time Travel; BigQuery — Data retention with time travel and fail-safe

## store/use-transient-temporary-for-ephemeral

*Use transient or temporary tables for ephemeral and staging data*

Staging, scratch, and reproducible intermediate tables in Snowflake must be created TRANSIENT or TEMPORARY rather than permanent, unless the contract records a recovery requirement for them.

Why: Permanent tables pay Time Travel plus 7-day fail-safe storage on every byte including throwaway staging copies; transient/temporary tables drop fail-safe and bound retention, which is the cheapest correct lifetime for da…

```sql
-- bad
CREATE TABLE staging.orders_load (...);
```
```sql
-- good
CREATE TRANSIENT TABLE staging.orders_load (...);
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Snowflake — Temporary and Transient Tables

## store/vacuum-expire-snapshots-with-retention-floor

*Expire snapshots and vacuum only above a declared retention floor*

Snapshot-expiration (Iceberg expire_snapshots) and file-removal (Delta VACUUM, Iceberg delete-orphan-files) jobs must declare a retention floor of at least 7 days (168 hours), refuse to run below it, and run on a schedule — never as ad-hoc zero-retention cleanups to reclaim space.

Why: VACUUM with RETAIN 0 or aggressive snapshot expiry permanently destroys the only recovery path from a bad overwrite and breaks concurrent readers and time travel; the disk savings are one-off while the data-loss risk is …

```sql
-- bad
VACUUM events RETAIN 0 HOURS;
```
```sql
-- good
VACUUM events RETAIN 168 HOURS; -- weekly job, floor declared in contract
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: assure,change
- severity: assure=error, change=error
- sources: Delta Lake — Table utility commands (VACUUM / history); Iceberg — Maintenance (expire snapshots, remove orphan files)
