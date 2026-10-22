# ingest — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## ingest/cap-intake-cost-and-volume

*Intake paths declare volume and cost ceilings with shutoff behavior*

Every intake MUST declare an expected volume band and a cost ceiling (bytes scanned, rows per hour, dollars per day - whichever the platform bills), plus what happens past the ceiling: alert, throttle, or halt. An uncapped intake on a metered platform fails review.

Why: Intake is where runaway bills start: a debug-log stream pointed at the warehouse, a backfill with no end date, a sensor-driven full scan per poke. Without ceilings, the first sign of trouble is the invoice.

```sql
-- bad
# no volume expectation, no ceiling, backfill unbounded
load everything under s3://archive/ into events_raw
```
```sql
-- good
intake events_raw: expected 50-80 GB/day; halt + page past 200 GB/day;
backfills require explicit row cap and expiry
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Introduction to loading data; What is Auto Loader?

## ingest/cdc-replica-identity-required

*CDC sources set replica identity so updates and deletes replicate*

Any Postgres table captured by logical-replication CDC MUST have its replica identity set to cover UPDATE and DELETE payloads (FULL, or a key the subscriber handles). Tables left on the default identity whose key cannot identify updated rows MUST be flagged before the connector goes live.

Why: With an insufficient replica identity, UPDATEs and DELETEs arrive without enough key information to apply: subscribers either error, skip the change, or apply it to the wrong row. The replica silently diverges from the s…

```sql
-- bad
create publication app_pub for table users;
-- users has no primary key; default replica identity
```
```sql
-- good
alter table users replica identity full;
create publication app_pub for table users;
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=error, build=error
- sources: Chapter 29. Logical Replication

## ingest/checkpoint-streaming-intake

*Streaming intake checkpoints progress for exactly-once resume*

Every streaming/file-watch intake MUST declare a durable checkpoint (offset store, checkpoint directory) that is separate from its output, and resume from it after restarts. A stream with no checkpoint location fails review.

Why: Without durable progress tracking, every restart replays from scratch (massive duplicates) or from latest (dropped data). Checkpointing is what converts at-least-once delivery into effectively-once results.

```sql
-- bad
spark.readStream.format("cloudFiles").load("s3://landing/events/")
# no checkpointLocation: restart behavior undefined
```
```sql
-- good
spark.readStream.format("cloudFiles")
  .option("checkpointLocation", "s3://meta/checkpoints/events/")
  .load("s3://landing/events/")
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=error, build=error
- sources: What is Auto Loader?

## ingest/choose-batch-vs-streaming-deliberately

*Batch vs streaming is chosen from latency need, not fashion*

The choice of batch vs streaming intake MUST be recorded with the latency requirement that drives it (e.g. "needed within 15 minutes for fraud review" vs "next-day reporting tolerates daily batch"). Streaming with no stated sub-hour consumer, or batch missing a stated daily need, fails review.

Why: Streaming costs more in every dimension - operations, state management, exactly-once machinery, debugging - and buys nothing when the consumer reads daily. Unjustified streaming is the most expensive kind of over-enginee…

```sql
-- bad
# real-time pipeline; consumer: "daily KPI email, mornings"
architecture: kafka -> flink -> warehouse (no latency note)
```
```sql
-- good
# latency note: fraud queue needs <15min; KPI email uses daily batch
fraud signals: streaming (15min SLA); reporting marts: daily batch
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: explore,build
- severity: build=advisory, explore=advisory
- sources: Debezium Architecture; Introduction to loading data

## ingest/declare-source-freshness

*Every ingested source declares a freshness threshold*

Every source consumed by a pipeline MUST declare the timestamp column that defines its freshness (a loaded-at or event-time field) and a maximum acceptable data age with a warn and an error limit, and the pipeline MUST evaluate that threshold on each run, reporting the run time, the newest data timestamp and the resulting lag. Static reference tables that never change are marked frozen instead. Sources that feed critical outputs additionally declare a criticality tier, an owner and an alert route, so a breach pages a person rather than landing in an unmonitored channel; non-critical sources need the thresholds only.

Why: Without a declared freshness limit, a silently stalled connector looks identical to a healthy one: downstream models keep building on increasingly stale data and nobody is paged. Declared thresholds turn "is this data fr…

