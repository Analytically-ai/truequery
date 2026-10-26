# Evidence and limits

TrueQuery is a preview (0.1). This page says what we've measured, how we
measured it, and what we haven't checked yet. It follows the same rule as
the receipt: a pass never claims more than was verified.

## The checker

- **Precision on real code.** We ran the checker over 10 public
  repositories: dbt projects (jaffle-shop, jaffle-shop-classic,
  dbt-project-evaluator, dbt-artifacts, and a BigQuery warehouse from
  cal-itp), Mattermost's PostgreSQL migrations, MediaWiki, the PostgreSQL
  regression suite, SQLMesh's examples and the Spider text-to-SQL dev set.
  We labelled every finding by hand. Checks that misfired were fixed or
  switched off by default, which is why 15 of the 25 checks are on by
  default and the other 10 wait until they prove precise.
- **Tests.** 319 automated tests run on Linux, macOS and Windows with
  Python 3.11 to 3.13. 22 practices in the standard carry an executable
  proof that runs on every change.
- **Never a false "clean".** A file the checker can't parse, can't decode
  or that is over the 10 MB limit is listed as not checked, never as clean.

## The skill

- **Builds.** In an internal comparison on 8 build tasks, runs with
  TrueQuery shipped tests, a contract and recorded decisions in 7 of 8.
  Runs without it did so in 0 of 8. Answers to analytical questions were
  about equal with and without. We graded these ourselves; it was not blind.
- **Tokens.** In one hands-on test, a single pass with TrueQuery used about
  2.6 times the tokens of a pass without it, because it reads more and
  checks more. Reaching merge-ready work took about 260k tokens with
  TrueQuery against about 454k without, because fewer rounds of fixes were
  needed. That is one test, not a benchmark.

## What cuts against us

Agents rarely fall for classic SQL traps on their own. In our testing,
their mistakes were mostly semantic: the wrong definition, a made-up
column, an unstated assumption. That's why TrueQuery leads with the
procedure, your definitions and the receipt, and treats the checker as a
safety floor rather than the headline.

## Not checked yet

- **No blind, independent comparison.** That's the next evidence step.
- **No outside testers' repositories yet.** Preview testers are the step
  after that.
- **Dialects.** Tested on real code in BigQuery, PostgreSQL and DuckDB.
  Snowflake, Redshift, Databricks and Spark, T-SQL, MySQL, Trino and the
  other dialects sqlglot supports are parsed, with less test coverage.
- **Other agents.** Hook configs for Codex and Muse Code are included but
  not yet verified live.
- **Your warehouse.** The checker reads files. It doesn't connect to a
  warehouse, so it can't see column types, data or row counts, and the
  receipt says so every time.

## Help us fill the gaps

Found a false alarm or a miss? [Report it here](https://github.com/Analytically-ai/truequery/issues/new?template=false_alarm.yml). It's the
most useful feedback we can get.
