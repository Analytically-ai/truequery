# TrueQuery engine

The offline checker behind the TrueQuery plugin. It installs two
commands, `truequery` and its short alias `tq`.

```bash
uv tool install <path-to-this-directory>
truequery detect path/to/query.sql
```

It runs offline and gives deterministic output, with no LLM and no
network calls. See the repository README for the full guide.
