"""Run SQL from argv[1] against warehouse.duckdb in cwd (read-only)."""
import os
import sys

import duckdb

db = os.path.join(os.getcwd(), "warehouse.duckdb")
con = duckdb.connect(db, read_only=True)
# Pin UTC so query results never depend on this machine's time zone.
con.execute("SET TimeZone='UTC'")
try:
    rows = con.execute(sys.argv[1]).fetchall()
except Exception as exc:  # noqa: BLE001
    print(f"ERROR: {exc}")
    sys.exit(1)
for row in rows:
    print(" | ".join("" if v is None else str(v) for v in row))
