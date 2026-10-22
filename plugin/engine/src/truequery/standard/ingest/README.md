# Ingestion Practice Pack (`ingest`) — DRAFT, staged

Scope: connectors, batch and streaming intake, CDC, file intake, seeds,
source freshness declarations, and intake boundary contracts. NOT
source-system internals (app instrumentation, CDC source tuning) and NOT
in-warehouse reshaping after landing (see `model` pack).

All sources below were live-fetched on 2026-10-12. Practices paraphrase;
no copied text.

## Top 5 to enforce first (and why)

1. **declare-source-freshness** (detector) — the cheapest tripwire for
   stalled connectors, and now the one canonical freshness record (it
   absorbed the tiered-SLO and dq timeliness records).
2. **cdc-replica-identity-required** (runtime, error) — a CDC table without
   a usable replica identity loses updates and deletes silently; checkable
   against the source catalog before the connector goes live.
3. **raw-landing-append-only** (contract, error) — an immutable landing zone
   makes every incident (bad file, poisoned batch, late correction)
   precisely reversible instead of forensic guesswork.
4. **quarantine-invalid-rows-at-intake** (contract) — silent drops become
   wrong numbers with no evidence trail; quarantine preserves
   recoverability.
5. **checkpoint-streaming-intake** (detector, error) — a stream without a
   durable checkpoint re-reads or skips data on restart; the missing option
   is visible in code.

`idempotent-batch-loads` remains the costliest common intake failure, but
from code alone it is only a heuristic, so it is advisory here; the
re-run check itself is runtime (`model/idempotent-reexec`).

## Existing tools covering parts (integration notes)

- **dbt source freshness + data tests** cover declare-source-freshness,
  reference-sources-not-raw-tables, and seeds-small-static-only (project
  structure). TrueQuery adds the owner/alert-route half of
  declare-source-freshness and cross-tool uniformity.
- **SQLFluff** covers SQL shape inside loaders but nothing about intake
  semantics (idempotency, quarantine, lineage); complementary.
- **dbt-project-evaluator** covers project-structure hygiene adjacent to
  seeds/sources conventions; TrueQuery ingestion rules go deeper into
  runtime boundary contracts.
- **Great Expectations** covers row-count/dedupe/validity expectations
  (reconcile-row-counts-per-load, dedupe checks) at scan time;
  TrueQuery positions these as required per-load gates with declared
  tolerances.
- **Soda (SodaCL)** covers freshness/schema/duplicate checks and
  failed-row sampling (quarantine evidence); TrueQuery adds the policy
  layer (SLOs, delete propagation, rebuild contracts) Soda does not own.
- **Airflow sensors/deferrable operators** are the mechanism for
  `orch/deferrable-sensors-with-timeout` (which absorbed the former
  `ingest/bound-sensor-waits`) and require-completeness-marker; TrueQuery checks
  the configuration, not the engine.
- **Ruff** (for Python loaders/connectors): code-level hygiene only;
  no intake-semantics overlap.

## Files

25 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| cap-intake-cost-and-volume | runtime | warning |  |
| cdc-replica-identity-required | runtime | error |  |
| checkpoint-streaming-intake | detector | error |  |
| choose-batch-vs-streaming-deliberately | judgment | advisory |  |
| declare-source-freshness | detector | warning |  |
| dedupe-on-natural-key-before-append | judgment | advisory |  |
| handle-schema-drift-explicitly | contract | warning | no |
| idempotent-batch-loads | detector | advisory |  |
| isolate-source-failures | judgment | advisory |  |
| landing-rebuildable-from-source | contract | warning | no |
| monitor-cdc-lag-and-slots | runtime | warning |  |
| pin-connector-and-schema-versions | detector | warning |  |
| pin-file-format-options | detector | warning |  |
| propagate-deletes-explicitly | contract | warning | no |
| quarantine-invalid-rows-at-intake | contract | warning | no |
| raw-landing-append-only | contract | error | no |
| reconcile-row-counts-per-load | runtime | warning |  |
| record-file-lineage-at-intake | detector | warning |  |
| reference-sources-not-raw-tables | detector | warning |  |
| require-completeness-marker | judgment | advisory |  |
| scan-pii-at-landing | contract | error | yes |
| seeds-small-static-only | detector | warning |  |
| test-intake-against-fixtures | detector | advisory |  |
| watermark-and-late-data-policy | contract | warning | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 9 |
| contract | 7 |
| runtime | 4 |
| judgment | 4 |
| **total** | **24** |

Severity: 4 error, 14 warning, 6 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