```sql
-- bad
sources:
  - name: shopify
    tables:
      - name: orders   # no freshness block; staleness is invisible
  - name: payments     # feeds the board pack; check but no owner or route
    freshness: {error_after: {count: 24, period: hour}}
```
```sql
-- good
sources:
  - name: shopify
    loaded_at_field: _etl_loaded_at
    tables:
      - name: orders
        freshness:
          warn_after: {count: 12, period: hour}
          error_after: {count: 24, period: hour}
  - name: payments
    loaded_at_field: _etl_loaded_at
    freshness:
      warn_after: {count: 6, period: hour}
      error_after: {count: 24, period: hour}
    meta: {criticality: tier-1, owner: payments-data, alert_route: pagerduty://data-oncall}
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Add sources to your DAG; Asset freshness policies

## ingest/dedupe-on-natural-key-before-append

*Loads declare a natural key and dedupe on it before append*

Every table populated by repeated loads MUST declare a natural (business) key, and each load MUST remove duplicates on that key before committing. A repeatedly-loaded table with no declared key fails review.

Why: Upstream systems routinely redeliver: API pagination overlaps, CDC replays, file re-drops. Without a declared key there is no definition of "same row twice", so redelivery silently becomes duplication and every COUNT and…

```sql
-- bad
create table orders_landing (order_id bigint, amount numeric);
-- loaded by daily append; no uniqueness anywhere
```
```sql
-- good
create table orders_landing (order_id bigint primary key, amount numeric);
-- loader dedupes staging on order_id before merge
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: SodaCL metrics and checks

## ingest/handle-schema-drift-explicitly

*File and stream intake declares a schema-evolution policy*

Every file/stream intake MUST declare how it handles new, renamed, or mistyped columns: reject the batch, quarantine the extra fields (e.g. a rescue column), or widen the schema under a named policy. An intake with no evolution policy fails review.

Why: Upstream schemas change without notice. Without a declared policy, new columns are silently dropped (lost data) or mistyped columns corrupt the load (failed batches at 3am). An explicit policy makes each change a visible…

```sql
-- bad
# no schema policy: new partner columns silently dropped
df = spark.read.csv("s3://landing/partner/")
```
```sql
-- good
# new columns rescued, batch still loads, team notified
spark.readStream.format("cloudFiles")
  .option("cloudFiles.schemaEvolutionMode", "rescue")
  .load("s3://landing/partner/")
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: What is Auto Loader?

## ingest/idempotent-batch-loads

*Batch loads are idempotent and safely replayable*

Every batch load MUST be replayable with identical results: re-running a batch for the same window MUST NOT create duplicates or partial state. Acceptable mechanisms are merge/upsert on a declared key, partition overwrite of the batch window, or transactional load-and-swap. Plain append with no dedupe key fails review.

Why: Non-idempotent loads turn every retry, backfill, or late-data re-run into duplicate rows. Duplicates inflate counts and revenue figures, and cleanup requires pinpointing which copies are spurious.

```sql
-- bad
-- plain append, re-runs duplicate
insert into orders select * from staging_orders;
```
```sql
-- good
-- merge on the natural key: re-runs converge
merge into orders t using staging_orders s
  on t.order_id = s.order_id
when matched then update set * when not matched then insert *;
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=advisory, build=advisory, change=advisory
- sources: Introduction to loading data

## ingest/isolate-source-failures

*One source's failure never blocks unrelated sources*

Pipeline structure MUST isolate per-source intake so a single source's failure (stalled, malformed, late) fails only its own branch and pages its own owner. A monolithic intake where one bad source blocks all sources fails review.

Why: Coupled intake converts one partner's outage into a platform-wide data freeze: every downstream model goes stale because one CSV was malformed. Isolation keeps 90% of the platform fresh while the broken source is fixed, …

