# sql — TrueQuery practices

Self-contained: everything below comes from the standard records, no other file needed.

## sql/accidental-cross-join

*Every join of two tables carries an explicit join predicate*

No query block joins two table expressions without an explicit linking predicate (ON/USING) unless the join is written as an explicit CROSS JOIN; comma-separated FROM lists contain at most one table expression. This record owns the comma-join ban for the whole pack.

Why: A missing predicate multiplies row counts (m x n) while the query stays syntactically valid, so nothing fails loudly. At production scale this exhausts memory or disk and, worse, silently feeds wrong rows downstream.

```sql
-- bad
SELECT COUNT(*) FROM customers, orders;
```
```sql
-- good
SELECT COUNT(*) FROM customers JOIN orders ON orders.customer_id = customers.id;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Joins Between Tables (PostgreSQL tutorial); FROM and JOIN Clauses (DuckDB documentation)
- proof bad: `SELECT COUNT(*), SUM(amount) FROM cust, ords` → [(6, 120)]
- proof good: `SELECT COUNT(*), SUM(amount) FROM cust JOIN ords ON ords.cid = cust.id` → [(3, 60)]

## sql/average-of-ratios

*Aggregate a ratio as SUM(numerator) / SUM(denominator), not AVG of row ratios*

When a query reports a ratio (rate, share, conversion, margin) over more than one row, it divides the summed numerator by the summed denominator. It does not AVG a column or expression that is already a per-row ratio, unless the unweighted mean of row ratios is explicitly what is wanted and is labelled as such.

Why: AVG(rate) gives every row equal weight regardless of its denominator, so a day with 2 visits counts as much as a day with 100. The result is a different number from the overall rate, often by a large margin, and nothing …

```sql
-- bad
SELECT AVG(conversions / visits) AS cvr FROM daily_funnel;
```
```sql
-- good
SELECT SUM(conversions) / SUM(visits) AS cvr FROM daily_funnel;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore,build,change
- severity: build=warning, change=warning, explore=warning
- sources: Aggregate Functions (PostgreSQL); Aggregate Functions (DuckDB documentation)
- proof bad: `SELECT round(AVG(conversions / visits), 4) AS cvr FROM daily_funnel` → [(0.3,)]
- proof good: `SELECT round(SUM(conversions) / SUM(visits), 4) AS cvr FROM daily_funnel` → [(0.1078,)]

## sql/between-date-on-timestamp

*Filter timestamps with a half-open range, not BETWEEN date literals*

A filter on a TIMESTAMP (or DATETIME) column uses a half-open range, ts >= start AND ts < end_exclusive, never BETWEEN with date-only bounds such as BETWEEN '2024-01-01' AND '2024-01-31'.

Why: BETWEEN includes both endpoints, and a date-only bound compared with a timestamp means midnight. BETWEEN '2024-01-01' AND '2024-01-31' therefore keeps only the first instant of January 31 and drops the rest of that day. …

```sql
-- bad
SELECT SUM(amount) FROM orders WHERE order_ts BETWEEN '2024-01-01' AND '2024-01-31';
```
```sql
-- good
SELECT SUM(amount) FROM orders WHERE order_ts >= '2024-01-01' AND order_ts < '2024-02-01';
```

- checked by truequery: yes
- enforcement: detector
- modes: explore,build,change
- severity: build=warning, change=warning, explore=warning
- sources: Comparison Functions and Operators (PostgreSQL); Date/Time Functions and Operators (PostgreSQL); Comparison Operators (DuckDB documentation)
- proof bad: `SELECT COUNT(*), SUM(amount) FROM orders WHERE order_ts BETWEEN '2024-01-01' AND '2024-01-31'` → [(3, 60)]
- proof good: `SELECT COUNT(*), SUM(amount) FROM orders WHERE order_ts >= '2024-01-01' AND order_ts < '2024-02-01'` → [(4, 100)]

## sql/case-without-else-in-aggregate

*A CASE that feeds arithmetic or an aggregate states its ELSE*

A CASE expression whose result feeds an aggregate (SUM, AVG), arithmetic, or a filter has an explicit ELSE branch (ELSE 0, ELSE NULL, or ELSE an explicit bucket such as 'unknown'), so the value for unmatched rows is a visible decision.

Why: A CASE with no ELSE returns NULL for rows that match no branch. SUM over a group with no matching rows is then NULL rather than 0, and NULL propagates through arithmetic: gross - refunds becomes NULL for that group. A la…

