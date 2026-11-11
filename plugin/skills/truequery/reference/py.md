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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Python models | SQLMesh docs

## py/pandas-dedupe-unstable-sort

*Dedupe after a sort with a unique tiebreaker or a stable sort*

A sort_values that decides which row drop_duplicates keeps (keep="first" or "last") sorts by the deciding column plus a unique tiebreaker, or uses kind="stable" when the input order is the intended tiebreaker.

Why: sort_values on one column uses quicksort by default, which is not stable: rows that tie on the sort key come out in an arbitrary order. The row that drop_duplicates then keeps for a key is arbitrary among the ties, and c…

```sql
-- bad
latest = df.sort_values("ts").drop_duplicates("id", keep="last")
```
```sql
-- good
latest = df.sort_values(["ts", "capture_id"]).drop_duplicates("id", keep="last")
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: pandas.DataFrame.sort_values | pandas docs; pandas.DataFrame.drop_duplicates | pandas docs
- proof bad: `kept = df.sort_values("ts").drop_duplicates("id", keep="last")
result = kept.sort_values("id")["capture"].tolist()
` → [9, 1, 5]
- proof good: `kept = df.sort_values(["ts", "capture"]).drop_duplicates("id", keep="last")
result = kept.sort_values("id")["capture"].tolist()
` → [9, 13, 17]

## py/pandas-fillna-zero-on-money

*Don't fill missing money amounts with 0*

pandas code doesn't replace missing values in a money column (a contract column in minor or major currency units that may be empty) with 0, unless 0 is the true amount and that is stated; missing amounts stay missing, so mean(), count() and the like skip them.

Why: A missing amount means "not known", not "zero". fillna(0) turns every unknown amount into a real one: the mean drops, count() includes rows that have no amount, and minimums become 0. Nothing fails, and the numbers look …

```sql
-- bad
avg_refund = refunds["amount_cents"].fillna(0).mean()
```
```sql
-- good
avg_refund = refunds["amount_cents"].mean()  # skips unknown amounts
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,explore
- severity: build=warning, change=warning, explore=warning
- sources: pandas.Series.mean | pandas docs; pandas.DataFrame.fillna | pandas docs
- proof bad: `result = [df["amount_cents"].fillna(0).mean(), df["amount_cents"].fillna(0).count()]
` → [1333.3333333333333, 3]
- proof good: `result = [df["amount_cents"].mean(), df["amount_cents"].count()]
` → [2000.0, 2]

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

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Python models | SQLMesh docs

## py/pandas-groupby-dropna

*Say what happens to missing group keys with dropna=*

Every groupby in pipeline code passes dropna= explicitly: dropna=False to keep a group for rows whose key is missing, or dropna=True to state that dropping them is intended.

Why: groupby drops rows whose key is NaN or None by default. Their values vanish from every group, so totals by group no longer add up to the overall total, with no warning. The SQL equivalent keeps a NULL group, so a pipelin…

```sql
-- bad
by_region = df.groupby("region")["amt"].sum()
```
```sql
-- good
by_region = df.groupby("region", dropna=False)["amt"].sum()
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: pandas.DataFrame.groupby | pandas docs
- proof bad: `result = df.groupby("region")["amt"].sum().reset_index()
` → [('EU', 10), ('US', 20)]
- proof good: `result = df.groupby("region", dropna=False)["amt"].sum().reset_index()
` → [('EU', 10), ('US', 20), (None, 5)]

## py/pandas-groupby-first-mixes-rows

*Pick whole rows per group, not groupby().first() / .last()*

Code that keeps one row per group keeps a whole input row (sort, then .nth(0) / .nth(-1), or drop_duplicates on the keys). groupby(...).first() and .last() are used only where mixing columns from different rows is intended, or with skipna=False.

Why: GroupBy.first() and .last() take the first (last) non-null value of each column separately. When a group's first row has a null in some column, that column is filled from a later row, so one output row can combine values…

```sql
-- bad
latest = df.groupby("customer_id").first()
```
```sql
-- good
latest = df.groupby("customer_id").nth(0)
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: pandas.core.groupby.DataFrameGroupBy.first | pandas docs; pandas.core.groupby.DataFrameGroupBy.nth | pandas docs
- proof bad: `result = df.groupby("id").first().reset_index()
` → [(1, 'b@x', '111')]
- proof good: `result = df.groupby("id").nth(0)
` → [(1, None, '111')]

## py/pandas-merge-validate

*State the expected cardinality of every merge with validate=*

Every pandas merge or join in pipeline code states the cardinality it expects with validate= ("one_to_one", "one_to_many", "many_to_one"), so a duplicate key raises instead of multiplying rows.

Why: A merge on a key that is duplicated on one side returns one row per matching pair. A lookup table that gains a duplicate key silently doubles the matching fact rows, and every sum after it is inflated. validate= turns th…

