# gov — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## gov/audit-columns-or-snapshots

*Mutable governed tables keep an audit trail*

Mutable production tables either carry updated_at/updated_by-style audit columns maintained on every write, or are covered by a snapshot/history strategy recording row validity over time, so any row's last change is attributable without warehouse forensics.

Why: Without a change trail, a wrong-number incident or suspected tampering cannot attribute which job changed which rows when; investigation stalls and the same bad write recurs because its source was never identified.

```sql
-- bad
Mutable dim_customers with no audit columns and no snapshot.
```
```sql
-- good
dim_customers carries updated_at/updated_by maintained by the merge, plus a snapshot on the PII subset.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=advisory, build=advisory, change=advisory
- sources: Add snapshots to your DAG; The DataOps Manifesto

## gov/column-grants-for-pii

*PII columns granted per-column, and queries name columns explicitly*

Access to classified PII columns is granted at column level rather than whole-table SELECT wherever consumers do not need the full row, and production queries against PII-bearing tables list columns explicitly instead of SELECT *.

Why: Whole-table grants plus SELECT * silently expand PII access every time a sensitive column is added: a consumer approved for order totals starts receiving emails with no new grant and no review.

```sql
-- bad
SELECT * FROM users; with reporting_role holding SELECT on users including email and ssn.
```
```sql
-- good
GRANT SELECT (id, name) ON users TO support_role; queries list exactly those columns.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: GRANT (PostgreSQL documentation)

## gov/declared-model-owner

*Every production dataset declares an owner*

Every production model or dataset ships with a declared owning team or individual recorded in the project (for example a dbt group with an owner entry, or an equivalent ownership field in the dataset contract), so a reviewer can determine who approves access, schema, and retention decisions without asking around.

Why: Unowned datasets accumulate silent consumers; when a breach, wrong-number incident, or erasure request arrives, nobody can authorize the fix and response stalls while PII keeps flowing.

```sql
-- bad
marts/churn.sql ships with no group and no owner field anywhere.
```
```sql
-- good
marts/churn.sql belongs to group growth with a named owner and contact.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Add groups to your DAG; Data Mesh Principles and Logical Architecture

## gov/description-coverage-for-shared-models

*Shared and contracted models are documented in the catalog*

Every public, contracted, or exposure-facing model and each of its columns carries a human description in the catalog (a reviewer can tell what a column means and whether it is sensitive without reading SQL).

Why: Undocumented shared models force every consumer to guess semantics; misread columns produce wrong numbers at scale, and sensitivity guesses miss PII that a one-line description would have flagged.

```sql
-- bad
Public marts_orders ships with five undescribed columns including val and flag2.
```
```sql
-- good
marts_orders documents every column, e.g. total_cents: charge amount in cents, tax inclusive.
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: About documentation; Data Mesh Principles and Logical Architecture

## gov/erasure-path-documented

*Personal-data tables document an erasure path*

Every table classified as holding personal data documents how a subject's rows are deleted or anonymized on request, including history and snapshots — or records a signed exemption where erasure is genuinely impossible (for example append-only regulatory archives).

Why: Erasure requests arrive with legal deadlines; append-only snapshots and cached marts discovered mid-request turn a routine deletion into a fire-drill that misses copies and leaves the team non-compliant.

```sql
-- bad
users classified PII; no documented deletion procedure; snapshots accumulate indefinitely.
```
```sql
-- good
users documents erase_user(id) procedure deleting base rows, snapshot history entries, and the vendor-sync exposure, with a run log.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Add snapshots to your DAG; Introduction (Apache Iceberg documentation)

## gov/exposures-registered

*Downstream consumers are registered as exposures*

Every production dashboard, report, scheduled export, reverse-ETL sync, notebook, or ML consumer of a governed model or governed metric is registered as a dbt exposure (or an equivalent consumer registry entry) whose depends_on names the models, sources or metrics it reads, with an owner. Impact analysis for schema changes, metric changes, access revocations and deletions can then enumerate every consumer before the change ships. Ad-hoc exploration needs no registration until it becomes recurring.

Why: Unregistered consumers are invisible stakeholders. A renamed or reshaped mart silently breaks dashboards owned by other teams; a metric fix never reaches a consumer nobody knew about; and revoking a column or erasing a u…

```sql
-- bad
# Nightly vendor sync and the "Weekly revenue" dashboard read
# mart.orders and marts_users; no exposure file exists.
```
```sql
-- good
exposures:
  - name: weekly_revenue_dashboard
    type: dashboard
    owner: { name: exec-reporting }
    depends_on:
      - ref('orders')
      - metric('revenue')
  - name: vendor_sync
    type: application
    owner: { name: growth-eng }
    depends_on:
      - ref('marts_users')
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=advisory, build=advisory, change=advisory
- sources: Add Exposures to your DAG; Build your metrics