```sql
-- bad
SELECT region, SUM(CASE WHEN type = 'sale' THEN amt END) - SUM(CASE WHEN type = 'refund' THEN amt END) AS net FROM tx GROUP BY region;
```
```sql
-- good
SELECT region, SUM(CASE WHEN type = 'sale' THEN amt ELSE 0 END) - SUM(CASE WHEN type = 'refund' THEN amt ELSE 0 END) AS net FROM tx GROUP BY region;
```

- checked by truequery: yes
- enforcement: detector
- modes: explore,build,change
- severity: build=advisory, change=advisory, explore=advisory
- sources: Conditional Expressions (PostgreSQL); Aggregate Functions (PostgreSQL); CASE Expression (DuckDB documentation)
- proof bad: `SELECT SUM(net) FROM (SELECT region, SUM(CASE WHEN type = 'sale' THEN amt END) - SUM(CASE WHEN type = 'refund' THEN amt END) AS net FROM tx GROUP BY region)` → [(90,)]
- proof good: `SELECT SUM(net) FROM (SELECT region, SUM(CASE WHEN type = 'sale' THEN amt ELSE 0 END) - SUM(CASE WHEN type = 'refund' THEN amt ELSE 0 END) AS net FROM tx GROUP BY region)` → [(140,)]

## sql/count-star-after-left-join

*Count child rows with COUNT(child.key), not COUNT(*), after a LEFT JOIN*

When a query LEFT JOINs a parent to a child table and counts children per parent, it counts a non-null child column (COUNT(child.key)), never COUNT(*) or COUNT(1).

Why: A LEFT JOIN keeps each unmatched parent as one row with NULLs on the child side. COUNT(*) counts rows, so every parent with zero children is reported as having one. Zero-activity cohorts disappear from the data with no e…

```sql
-- bad
SELECT c.id, COUNT(*) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id;
```
```sql
-- good
SELECT c.id, COUNT(o.order_id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id;
```

- checked by truequery: yes
- enforcement: detector
- modes: explore,build,change,assure
- severity: assure=error, build=error, change=error, explore=warning
- sources: Aggregate Functions (PostgreSQL); Aggregate Functions (DuckDB documentation); Joins Between Tables (PostgreSQL tutorial)
- proof bad: `SELECT c.id, COUNT(*) AS n_orders FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id ORDER BY c.id` → [(1, 2), (2, 1), (3, 1)]
- proof good: `SELECT c.id, COUNT(o.order_id) AS n_orders FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id ORDER BY c.id` → [(1, 2), (2, 1), (3, 0)]

## sql/distinct-dedup-bandaid

*SELECT DISTINCT never stands in for a grain fix*

SELECT DISTINCT over a join is rejected unless the author records the business key it dedupes by and why duplicates are legitimate; the default fix is correcting the join grain, pre-aggregating, or using EXISTS.

Why: DISTINCT dedupes whole rows, not business keys. Used to undo a fanout, it also collapses legitimately identical rows (two orders of the same amount), so totals computed over the "deduplicated" set are silently low; and i…

```sql
-- bad
SELECT SUM(amt) FROM (SELECT DISTINCT o.cid, o.amt FROM orders o JOIN tags t ON t.cid = o.cid);
```
```sql
-- good
SELECT SUM(amt) FROM orders o WHERE EXISTS (SELECT 1 FROM tags t WHERE t.cid = o.cid);
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,explore
- severity: build=warning, change=warning, explore=warning
- sources: SELECT (PostgreSQL); Subquery Expressions (PostgreSQL)
- proof bad: `SELECT SUM(amt) FROM (SELECT DISTINCT o.cid, o.amt FROM orders o JOIN tags t ON t.cid = o.cid)` → [(40,)]
- proof good: `SELECT SUM(amt) FROM orders o WHERE EXISTS (SELECT 1 FROM tags t WHERE t.cid = o.cid)` → [(50,)]

## sql/explore-limit-is-not-a-sample

*Do not judge a table from its first LIMIT n rows; sample it*

When an analyst looks at example rows to understand a table (value formats, typical values, which columns are populated), the rows come from the engine's sampling clause (TABLESAMPLE / USING SAMPLE) or from an ORDER BY that is chosen on purpose, not from a bare SELECT * ... LIMIT n. Conclusions about "what the data looks like" are not drawn from the first n rows.

Why: A bare LIMIT returns whichever rows the engine reaches first. In practice that is often the oldest load, one file, or one partition (DuckDB, for example, preserves insertion order for this query shape), so the rows shown…

```sql
-- bad
SELECT * FROM events LIMIT 20;
```
```sql
-- good
SELECT * FROM events USING SAMPLE 20 ROWS;
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=advisory
- sources: SELECT (PostgreSQL); Order Preservation (DuckDB documentation); Samples (DuckDB documentation)

