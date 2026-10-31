# Evidence and limits

TrueQuery is a preview (0.1). This page says what we've measured, how we
measured it, and what we haven't checked yet. It follows the same rule as
the receipt: a pass never claims more than was verified.

## On vague requests, no silent mistakes

**On vague, real-world requests, TrueQuery made no silent mistakes. Plain
Claude Code made three.** Same model (Claude Opus 5.5), same 10 requests,
typed the way a busy engineer would, graded blind against answers fixed in
advance. With TrueQuery, every build also shipped tests and a contract, for
about 20% more tokens.

The requests were things like "how much money did we make last month?",
"build a daily refunds table" and "can you review the marts before I merge",
asked of a demo shop and a real bank dataset, both with real traps. Both arms
got the same prompt text. The scoring rules were written before any run, and
the answers were graded with the arm hidden.

| | Plain Claude Code | With TrueQuery |
|---|---|---|
| Silently wrong answers | 3 | **0** |
| Builds with tests and a contract | 0 of 1 | **1 of 1** |
| Cost for the 10 tasks | $2.59 | $3.08 (+19%) |

**The three mistakes:**
- a monthly revenue figure that put orders in the wrong month, because the
  warehouse build cuts days in Los Angeles time while the house rule says UTC;
- two answers that reported Czech crowns as dollars, because the data
  dictionary says USD.

**Why TrueQuery avoided them:** `/truequery init` found the time-zone problem
and asked about the currency once, then recorded both in `DATA.md`. Every later request used them. A plain agent
rediscovers the data from scratch each time, and sometimes misses.

**Reviews:** in `/truequery review`, TrueQuery found about 16 of the 18 known
issues in the demo shop's marts, close to what plain Claude Code found. It also quotes
the checker's receipt, checks the code against every house definition, and
labels each finding "confirmed in data" or "by reading".

Everything is published in
[benchmarks/messy-requests](benchmarks/messy-requests/): the rubric, every
answer with the arm hidden, the key, the definitions the TrueQuery arm started
from, and the runner.

**Caveats:**
- One run per task; repeated runs are next.
- The TrueQuery arm started from the definitions of one `/truequery init`
  session per project, which is how the product is meant to be used.
- We built the benchmark and graded it ourselves; it hasn't been
  independently replicated.

## Where it fits

On precisely worded questions, Claude is already accurate, and TrueQuery adds
little there beyond the receipt. Its value is in vague, everyday requests,
where your definitions and the data's traps decide whether the number is
right.

## Earlier results

- **Builds.** In an internal comparison on 8 build tasks, runs with TrueQuery
  shipped tests, a contract and recorded decisions in 7 of 8, against 0 of 8
  without it. We graded these ourselves.
- **Tokens.** In one hands-on test, a single pass used about 2.6 times the
  tokens, but reaching merge-ready work took about 260k tokens with TrueQuery
  against about 454k without, because fewer rounds of fixes were needed. Across
  the benchmark above, the difference was +19%.
- **Checker precision on real code.** We ran the checker over 10 public
  repositories: dbt projects (jaffle-shop, jaffle-shop-classic,
  dbt-project-evaluator, dbt-artifacts, and a BigQuery warehouse from
  cal-itp), Mattermost's PostgreSQL migrations, MediaWiki, the PostgreSQL
  regression suite, SQLMesh's examples and the Spider text-to-SQL dev set. We
  labelled every finding by hand. Checks that misfired were fixed or switched
  off by default, which is why 15 of the 25 checks are on by default and the
  other 10 wait until they prove precise.
- **Tests.** More than 300 automated tests run on Linux, macOS and Windows
  with Python 3.11 to 3.14, including the plugin's own scripts end to end. 22
  practices in the standard carry an executable proof that runs on every
  change. Snowflake and Redshift syntax (QUALIFY, FLATTEN, IFF,
  DISTKEY and SORTKEY, DELETE USING) has its own tests, contributed from
  outside the team, and so does SQLite (INSERT OR REPLACE, strftime,
  AUTOINCREMENT).
- **Never a false "clean".** A file the checker can't parse, can't decode or
  that is over the 10 MB limit is listed as not checked, never as clean.

## Not checked yet

- **Other agents.** Hook configs for Codex and Muse Code are included but not
  yet verified live ([#9](https://github.com/Analytically-ai/truequery/issues/9)).
- **Windows in a live session.** CI runs the plugin's scripts on Windows, but
  nobody has run TrueQuery in Claude Code on a real Windows machine yet
  ([#12](https://github.com/Analytically-ai/truequery/issues/12)).
- **Outside testers.** Preview testers are the next step.
- **Dialects.** Tested on real code in BigQuery, PostgreSQL and DuckDB, and on
  synthetic Snowflake, Redshift and SQLite workloads. Databricks and Spark,
  T-SQL, MySQL, Trino and the other dialects sqlglot supports are parsed,
  with less test coverage.
- **Your warehouse.** The checker reads files. It doesn't connect to a
  warehouse, so it can't see column types, data or row counts, and the
  receipt says so every time.

## Help us fill the gaps

Found a false alarm or a miss? [Report it here](https://github.com/Analytically-ai/truequery/issues/new?template=false_alarm.yml).
It's the most useful feedback we can get. Preview testers: tell us what it
got wrong in [#8](https://github.com/Analytically-ai/truequery/issues/8).
