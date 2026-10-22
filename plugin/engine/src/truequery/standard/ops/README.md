# TrueQuery Standard — Change and Operations (`ops`)

Scope: CI/CD, PR checks, plan-apply previews, disposable environments,
model versioning, cost guardrails, history-first drift detection,
incident handling, and warehouse adapter setup (adapter credential scoping
now lives in `gov/least-privilege-grants`). Transform logic,
modeling patterns, and language correctness live in other domains.

## Top 5 to enforce first (and why)

1. `ops/ci-pr-checks-required` (contract, warning) — A mandatory PR
   build-plus-test gate is the cheapest place to stop wrong numbers and
   broken SQL. Without it, every other practice is advisory.
2. `ops/plan-preview-before-apply` (contract, warning) — Reviewing the
   affected-model and backfill diff before prod apply prevents one-line
   changes from silently rewriting history.
3. `ops/model-versioning-breaking-changes` (detector, warning) — now the one
   breaking-change record (contract diffs, governed-metric changes,
   deprecation windows); a base/head diff catches in-place breaking edits.
4. `ops/full-refresh-approval` (detector, warning) — full refreshes are
   opt-in and recoverable (clone or time travel first); see
   `store/full-refresh-by-replace-not-delete-insert` for atomicity.
5. `ops/cost-estimate-before-run` (runtime, warning) — Pre-execution
   estimates (BigQuery dry run, Snowflake EXPLAIN) turn surprise bills into
   reviewable numbers while the query can still be rewritten.

Least-privilege adapter credentials moved to `gov/least-privilege-grants`.

## Existing tools covering parts (integration notes)

- dbt Cloud CI jobs + branch protection: covers `ci-pr-checks-required`
  (required status checks on PRs) and `disposable-dev-environments`
  (per-PR isolated schemas). TrueQuery should read CI job outcomes,
  not reimplement them.
- dbt State (`state:modified+`, deferral): covers
  `ci-slim-builds-only-changed`. TrueQuery's detector checks that CI
  commands use state selection rather than full rebuilds.
- SQLMesh plans + environments + change categories: covers
  `plan-preview-before-apply`, `breaking-change-categorization`,
  `disposable-dev-environments`, `dev-environment-expiry`, and
  `full-refresh-approval`. TrueQuery should consume plan output as the
  review artifact.
- dbt model versions (Mesh governance): covers
  `model-versioning-breaking-changes`, including its deprecation window.
- BigQuery dry runs / Snowflake EXPLAIN: cover
  `cost-estimate-before-run` and `cost-guardrail-confidence-levels`.
  TrueQuery must respect the honesty split (exact dollars vs warn-only
  estimates) rather than abstracting it away.
- Airflow best practices (staging env, upgrade/backup discipline) and
  dbt job monitoring/alerts: cover `staged-upgrades-with-backup`,
  `orch/idempotent-retries-with-limits` (formerly `ops/bounded-retries`),
  `run-artifact-retention`, and
  `post-deploy-data-validation`. TrueQuery checks configuration and
  gates; the orchestrator executes.
- Apache Iceberg snapshots/time travel: covers
  `pre-destructive-snapshot` (clone before overwrite) and underpins
  `drift-detection-history-first` (cheap metadata history before
  rescans).

## Sources

All sources live-fetched 2026-10-12; claims paraphrased, no copied text.

## Files

18 practices, all `status: draft`. `contract_required` applies only to `contract` records: `yes` means part of the small required set (grain, keys, PII classification); `no` means optional.

| slug | enforcement | severity | contract_required |
|---|---|---|---|
| breaking-change-categorization | judgment | warning |  |
| ci-pr-checks-required | contract | warning | no |
| ci-slim-builds-only-changed | contract | warning | no |
| cost-estimate-before-run | runtime | warning |  |
| cost-guardrail-confidence-levels | judgment | advisory |  |
| dev-environment-expiry | contract | warning | no |
| disposable-dev-environments | contract | warning | no |
| drift-detection-history-first | runtime | warning |  |
| full-refresh-approval | detector | warning |  |
| model-versioning-breaking-changes | detector | warning |  |
| plan-preview-before-apply | contract | warning | no |
| post-deploy-data-validation | runtime | warning |  |
| pre-destructive-snapshot | judgment | warning |  |
| prod-credentials-isolation | detector | error |  |
| run-artifact-retention | contract | warning | no |
| staged-upgrades-with-backup | contract | warning | no |

## Enforcement counts

| enforcement | count |
|---|---|
| detector | 3 |
| contract | 7 |
| runtime | 3 |
| judgment | 3 |
| **total** | **16** |

Severity: 1 error, 14 warning, 1 advisory. `error` is reserved for proven data loss, wrong answers, or security exposure.