```sql
-- bad
# one task, twelve sources, first exception aborts all
def load_all(): for s in SOURCES: load(s)
```
```sql
-- good
# per-source assets/tasks with independent retries and owners
@asset(owners=["team-payments"]) def raw_payments(): ...
@asset(owners=["team-crm"]) def raw_crm(): ...
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: Overview | Dagster Docs; Sensors

## ingest/landing-rebuildable-from-source

*Landing data is rebuildable from the source within declared retention*

Every intake contract MUST state how its landing data is rebuilt after loss: source retention window, replay method (snapshot + log, re-pull, re-drop), and who can trigger it. A landing zone with no documented rebuild path fails review.

Why: Landing zones get corrupted, dropped, or poisoned; without a rebuild path the team discovers at incident time that the source keeps 7 days and the damage is 30 days old. Declared retention plus a tested replay method bou…

```sql
-- bad
# no rebuild story: "the landing table IS the history"
intake: pg-cdc events -> landing.events (no retention/replay noted)
```
```sql
-- good
rebuild: snapshot + WAL replay; source retention 30d; replay runbook
linked; last replay drill 2026-09-15
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Chapter 29. Logical Replication

## ingest/monitor-cdc-lag-and-slots

*CDC lag and replication slots are monitored with alerts*

Every CDC pipeline MUST monitor end-to-end replication lag and the health of its replication slot / offset state, with an alert threshold declared in the pipeline contract. A CDC connector with no lag or slot monitoring fails review.

Why: A stalled or lagging CDC stream produces the most dangerous failure mode: fresh-looking dashboards on frozen data. Unconsumed slots also retain write-ahead logs on the source, risking source disk exhaustion and outages i…

```sql
-- bad
# connector deployed; no lag metric, no alert
curl -X POST /connectors -d @pg-source.json
```
```sql
-- good
# lag alert declared alongside the connector
alert: cdc_lag_seconds > 300 for 10m on connector pg-inventory
alert: pg_replication_slots.confirmed_flush_lag_bytes > 1GB
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Debezium Architecture; Chapter 29. Logical Replication

## ingest/pin-connector-and-schema-versions

*Connector and event-schema versions are pinned and reviewed on change*

Every intake MUST pin its connector version and, for event/CDC intake, the schema-registry subject version it decodes against. Version bumps MUST go through review like code changes. Floating "latest" connectors in production fail review.

Why: Connectors and event schemas change wire behavior: a connector upgrade can rename fields or alter snapshot locking; a schema-registry evolution can break every downstream decoder at once. Unpinned intake means the pipeli…

```sql
-- bad
image: debezium/connect:latest
# schema subject resolved fresh on every restart
```
```sql
-- good
image: debezium/connect:3.7.0
schema_version: inventory.orders-value v14 (reviewed 2026-09-30)
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Debezium Architecture

## ingest/pin-file-format-options

*File intake pins format options explicitly, never relies on sniffing*

Production file readers MUST declare delimiter, header presence, quoting, encoding, and column types explicitly. Auto-detection (schema sniffing) is allowed for exploration only and MUST be denied in scheduled intake code.

Why: Sniffers guess from a sample: a delimiter change, a stray quote, or a column that looks numeric in the first rows silently retypes or splits the whole file. Explicit options make the expected shape a contract that fails …

```sql
-- bad
select * from 's3://landing/orders.csv';  -- all sniffed
```
```sql
-- good
select * from read_csv('s3://landing/orders.csv',
  delim = ',', header = true,
  columns = {'order_id': 'BIGINT', 'amount': 'DECIMAL(12,2)'});
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: CSV Import

## ingest/propagate-deletes-explicitly

*Source deletes propagate under an explicit hard/soft policy*

Every replicated source MUST declare how source-side deletes propagate: hard-delete downstream, soft-delete flag, or ignore-with-log. A CDC or snapshot-diff intake with no delete policy fails review.

Why: Deletes are the quiet half of replication: connectors that stream only inserts/updates let deleted rows live forever downstream. Reports keep counting churned users, and privacy deletion requests (GDPR) silently fail to …

```sql
-- bad
# consumer applies inserts/updates; delete events unhandled
if event.op in ("c", "u"): apply(event)
```
```sql
-- good
if event.op == "d": soft_delete(event.key, deleted_at=event.ts)
elif event.op in ("c", "u"): apply(event)
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=warning, change=warning
- sources: Debezium Architecture; Introduction to streams

## ingest/quarantine-invalid-rows-at-intake

*Invalid rows are quarantined at intake, not dropped silently*

