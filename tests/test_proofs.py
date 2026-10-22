"""All 22 research proofs run green in DuckDB (folded in from run_proofs.py).

Each proof asserts the bad query returns the recorded wrong answer and
the fixed query returns the recorded right answer. setup_alt variants
run where present.
"""
import ast

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
    return {slug: r.proof for slug, r in records.items() if r.proof is not None}


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
