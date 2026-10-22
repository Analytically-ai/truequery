#!/bin/sh
# Build examples/shop/warehouse.duckdb (seed 42, deterministic).
# Needs Python with duckdb: uses `uv` when present, else python3.
set -e
cd "$(dirname "$0")"
. ./_python.sh
$PY gen_db.py
$PY build_marts.py
echo "wrote warehouse.duckdb"
