"""Preview non-SQL checks: dbt YAML/models + Airflow catchup."""
from truequery.analyzers import project_checks as pc


def rules(findings):
    return [f["rule"] for f in findings]


def test_incremental_without_unique_key_fires():
    sql = ("{{ config(materialized='incremental', "
           "incremental_strategy='merge') }}\nSELECT a FROM t")
    assert rules(pc.check_dbt_model(sql, "m.sql")) == [
        "model/unique-key-merge-key"]


def test_microbatch_without_unique_key_clean():
    sql = ("{{ config(materialized='incremental', "
           "incremental_strategy='microbatch', event_time='ts') }}\n"
           "SELECT a FROM t")
    assert pc.check_dbt_model(sql, "m.sql") == []


def test_incremental_with_unique_key_clean():
    sql = ("{{ config(materialized='incremental', unique_key='id') }}\n"
           "SELECT a FROM t")
    assert pc.check_dbt_model(sql, "m.sql") == []


def test_full_refresh_clean():
    sql = "{{ config(materialized='table') }}\nSELECT a FROM t"
    assert pc.check_dbt_model(sql, "m.sql") == []


YML_KEY_MISSING = """
models:
  - name: orders
    columns:
      - name: order_id
        tests: [unique]
sources:
  - name: raw
    loaded_at_field: _loaded_at
    tables:
      - name: payments
"""

YML_GOOD = """
models:
  - name: orders
    columns:
      - name: order_id
        tests: [unique, not_null]
sources:
  - name: raw
    loaded_at_field: updated_at
    freshness: {error_after: {count: 12, period: hour}}
    tables:
      - name: payments
"""


def test_schema_missing_tests_and_freshness_fire():
    got = rules(pc.check_schema_yaml(YML_KEY_MISSING, "schema.yml"))
    assert got == ["dq/uniqueness-key-no-duplicates",
                   "ingest/declare-source-freshness"]


def test_schema_good_is_clean():
    assert pc.check_schema_yaml(YML_GOOD, "schema.yml") == []


def test_schema_no_declared_key_stays_silent():
    yml = "models:\n  - name: notes\n    columns:\n      - name: body\n"
    assert pc.check_schema_yaml(yml, "schema.yml") == []


def test_airflow_missing_catchup_fires():
    py = ("from airflow import DAG\n"
          "dag = DAG('x', start_date=None)\n")
    assert rules(pc.check_python(py, "dag.py")) == [
        "orch/explicit-catchup-policy"]


def test_airflow_explicit_catchup_clean():
    py = ("from airflow import DAG\n"
          "dag = DAG('x', catchup=False)\n")
    assert pc.check_python(py, "dag.py") == []


def test_python_syntax_error_silent():
    assert pc.check_python("def broken(:\n", "x.py") == []


def test_unique_key_in_model_yaml_counts(tmp_path):
    """dbt lets the config block live in the model's properties YAML."""
    from truequery import std_registry
    from truequery.analyzers.project_checks import check_dbt_model
    sql = tmp_path / "fct_events.sql"
    sql.write_text("{{ config(materialized='incremental') }}\nselect 1 as id\n",
                   encoding="utf-8")
    assert check_dbt_model(sql.read_text(), str(sql), registry=std_registry)
    (tmp_path / "fct_events.yml").write_text(
        "models:\n  - name: fct_events\n    config:\n      unique_key: id\n",
        encoding="utf-8")
    assert check_dbt_model(sql.read_text(), str(sql),
                           registry=std_registry) == []