## sql/explore-profile-filter-columns

*Profile a column's NULLs and distinct values before filtering or grouping on it*

Before an ad-hoc query filters or groups on a column of an unfamiliar table, the analyst checks that column's NULL count and its distinct values (or top values with counts), and keeps that check with the analysis. The filter is then written against the values that actually occur, including NULL.

Why: Most silent wrong answers in exploration come from filters written against assumed values: <> 'x' dropping NULL rows, = 'Cancelled' missing 'cancelled' and 'CANCELLED', IN ('web', 'app') missing 'ios'. A one-line profile…

```sql
-- bad
SELECT SUM(amount) FROM orders WHERE country = 'US';
```
```sql
-- good
SELECT country, COUNT(*), COUNT(*) - COUNT(country) AS nulls FROM orders GROUP BY country ORDER BY 2 DESC; -- then filter on the values found
```

- checked by truequery: no (taught, not checked)
- enforcement: judgment
- modes: explore
- severity: explore=advisory
- sources: Summarize (DuckDB documentation); Aggregate Functions (PostgreSQL)

## sql/fanout-join-pre-aggregation

*Aggregates over a many-side join pre-aggregate before joining*

When a query joins a fact table to a table that holds multiple rows per join key and then aggregates a fact-side column, the many side is aggregated to the join grain in a CTE first, unless the join key is declared unique on the many side.

Why: Each fact row is duplicated once per matching many-side row, so SUM and COUNT silently inflate. This is the most common silent-corruption shape in analytics SQL, and reviewers miss it easily because the query reads corre…

```sql
-- bad
SELECT SUM(o.amount) FROM orders o JOIN items i ON i.order_id = o.order_id;
```
```sql
-- good
WITH i AS (SELECT order_id, SUM(qty) AS q FROM items GROUP BY order_id) SELECT SUM(o.amount) FROM orders o JOIN i ON i.order_id = o.order_id;
```

- checked by truequery: no (taught, not checked)
- enforcement: contract
- modes: build,change,assure,explore
- severity: assure=warning, build=warning, change=warning, explore=warning
- sources: Joins Between Tables (PostgreSQL tutorial); Aggregate Functions (DuckDB documentation); Optimize query computation (BigQuery)
- proof bad: `SELECT SUM(o2.amount) FROM o2 JOIN it ON it.order_id = o2.order_id` → [(400,)]
- proof good: `WITH i AS (SELECT order_id, SUM(qty) AS q FROM it GROUP BY order_id) SELECT SUM(o2.amount) FROM o2 JOIN i ON i.order_id = o2.order_id` → [(300,)]

## sql/group-by-bare-column

*In engines that allow it, GROUP BY queries never project bare ungrouped columns*

On engines that silently accept ungrouped columns in an aggregate query (MySQL with ONLY_FULL_GROUP_BY disabled, SQLite), every projected column under GROUP BY is a grouping key, wrapped in an aggregate, or wrapped in an explicit arbitrariness marker such as ANY_VALUE(). Engines that reject the query (DuckDB, Postgres, BigQuery, Snowflake, MySQL in its default mode) need no lint: the error already stops it.

Why: Where the engine accepts it, a bare column returns the value from one input row of the group, chosen by the engine, and reads as meaningful. Reports then show a plausible but arbitrary representative per group. MySQL doc…

