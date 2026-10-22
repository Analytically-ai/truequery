# Python-for-data correctness (py)

Scope: Python that runs data pipelines — not general Python style.

In scope:

- Orchestrator/model Python: Airflow DAG and task correctness, Dagster
  asset definitions and resources, dbt Python models, SQLMesh Python models.
- DataFrame-pattern pitfalls: collect-before-filter, N+1 queries in loops,
  chained-assignment bugs, row-loop vectorization.
- Dependency and pinning hygiene for pipeline deployables.
- Hook/adapter/client connection code and secret handling.

Out of scope (ruff territory): formatting, import order, naming, line
length, general lint. Out of scope: ML training internals (optimizers,
hyperparameters) — only the data plumbing around them.

## Top 5 to adopt first

1. `py/idempotent-task-logic` — retries and backfills are the
   orchestrator's core loop; non-idempotent writes turn one transient
   failure into duplicated rows. Cheapest catastrophic failure to prevent.
2. `ops/prod-credentials-isolation` (canonical home of the former
   `py/no-hardcoded-credentials`) — leaked warehouse credentials are a
   privacy/regulatory event, not just a bug. Highest blast radius.
3. `py/pin-pipeline-dependencies` — unpinned deps make identical commits
   resolve to different libraries on retry workers; silent numeric drift.
4. `py/dagster-asset-declared-dependencies` (and `py/dbt-python-model-contract`,
   `py/sqlmesh-python-model-execution-context` for your stack) — hidden
   reads break lineage, so backfills and invalidation silently miss data.
5. `py/pandas-no-chained-assignment` — silent no-op mutations ship dirty
   data with zero errors; one `.loc` habit fixes the whole class.

## Existing tools and what they cover

- Ruff (E/F/W, PLW2901): syntax, unused imports, part of chained
  assignment. Does NOT understand DAG lineage, idempotency, I/O-manager
  bypasses, N+1 query shapes, or interval isolation.
- `dbt parse` / `dbt build`: catches SQL ref errors, not Python-model
  hidden reads or unseeded randomness.
- `sqlmesh plan` + audits: catches what audits assert, not missing
  audits or undeclared external reads.
- pandas Copy-on-Write (default in pandas 3.0): removes cross-object
  side effects but does not warn on dead chained writes; tests still needed.
- pip / pip-tools / uv: pinning machinery; only works if a policy
  (`py/pin-pipeline-dependencies`) requires its use.

Files: one practice per `<slug>.yaml` (15 records). Conflict fragments
live in `CONFLICTS_FRAG.md`.

Merged away to remove cross-domain duplicates (review F3):

- `py/no-hardcoded-credentials` → `ops/prod-credentials-isolation`
- `py/airflow-no-top-level-io` → `orch/light-dag-parse-no-heavy-top-level`
- `py/airflow-dag-loader-test` → `orch/dag-ci-parse-and-unit-tests`

Folded into py records: `model/python-model-determinism` →
`py/deterministic-model-outputs`; `orch/versioned-reproducible-task-env` →
`py/pin-pipeline-dependencies`.

Severity (review F4/F6): `error` is kept only for `py/idempotent-task-logic`
(duplicate rows) and `py/pandas-no-chained-assignment` (silently lost
writes); other former errors are now warnings.
