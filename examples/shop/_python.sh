# Sourced by generate.sh and dq: pick a Python that has duckdb.
if command -v uv >/dev/null 2>&1; then
  PY="uv run --quiet --no-project --with duckdb --with pytz python"
elif python3 -c "import duckdb" 2>/dev/null; then
  PY="python3"
else
  echo "The demo needs Python with duckdb: install uv, or run: pip install duckdb pytz" >&2
  exit 1
fi
