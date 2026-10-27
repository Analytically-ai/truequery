# orch — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## orch/concurrency-pools-and-limits

*Fan-out against shared systems declares a concurrency cap*

Tasks that fan out against a shared, capacity-limited system (warehouse, source database, rate-limited API) MUST declare an explicit concurrency cap scoped to that system: in Airflow a named pool and/or max_active_tis_per_dag / max_active_tasks; in Dagster a concurrency pool, run tag limit, or run queue limit. They should not rely only on the deployment-wide default (in Airflow, every unassigned task shares default_pool, which starts at 128 slots).

Why: Both Airflow and Dagster document these limits specifically for protecting external systems that are overwhelmed by simultaneous work. Without a per-system cap, one wide mapped task can consume the shared slots and the e…

```sql
-- bad
load.expand(file=files)  # 500-way fan-out, default_pool only
```
```sql
-- good
load.partial(pool="warehouse_load", max_active_tis_per_dag=16).expand(file=files)
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Pools (Airflow 3.3.2 documentation); Scaling Airflow to optimize performance; Managing concurrency (Dagster docs)

## orch/dag-ci-parse-and-unit-tests

*Pipeline definitions pass a load test and unit tests in CI*

Every change to pipeline definitions MUST pass, in CI before merge, (1) a definition-load test that imports every DAG/definition file with no scheduler, metadata database or live connections and fails on any import error (Airflow DagBag import test or running each DAG file; Dagster `dg check defs` or the older `dagster definitions validate`), and (2) unit tests for task/asset logic touched by the change.

Why: A DAG file that fails to import does not fail a task; it disappears from the scheduler, so nothing alerts. Airflow's best-practices page recommends both a DAG loader test and unit tests, and Dagster's CLI provides a defi…

```sql
-- bad
# CI: pytest tests/test_app.py   -- nothing imports dags/
```
```sql
-- good
def test_dag_bag_imports():
    from airflow.models import DagBag
    bag = DagBag(dag_folder="dags/", include_examples=False)
    assert not bag.import_errors, bag.import_errors
# CI also runs tests/test_task_logic.py; Dagster projects run `dg check defs`
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Best Practices (Airflow 3.3.2 documentation), Testing a Dag; dg CLI reference (Dagster docs); Unit testing assets and ops (Dagster docs)

## orch/data-aware-scheduling-over-polling

*Prefer data-aware triggers over chained cron offsets*

When one pipeline consumes another pipeline's output on the same platform, ordering SHOULD use the platform's data-aware trigger (Airflow asset-aware scheduling, named Datasets before Airflow 3; Dagster declarative automation or asset sensors) rather than a downstream cron offset that assumes the upstream has finished. Note that an Airflow asset event means the producing task succeeded, not that the data passed any check.

Why: A cron offset encodes an assumed upstream duration. When the upstream slips, the downstream still runs and publishes on time from stale inputs, which looks healthy. Asset-aware triggers run the consumer when the producer…

```sql
-- bad
schedule="0 3 * * *"  # assumes upstream is done by 02:30
```
```sql
-- good
schedule=[Asset("warehouse.staging_orders")]  # Airflow 3; Dagster: AutomationCondition.eager()
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore,build
- severity: build=advisory, explore=advisory
- sources: Asset-aware scheduling (Airflow 3.3.2 documentation); Upgrading to Airflow 3; Declarative Automation (Dagster docs)

## orch/declared-asset-lineage

*Cross-pipeline data flow is declared, not hidden in code*

A dataset that one pipeline writes and another reads MUST be declared through the tool's dependency surface, not exist only as a path or table name inside task code: dbt `source()`/`ref()`, Dagster asset `deps` or asset parameters, Airflow task `outlets` with Assets (and the consuming DAG's asset schedule). Purely internal scratch objects are exempt.

Why: Declared dependencies are what each tool uses to order work and to show upstream/downstream relationships: dbt's source() creates a dependency between model and source, Dagster's deps make the edge explicit, and Airflow …

```sql
-- bad
pd.read_parquet("s3://events/raw/")  # produced by another DAG, nothing declared
```
```sql
-- good
@task(inlets=[Asset("s3://events/raw/")], outlets=[Asset("warehouse.mart_orders")])
# Dagster: @asset(deps=[raw_events]); dbt: {{ source('events', 'raw') }}
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Add sources to your DAG (dbt docs); Passing data between assets (Dagster docs); Asset-aware scheduling (Airflow 3.3.2 documentation)

