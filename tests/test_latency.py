"""Latency DoD: p50 under 1 second per file (warm, in-process).

This test guards against pathological regressions with headroom. 300 statements must check far below the bar.
"""

import statistics
import time

from truequery.engine import check_file

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
