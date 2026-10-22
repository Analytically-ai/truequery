# TrueQuery standard pack: Transformation and modeling (`model`)

Scope: SQL and Python model logic inside the warehouse — joins and grain,
incremental watermarks, lookback windows, unique and merge keys, snapshot/SCD
handling, Jinja macros, idempotency, layering (staging/intermediate/marts),
and dbt model contracts. Out of scope: metric definitions (pack `sem`) and
test-assertion authoring (pack `dq`); those packs own that machinery.

File naming follows
`standard/<domain>/<slug>.yaml` with `id: model/<slug>`.

## Enforce these five first

1. `model/grain-preserved-joins` (runtime, error) — fanout joins are the
   fastest route to wrong numbers; a runtime grain check catches them on
   every build.
2. `model/idempotent-reexec` (runtime, error) — retries and backfills are
   routine; without re-execution safety each one risks duplicated rows.
3. `model/merge-source-deduped` (detector, error) — duplicate merge keys
   silently apply stale values; the dedupe step is cheap and checkable in
   code review.
4. `model/unique-key-merge-key` (detector, error) — a merge strategy with no
   unique_key appends duplicates on every re-run; the missing config is
   visible in code.
5. `model/delete-handling-incremental` (contract) — ghost rows overstate
   actives and can retain data users asked to delete (privacy exposure,
   not just staleness).

dbt-community conventions (`staging-no-business-logic`,
`materialization-choice`, `dbt-contract-enforced`) are advisory and are not
enforced by the checker. `explicit-column-lists` is now the single no-`SELECT *`
record for the whole standard and is mode-aware.

## Existing tool coverage (integration points)

- dbt Project Evaluator (`dbt-project-evaluator`): covers project-structure
  conventions overlapping `staging-no-business-logic` and
  `materialization-choice` (layering, materialization defaults). TrueQuery
  should import its violations rather than re-implement structure checks.
- SQLFluff: covers SQL style and some structural lint overlapping
  `explicit-column-lists` (star-select rules; SQLFluff's are not mode-aware) and general model readability.
  TrueQuery adds the semantic layer (grain, watermark, key rules) SQLFluff
  cannot see.
- Ruff: covers Python syntax and lint for `py/deterministic-model-outputs` (which absorbed the former
  `model/python-model-determinism`)
  (unused imports, obvious nondeterminism helpers). TrueQuery adds the
  data-specific rules (ordering before dedupe, pinned deps, seed policy).
- Great Expectations: covers runtime data validation overlapping the runtime
  halves of `snapshot-scd2-validity`, `unique-key-merge-key`, and
  `full-refresh-parity` (uniqueness, not-null, row-count expectations run
  against the warehouse). TrueQuery should emit GX-compatible expectations
  where possible instead of duplicating runners.

## Files

20 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| dbt-contract-enforced | contract | advisory | no |
| delete-handling-incremental | contract | warning | no |
| explicit-column-lists | detector | warning |  |
| full-refresh-parity | runtime | error |  |
| grain-preserved-joins | runtime | error |  |
| idempotent-reexec | runtime | error |  |
| incremental-lookback-window | contract | warning | no |
| incremental-watermark-filter | detector | warning |  |
| jinja-macro-determinism | detector | warning |  |
| jinja-no-nested-curlies | detector | warning |  |
| materialization-choice | detector | advisory |  |
| merge-source-deduped | detector | error |  |
| microbatch-event-time | detector | warning |  |
| null-join-keys-excluded | contract | warning | no |
| snapshot-scd2-validity | runtime | error |  |
| snapshot-strategy-choice | judgment | warning |  |
| staging-no-business-logic | detector | advisory |  |
| surrogate-keys-stable | detector | error |  |
| unique-key-merge-key | detector | error |  |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 10 |
| contract | 4 |
| runtime | 4 |
| judgment | 1 |
| **total** | **19** |

Severity: 7 error, 9 warning, 3 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