```sql
-- bad
enriched = orders.merge(customers, on="customer_id")
```
```sql
-- good
enriched = orders.merge(customers, on="customer_id", validate="many_to_one")
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: pandas.DataFrame.merge | pandas docs
- proof bad: `result = orders.merge(customers, on="cid")["amt"].sum()
` → 50
- proof good: `lookup = customers.drop_duplicates("cid")
result = orders.merge(lookup, on="cid", validate="many_to_one")["amt"].sum()
` → 40

## py/pandas-no-chained-assignment

*Change a DataFrame in one step on the DataFrame itself, never through a chain*

Pipeline code that changes a pandas DataFrame does so in one step on the DataFrame itself: df.loc[rows, "col"] = v, or an explicit .copy() that is then used. A write through an intermediate selection (df[mask]["col"] = v, df["col"][mask] = v, df.query(...)["col"] = v, df.col[mask] = v) or an in-place method on one (df["col"].fillna(0, inplace=True)) is forbidden.

Why: The intermediate selection is a temporary object. Writing to it changes the temporary, not the DataFrame: with Copy-on-Write (the default since pandas 3.0) such a write never reaches the DataFrame, and before that it dep…

```sql
-- bad
df[df.region == "EU"]["vat"] = 0.2  # writes to a temporary copy
df["vat"].fillna(0, inplace=True)   # same: df never changes
```
```sql
-- good
df.loc[df.region == "EU", "vat"] = 0.2
df["vat"] = df["vat"].fillna(0)
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=error, change=error
- sources: Copy-on-Write (CoW) | pandas docs; Indexing and selecting data: returning a view versus a copy | pandas docs
- proof bad: `df[df.region == "EU"]["vat"] = 0.2
result = df
` → [('EU', 0.0), ('US', 0.0)]
- proof good: `df.loc[df.region == "EU", "vat"] = 0.2
result = df
` → [('EU', 0.2), ('US', 0.0)]

## py/pandas-no-row-loop-mutations

*Never write to the row of an iterrows() loop*

Code that changes a DataFrame inside a `for i, row in df.iterrows()` loop writes through the DataFrame (df.loc[i, "col"] = v), or computes the column without a loop. Assigning to the loop's row (row["col"] = v, row.col = v) is forbidden unless the row itself is then used (collected, returned, passed on).

Why: iterrows() yields each row as a new Series built for the loop. With Copy-on-Write (the default since pandas 3.0) it is always a copy: writing to it changes that copy, which is thrown away at the next iteration. Before pa…

```sql
-- bad
for i, row in df.iterrows():
    row["total"] = row["qty"] * row["price"]  # changes a copy
```
```sql
-- good
df["total"] = df["qty"] * df["price"]
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=error, change=error
- sources: pandas.DataFrame.iterrows | pandas docs
- proof bad: `for i, row in df.iterrows():
    row["total"] = row["qty"] * row["price"]
result = df
` → [(1, 10, 0), (2, 20, 0)]
- proof good: `df["total"] = df["qty"] * df["price"]
result = df
` → [(1, 10, 10), (2, 20, 40)]

## py/pandas-read-csv-contract-dtype

*Read a contracted file's string keys as strings*

When a contract declares a key column as a string, pandas code that reads that contract's CSV file passes a dtype (or a converter) for the key, so identifiers that look like numbers stay text.

Why: read_csv infers each column's type from its values. A key such as 007 or 0042 looks like a number, so pandas reads it as an integer and drops the leading zeros. The read succeeds, but joins and lookups against the same k…

```sql
-- bad
orders = pd.read_csv("data/orders.csv")
```
```sql
-- good
orders = pd.read_csv("data/orders.csv", dtype={"order_id": "string"})
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: pandas.read_csv | pandas docs
- proof bad: `result = pd.read_csv(io.StringIO(csv))["order_id"]
` → [7, 42]
- proof good: `result = pd.read_csv(io.StringIO(csv), dtype={"order_id": "string"})["order_id"]
` → ['007', '042']

## py/pandas-to-sql-replace

*Load into an existing table without to_sql(if_exists="replace")*

Pipeline code that reloads a table empties it (DELETE or TRUNCATE) and writes with to_sql(if_exists="append"), or creates the table itself; to_sql(if_exists="replace") is not used on a table whose definition matters.

Why: if_exists="replace" drops the table and creates a new one from the DataFrame's dtypes. The primary key, NOT NULL and other constraints, indexes, declared column types (NUMERIC(10,2) becomes a float, a date may become tex…

```sql
-- bad
df.to_sql("sales", con, if_exists="replace", index=False)
```
```sql
-- good
con.execute("DELETE FROM sales")
df.to_sql("sales", con, if_exists="append", index=False)
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: pandas.DataFrame.to_sql | pandas docs
- proof bad: `df.to_sql("sales", con, if_exists="replace", index=False)
result = con.execute("PRAGMA table_info(sales)").fetchall()
` → [(0, 'id', 'INTEGER', 0, None, 0), (1, 'amt', 'REAL', 0, None, 0)]
- proof good: `con.execute("DELETE FROM sales")
df.to_sql("sales", con, if_exists="append", index=False)
result = con.execute("PRAGMA table_info(sales)").fetchall()
` → [(0, 'id', 'INTEGER', 0, None, 1), (1, 'amt', 'NUMERIC(10,2)', 1, None, 0)]

## py/pandas-write-mode-violation

*Write an output the way its contract says it's written*

When an output contract declares how the output is written (write_mode: append, overwrite_partition or merge_on_key), pandas code that writes that output doesn't do the opposite: it doesn't replace an append-only table or file, doesn't append blindly to a table that is merged on its key, and doesn't replace a whole table that is overwritten one partition at a time.

Why: The write mode is what keeps reruns and history correct. Replacing an append-only output deletes every earlier load; appending to a merge-on-key output adds a second copy of each key on every rerun; replacing a table tha…

```sql
-- bad
day.to_sql("events", con, if_exists="replace", index=False)
```
```sql
-- good
day.to_sql("events", con, if_exists="append", index=False)
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=error, change=error
- sources: pandas.DataFrame.to_sql | pandas docs; pandas.DataFrame.to_csv | pandas docs
- proof bad: `day.to_sql("events", con, if_exists="replace", index=False)
result = con.execute("SELECT id FROM events ORDER BY id").fetchall()
` → [(3,)]
- proof good: `day.to_sql("events", con, if_exists="append", index=False)
result = con.execute("SELECT id FROM events ORDER BY id").fetchall()
` → [(1,), (2,), (3,)]

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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
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

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Python models | SQLMesh docs