```sql
-- bad
SELECT dept, emp, COUNT(*) FROM staff GROUP BY dept;
```
```sql
-- good
SELECT dept, MIN(emp), COUNT(*) FROM staff GROUP BY dept;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure,explore
- severity: assure=warning, build=warning, change=warning, explore=warning
- sources: MySQL Handling of GROUP BY (MySQL 8.4 Reference Manual); SELECT, section Bare columns in an aggregate query (SQLite); SELECT (PostgreSQL)
- proof bad: `SELECT dept, emp, COUNT(*) FROM g GROUP BY dept ORDER BY dept` → ERROR: BinderException
- proof good: `SELECT dept, MIN(emp), COUNT(*) FROM g GROUP BY dept ORDER BY dept` → [('e', 'a', 2), ('f', 'c', 1)]

## sql/insert-explicit-column-list

*INSERT INTO names its target columns*

Every INSERT INTO names the target columns explicitly and projects the source in the same order (or uses a by-name form such as DuckDB's INSERT INTO ... BY NAME); positional INSERT without a column list is rejected.

Why: Positional inserts bind by declared column order, not by name. When the source has the same number of compatible columns in a different order, the insert succeeds and silently writes values into the wrong columns; the de…

```sql
-- bad
INSERT INTO customers SELECT * FROM staging_customers;
```
```sql
-- good
INSERT INTO customers (first_name, last_name) SELECT first_name, last_name FROM staging_customers;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: INSERT (PostgreSQL); INSERT Statement (DuckDB documentation)
- proof bad: `INSERT INTO tgt SELECT * FROM src; SELECT first_name, last_name FROM tgt ORDER BY first_name` → [('Jones', 'Ben'), ('Smith', 'Anna')]
- proof good: `INSERT INTO tgt (first_name, last_name) SELECT first_name, last_name FROM src; SELECT first_name, last_name FROM tgt ORDER BY first_name` → [('Anna', 'Smith'), ('Ben', 'Jones')]

## sql/integer-division-truncation

*Divide integers only after casting to a non-integer type*

In dialects where integer / integer yields an integer (Postgres, SQL Server, SQLite), a division whose operands are both integer-typed casts one operand to NUMERIC/DECIMAL/DOUBLE (or multiplies by 1.0) before dividing. In DuckDB, the integer-division operator // (and divide()) is used only when truncation is intended.

Why: Postgres, SQL Server and SQLite truncate integer division toward zero, so a ratio of counts (conversions / visits) is silently 0 for every row where the numerator is smaller than the denominator. Dashboards show 0 percen…

```sql
-- bad
SELECT conversions / visits AS cvr FROM daily_funnel;  -- Postgres, SQL Server, SQLite
```
```sql
-- good
SELECT conversions::numeric / visits AS cvr FROM daily_funnel;
```

- checked by truequery: yes
- enforcement: detector
- modes: explore,build,change
- severity: build=warning, change=warning, explore=warning
- sources: Mathematical Functions and Operators (PostgreSQL); / (Division) (Transact-SQL); SQL Language Expressions (SQLite); Numeric Functions (DuckDB documentation); Arithmetic operators (Snowflake); Operators (BigQuery); Arithmetic Operators (MySQL 8.4 Reference Manual)
- proof bad: `SELECT day, conversions // visits AS cvr FROM daily_funnel ORDER BY day` → [(1, 0), (2, 0)]
- proof good: `SELECT day, round(CAST(conversions AS DOUBLE) / visits, 4) AS cvr FROM daily_funnel ORDER BY day` → [(1, 0.0308), (2, 0.05)]

## sql/keyset-pagination

*Paginated reads use keyset seeks, not OFFSET*

Queries that page through a table that can change between page fetches use a keyset predicate on a unique ordered key (WHERE key > :last_seen ORDER BY key LIMIT n) instead of LIMIT plus OFFSET, and page ordering always includes a unique tiebreaker. Large literal OFFSETs (default threshold 10,000) are flagged for cost as well.

Why: Rows inserted or deleted ahead of the cursor between page fetches shift every OFFSET, so rows are skipped or delivered twice and the export still reports success. OFFSET also forces the engine to produce and discard the …