## gov/group-private-access

*Internal models default to restricted access*

Non-public dbt models belong to a group with restricted (private) access unless they are explicitly designated public; only contracted public models may be referenced across group or project boundaries.

Why: Without a default-restricted boundary, any team can hard-depend on any internal model, so an access revocation or column removal silently breaks unknown consumers and leaks data the owning team never approved.

```sql
-- bad
access: public on stg_payments with no contract; three other groups ref it.
```
```sql
-- good
stg_payments is access: private; only contracted marts_orders (public) is referenced cross-group.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=advisory, build=advisory
- sources: Add groups to your DAG; About dbt Mesh

## gov/least-privilege-grants

*Roles and credentials get least privilege, separated by job and environment*

Privileges granted to non-owner roles name exactly the privileges needed (for example SELECT on a table, USAGE on a schema); GRANT ALL PRIVILEGES to application, analyst, or shared roles is not permitted in reviewed SQL. Ingestion writers, transformation executors and read consumers each use a distinct role with only their job's grants, so no single shared role spans write and broad-read access to governed tables. Each environment's warehouse credential (dev, CI, prod adapter profiles and service accounts) is scoped to its workload: read-only where nothing is written, writes limited to its own schemas, no admin role attached to CI, and production roles narrower in human access than development roles.

Why: Over-broad grants turn every downstream credential into a de facto owner. A compromised BI user that also holds DELETE and TRUNCATE can destroy or exfiltrate data far beyond its job; a shared super-role means a dashboard…

```sql
-- bad
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA marts TO analyst_role;
-- one role analytics_rw owns dbt runs, manual writes and dashboard reads;
-- the same admin credential appears in the dev, CI and prod profiles
```
```sql
-- good
GRANT SELECT ON marts.orders TO reporting_reader;
-- loader_writer, dbt_executor and reporting_reader each hold only their job's grants;
-- the CI role can write only CI schemas; the prod role is deploy-only
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: GRANT (PostgreSQL documentation); Privileges (PostgreSQL documentation); Role Membership (PostgreSQL documentation); Continuous integration jobs in dbt

## gov/lineage-from-declared-sources

*Models read only from declared lineage*

Every table a model reads appears in declared lineage (refs, sources, or an explicit external-table registry); raw table names or fully-qualified paths with no declaration are not permitted in reviewed SQL.

Why: Undeclared reads are invisible to impact analysis and access review: a model can ingest a PII table nobody approved, and revoking the grant later misses the dependency nobody recorded.

```sql
-- bad
SELECT ... FROM prod.legacy.emails_dump with no source declaration.
```
```sql
-- good
SELECT ... FROM {{ source('legacy', 'emails_dump') }} with the source declared, classified, and owned.
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: Add sources to your DAG; Add Exposures to your DAG

## gov/mesh-public-contracts-only

*Cross-project dependencies use public contracted models only*

In a multi-project setup, a project may depend only on another project's models that are both public and governed by an enforced contract; private or uncontracted upstream models must not be referenced.

Why: Cross-project links to uncontracted models export one team's internal decisions (including which PII columns exist) to another team's runtime with no enforcement point, so privacy and breaking-change controls cannot hold…

```sql
-- bad
ref to upstream int_emails model where int_emails is private.
```
```sql
-- good
ref to upstream marts_customers which is public with an enforced contract.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: About dbt Mesh; Add data tests to your DAG

## gov/no-bypassrls-roles

*Application roles cannot bypass row-level security*

No application, analyst, or service role is created with or granted the BYPASSRLS attribute, and table owners subject to policy review do not rely on the owner exemption to read rows other roles cannot see.

Why: A single BYPASSRLS role anywhere in the application path silently voids every row filter: tenant isolation and region restrictions stop applying for that connection while every audit still claims policies are enabled.

```sql
-- bad
CREATE ROLE etl LOGIN BYPASSRLS;
```
```sql
-- good
CREATE ROLE etl LOGIN; -- row policies apply to every app connection
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Row Security Policies (PostgreSQL documentation); Role Membership (PostgreSQL documentation)

## gov/no-public-privileges

*No privileges granted to PUBLIC on governed data*

No GRANT of any privilege on production schemas, tables, or sequences names the PUBLIC pseudo-role; every consumer accesses governed data through a named role with an owner.

Why: A grant to PUBLIC silently includes every current and future role in the database, so PII tables become world-readable the moment a new login is created, with no review catching it.

```sql
-- bad
GRANT SELECT ON app.users TO PUBLIC;
```
```sql
-- good
GRANT SELECT ON app.users TO reporting_role;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: GRANT (PostgreSQL documentation); Privileges (PostgreSQL documentation)

