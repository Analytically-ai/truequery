# py — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## py/airflow-isolate-task-dependencies

*Isolate conflicting task dependencies instead of sharing one image*

When tasks in one DAG need mutually incompatible Python packages, run the conflicting task in an isolated environment (virtualenv/external-Python / container operator) rather than forcing one shared image to satisfy both.

Why: One shared image for conflicting pins resolves to "works for neither": silent version drift between local dev and the worker image produces runs that pass in CI and corrupt or crash in production, with the failure surfac…

```sql
-- bad
# requirements.txt pins pandas==2.2; legacy task still coded against pandas 1.x
PythonOperator(task_id="legacy_read", python_callable=legacy_read)
```
```sql
-- good
PythonOperator(
    task_id="legacy_read",
    python_callable=legacy_read,
    requirements=["pandas==1.5.3"],
)
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Best Practices — Apache Airflow (Handling conflicting/complex Python dependencies)

## py/dagster-asset-declared-dependencies

*Dagster assets must declare dependencies through parameters, not hidden reads*

An @asset function must receive every upstream dependency as a function argument (or AssetIn) so the asset graph contains it; it must not read another asset's data via direct I/O (file paths, warehouse queries, ad-hoc clients) inside its body.

Why: Hidden reads make the Dagster UI lineage a lie: re-materializing an upstream asset does not trigger the hidden downstream consumer, so stale data ships silently and root-cause tracing during incidents points at the wrong…

```sql
-- bad
@asset
def daily_report():
    df = pd.read_parquet("s3://bucket/upstream.parquet")  # hidden dep
    return df.groupby("k").sum()
```
```sql
-- good
@asset
def daily_report(upstream_df: pd.DataFrame):
    return upstream_df.groupby("k").sum()
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Defining assets | Dagster Docs

## py/dagster-io-manager-not-manual-io

*Dagster assets must return values via I/O managers, not manual writes*

An @asset function must return its computed value (or yield an Output/AssetMaterialization) and let the configured I/O manager persist it; it must not perform its own persistence with to_csv/to_parquet/ to_sql/open(path, "w") calls inside the compute body.

Why: Manual writes split the asset's state in two: Dagster tracks one location while downstream consumers read another, so re-materialization, memoization, and environment swaps (dev vs prod storage) silently diverge and back…

```sql
-- bad
@asset
def kpis(orders: pd.DataFrame):
    df = orders.groupby("ds").sum()
    df.to_parquet("/data/kpis.parquet")  # bypasses I/O manager
    return df
```
```sql
-- good
@asset(io_manager_key="warehouse_io")
def kpis(orders: pd.DataFrame):
    return orders.groupby("ds").sum()
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: I/O managers | Dagster Docs

## py/dagster-resources-for-external-clients

*External clients must be injected as Dagster resources, not built inline*

Connections to databases, warehouses, and APIs inside assets and ops must be obtained from declared Dagster Resources (constructor-injected, environment-configured), never instantiated from inline credentials, module-level globals, or per-call constructors inside the compute body.

Why: Inline clients hardcode environment bindings into code: the same asset cannot run against dev/staging/prod without edits, credentials leak into logs and code history, and tests cannot substitute fakes, so every environme…

```sql
-- bad
@asset
def users():
    conn = psycopg2.connect(host="prod-db", password="s3cret")
    return pd.read_sql("SELECT * FROM users", conn)
```
```sql
-- good
class WarehouseResource(ConfigurableResource):
    host: str
    password: SecretStr
    def get_conn(self): ...
@asset
def users(warehouse: WarehouseResource):
    return pd.read_sql("SELECT * FROM users", warehouse.get_conn())
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: External resources | Dagster Docs

## py/dbt-python-model-contract

*dbt Python models must declare schema and use the dbt session context*

Every dbt Python model must define a model(df, session) entry point that returns a DataFrame the declared adapter accepts, must read upstream data only through dbt.ref()/dbt.source() (not ad-hoc reads), and must not rely on undeclared packages outside the packages documented for the adapter.

Why: A Python model that reads arbitrary tables or returns an undeclared shape breaks dbt's DAG contract: ref-based lineage, docs, and partial parsing silently diverge from what actually runs, so a renamed upstream column pro…

```sql
-- bad
import pandas as pd
def model(dbt, session):
    df = session.table("analytics.orders_v2").to_pandas()  # bypasses lineage
    return df.assign(total=df["qty"] * df["price"])
```
```sql
-- good
def model(dbt, session):
    df = dbt.ref("orders")
    return df.withColumn("total", df["qty"] * df["price"])
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Python models | dbt Developer Hub

## py/deterministic-model-outputs

*Python models and transforms give the same output for the same input*

Python executed as a data model or pipeline transform (dbt Python models, SQLMesh Python models, Dagster assets, Airflow task callables) must be deterministic for fixed inputs: random sampling/splitting uses an explicitly seeded generator created inside the run; any order-sensitive step (drop_duplicates, keep first/last, head, cumulative ops) is preceded by a sort on a key that fully orders the rows; no output value or row order depends on wall-clock reads (now(), today(), time.time), uuid4, or set/dict iteration order of unordered data. Unavoidable nondeterminism must be declared and excluded from tests.

Why: Reruns and retries must reproduce the same result: SQLMesh defines idempotency as the same inputs producing the same outputs, and Airflow says now() should never be used inside a task for critical computation. An unseede…

```sql
-- bad
def model(dbt, session):
    df = dbt.ref("stg_events").to_pandas()
    return df.drop_duplicates(subset=["event_id"])  # unsorted: winner varies