## orch/deferrable-sensors-with-timeout

*Sensors declare a timeout, a deliberate interval, and a non-blocking mode*

Every Airflow sensor MUST set an explicit `timeout` sized to the real expected wait (the built-in default is seven days) and an explicit `poke_interval` proportional to how often the awaited data can actually land. A sensor expected to wait longer than a few minutes MUST NOT hold a worker slot while waiting: use `deferrable=True` (requires a running triggerer) or, where no deferrable variant exists, `mode="reschedule"`. Plain `poke` mode is acceptable only for short waits. Sensors polling a metered warehouse or rate-limited API MUST NOT poke at second-level intervals.

Why: In the default poke mode a sensor occupies a worker slot for its whole runtime; with the seven-day default timeout, a sensor on data that never arrives can hold that slot for a week and hides the missing data as "running…

```sql
-- bad
FileSensor(task_id="wait", filepath="/landing/partner/")
# poke mode, 60s default interval, 7-day default timeout
```
```sql
-- good
S3KeySensor(task_id="wait", bucket_key="s3://landing/orders/{{ ds }}/_SUCCESS",
            deferrable=True, poke_interval=300, timeout=6 * 3600, soft_fail=False)
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Sensors (Airflow 3.3.2 documentation); Deferrable Operators & Triggers (Airflow 3.3.2 documentation); Airflow sensors

## orch/explicit-catchup-policy

*Scheduled DAGs set catchup explicitly*

Every scheduled Airflow DAG MUST pass `catchup` explicitly (False, or True with a bounded start_date and rerun-safe logic) instead of inheriting `[scheduler] catchup_by_default`. The inherited value is version- and config-dependent: it defaulted to True in Airflow 2.x and defaults to False from Airflow 3.0, and any deployment can override it.

Why: With catchup on, unpausing or first deploying a DAG creates a run for every missed interval since start_date. The same DAG file therefore behaves differently across Airflow 2 and 3 or across deployments with different co…

```sql
-- bad
DAG("x", schedule="@daily", start_date=pendulum.datetime(2024, 1, 1, tz="UTC"))
```
```sql
-- good
DAG("x", schedule="@daily", start_date=pendulum.datetime(2024, 1, 1, tz="UTC"), catchup=False)
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Upgrading to Airflow 3; Dag Runs (Airflow 3.3.2 documentation), Catchup

## orch/explicit-schedule-timezone

*Schedules declare their timezone explicitly*

Every time-based schedule MUST state its timezone in code (explicit UTC is fine) rather than inheriting it from deployment config. Airflow: a timezone-aware start_date built with pendulum, or a timetable given a timezone such as CronTriggerTimetable(..., timezone=...). Dagster: execution_timezone on the schedule (or timezone on the partitions definition for partitioned jobs).

Why: Airflow applies `[core] default_timezone` (utc on a fresh install, but configurable to `system` or any zone) to naive datetimes, and Dagster runs schedules without a timezone in UTC. A DAG that relies on the inherited va…