```sql
-- bad
SELECT id FROM p ORDER BY id LIMIT 2 OFFSET 2;
```
```sql
-- good
SELECT id FROM p WHERE id > 2 ORDER BY id LIMIT 2;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: LIMIT and OFFSET (PostgreSQL); Troubleshoot query issues (BigQuery)
- proof bad: `SELECT id FROM p12 ORDER BY id LIMIT 2 OFFSET 2` → [(3,), (4,)]
- proof good: `SELECT id FROM p12 WHERE id > 2 ORDER BY id LIMIT 2` → [(3,), (4,)]

## sql/left-join-null-rejecting-where

*Filters on the nullable side of an outer join live in ON*

Predicates on columns of the nullable side of a LEFT, RIGHT, or FULL join appear in the ON clause; the WHERE clause never null-rejects those columns unless the inner-join conversion is intended and recorded.

Why: A WHERE predicate that null-rejects eliminates the unmatched rows the outer join was written to keep, silently converting it to an inner join. Reports undercount with no error.

```sql
-- bad
SELECT c.id FROM c LEFT JOIN o ON o.cid = c.id WHERE o.status = 'done';
```
```sql
-- good
SELECT c.id FROM c LEFT JOIN o ON o.cid = c.id AND o.status = 'done';
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure,explore
- severity: assure=warning, build=warning, change=warning, explore=warning
- sources: Joins Between Tables (PostgreSQL tutorial); Table Expressions (PostgreSQL)
- proof bad: `SELECT lc.id, lo.status FROM lc LEFT JOIN lo ON lo.cid = lc.id WHERE lo.status = 'done' ORDER BY 1` → [(1, 'done')]
- proof good: `SELECT lc.id, lo.status FROM lc LEFT JOIN lo ON lo.cid = lc.id AND lo.status = 'done' ORDER BY 1` → [(1, 'done'), (2, None)]

## sql/limit-requires-order-by

*Every LIMIT carries an ORDER BY*

Any query block with LIMIT or FETCH FIRST also has an ORDER BY, unless the limit is a bare existence probe (LIMIT 1 whose projected values are not used). The ORDER BY must also be over a key that is unique in the result. These are two separate checks: the detector flags only a MISSING ORDER BY; proving that the ORDER BY keys are unique needs declared uniqueness facts and is left to contract mode.

Why: Without ordering, which rows come back is unspecified and can change between runs after reloads, compaction, or parallelism changes. Dashboards, samples, and tests built on unordered limits flap. A non-unique ORDER BY ha…

```sql
-- bad
SELECT id FROM t LIMIT 2;
```
```sql
-- good
SELECT id FROM t ORDER BY id LIMIT 2;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure,explore
- severity: assure=warning, build=warning, change=warning, explore=warning
- sources: SELECT (PostgreSQL); Order Preservation (DuckDB documentation)
- proof bad: `SELECT id FROM t5 LIMIT 2` → [(3,), (1,)]
- proof good: `SELECT id FROM t5 ORDER BY id LIMIT 2` → [(1,), (2,)]

## sql/natural-join-explicit-keys

*Joins name their keys explicitly, never NATURAL JOIN*

Joins name their keys with an explicit ON or USING list; NATURAL JOIN is rejected. (Comma joins without a predicate are covered by sql/accidental-cross-join, not by this record.)

Why: NATURAL JOIN infers keys from shared column names, so adding an audit column to both tables silently rewrites join semantics with no query edit. If the tables share no column names it degrades to a CROSS JOIN. The query …

```sql
-- bad
SELECT * FROM a NATURAL JOIN b;
```
```sql
-- good
SELECT * FROM a JOIN b ON a.id = b.id;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change
- severity: build=warning, change=warning
- sources: Table Expressions (PostgreSQL); FROM and JOIN Clauses (DuckDB documentation)
- proof bad: `SELECT n1.id, val, val2 FROM n1 NATURAL JOIN n2` → []
- proof good: `SELECT n1.id, val, val2 FROM n1 JOIN n2 ON n1.id = n2.id` → [(1, 'a', 'b')]

## sql/negative-filter-drops-nulls

*Negative filters on nullable columns state what happens to NULLs*

A negative predicate on a nullable column (col <> 'x', col != 'x', col NOT LIKE 'p%', col NOT IN ('a', 'b')) makes an explicit NULL decision in the same query: IS DISTINCT FROM 'x', OR col IS NULL, AND col IS NOT NULL, or a COALESCE. This is a heuristic: excluding NULLs may be intended, and the rule only asks that the choice be visible.

Why: "Not cancelled" reads as including rows with an unknown status, but under three-valued logic NULL <> 'cancelled' is unknown, so those rows are dropped. The query runs, the count looks plausible, and the NULL rows vanish …

