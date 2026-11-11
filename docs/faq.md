# FAQ

Back to the [README](../README.md).

**Why not just put these rules in CLAUDE.md?**
Instructions in CLAUDE.md are a good start, but nothing checks that the
agent followed them, and an agent reviewing its own work is blind in the
same places as its first draft. TrueQuery gives the agent a full procedure
and 234 sourced practices, then backs them with a deterministic checker
that runs on every edit and a receipt that says what wasn't verified. It
works alongside your CLAUDE.md; your team's definitions go in `DATA.md`.

**Why not just run SQLFluff?**
SQLFluff checks style and layout. TrueQuery checks whether the logic
returns the right answer: a `NOT IN` that one NULL empties, a `WHERE` that
turns a `LEFT JOIN` into an inner join, a `COUNT(*)` that counts missing
rows. They don't overlap, so use both.

**What does it cost in tokens?**
A little more. On 10 vague, realistic requests, TrueQuery cost about 19% more
than plain Claude Code in tokens, and made no silent mistakes where plain
Claude Code made three. The checker itself uses no tokens: it's plain code running
on your machine. See [Evidence and limits](evidence.md).

**Which SQL dialects does the checker handle?**
It parses SQL with [sqlglot](https://github.com/tobymao/sqlglot). Tested on
real code: BigQuery, PostgreSQL and DuckDB. Tested on synthetic workloads:
Snowflake, Redshift and SQLite. Databricks and Spark, T-SQL, MySQL, Trino
and sqlglot's other dialects are parsed too, with less test coverage, so
false alarms there are especially welcome. `/truequery init` detects the
dialect from your dbt or SQLMesh config. You can also set it in `DATA.md`
or with `--dialect`. A file that doesn't parse is reported as not
checked, never as clean.

**What do I do about a false alarm?**
1. [Report it](https://github.com/Analytically-ai/truequery/issues/new?template=false_alarm.yml). It's the most useful feedback we get.
2. Ignore one line, with a reason:
   `truequery ignore add <rule> <path:line> --reason "..."`
3. Or switch the rule off for the project:
   `truequery rules set <rule> --enabled off` (add `--scope user` to
   switch it off just for you).

**Does my code or data leave my machine?**
The checker makes no network calls and sends no telemetry. Claude Code
still talks to its model provider as it always does, and TrueQuery doesn't
change that. `truequery verify` reads local data files on your machine,
read-only, and never connects to your warehouse. It shows counts and at
most five key values (and, for a freshness check, the newest timestamp),
never other values. See [Checking your
data](data.md).

**Does checking data mean installing something?**
Only for data files and DuckDB databases, and only when you say yes. SQLite
needs nothing extra. `truequery setup data` prints the command and what it
downloads (DuckDB from PyPI, 15–25 MB) and installs it only with `--yes`.
Until then, `verify` checks what it can and the receipt names what it
couldn't.