```sql
-- bad
DAG("rev", schedule="0 2 * * *", start_date=datetime(2024, 1, 1))  # naive
```
```sql
-- good
DAG("rev", schedule=CronTriggerTimetable("0 2 * * *", timezone="America/New_York"),
    start_date=pendulum.datetime(2024, 1, 1, tz="America/New_York"))
# Dagster: ScheduleDefinition(..., cron_schedule="0 2 * * *", execution_timezone="America/New_York")
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Time zones (Airflow 3.3.2 documentation); Timetables (Airflow 3.3.2 documentation); Customizing a schedule's execution timezone (Dagster docs)

## orch/failure-alert-routing

*Production pipelines have an owner, routed failure alerts, and a runbook*

Every production pipeline (Airflow DAG, Dagster job/asset group, dbt Cloud job) MUST declare (1) a real owning team or rotation, not a placeholder; (2) a failure notification wired to that owner's channel, carrying enough context to start triage (pipeline, run id, logical date or partition, error, link to the run/logs); and (3) a link to a runbook covering triage, rerun/rollback, and who to tell downstream.

Why: Failures in unowned pipelines sit unnoticed or bounce between teams while stale data is read as fresh; alerts without context make on-call start from zero. All three tools provide the hooks: Airflow failure callbacks and…

```sql
-- bad
DAG("x", default_args={"owner": "airflow"})  # placeholder owner, no failure callback, no runbook
```
```sql
-- good
DAG("x",
    default_args={"owner": "data-platform"},
    owner_links={"data-platform": "https://wiki.example.com/runbooks/x"},
    on_failure_callback=SlackNotifier(channel="#data-platform-alerts",
        text="{{ dag.dag_id }} {{ run_id }} failed: {{ ti.log_url }}"))
# Dagster: @asset(owners=["team:data-platform"]) + make_slack_on_run_failure_sensor(
#   channel="#data-platform-alerts", webserver_base_url=...)
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Callbacks (Airflow 3.3.2 documentation); Add Owner Links to DAG (Airflow 3.3.2 documentation); Asset metadata and tags (Dagster docs); dagster-slack API reference; Monitor jobs and alerts (dbt docs); Introduction (Site Reliability Engineering, Google)

## orch/idempotent-backfill-windowing

*Backfills are windowed, capped, and rerunnable*

Reprocessing of historical windows MUST run as an explicitly bounded backfill (a from/to date or partition range plus a cap on concurrent runs) over logic that writes the run's own interval or partition in a rerun-safe way (overwrite of that partition, or MERGE/UPSERT on a key), so replaying a window twice leaves the target unchanged.

Why: Airflow re-runs every DAG instance in a backfill range and retries failed tasks, and its best-practices page warns that INSERT on re-run can create duplicate rows and recommends UPSERT and reading/writing a specific part…

```sql
-- bad
INSERT INTO fct_orders SELECT * FROM stg_orders WHERE order_date >= '{{ ds }}'
-- backfilled with no end date and no concurrency cap
```
```sql
-- good
airflow backfill create --dag-id orders --from-date 2026-09-01 --to-date 2026-09-07 --max-active-runs 3
-- task: DELETE FROM fct_orders WHERE order_date = '{{ ds }}'; INSERT ... WHERE order_date = '{{ ds }}'
-- (in one transaction) or MERGE ... ON (order_id)
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: change,assure
- severity: assure=error, change=error
- sources: Best Practices (Airflow 3.3.2 documentation), Writing a Dag / idempotency; Dag Runs (Airflow 3.3.2 documentation), Backfill; Backfilling data (Dagster docs)

## orch/idempotent-retries-with-limits

*Tasks declare bounded retries with backoff over rerun-safe writes*

Every task or asset that touches external systems MUST declare its retry policy explicitly: a small finite retry count plus a delay, with exponential backoff where the platform supports it. Its writes MUST be safe to repeat for the same logical unit, because a retry reruns the whole task. When retries are exhausted the task must fail loudly to its owner (see orch/failure-alert-routing); inputs that fail deterministically (malformed files) should be quarantined rather than retried again on every run.

Why: Airflow's built-in default is zero retries (`[core] default_task_retries = 0`, 300 s delay), so a pipeline that declares nothing fails on routine transient faults; Astronomer notes that two retries protect against most p…

```sql
-- bad
PythonOperator(task_id="load", python_callable=load)  # inherits 0 retries; INSERT INTO mart ...
```
```sql
-- good
PythonOperator(task_id="load", python_callable=load, retries=3,
               retry_delay=timedelta(minutes=5), retry_exponential_backoff=True)