## gov/no-raw-pii-in-seeds

*Seed files contain no raw PII*

Static seed files committed to the repository contain no raw emails, phone numbers, government IDs, or other direct identifiers; examples use clearly synthetic values (for example example.com addresses).

Why: Seeds are copied to every clone, CI run, and dev database; raw PII in a seed becomes an unrevocable, unaudited distribution of personal data to everyone with repo access, including future forks.

```sql
-- bad
seeds/users.csv row: 42, maria.garcia@example.com, +1-415-555-0132.
```
```sql
-- good
seeds/users.csv row: 42, user42@example.com, +1-555-010-0042 (documented fixture range).
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Add data tests to your DAG; Data Mesh Principles and Logical Architecture

## gov/pii-classification-declared

*PII and sensitive columns are classified in the contract*

Every column holding personal, contact, credential, financial, health, or location data is tagged with its sensitivity class in the model or dataset contract (schema metadata), and unclassified columns on a classified table are explicitly marked reviewed, not left blank.

Why: Row filters, masks, grants, and retention rules all key off classification; unclassified columns default to unprotected, so every downstream control silently skips the data nobody labeled.

```sql
-- bad
schema.yml for users lists columns id, email, phone with no tags or meta.
```
```sql
-- good
schema.yml tags email and phone as pii-contact and id as non-sensitive-reviewed.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: explore,build,assure
- severity: assure=warning, build=warning, explore=warning
- sources: About documentation; About dbt models

## gov/production-pii-not-in-dev

*Development environments do not read production PII*

Development and sandbox runs resolve sources classified as holding PII to synthetic, sampled-and-redacted, or staging equivalents, never to the production tables; the source-to-environment mapping is declared and reviewable.

Why: Dev databases are copied to laptops, kept indefinitely, and rarely audited; pointing them at production PII silently multiplies every breach surface while bypassing the access controls placed on production.

```sql
-- bad
profiles.yml dev target: production database and schema with raw users table.
```
```sql
-- good
dev target resolves users to seeds/synthetic or a redacted staging clone; mapping declared in the project.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=error, build=error
- sources: Add sources to your DAG; SQLMesh documentation

## gov/redacting-views-for-pii

*Non-privileged consumers read PII through redacting views*

Roles that do not need raw personal data are granted access only to views that hash, truncate, or redact classified columns; raw tables stay restricted to the owning pipeline role and named break-glass roles.

Why: Granting raw tables to broad audiences and trusting each query to redact fails open: one ad-hoc export bypasses every redaction habit at once, while views make the safe shape the only shape available.

```sql
-- bad
GRANT SELECT ON users TO analyst_role; analysts expected to redact manually.
```
```sql
-- good
analyst_role holds SELECT on users_redacted (email hashed, phone last-four only); raw users stays with etl_owner.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: GRANT (PostgreSQL documentation); Privileges (PostgreSQL documentation)

## gov/retention-declared

*Personal-data tables declare a retention window*

Every table classified as holding personal data declares a retention window (how long rows and history are kept) and the mechanism that enforces it (partition expiry, snapshot pruning, table VACUUM/retention settings, or a scheduled delete job).

Why: Data kept forever is breached forever: without a declared window, stale PII accumulates in snapshots and history far past its purpose, and every incident's scope — and regulatory exposure — grows with age.

```sql
-- bad
events table holds device_id with no retention note and unbounded snapshot history.
```
```sql
-- good
events declares retention: 400 days via monthly partition drop plus snapshot history pruned to 13 months.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,assure
- severity: assure=warning, build=warning
- sources: Introduction (Apache Iceberg documentation); Welcome to the Delta Lake documentation

## gov/row-policy-for-sensitive-tables

*Sensitive tables carry an enforced row filter*

Every production table classified as holding personal or tenant-scoped data has row-level security enabled with at least one policy recorded in reviewed DDL; a classified table with no policy declaration fails review.

Why: Postgres tables have no policies by default, so anyone with table privileges sees all rows; without a declared policy, tenant and region scoping lives only in application code and one forgotten WHERE clause leaks another…

```sql
-- bad
customers classified as containing PII; no ENABLE ROW LEVEL SECURITY in DDL.
```
```sql
-- good
ALTER TABLE customers ENABLE ROW LEVEL SECURITY plus a CREATE POLICY tenant_isolation scoping rows to the current tenant.
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Row Security Policies (PostgreSQL documentation)
