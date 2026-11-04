# TrueQuery in pre-commit and GitHub Actions

The TrueQuery checker (`truequery`) runs in pre-commit and in GitHub Actions with the same guarantees as in your terminal: offline, deterministic, and no LLM in the check path. Back to the [README](../README.md).

## pre-commit

Add TrueQuery to `.pre-commit-config.yaml`:

```yaml
repos:
  - repo: https://github.com/Analytically-ai/truequery
    rev: v0.1.6
    hooks:
      - id: truequery
```

`truequery` runs `truequery detect` on staged `.sql` files. To check both `.sql` and `.py` files (for SQL inside Python files, such as Airflow DAGs and scripts, and for pandas code), use `truequery-python`:

```yaml
repos:
  - repo: https://github.com/Analytically-ai/truequery
    rev: v0.1.6
    hooks:
      - id: truequery-python
```

### Exit codes in pre-commit

- `0`: every staged file was checked and had no findings at or above `--fail-on` (default `error`).
- `1`: findings at or above `--fail-on`.
- `2`: usage or configuration error.
- `3`: no findings at or above `--fail-on`, but at least one file could not be checked (unparseable SQL, procedural blocks such as PL/pgSQL or `BEGIN … END`, undecodable text, or files over the 10 MB limit). Passing `args: [--allow-unverified]` turns exit code `3` into `0` so pre-commit only blocks commits on verified findings.

## GitHub Actions

Run TrueQuery on pull requests and pushes with the composite action in this repository. By default it runs `truequery review` and writes the findings and the **TrueQuery receipt** to the job summary (`$GITHUB_STEP_SUMMARY`), with no special token permissions required:

```yaml
name: truequery
on: [push, pull_request]

permissions:
  contents: read

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: Analytically-ai/truequery@v0.1.6
        with:
          path: .
```

### Inputs

| Input | Default | Description |
|---|---|---|
| `path` | `.` | Space-separated files or folders to check |
| `command` | `review` | `review` (findings plus the receipt) or `detect` (findings only) |
| `fail-on` | `error` | Lowest severity that fails the job (`error`, `warning`, or `advisory`) |
| `dialect` | `""` | SQL dialect override (e.g. `postgres`, `bigquery`, `snowflake`, `duckdb`, `tsql`); uses `DATA.md` or auto-detection when empty |
| `mode` | `""` | Mode override (`explore`, `build`, `change`, or `assure`) |
| `allow-unverified` | `false` | Set to `true` to exit `0` when some files could not be checked (turns exit code `3` into `0`) |
| `sarif-file` | `""` | Optional path to write a SARIF 2.1.0 report for GitHub Code Scanning |
| `summary` | `true` | Write the findings and receipt to `$GITHUB_STEP_SUMMARY` |

### Uploading SARIF to GitHub Code Scanning

```yaml
      - uses: Analytically-ai/truequery@v0.1.6
        id: truequery
        continue-on-error: true
        with:
          path: models/
          sarif-file: truequery.sarif

      - uses: github/codeql-action/upload-sarif@v3
        if: always()
        with:
          sarif_file: truequery.sarif
```
