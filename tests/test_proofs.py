"""Every research proof runs green: SQL proofs in DuckDB (folded in from
run_proofs.py), pandas proofs in pandas.

Each proof asserts the bad code returns the recorded wrong answer and
the fixed code returns the recorded right answer. setup_alt variants
run where present.

pandas proofs were checked against pandas 3.1.0 and numpy 2.4.6, the
versions pinned in the engine's `test` extra.
"""
import ast
import math
import warnings

import duckdb
import pytest

from truequery import schema


def norm(value):
    if isinstance(value, str):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value
    return value


def run_case(record_id, proof, setup, expect_bad, expect_good):
    # fresh connection per query: setups use CREATE TABLE (no OR REPLACE).
    # a proof may record an error as the bad outcome ("ERROR: <Class>").
    bad_con = duckdb.connect()
    try:
        bad_con.execute(setup)
        try:
            bad_rows = bad_con.execute(proof.bad).fetchall()
        except Exception as exc:
            bad_rows = f"ERROR: {type(exc).__name__}"
    finally:
        bad_con.close()
    assert norm(bad_rows) == norm(expect_bad), (
        f"{record_id}: bad query returned {bad_rows!r}, "
        f"proof says {expect_bad!r}")
    good_con = duckdb.connect()
    try:
        good_con.execute(setup)
        good_rows = good_con.execute(proof.good).fetchall()
    finally:
        good_con.close()
    assert norm(good_rows) == norm(expect_good), (
        f"{record_id}: good query returned {good_rows!r}, "
        f"proof says {expect_good!r}")


def all_proofs():
    records = schema.load_all()
    return {slug: r.proof for slug, r in records.items()
            if r.proof is not None and r.proof.engine == "duckdb"}


def pandas_proofs():
    records = schema.load_all()
    return {slug: r.proof for slug, r in records.items()
            if r.proof is not None and r.proof.engine == "pandas"}


def _plain(value):
    """A pandas/numpy result as plain Python, the shape DuckDB proofs
    record: a DataFrame is a list of row tuples, a Series a list, NaN
    and None are None."""
    import pandas as pd
    if isinstance(value, pd.DataFrame):
        return [tuple(_plain(v) for v in row)
                for row in value.itertuples(index=False, name=None)]
    if isinstance(value, (pd.Series, pd.Index)):
        return [_plain(v) for v in value.tolist()]
    if isinstance(value, (list, tuple)):
        return type(value)(_plain(v) for v in value)
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        value = value.item()  # numpy scalar
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def run_pandas(setup, code):
    """Run setup then code in a fresh namespace; return `result`."""
    pytest.importorskip("pandas")
    import pandas as pd
    space = {"pd": pd}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # e.g. ChainedAssignmentError
        exec(setup, space)  # noqa: S102 (our own proof text)
        exec(code, space)  # noqa: S102
    return _plain(space["result"])


@pytest.mark.parametrize("slug", sorted(pandas_proofs()))
def test_pandas_proof_bad_and_good(slug):
    proof = pandas_proofs()[slug]
    bad = run_pandas(proof.setup, proof.bad)
    assert bad == norm(proof.bad_result), (
        f"{slug}: bad code returned {bad!r}, proof says {proof.bad_result!r}")
    good = run_pandas(proof.setup, proof.good)
    assert good == norm(proof.good_result), (
        f"{slug}: good code returned {good!r}, "
        f"proof says {proof.good_result!r}")


def test_pandas_proofs_cover_every_pandas_rule():
    from truequery.analyzers import pandas_checks
    assert set(pandas_proofs()) == set(pandas_checks.RULES)


@pytest.mark.parametrize("slug", sorted(all_proofs()))
def test_proof_bad_and_good(slug):
    proof = all_proofs()[slug]
    run_case(slug, proof, proof.setup, proof.bad_result, proof.good_result)


@pytest.mark.parametrize("slug", sorted(
    s for s, p in all_proofs().items() if p.setup_alt))
def test_proof_alt_setup(slug):
    proof = all_proofs()[slug]
    run_case(slug + "[alt]", proof, proof.setup_alt,
             proof.bad_result_alt or proof.bad_result, proof.good_result)