# load() runs MERGE INTO mart USING staging ON key ...
# Dagster: @asset(retry_policy=RetryPolicy(max_retries=3, delay=30, backoff=Backoff.EXPONENTIAL))
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Configuration Reference (Airflow 3.3.2 documentation); Best Practices (Airflow 3.3.2 documentation); DAG writing best practices in Apache Airflow; Op retries (Dagster docs)

## orch/light-dag-parse-no-heavy-top-level

*DAG files do no I/O or heavy work at parse time*

Code that runs when an Airflow DAG file is imported (module level, and module-level expressions inside the DAG context) MUST be limited to building operators and their dependencies. It MUST NOT open connections, run queries, call HTTP APIs, or read Airflow Variables/Connections; such work goes inside task callables or Jinja templates (e.g. `{{ var.value.x }}`), and expensive library imports go inside the callable that needs them. Configuration needed at parse time comes from environment variables or files shipped next to the DAG.

Why: The DAG processor re-parses DAG files continuously, so top-level work is repeated on every parse, not once per run. Variable reads at top level hit the metadata database on every parse; a slow or unreachable dependency s…

```sql
-- bad
import requests
CONFIG = requests.get("https://cfg.internal/dag-params").json()  # runs on every parse
TABLES = Variable.get("tables", deserialize_json=True)           # metadata DB hit on every parse
```
```sql
-- good
@task
def load_config():
    import requests
    return requests.get("https://cfg.internal/dag-params").json()  # runs only at execution
# or: BashOperator(..., bash_command="run.sh {{ var.value.tables }}")
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Best Practices (Airflow 3.3.2 documentation), Top level Python Code; Dynamic Dag Generation (Airflow 3.3.2 documentation); Configuration Reference (Airflow 3.3.2 documentation)

## orch/no-circular-dependencies

*Task graphs are acyclic and stable from one parse to the next*

Pipeline graphs MUST be acyclic, and their structure (task ids, edges, number of tasks) MUST be the same on every parse of unchanged code. Task ids and edges must not be derived from wall-clock time, random values, unordered iteration (sets, unsorted directory listings), or external queries at parse time. Fan-out whose width depends on upstream results uses the platform's runtime mechanism (Airflow dynamic task mapping), not parse-time generation.

Why: Airflow rejects cycles when a DAG is loaded (AirflowDagCycleException), so a cycle surfaces as an import error that the CI load test catches (see orch/dag-ci-parse-and-unit-tests). The subtler failure is a graph that cha…

```sql
-- bad
for t in set(hook.get_records("select table_name from information_schema.tables")):
    PythonOperator(task_id=f"load_{t[0]}", ...)  # parse-time query, unordered
```
```sql
-- good
TABLES = sorted(json.load(open(Path(__file__).parent / "tables.json")))
for t in TABLES:
    PythonOperator(task_id=f"load_{t}", ...)
# or: load.expand(table=list_tables())  # runtime fan-out via dynamic task mapping
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: airflow.exceptions API reference (Airflow 3.0.2); Dynamic Dag Generation (Airflow 3.3.2 documentation)

## orch/partitioned-assets-windowing

*Large time-ranged datasets are partitioned so fixes reprocess only a window*

A large dataset that is built from time-ranged (or key-ranged) inputs SHOULD be modeled so that one window can be reprocessed on its own: Dagster partitioned assets (e.g. DailyPartitionsDefinition, code reading context.partition_key or the partition time window), or Airflow tasks that read and write only their run's data interval. A fix to one week then reprocesses that week, not the full history. Whether a backfill runs one run per partition or a single run over a range is a separate choice; both are fine.

Why: Dagster describes partitioning as the way to manage large datasets incrementally and launches a run per selected partition; Airflow's best practices ask tasks to read and write a specific partition keyed by the data inte…

