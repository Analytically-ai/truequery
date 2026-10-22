# ops — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## ops/breaking-change-categorization

*Categorize each model change as breaking or non-breaking and backfill accordingly*

Every directly modified model in a plan must carry an explicit breaking or non-breaking categorization; breaking changes backfill the model and its downstream dependents, non-breaking changes backfill only the model itself.

Why: Defaulting everything to breaking wastes compute on full downstream backfills; defaulting everything to non-breaking silently leaves downstream tables inconsistent with their parents. Explicit categorization forces the a…

```sql
-- bad
All changes backfilled downstream by default with no recorded category.
```
```sql
-- good
Plan records the filter-logic change as breaking and the new-column addition as non-breaking with no downstream backfill.
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: change
- severity: change=warning
- sources: Plans (SQLMesh concepts)

## ops/ci-pr-checks-required

*Every pull request must run automated build and data tests before merge*

Every pull request that touches pipeline code, SQL models, or pipeline configuration must trigger an automated CI run that builds the changed scope and executes its data tests; merging with a red or missing CI check is treated as a process violation.

Why: Untested merges are the cheapest point at which wrong numbers, broken SQL, or schema drift enter production. A mandatory CI gate moves failure discovery from the production run (expensive, user-visible) to the pull reque…

```sql
-- bad
Merging a model change with no CI run, or with a failing test suite.
```
```sql
-- good
PR shows green "build + test changed scope" check; merge blocked until it passes.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: change,assure
- severity: assure=warning, change=warning
- sources: About continuous integration (CI) in dbt; Continuous integration jobs in dbt

## ops/ci-slim-builds-only-changed

*CI builds only changed models and their downstream dependents*

CI runs must select only the models changed in the pull request plus their downstream dependents (state-based selection), rather than rebuilding the whole project on every PR.

Why: Full-project rebuilds on every PR waste warehouse spend, slow the feedback loop from minutes to hours, and teach developers to bypass CI. Slim builds keep the gate fast enough that it is actually used.

```sql
-- bad
ci-command rebuilds the entire project on every pull request.
```
```sql
-- good
ci-command builds state:modified+ with deferral to production state.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: change,assure
- severity: assure=warning, change=warning
- sources: About dbt State; Continuous integration jobs in dbt

## ops/cost-estimate-before-run

*Estimate bytes scanned and cost before running large or new queries*

Before executing a new, modified, or historically expensive query or model in production, the author must obtain a pre-execution estimate (dry run, EXPLAIN, or byte-scan estimate) and record it alongside the change.

Why: A single unexamined full-table scan can cost more than a year of CI. Pre-execution estimates convert surprise bills into reviewable numbers at the only point where the query can still be rewritten.

```sql
-- bad
Running a new 12-month backfill with no estimate.
```
```sql
-- good
Dry run reports 38 GB estimated; reviewer confirms it matches expectations before the real run.
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Run a query (BigQuery documentation); EXPLAIN (Snowflake documentation)

## ops/cost-guardrail-confidence-levels

*Present cost guardrails honestly by warehouse capability, never as uniform blocks*

Cost controls must reflect what each warehouse can actually know pre-execution: hard-block thresholds are allowed only where exact pre-execution pricing exists (BigQuery dry-run dollars); everywhere else guardrails must be warn-only estimates until calibrated.

Why: A uniform "cost guardrail" abstraction lies: it hard-blocks where it cannot measure and waves through where it should warn. False blocks train teams to disable the guardrail; false passes train them to ignore it. Both en…

```sql
-- bad
One threshold labeled "max cost $50" applied identically on BigQuery and Snowflake.
```
```sql
-- good
BigQuery job hard-blocked above its dry-run dollar estimate; Snowflake job warns on estimated scan footprint with calibration status shown.
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: assure
- severity: assure=advisory
- sources: Run a query (BigQuery documentation); EXPLAIN (Snowflake documentation)

## ops/dev-environment-expiry

*Disposable environments expire and are torn down automatically*

Every non-production environment must have a defined expiry or teardown trigger (branch merge, age limit, explicit destroy step) so that stale dev schemas and datasets cannot accumulate indefinitely.

Why: Orphaned dev schemas confuse consumers, leak storage spend, and can be mistaken for production truth months later. Automatic teardown keeps the warehouse namespace legible and the bill bounded.

```sql
-- bad
Dev schemas persist forever after their branches are merged.
```
```sql
-- good
Merging the branch triggers a job that drops its dev schema; schemas older than 30 days are flagged for removal.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Environments (SQLMesh concepts); The DataOps Manifesto

## ops/disposable-dev-environments