```sql
-- bad
SELECT COUNT(*) FROM orders WHERE status <> 'cancelled';
```
```sql
-- good
SELECT COUNT(*) FROM orders WHERE status IS DISTINCT FROM 'cancelled';
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore,build,change
- severity: build=warning, change=warning, explore=warning
- sources: Comparison Functions and Operators (PostgreSQL); Pattern Matching (PostgreSQL); NULL Values (DuckDB documentation)
- proof bad: `SELECT (SELECT COUNT(*) FROM orders WHERE status <> 'cancelled'), (SELECT COUNT(*) FROM orders WHERE status NOT LIKE 'cancel%'), (SELECT COUNT(*) FROM orders WHERE channel NOT IN ('store'))` → [(2, 2, 2)]
- proof good: `SELECT (SELECT COUNT(*) FROM orders WHERE status IS DISTINCT FROM 'cancelled'), (SELECT COUNT(*) FROM orders WHERE status NOT LIKE 'cancel%' OR status IS NULL), (SELECT COUNT(*) FROM orders WHERE channel NOT IN ('store') OR channel IS NULL)` → [(3, 3, 3)]

## sql/nondeterministic-merge-keys

*Merge and dedupe keys never use nondeterministic functions*

Merge keys, dedupe keys, and incremental watermarks never call per-row nondeterministic functions (RAND, RANDOM, UUID, GEN_RANDOM_UUID, NEWID); wall-clock calls in keys are replaced by deterministic business keys, and templates are checked pre-render so Jinja markers like is_incremental do not hide the call.

Why: A key that changes per row or per run defeats matching by construction: every rerun produces new keys, so a MERGE inserts instead of updating and reruns are never idempotent. The table silently grows by one full copy of …

```sql
-- bad
MERGE INTO t USING (SELECT uuid() AS uid, order_id, amt FROM s) s ON t.uid = s.uid WHEN NOT MATCHED THEN INSERT VALUES (s.uid, s.order_id, s.amt);
```
```sql
-- good
MERGE INTO t USING s ON t.order_id = s.order_id WHEN MATCHED THEN UPDATE SET amt = s.amt WHEN NOT MATCHED THEN INSERT (order_id, amt) VALUES (s.order_id, s.amt);
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: MERGE INTO Statement (DuckDB documentation); Utility Functions (DuckDB documentation); Configure incremental models (dbt)
- proof bad: `MERGE INTO tgt USING (SELECT uuid()::VARCHAR AS uid, order_id, amt FROM src) s ON tgt.uid = s.uid WHEN MATCHED THEN UPDATE SET amt = s.amt WHEN NOT MATCHED THEN INSERT (uid, order_id, amt) VALUES (s.uid, s.order_id, s.amt); MERGE INTO tgt USING (SELECT uuid()::VARCHAR AS uid, order_id, amt FROM src) s ON tgt.uid = s.uid WHEN MATCHED THEN UPDATE SET amt = s.amt WHEN NOT MATCHED THEN INSERT (uid, order_id, amt) VALUES (s.uid, s.order_id, s.amt); SELECT COUNT(*) FROM tgt` → [(4,)]
- proof good: `MERGE INTO tgt USING (SELECT uuid()::VARCHAR AS uid, order_id, amt FROM src) s ON tgt.order_id = s.order_id WHEN MATCHED THEN UPDATE SET amt = s.amt WHEN NOT MATCHED THEN INSERT (uid, order_id, amt) VALUES (s.uid, s.order_id, s.amt); MERGE INTO tgt USING (SELECT uuid()::VARCHAR AS uid, order_id, amt FROM src) s ON tgt.order_id = s.order_id WHEN MATCHED THEN UPDATE SET amt = s.amt WHEN NOT MATCHED THEN INSERT (uid, order_id, amt) VALUES (s.uid, s.order_id, s.amt); SELECT COUNT(*) FROM tgt` → [(2,)]

## sql/not-in-nullable-subquery

*NOT IN is never used against a nullable subquery*

Any NOT IN whose subquery column is nullable is rewritten as NOT EXISTS (correlated) or the subquery explicitly excludes NULLs.

Why: Three-valued logic poisons the whole predicate: a single NULL in the subquery makes NOT IN unknown for every row that is not otherwise matched, so the query returns zero rows. It runs fine and the answer is silently empt…

