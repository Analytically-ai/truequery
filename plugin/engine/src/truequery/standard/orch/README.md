# Orchestration standard (draft, staged)

Scope: DAG/task graphs, schedules, sensors, retries, timeouts, lateness
alerts, asset lineage declarations, backfill windowing, CI wiring. NOT data
assertions (see `dq` pack) and NOT environment provisioning/infra sizing.

## Sourcing status

The first staging of this domain lost its sources: every record cited
template placeholders (review finding F1). All records were re-sourced on
2026-10-11 from primary documentation (Apache Airflow 3.3.2 docs, Dagster
docs, dbt docs) plus Astronomer guides, the PostgreSQL manual and Google's
SRE book where noted. Each URL in a record was fetched on that date. Where a
record goes beyond what its sources say, its `supports` field says so, and
`confidence` was reset to match. Paraphrased throughout; no copied text.

Version notes that affect several records: Airflow 3.0 changed
`catchup_by_default` to False, made bare cron strings resolve to
`CronTriggerTimetable`, renamed Datasets to Assets, and removed SLAs
(replaced by Deadline Alerts in 3.1, still experimental).

## Files (16)

- detector (11): concurrency-pools-and-limits, dag-ci-parse-and-unit-tests,
  declared-asset-lineage, deferrable-sensors-with-timeout,
  explicit-catchup-policy, explicit-schedule-timezone,
  idempotent-retries-with-limits, light-dag-parse-no-heavy-top-level,
  no-circular-dependencies, partitioned-assets-windowing (advisory),
  task-timeout-execution-limit.
- contract (2): failure-alert-routing, sla-deadline-alerts.
- runtime (2): idempotent-backfill-windowing, task-atomicity-no-partial-writes.
- judgment (1): data-aware-scheduling-over-polling.

Merged away (review F3): `no-secrets-in-dag-code` into
`ops/prod-credentials-isolation`; `sensor-poke-interval-discipline` into
`deferrable-sensors-with-timeout`; `dag-owner-oncall-declared` into
`failure-alert-routing`; `versioned-reproducible-task-env` into
`py/pin-pipeline-dependencies`. Records from other domains folded into this
one: `py/airflow-no-top-level-io`, `py/airflow-dag-loader-test`,
`ingest/bound-sensor-waits`, `ops/bounded-retries`,
`ops/incident-alert-runbook`.

## Top 5 to enforce first + why

1. `orch/idempotent-backfill-windowing` (error, runtime): a replay over
   INSERT-only logic double-counts every replayed interval.
2. `orch/task-atomicity-no-partial-writes` (error, runtime): partial outputs
   read downstream publish wrong numbers that look successful.
3. `orch/dag-ci-parse-and-unit-tests` (warning, detector): a DAG that fails
   to import disappears without a task failure; a loader test catches it
   before merge.
4. `orch/light-dag-parse-no-heavy-top-level` (warning, detector): parse-time
   I/O slows every parse and can time out the import; a precise AST check.
5. `orch/deferrable-sensors-with-timeout` (warning, detector): the seven-day
   default sensor timeout in poke mode holds worker slots for days.

Credentials in pipeline code are covered by `ops/prod-credentials-isolation`
(error) and should be enforced alongside these.

## Existing tools covering parts (integration notes)

- Airflow itself: DAG import errors and cycle rejection at load, pools,
  Deadline Alerts (3.1+), `airflow backfill create` with range and
  max-active-runs, `[core]` defaults for retries and timeouts. TrueQuery
  findings should map to these native knobs rather than invent new config.
- Dagster: `dg check defs` for definition loading, partitions and backfill
  policies, concurrency pools, run monitoring timeouts, freshness policies,
  run-failure sensors and asset owners.
- dbt: source freshness thresholds cover lateness for source-fed models;
  TrueQuery SLA checks should accept a dbt freshness block as evidence.
- SQLFluff / ruff: style and Python lint only; no orchestration semantics.
- Great Expectations / Soda: runtime data assertions; TrueQuery
  orchestration checks run earlier (parse/CI time) and should gate those
  suites, not duplicate them.
