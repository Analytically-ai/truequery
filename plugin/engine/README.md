# TrueQuery engine

The offline checker behind [TrueQuery](https://github.com/Analytically-ai/truequery).
It installs two commands, `truequery` and its short alias `tq`.

```bash
pip install truequery
uvx truequery detect .
```

It looks for SQL that silently returns the wrong answer: a `NOT IN` that one
NULL empties, a `WHERE` that turns a `LEFT JOIN` into an inner join, a
`COUNT(*)` that counts missing rows, an unguarded `UPDATE` or `DELETE`.
Twenty-five checks ship with the preview, 15 of them on by default, each
promoted only when it proves precise on real projects. Files it can't parse,
can't decode, or that are over the 10 MB limit are listed as not checked,
never as clean.

It runs offline and gives deterministic output, with no LLM and no network
calls: plain code on your machine, in well under a second per file. Text,
JSON and SARIF output; exit codes 0 (clean), 1 (findings), 2 (usage or config
error) and 3 (some files couldn't be checked). See the
[repository README](https://github.com/Analytically-ai/truequery) for the
full guide: the skill, the receipt, hooks, and the standard behind the checks.