```
```sql
-- good
def model(dbt, session):
    df = dbt.ref("stg_events").to_pandas()
    df = df.sort_values(["event_id", "occurred_at", "ingest_id"])
    df = df.drop_duplicates(subset=["event_id"], keep="last")
    rng = np.random.default_rng(seed=42)  # seeded inside the run, not module-global state
    return df.sample(n=1000, random_state=rng)
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Glossary, Idempotency (SQLMesh docs); Best Practices (Airflow 3.3.2 documentation); Random Generator (NumPy documentation); pandas.DataFrame.drop_duplicates (pandas documentation)

## py/hook-adapter-connection-lifecycle

*Hooks and adapters must scope connections per run and always release them*

Custom hook/adapter/client code used by pipeline Python must acquire connections lazily inside the run (not at import), reuse one connection per task execution, and release it in a finally/context-manager path; per-row connects and unclosed clients are forbidden.

Why: A hook that connects per call or leaks clients exhausts pools and file descriptors under backfill parallelism: healthy tasks fail with connection timeouts, the scheduler throttles the whole deployment, and the incident l…

```sql
-- bad
def fetch_user(uid):
    conn = psycopg2.connect(DSN)  # per-row connect, never closed
    return query(conn, uid)
```
```sql
-- good
from contextlib import contextmanager
@contextmanager
def warehouse_conn():
    conn = psycopg2.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: External resources (Connecting to databases) | Dagster Docs; Best Practices — Apache Airflow

## py/idempotent-task-logic

*Pipeline Python callables must be idempotent across retries and backfills*

Any Python callable run by an orchestrator (task, asset, model) must produce the same warehouse state when retried or backfilled for the same logical input: writes must be overwrite/merge keyed on the run's logical partition, never blind appends, and side effects (emails, API posts, file moves) must be guarded by the run/partition key.

Why: Orchestrators retry failed tasks and backfill past partitions by design; a non-idempotent callable turns one transient failure into duplicated rows or double-charged side effects, and restatements corrupt history instead…

```sql
-- bad
def load_kpis(df, engine):
    df.to_sql("daily_kpis", engine, if_exists="append", index=False)
```
```sql
-- good
def load_kpis(df, engine, ds):
    with engine.begin() as tx:
        tx.execute(text("DELETE FROM daily_kpis WHERE ds = :ds"), {"ds": ds})
        df.to_sql("daily_kpis", tx, if_exists="append", index=False)
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Glossary (Idempotency) | SQLMesh docs; Partitions and backfills | Dagster Docs

## py/no-n-plus-one-queries-in-loops

*Never issue per-iteration queries or API calls from Python loops*

Pipeline Python must not execute a query, API call, or warehouse round trip inside a loop over rows/keys/partitions; such access must be batched into a single set-based query (IN clause, join) or a bulk API call before the loop.

Why: N+1 access turns one fast set operation into thousands of round trips: latency multiplies, connection pools exhaust, source APIs rate-limit, and a job that should take seconds runs for hours while holding warehouse slots…

```sql
-- bad
for oid in order_ids:
    cur.execute("SELECT * FROM orders WHERE id = %s", (oid,))
    rows.append(cur.fetchone())
```
```sql
-- good
cur.execute("SELECT * FROM orders WHERE id = ANY(%s)", (order_ids,))
rows = cur.fetchall()
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Python models | SQLMesh docs

## py/pandas-filter-before-collect

*Push filters and projections to the engine before collecting to pandas*

Pipeline Python that needs a subset of a warehouse table must apply WHERE filters and column projections in the query (or lazy frame) before collecting to pandas; fetching full tables and filtering in memory is forbidden above trivial reference-data sizes.

Why: Collect-before-filter moves gigabytes the warehouse could have discarded in milliseconds: workers OOM, task memory limits kill the run, and the query slot burns for the transfer instead of serving production traffic.

```sql
-- bad
df = context.fetchdf("SELECT * FROM events")
day = df[df["ds"] == execution_date]
```
```sql
-- good
df = context.fetchdf(
    "SELECT ds, user_id, revenue FROM events WHERE ds = '2026-10-11'")
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Python models | SQLMesh docs

## py/pandas-no-chained-assignment

*Mutate DataFrames with .loc/.iloc single-step assignment, never chains*

Pipeline code that mutates a pandas DataFrame must do so with a single .loc/.iloc assignment (or an explicit .copy() first); chained subscript-then-assign (df[a][b] = v, df.query(...)[col] = v) is forbidden.