```sql
-- bad
@asset
def orders(): ...  # full-history rebuild is the only option
```
```sql
-- good
@asset(partitions_def=DailyPartitionsDefinition(start_date="2024-01-01"))
def orders(context):
    day = context.partition_key  # reprocess a single day
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Partitioning assets (Dagster docs); Backfilling data (Dagster docs); Best Practices (Airflow 3.3.2 documentation)

## orch/sla-deadline-alerts

*Pipelines with a freshness commitment alert on lateness, not just failure*

A pipeline with a downstream freshness commitment MUST declare a machine-readable lateness check with an alert destination, because a late run that eventually succeeds raises no failure alert. Use the mechanism of the version in use: Airflow 3.1+ Deadline Alerts (the `deadline` DAG argument; experimental in 3.1); Airflow 2.x `sla` / `sla_miss_callback` (removed in Airflow 3.0, so do not write new code against it); Dagster freshness policies with a freshness alert; dbt source freshness (`warn_after` / `error_after`) for source-fed models.

Why: Silent lateness is easy to miss: dashboards render, the run succeeds hours later, and nobody is paged. Each tool has a native lateness signal, but they differ by version and two of them are new or in preview, which is wh…

```sql
-- bad
DAG("exec_report", schedule="0 6 * * *")  # no lateness signal
DAG("exec_report", sla_miss_callback=page)  # Airflow 3: SLA feature removed
```
```sql
-- good
DAG("exec_report", schedule="0 6 * * *",
    deadline=DeadlineAlert(reference=DeadlineReference.DAGRUN_QUEUED_AT,
                           interval=timedelta(hours=1),
                           callback=AsyncCallback(SlackWebhookNotifier, kwargs={...})))
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Deadline Alerts (Airflow 3.3.2 documentation); Tasks (Airflow 3.3.2 documentation); Asset freshness policies (Dagster docs); Add sources to your DAG (dbt docs)

## orch/task-atomicity-no-partial-writes

*Task outputs become visible all at once, never half-written*

Each task MUST publish its output in one atomic step so that a failed or retried task never leaves a half-written result that downstream readers can see: a database transaction around delete+insert or MERGE, a partition overwrite/swap, a table-format commit, or writing to a staging location and publishing with a single final step (for file outputs, a completion marker that downstream readers require).

Why: A database transaction is all-or-nothing and its intermediate state is not visible to other sessions until commit; without that, a task that fails midway exposes partial data. Orchestrators make this worse because downst…

```sql
-- bad
for h in hours:
    df_h.write.parquet(f"s3://mart/daily/{day}/{h}.parquet")  # each file visible as written
```
```sql
-- good
BEGIN;
DELETE FROM mart.daily WHERE day = '{{ ds }}';
INSERT INTO mart.daily SELECT ... WHERE day = '{{ ds }}';
COMMIT;
-- files: write to s3://mart/_staging/{run_id}/, then write the _SUCCESS marker readers wait on
```

- checked by truequery: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Transactions (PostgreSQL documentation); Best Practices (Airflow 3.3.2 documentation)

## orch/task-timeout-execution-limit

*Long-running work declares an execution timeout*

Tasks or runs whose duration depends on external systems (extracts, bulk loads, full refreshes, training) MUST declare an execution limit after which they fail loudly: Airflow `execution_timeout` (or a deployment-wide `[core] default_task_execution_timeout`); Dagster the `dagster/max_runtime` run tag or `run_monitoring.max_runtime_seconds`, with run monitoring enabled.

Why: Airflow's default is no execution timeout ("never timed out by default"), and Dagster enforces run timeouts only when run monitoring is enabled. A task hung on a locked table or a stalled connection therefore holds its s…

```sql
-- bad
PythonOperator(task_id="extract", python_callable=full_dump)  # never times out by default
```
```sql
-- good
PythonOperator(task_id="extract", python_callable=full_dump, execution_timeout=timedelta(hours=2))
# Dagster: define_asset_job("nightly", tags={"dagster/max_runtime": 7200})
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Configuration Reference (Airflow 3.3.2 documentation); Tasks (Airflow 3.3.2 documentation); Deferrable Operators & Triggers (Airflow 3.3.2 documentation); Run monitoring (Dagster docs)