*Test every change in an isolated disposable environment before production*

Each branch or change set must be testable in an isolated environment (separate schema, namespace, or virtual environment) created on demand from production state, where writes never affect production tables.

Why: Testing in production schemas corrupts live data, blocks concurrent work, and makes experiments expensive. Cheap isolated environments are the precondition for every other change-safety practice.

```sql
-- bad
All development runs write to the production schema.
```
```sql
-- good
Branch feature-x builds into its own schema; dropping it after merge is safe and automatic.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: Environments (SQLMesh concepts); The DataOps Manifesto

## ops/drift-detection-history-first

*Detect pipeline drift from run history fingerprints before rescanning data*

Drift detection must start from recorded run history (query fingerprints, row counts, schema hashes over time) and only trigger data rescans when history shows an unexplained deviation.

Why: Scan-first drift detection re-reads production tables on every check, billing real compute for a question history usually answers for free. History-first keeps monitoring cheap enough to run continuously, which is what m…

```sql
-- bad
Full re-profile of every table on every schedule tick.
```
```sql
-- good
Row-count and schema-hash time series trigger a targeted re-profile only when the trend breaks.
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: assure
- severity: assure=warning
- sources: Monitor jobs and alerts (dbt); Introduction (Apache Iceberg documentation)

## ops/full-refresh-approval

*Full refreshes are opt-in, approved, and recoverable*

Any operation that rebuilds a table from scratch (full refresh, truncate-and-load, partition overwrite beyond the agreed window) must require an explicit flag, confirmation step, or approval; it must never be the default path of a routine run. Before the approved refresh replaces the table, the prior state must be recoverable: a clone or snapshot taken first, or a time-travel window long enough to cover the run, with the rollback command recorded. Tables that are trivially rebuildable from retained source data may declare rebuild-from-source as their rollback instead.

Why: Full refreshes erase history-dependent state, multiply cost, and can destroy the only copy of late-arriving corrections. They are also the highest data-loss-risk load: a bad source extract or a mid-load failure leaves an…

```sql
-- bad
# dbt_project.yml: every nightly run rebuilds all history
models:
  +full_refresh: true
-- scheduled job
truncate table dim_customers;
copy dim_customers from 's3://extracts/customers.csv';
```
```sql
-- good
# incremental by default; a backfill job passes --full-refresh with a recorded reason
create table dim_customers_backup clone dim_customers;
copy dim_customers from 's3://extracts/customers.csv';
-- rollback: alter table dim_customers swap with dim_customers_backup
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Plans (SQLMesh concepts); About continuous integration (CI) in dbt; Introduction - Apache Iceberg

## ops/model-versioning-breaking-changes

*Ship breaking model and metric changes as new versions with a deprecation window*

A change that breaks a shared model's or governed metric's guarantees ships as a new version alongside the old one instead of being edited in place. For models this covers dropping or renaming columns, changing or narrowing types, tightening constraints, and changing grain or semantics. For governed metrics it covers changes to logic, grain, filters or name; these ship as a new version or a renamed metric with a deprecation alias, are reviewed by the metric owner, and are announced to registered consumers. Consumers migrate by explicitly pinning to the new version. The prior version keeps building until every pinned consumer has migrated or an announced sunset date passes; removing it early is a violation.

Why: In-place breaking changes force a flag day: every consumer must update at once or break, with no rollback. Dashboards fail, vendor syncs drop columns silently, privacy guarantees negotiated on the old shape disappear, an…

```sql
-- bad
# orders v1 changes a column type in place; v1 deleted the day after v2 ships;
# active_user redefined in place under the same name.
```
```sql
-- good
models:
  - name: orders
    latest_version: 2
    versions:
      - v: 2            # new type for amount
      - v: 1
        deprecation_date: 2027-01-31   # keeps building until consumers migrate
metrics:
  - name: active_user_v2
    description: Supersedes active_user (login-only); v1 removal 2027-01.
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Model versions (dbt Mesh governance); SQLMesh documentation; Add Exposures to your DAG

## ops/plan-preview-before-apply

*Review a plan diff showing affected models and backfill scope before any production apply*

Before applying changes to production, the operator must generate and review a plan that lists added, removed, directly modified, and indirectly modified models together with the date range that will be backfilled; production applies without a reviewed plan are violations.

Why: Blind applies turn a one-line filter change into a silent full-history rewrite or drop models the author did not know existed. A reviewed plan surfaces downstream blast radius while it is still cheap to stop.