Why: Chained assignment may write to a temporary copy instead of the frame, silently dropping the mutation: a cleaning step that looks applied in code review never lands, and downstream models train or report on dirty data wi…

```sql
-- bad
df[df.region == "EU"]["vat"] = 0.2  # may write to a temp copy
```
```sql
-- good
df.loc[df.region == "EU", "vat"] = 0.2
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=error, change=error
- sources: Copy-on-Write (CoW) | pandas docs

## py/pandas-no-row-loop-mutations

*Vectorize DataFrame transforms instead of looping rows in Python*

Row-wise Python loops over DataFrames (for + iterrows/itertuples/index access, df.apply with row-wise Python logic where a vectorized op exists) must be replaced with vectorized pandas/NumPy operations in pipeline and model code paths that scale with data volume.

Why: Row loops are orders of magnitude slower than vectorized ops: a feature job that finishes in minutes on a sample blows past its task timeout and memory budget on full data, turning every backfill into a runaway run that …

```sql
-- bad
for idx, row in df.iterrows():
    df.at[idx, "total"] = row["qty"] * row["price"]
```
```sql
-- good
df["total"] = df["qty"] * df["price"]
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Enhancing performance | pandas docs

## py/pin-pipeline-dependencies

*Pin and lock the runtime environment of pipeline code*

Every deployable pipeline unit (Airflow DAG folder and its task images, Dagster code location, dbt Python model, SQLMesh project) must run in a pinned environment: exact versions (==) or a lockfile for Python dependencies, Airflow installed against its release constraints file, dbt Python model `packages` pinned with ==, and container images referenced by immutable tag or digest. Rebuilds, retries and backfills install from that pin, never from floating specifiers.

Why: Floating specifiers let identical code resolve to different libraries on a new worker, a retry, or a backfill months later, so the same commit can produce different data with no diff to inspect. pip defines pinning as ==…

```sql
-- bad
# requirements.txt
pandas
dbt-snowflake>=1.5
# KubernetesPodOperator(image="registry/etl:latest")
```
```sql
-- good
# requirements.txt (compiled from requirements.in by pip-tools / uv)
pandas==2.2.3
dbt-snowflake==1.8.2
# KubernetesPodOperator(image="registry/etl@sha256:<digest>")
# dbt.config(packages=["numpy==1.23.1"])
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Repeatable Installs (pip documentation); Installation from PyPI (Airflow 3.3.2 documentation); Python models (dbt docs); Best Practices (Airflow 3.3.2 documentation), Handling conflicting/complex Python dependencies

## py/sqlmesh-audit-python-outputs

*Attach blocking audits to Python models, not just SQL models*

Every production Python model must carry at least one blocking audit (row-count/division, not-null, uniqueness, or fresh-interval check) that runs on each plan/interval, because Python-side transforms escape the static checks that cover SQL models.

Why: Python models can emit any DataFrame, so schema drift, empty frames from a failed API fetch, or duplicated keys pass evaluation silently; without a blocking audit the bad interval promotes to production and poisons every…

```sql
-- bad
@model("marts.fx_rates", columns={"ds": "date", "rate": "double"})
def execute(context, start, end, execution_time, **kwargs):
    return fetch_rates(start, end)  # no audit if API returns empty
```
```sql
-- good
@model("marts.fx_rates", columns={"ds": "date", "rate": "double"})
def execute(context, start, end, execution_time, **kwargs):
    return fetch_rates(start, end)
# audits/fx_rates.sql: AUDIT (name assert_fx_rows_present, ...);
# SELECT * FROM @this_model WHERE ds BETWEEN @start_ds AND @end_ds
# GROUP BY ds HAVING COUNT(*) = 0
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Auditing | SQLMesh docs

## py/sqlmesh-python-model-execution-context

*SQLMesh Python models must declare columns and read upstream via context*

A SQLMesh Python model must declare its output schema in the @model columns argument, define execute(context, start, end, execution_time), and fetch upstream data through the ExecutionContext (not through separate connections or undeclared external reads) for every incremental model run.

Why: Undeclared reads bypass SQLMesh's interval isolation: a backfill for one time slice silently mixes in other slices' data, producing duplicate or missing rows that virtual-update previews cannot catch because the lineage …

```sql
-- bad
import snowflake.connector
@model("marts.revenue", columns={"ds": "date", "rev": "double"})
def execute(context, start, end, execution_time, **kwargs):
    conn = snowflake.connector.connect(**CREDS)  # ignores start/end
    return conn.cursor().execute("SELECT * FROM orders").fetch_pandas_all()
```
```sql
-- good
@model("marts.revenue", columns={"ds": "date", "rev": "double"},
       kind=dict(name=ModelKindName.INCREMENTAL_BY_TIME_RANGE,
                 time_column="ds"))
def execute(context, start, end, execution_time, **kwargs):
    return context.fetchdf("SELECT ds, rev FROM upstream_orders")
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Python models | SQLMesh docs