Every intake path MUST declare what happens to rows that fail validation: either reject the batch or route failing rows to a quarantine location with the failure reason attached. A loader that silently discards invalid rows fails review.

Why: Silent drops turn data-quality problems into wrong numbers: row counts stop reconciling, metrics drift, and nobody can enumerate what was lost. A quarantine table preserves evidence and makes the loss measurable and reco…

```sql
-- bad
# loader skips bad rows with no record
for row in reader:
    try:
        load(row)
    except ValueError:
        continue
```
```sql
-- good
checks for raw_orders:
  - schema:
      fail: when wrong column type
  - row_count > 0
# failing rows routed to raw_orders_quarantine with reason
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SodaCL metrics and checks; CSV Import

## ingest/raw-landing-append-only

*Raw landing zones are append-only and never updated in place*

Tables or paths designated as raw landing MUST be append-only: no UPDATE, DELETE, or in-place overwrite of landed records. Corrections arrive as new records (new files, new CDC versions); change capture over the landing area uses offsets/streams, never mutation. Any in-place write to a landing object fails review.

Why: Mutating the landing zone destroys the audit trail: replays become unreproducible, late-arriving corrections cannot be distinguished from originals, and forensic questions ("what did we know on Oct 1?") become unanswerab…

```sql
-- bad
update landing.orders set status = 'fixed' where day = '2026-10-01';
```
```sql
-- good
-- corrections land as new records; consumers read the change stream
create stream landing_orders_changes on table landing.orders;
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,change
- severity: build=error, change=error
- sources: Introduction to streams; Introduction - Apache Iceberg

## ingest/reconcile-row-counts-per-load

*Each load reconciles source counts against landed counts*

Every batch load MUST compare the source-side record count (or a control total) against the landed count and fail or alert past a declared tolerance. A loader that lands rows without a count reconciliation fails review.

Why: Counts are the cheapest completeness signal: truncation, partial files, and connector pagination bugs all show up as count gaps long before anyone spots wrong analytics. Without reconciliation, partial loads pass as comp…

```sql
-- bad
# lands whatever arrived; no comparison
load_batch(file); mark_success(batch_id)
```
```sql
-- good
checks for raw_events_daily:
  - row_count between 9_500_000 and 10_500_000
# batch fails to quarantine when outside tolerance
```

- checked by tq: no (taught, not checked)
- enforcement: runtime
- modes: build,assure
- severity: assure=warning, build=warning
- sources: SodaCL metrics and checks

## ingest/record-file-lineage-at-intake

*Each loaded row carries its source file and load identity*

Every file-based load MUST attach provenance columns to each row: source file path (or equivalent object id), load batch id, and load timestamp. A file intake that lands rows with no trace of which file they came from fails review.

Why: Without file lineage, a corrupt or duplicate file cannot be scoped: teams cannot answer "which rows came from the bad drop?" and must reload whole windows or leave poisoned rows in place. Provenance turns file problems i…

```sql
-- bad
copy orders from 's3://landing/orders-2026-10-01.csv';
-- no record of which file produced these rows
```
```sql
-- good
copy orders (order_id, amount, _source_file, _batch_id, _loaded_at)
from 's3://landing/orders-2026-10-01.csv';
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=warning, build=warning
- sources: What is Auto Loader?; Introduction to loading data

## ingest/reference-sources-not-raw-tables

*Models reference declared sources, never raw tables directly*

In dbt projects, every read of raw landing data MUST go through a declared source reference (source observations), never a hard-coded database.schema.table name. Any raw table name appearing in model SQL that is not backed by a source declaration fails review.

Why: Hard-coded raw table names bypass the source boundary contract: freshness checks, lineage, and renames all silently stop applying. A renamed upstream table then breaks models at runtime instead of being caught as a contr…

```sql
-- bad
select * from analytics_raw.shopify_orders
```
```sql
-- good
select * from {{ source('shopify', 'orders') }}
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Add sources to your DAG

## ingest/require-completeness-marker

*Batch intake waits for a completeness marker, not a timer*

Every batch intake MUST gate on an explicit completeness signal from the producer (marker file, manifest, partition-ready flag, or API completion status) rather than a fixed clock delay. Time-guessed pickups of still-being-written data fail review.