```sql
-- bad
SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM banned);
```
```sql
-- good
SELECT id FROM users u WHERE NOT EXISTS (SELECT 1 FROM banned b WHERE b.user_id = u.id);
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure,explore
- severity: assure=error, build=error, change=error, explore=warning
- sources: Subquery Expressions (PostgreSQL); NULL Values (DuckDB documentation)
- proof bad: `SELECT id FROM users WHERE id NOT IN (SELECT user_id FROM banned) ORDER BY id` → []
- proof good: `SELECT id FROM users u WHERE NOT EXISTS (SELECT 1 FROM banned b WHERE b.user_id = u.id) ORDER BY id` → [(1,), (3,)]

## sql/null-equality-comparison

*Test for NULL with IS NULL, never with = NULL or <> NULL*

A comparison against the NULL literal is written IS NULL / IS NOT NULL (or IS [NOT] DISTINCT FROM for a nullable value); the forms = NULL, <> NULL and != NULL never appear in a predicate.

Why: An ordinary comparison with NULL yields NULL (unknown), never true, so WHERE x = NULL and WHERE x <> NULL both filter out every row. The query runs without error and silently returns nothing; a JOIN ON a.x = NULL matches…

```sql
-- bad
SELECT id FROM customers WHERE email = NULL;
```
```sql
-- good
SELECT id FROM customers WHERE email IS NULL;
```

- checked by truequery: yes
- enforcement: detector
- modes: explore,build,change,assure
- severity: assure=error, build=error, change=error, explore=warning
- sources: Comparison Functions and Operators (PostgreSQL); NULL Values (DuckDB documentation); SET ANSI_NULLS (Transact-SQL)
- proof bad: `SELECT (SELECT COUNT(*) FROM customers WHERE email = NULL), (SELECT COUNT(*) FROM customers WHERE email <> NULL)` → [(0, 0)]
- proof good: `SELECT (SELECT COUNT(*) FROM customers WHERE email IS NULL), (SELECT COUNT(*) FROM customers WHERE email IS NOT NULL)` → [(1, 2)]

## sql/random-sampling-no-sort

*Sample large tables without sorting them by RANDOM()*

This is a performance practice, not a correctness rule. ORDER BY RANDOM() LIMIT n returns a valid uniform random sample of exactly n rows; it is correct. On large tables, prefer the engine's sampling clause (TABLESAMPLE / USING SAMPLE) or a single-pass random filter, because the ORDER BY form generates a key for and sorts every row to keep n of them.

Why: The ORDER BY RANDOM() form costs a full scan plus a sort of the whole table, so cost scales with table size, not sample size. Engine sampling clauses avoid the sort. They have different statistical properties (system/blo…

```sql
-- bad
SELECT id FROM big_events ORDER BY random() LIMIT 100; -- a correct uniform sample, but it sorts every row of a large table
```
```sql
-- good
SELECT id FROM big_events USING SAMPLE 100 ROWS; -- DuckDB reservoir sample of exactly 100 rows, no full sort
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: explore,build
- severity: build=advisory, explore=advisory
- sources: Samples (DuckDB documentation); SELECT (PostgreSQL)

## sql/row-number-dedupe-tiebreaker

*ROW_NUMBER dedupe orders by a unique tiebreaker*

Every ROW_NUMBER used to pick one row per group (QUALIFY rn = 1 or equivalent) has an ORDER BY whose columns are unique within the partition.

Why: Without a stable order the surviving row is arbitrary; with a non-unique order key, tied rows can swap winners between runs or reloads. Golden-record tables then flap and downstream diffs never settle.

```sql
-- bad
SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY email ORDER BY ts) AS rn FROM t) WHERE rn = 1;
```
```sql
-- good
SELECT * FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY email ORDER BY ts DESC, capture_id DESC) AS rn FROM t) WHERE rn = 1;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure
- severity: assure=warning, build=warning, change=warning
- sources: SELECT (PostgreSQL); Window Functions (DuckDB documentation); Non-Deterministic Behavior (DuckDB documentation)
- proof bad: `SELECT email, addr FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY email ORDER BY ts DESC) AS rn FROM t1) WHERE rn = 1` → [('a@x', 'old')]
- proof good: `SELECT email, addr FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY email ORDER BY ts DESC, capture_id DESC) AS rn FROM t1) WHERE rn = 1` → [('a@x', 'new')]

## sql/sargable-filter-bare-column

*Filter columns stay bare so partition and cluster pruning applies*

This is a performance rule, not a correctness rule: the wrapped and bare forms return the same rows. A filter on a partition, cluster, or sort key compares the bare column (range predicates for timestamps) rather than a function or cast wrapped around the column, so the engine can prune.

Why: Wrapping the key column can force per-row evaluation and defeat metadata pruning, turning a cheap pruned scan into a full-table scan. On pay-per-byte warehouses every run pays the multiplier. Whether a given wrapper defe…

```sql
-- bad
SELECT v FROM ev WHERE CAST(ts AS DATE) = DATE '2024-01-15';
```
```sql
-- good
SELECT v FROM ev WHERE ts >= TIMESTAMP '2024-01-15' AND ts < TIMESTAMP '2024-01-16';
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change
- severity: build=advisory, change=advisory
- sources: Query partitioned tables (BigQuery); Query clustered tables (BigQuery); Micro-partitions and Data Clustering (Snowflake)

