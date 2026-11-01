"""Latency DoD: p50 under 1 second per file (warm, in-process).

This test guards against pathological regressions with headroom. 300 statements must check far below the bar.
"""

import statistics
import time

from truequery.engine import check_file, check_python_source

BIG = "\n".join(
    f"SELECT a{i}, b{i} FROM t{i} JOIN u{i} ON t{i}.id = u{i}.id "
    f"WHERE x{i} > {i} ORDER BY a{i} LIMIT 10;"
    for i in range(300)
)


def test_p50_under_one_second(tmp_path):
    target = tmp_path / "big.sql"
    target.write_text(BIG, encoding="utf-8")
    samples = []
    for _ in range(5):
        started = time.monotonic()
        findings, diagnostics, _lookups = check_file(str(target))
        samples.append(time.monotonic() - started)
    assert diagnostics == []
    assert findings == []
    assert statistics.median(samples) < 1.0


def _big_python():
    """~2,000 lines of Python with 50 SQL fragments of every kind."""
    lines = ["import sqlite3", ""]
    for i in range(50):
        kind = i % 4
        lines += [f"def step_{i}(con, rows):"]
        if kind == 0:
            lines += ['    return con.execute("""',
                      f"        SELECT a{i}, b{i}", f"        FROM t{i}",
                      f"        JOIN u{i} ON t{i}.id = u{i}.id",
                      f"        WHERE x{i} > ?", '    """, (1,))']
        elif kind == 1:
            lines += [f'    return con.execute(f"SELECT a{i} FROM {{table}} '
                      f'WHERE id = %s", (1,))']
        elif kind == 2:
            lines += [f'    q = "SELECT a{i} " + \\',
                      f'        "FROM t{i} WHERE b = :b"',
                      "    return con.execute(q, {'b': 1})"]
        else:
            lines += ["    return con.execute(rows)"]
        # filler: ordinary code with no SQL
        for j in range(35):
            lines.append(f"    total_{j} = sum(r[{j % 5}] for r in rows)")
        lines.append("")
    return "\n".join(lines) + "\n"


def test_large_python_file_p50_under_one_second():
    text = _big_python()
    assert 1900 <= text.count("\n") <= 2100
    samples = []
    for _ in range(5):
        started = time.monotonic()
        findings, fragments, _spans = check_python_source(text, "big.py")
        samples.append(time.monotonic() - started)
    assert len(fragments) == 50
    assert sum(1 for f in fragments if f.status == "dynamic") == 12
    assert findings == []
    assert statistics.median(samples) < 1.0