Why: Writers and readers race: a job that picks up files "30 minutes after the hour" will eventually read a half-written file, loading truncated data that passes every schema check. Producer-side completion markers eliminate …

```sql
-- bad
# cron at 02:30; hopes the upload finished
copy sales from 's3://drops/partner/2026-10-11/';
```
```sql
-- good
# waits for the producer's marker, then loads the manifest
sensor: s3://drops/partner/2026-10-11/_SUCCESS
copy sales from manifest 's3://drops/partner/2026-10-11/manifest.json';
```

- checked by tq: no (taught, not checked)
- enforcement: judgment
- modes: build
- severity: build=advisory
- sources: Sensors; Introduction to loading data

## ingest/scan-pii-at-landing

*Landing data is scanned for unexpected sensitive fields*

Every intake contract MUST list the sensitivity classification of each landed field, and each load MUST run a scan for undeclared sensitive patterns (national ids, card numbers, emails) in free-text and new columns. Unclassified new columns fail review until classified.

Why: Sensitive data enters through intake: partner file adds, free-text fields, schema drift. Once PII spreads to logs, marts, and exports, containment requires coordinated deletes across systems. Catching it at the landing b…

```sql
-- bad
# new notes column landed with no classification, no scan
landing tickets now includes agent_notes (free text)
```
```sql
-- good
# contract classifies the column; scan gates the load
- name: agent_notes
  classification: confidential-free-text
  scan: [phone, email, national-id]
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: SodaCL metrics and checks

## ingest/seeds-small-static-only

*Seeds carry small static reference data only*

dbt seeds MUST be limited to small, slowly-changing reference datasets (size and change-frequency limits set by the project), version-controlled as CSVs. Fact data, large dimensions, or frequently-updated files MUST enter through a proper loader, not seeds. An oversized or volatile seed fails review.

Why: Seeds load from version control on every run: large seeds bloat run time, churn diffs, and bypass every intake guard (freshness, quarantine, lineage). Teams that seed fact extracts get slow runs and unreviewable thousand…

```sql
-- bad
seeds:
  +post-hook: "select 1"
# data/seeds/clickstream_2026_10.csv (412 MB) committed as a seed
```
```sql
-- good
# data/seeds/country_codes.csv (248 rows, changes yearly)
# clickstream arrives via the events connector, not version control
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Add Seeds to your DAG

## ingest/test-intake-against-fixtures

*Intake logic is tested against fixture files and edge rows*

Every intake transformation (parsing, typing, filtering, keying) MUST have tests running against checked-in fixture files that include edge cases: malformed rows, null keys, exotic encodings, schema-drifted variants. Untested intake parsing fails review.

Why: Intake parsing is the code least tested and most exposed to hostile input: real-world files contain BOMs, mixed line endings, null bytes, and surprise encodings. Without fixtures, each novelty is discovered in production…

```sql
-- bad
# parsers/partner_csv.py has no tests directory entry at all
tests/fixtures/intake/  (empty)
```
```sql
-- good
tests/fixtures/intake/partner_v1.csv
tests/fixtures/intake/partner_v1_bom_utf16.csv
tests/fixtures/intake/partner_v2_new_column.csv
tests/fixtures/intake/partner_malformed_row.csv
```

- checked by tq: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=advisory, build=advisory, change=advisory
- sources: CSV Import

## ingest/watermark-and-late-data-policy

*Streaming intake declares watermarks and a late-data policy*

Every streaming intake that aggregates by event time MUST declare a watermark (how late data may be) and what happens to later-than-watermark records: drop with counting, side-output, or reprocess. A streaming aggregate with no watermark or late-data rule fails review.

Why: Late events are normal (mobile offline, retries, clock skew). Without a policy, late data either silently vanishes from aggregates or forces unbounded state that OOMs the stream. The policy decides openly which cost the …

```sql
-- bad
events.groupBy(window("event_ts", "1 hour")).count()
# no watermark: state grows without bound
```
```sql
-- good
events.withWatermark("event_ts", "2 hours")
  .groupBy(window("event_ts", "1 hour")).count()
# later-than-2h records counted to late_events side output
```

- checked by tq: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: What is Auto Loader?; Debezium Architecture
