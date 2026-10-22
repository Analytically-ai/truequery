# Demo shop

A small synthetic online shop: a DuckDB warehouse and a dbt project.

```bash
./generate.sh        # builds warehouse.duckdb (needs Python with duckdb; uses uv when installed)
./dq "SHOW TABLES"   # read-only SQL against it
```

Prompts to try, one per role: `docs/try-the-demo.md` in the TrueQuery repo.