## sql/set-operation-column-position

*Set-operation branches align columns explicitly by position*

Every branch of a UNION, INTERSECT, or EXCEPT projects the same columns in the same order with compatible types, verified by alias and type comparison across branches; branches never use SELECT * and branches with mismatched names fail review.

Why: Set operations pair columns by position, not by name. A reordered branch silently merges codes into names, and the query returns wrong rows with no error whenever the swapped types are compatible.

```sql
-- bad
SELECT name, code FROM a UNION ALL SELECT code, name FROM b;
```
```sql
-- good
SELECT name, code FROM a UNION ALL SELECT name, code FROM b;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: Combining Queries (UNION, INTERSECT, EXCEPT) (PostgreSQL); Set Operations (DuckDB documentation)
- proof bad: `SELECT name, code FROM a UNION ALL SELECT * FROM b ORDER BY name` → [('2', 'bob'), ('alice', '1')]
- proof good: `SELECT name, code FROM a UNION ALL SELECT name, code FROM b ORDER BY name` → [('alice', '1'), ('bob', '2')]

## sql/unguarded-update-delete

*UPDATE and DELETE always carry a filter or an explicit full-table guard*

Every UPDATE or DELETE has a WHERE clause (or WHERE CURRENT OF), or carries an explicit full-table marker such as a deliberate TRUNCATE or a reviewed full_refresh flag; unguarded whole-table mutations are rejected.

Why: A missing filter applies the mutation to every row. In a warehouse this is large-scale data loss or corruption, recoverable only through time travel or rebuild, and the statement itself reports success.

```sql
-- bad
UPDATE acct SET bal = 0;
```
```sql
-- good
UPDATE acct SET bal = 0 WHERE id = 1;
```

- checked by truequery: yes
- enforcement: detector
- modes: build,change,assure
- severity: assure=error, build=error, change=error
- sources: DELETE (PostgreSQL); UPDATE (PostgreSQL); DML syntax (BigQuery)
- proof bad: `UPDATE acct SET bal = 0; SELECT id, bal FROM acct ORDER BY id` → [(1, 0), (2, 0)]
- proof good: `UPDATE acct SET bal = 0 WHERE id = 1; SELECT id, bal FROM acct ORDER BY id` → [(1, 0), (2, 200)]

## sql/union-all-default

*Bare UNION states its dedup intent; UNION ALL is the default for appends*

UNION and UNION ALL answer different questions, and which one is right depends on intent. UNION (DISTINCT) is correct when the author wants the set of distinct rows across branches; UNION ALL is correct when appending rows that can legitimately repeat (events, transactions, log lines). A bare UNION is flagged until the author records that dedup is intended; appends use UNION ALL.

Why: UNION silently removes duplicate rows across and within branches. When the rows are separate real-world facts that happen to be identical (two identical payments, two webhook deliveries), that removal is data loss and ev…

```sql
-- bad
SELECT payment_id, amount FROM card_payments UNION SELECT payment_id, amount FROM wallet_payments;
```
```sql
-- good
SELECT payment_id, amount FROM card_payments UNION ALL SELECT payment_id, amount FROM wallet_payments;
```

- checked by truequery: no (taught, not checked)
- enforcement: detector
- modes: build,change,explore
- severity: build=warning, change=warning, explore=warning
- sources: Combining Queries (UNION, INTERSECT, EXCEPT) (PostgreSQL); Set Operations (DuckDB documentation); Query syntax, Set operators (BigQuery)
- proof bad: `SELECT SUM(amount) FROM (SELECT amount FROM e1 UNION SELECT amount FROM e2)` → [(60,)]
- proof good: `SELECT SUM(amount) FROM (SELECT amount FROM e1 UNION ALL SELECT amount FROM e2)` → [(80,)]