```sql
-- bad
Applying local changes straight to prod with no preview step.
```
```sql
-- good
Plan output lists 1 direct plus 3 indirect models and a 7-day backfill window; reviewer approves, then apply runs.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Plans (SQLMesh concepts)

## ops/post-deploy-data-validation

*Run data tests after every production deploy before exposing new data*

After a production deployment completes, the pipeline must execute its data test suite against the new production state and gate downstream exposure (dashboard refresh, reverse sync, ML retrain) on a pass; failures roll back exposure, not just page someone.

Why: A deploy that builds successfully but produces wrong rows is the most dangerous outcome: green infrastructure, red data. Post-deploy validation is the last point where bad data can be stopped before consumers act on it.

```sql
-- bad
Deploy job ends with "build succeeded"; dashboards refresh unconditionally.
```
```sql
-- good
Deploy job runs tests post-build; dashboard refresh and sync tasks depend on the test task passing.
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Monitor jobs and alerts (dbt); About continuous integration (CI) in dbt

## ops/pre-destructive-snapshot

*Snapshot or time-travel checkpoint before any destructive production operation*

Before dropping, overwriting, or restating a production table, the operator must create a restorable checkpoint (clone, snapshot, or recorded time-travel offset) and record how to restore from it.

Why: Destructive operations are occasionally necessary (GDPR deletes, restatements), but without a checkpoint a typo or wrong predicate becomes permanent data loss. A clone costs little; a lost table costs everything.

```sql
-- bad
Dropping and recreating a prod table in the same script with no backup step.
```
```sql
-- good
Script clones the table first, records the clone name in the change ticket, then runs the overwrite.
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: change
- severity: change=warning
- sources: Introduction (Apache Iceberg documentation)

## ops/prod-credentials-isolation

*Credentials never appear in code, configs, logs, or chat output*

Passwords, tokens, private keys, and connection strings with embedded credentials must not appear in committed files (DAG files, asset modules, hook/adapter code, notebooks promoted to jobs, profiles.yml and other configs), CI logs, or pasted debugging output. Code references credentials by name, and the values come from the orchestrator's connection/secret store (Airflow Connections or a secrets backend; Dagster EnvVar-backed resources), environment-injected config, or a secret manager. Production credentials live only in the production secret store and deployment system, never in developer configs.

Why: A committed secret spreads to every clone, fork, mirror and log archive and stays in git history after the line is deleted, so it must be revoked and rotated, often across several environments. Airflow's best practices s…

```sql
-- bad
conn = snowflake.connector.connect(account="acme", user="etl", password="Winter2024!")
# or profiles.yml with the prod password committed to the repo
```
```sql
-- good
hook = SnowflakeHook(snowflake_conn_id="prod_dw")  # value lives in the secrets backend
# Dagster: SnowflakeResource(password=dg.EnvVar("SNOWFLAKE_PASSWORD"))
# dbt: password: "{{ env_var('DBT_ENV_SECRET_PASSWORD') }}"
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Best Practices (Airflow 3.3.2 documentation); Secrets Backend (Airflow 3.3.2 documentation); Using environment variables and secrets (Dagster docs); env_var (dbt docs); Secrets Management Cheat Sheet; About secret scanning (GitHub Docs)

## ops/run-artifact-retention

*Retain run artifacts, manifests, and logs for every production run*

Every production run must persist its artifacts (manifest, run results, logs, and the exact code commit) for a defined retention period so any past run is reproducible and auditable.

Why: Without retained artifacts, incidents are undebuggable ("what code produced Tuesday's numbers?") and state-based CI has nothing to diff against. Artifacts are the memory of the pipeline.

```sql
-- bad
Production runs log to console only; nothing is stored after the container exits.
```
```sql
-- good
Each run stores manifest plus run results plus commit SHA for 90 days; any figure can be traced to its run.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: assure
- severity: assure=warning
- sources: About dbt State; The DataOps Manifesto

## ops/staged-upgrades-with-backup

*Test platform and orchestrator upgrades in staging with a backup first*

Upgrades to the orchestrator, warehouse platform behavior, or shared pipeline libraries must first roll through a staging environment, and stateful components (metadata database, state store) must be backed up before the production upgrade begins.

Why: Upgrades change scheduling semantics, retry behavior, and SQL dialects in ways unit tests cannot catch. An untested production upgrade couples the blast radius of every pipeline into one event with no restore point.

```sql
-- bad
Upgrading the production orchestrator in place on a Friday with no backup.
```
```sql
-- good
Upgrade deploys to staging, integration-test DAGs run green, metadata DB is backed up, then prod upgrades.
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: change,assure
- severity: assure=warning, change=warning
- sources: Best Practices (Airflow documentation)
