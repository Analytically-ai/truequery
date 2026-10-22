# Storage and physical design (`store`) — draft practice set

Scope: table formats (Iceberg/Delta), partitioning, clustering/sort keys, DDL
(wildcard DDL is covered by `model/explicit-column-lists`), retention, and time travel. Warehouse sizing
and slot/compute advice are intentionally out of scope (advisory at most).

All cited URLs were live-fetched on
2026-10-12 and are paraphrased, not quoted.

## Top 5 to enforce first (and why)

1. `vacuum-expire-snapshots-with-retention-floor` (detector, error) — a
   single `VACUUM RETAIN 0` destroys the only recovery path; irreversible
   data loss beats every performance concern.
2. `full-refresh-by-replace-not-delete-insert` (detector, warning) —
   DELETE+INSERT exposes readers to empty tables and turns a failed reload
   into total loss; atomic replace is checkable from code alone. Pairs with
   `ops/full-refresh-approval`.
3. `no-high-cardinality-partition-column` (contract, warning) — partitioning
   by user/order id creates millions of tiny files; exact once cardinality
   is declared, a name-based hint otherwise.
4. `time-travel-retention-explicit` (contract, warning) — silent platform
   defaults decide how far back you can recover; declaring the window per
   recovery class makes it reviewable.
5. `compact-small-files-optimize` (runtime, warning) — streaming/small-batch
   writes silently degrade every read; file-count sampling makes it
   measurable.

Wildcard DDL (`SELECT *` in CTAS and views) is covered by
`model/explicit-column-lists`.

## Existing tools covering parts (integration notes)

- SQLFluff: layout/style and some structural lint; does not understand partition
  specs, clustering keys, or retention semantics — TrueQuery `store` rules
  complement it (cf. `model/explicit-column-lists`, `declare-not-null-check-constraints`).
- dbt Project Evaluator / dbt contracts: covers model-layer config discipline
  (materializations, contracts, incremental watermarks) — overlaps
  `explicit-physical-types`, `incremental-watermark-partition-alignment`;
  TrueQuery should defer to dbt's contract where present and check the physical
  DDL it renders.
- Great Expectations / Soda: row-level data validation, not physical layout —
  no overlap on partitioning/clustering/retention; `declare-not-null-check-constraints`
  is the bridge (engine-enforced constraints vs GX/Soda post-hoc checks).
- Platform-native automation (BigQuery auto re-clustering, Snowflake automatic
  clustering, Delta auto-optimize/auto-compact, Iceberg catalog procedures):
  covers the maintenance execution — TrueQuery checks the declaration and
  scheduling (`prefer-auto-liquid-clustering`, `rewrite-manifests`), not the
  execution itself.

## Files

19 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| cluster-on-frequent-filters-joins | contract | warning | no |
| compact-small-files-optimize | runtime | warning |  |
| declare-not-null-check-constraints | contract | warning | no |
| declare-sort-order-for-range-queries | contract | advisory | no |
| enable-change-feed-for-incremental-consumers | contract | advisory | no |
| evolve-partitions-by-metadata-not-rewrite | judgment | advisory |  |
| explicit-physical-types-no-implicit-casts | contract | warning | no |
| full-refresh-by-replace-not-delete-insert | detector | warning |  |
| incremental-watermark-partition-alignment | contract | warning | no |
| no-high-cardinality-partition-column | contract | warning | no |
| partition-expiration-retention-policy | contract | warning | no |
| partition-on-filter-column | contract | warning | no |
| partition-predicate-required | runtime | warning |  |
| prefer-auto-liquid-clustering-over-manual-tune | judgment | advisory |  |
| rewrite-manifests-and-stats-periodically | runtime | warning |  |
| single-open-format-per-medallion-layer | judgment | advisory |  |
| time-travel-retention-explicit | contract | warning | no |
| use-transient-temporary-for-ephemeral | detector | warning |  |
| vacuum-expire-snapshots-with-retention-floor | detector | error |  |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 3 |
| contract | 10 |
| runtime | 3 |
| judgment | 3 |
| **total** | **19** |

Severity: 1 error, 13 warning, 5 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

