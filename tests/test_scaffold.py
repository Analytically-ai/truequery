"""`truequery scaffold` (0.2 part E): a pipeline's boundaries from contracts.

The generated code is plain pandas (it never imports TrueQuery), the same
contract gives the same bytes, files are never written over without
--force, the code passes `truequery review` with every check on, and its
own tests pass on the pinned pandas."""
import ast
import os
import re
import subprocess
import sys

import pytest
import yaml

from truequery import cli
from truequery import schema
from truequery import scaffold as SC

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
import r2_projects as R2  # noqa: E402
import test_contract_checks as TCC  # noqa: E402
import test_pandas_contracts as TPC  # noqa: E402

DATA_MD = ("# Data\n\n```tq-config\nbases:\n  data_root: ./data\n"
           "  remote: {env: DATA_ROOT, default: /srv/data}\n```\n")

EXTRA = {
    "jsonl_events": """version: 2
dataset: jsonl events
kind: files
location: {path: logs/*.jsonl, format: jsonl}
grain: one row per event
keys: [[source, seq]]
columns:
  source: {type: string, values: [web, app]}
  seq: {type: integer}
  ok: {type: boolean}
  seen_at: {type: timestamp, time_zone: "local:Europe/Prague"}
  note: {type: string}
""",
    "json_doc": """version: 2
dataset: doc
kind: files
location: {path: doc.json, format: json}
grain: one row per id
keys: [id]
columns:
  id: {type: string}
  at: {type: timestamp, time_zone: naive}
""",
    "partitioned": """version: 2
dataset: class
kind: files
location: {path: "events/dt=*/*.csv", format: csv, delimiter: ";"}
grain: one row per id per day
keys: [[id, dt]]
partition_key: dt
columns:
  id: {type: string, nullable: false}
  dt: {type: date}
  amount: {type: number}
  flag: {type: boolean}
""",
    "boolean_key": """version: 2
dataset: flags
grain: one row per flag
keys: [flag]
columns:
  flag: {type: boolean}
  code: {type: integer, values: [1, 2]}
""",
    "untyped": """version: 2
dataset: loose
kind: files
location: {path: loose.tsv, format: csv, delimiter: "\\t"}
grain: one row per k
keys: [k]
columns:
  k: {}
  v: {type: string, values: {a: {means: present}, b: null}, nullable: true}
""",
}


def every_contract() -> dict[str, str]:
    """Each v2 contract the test suite uses, plus a few for the formats
    and shapes they don't cover."""
    found: dict[str, str] = {}
    for mod, label in ((TPC, "pandas"), (TCC, "sql")):
        for name, value in sorted(vars(mod).items()):
            if (name.isupper() and isinstance(value, str)
                    and value.startswith("version: 2\n")):
                found[f"{label}_{name.lower()}"] = value
    found["r2_fetch_log"] = R2.FETCH_CONTRACT + (
        "  status:\n    type: string\n    values:\n"
        "      fetched: {means: present}\n"
        "      failed: {means: absent_unexpected}\n"
        "  local_path: {type: path, relative_to: data_root, "
        "status_column: status}\n"
        "  bytes_saved: {type: number, unit: KB, size_of: local_path}\n")
    found.update(EXTRA)
    return found


def _unique(label: str, text: str) -> str:
    """One dataset name per contract, so their modules don't collide."""
    return re.sub(r"^dataset: .*$", f"dataset: {label}", text, count=1,
                  flags=re.MULTILINE)


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("scaffold")
    (root / "DATA.md").write_text(DATA_MD, encoding="utf-8")
    cdir = root / "contracts"
    cdir.mkdir()
    for label, text in every_contract().items():
        keep_name = label in EXTRA
        (cdir / f"{label}.yaml").write_text(
            text if keep_name else _unique(label, text), encoding="utf-8")
    old = os.getcwd()
    os.chdir(root)
    try:
        assert cli.main(["contract", "validate"]) == 0
        paths = sorted(str(p.relative_to(root)) for p in cdir.glob("*.yaml"))
        assert cli.main(["scaffold", *paths]) == 0
    finally:
        os.chdir(old)
    return root


def test_every_contract_in_the_tests_scaffolds(project):
    modules = sorted((project / "pipeline").glob("*_io.py"))
    tests = [p for p in modules if p.name.startswith("test_")]
    assert len(modules) - len(tests) == len(every_contract())
    for path in modules:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)  # it compiles
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            assert not any(n.split(".")[0] == "truequery" for n in names), \
                path.name


def _all_rules() -> list[str]:
    return [f"--enable-rule={slug}" for slug, rec in
            sorted(schema.load_all().items()) if rec.implemented]


def test_the_generated_code_passes_review_with_every_check_on(project, capsys,
                                                              monkeypatch):
    monkeypatch.chdir(project)
    code = cli.main(["review", "pipeline", *_all_rules()])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "no findings" in out
    with_pandas = sum(1 for p in (project / "pipeline").glob("*.py")
                      if "import pandas" in p.read_text(encoding="utf-8"))
    assert with_pandas >= len(every_contract())
    assert f"pandas in {with_pandas} Python files" in out


def test_the_generated_tests_pass_on_the_pinned_pandas(project):
    pandas = pytest.importorskip("pandas")
    if pandas.__version__ != "3.1.0":
        pytest.skip(f"pandas {pandas.__version__}, not the pinned 3.1.0")
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "pipeline"], cwd=project, capture_output=True, text=True,
        timeout=300)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-2000:]
    passed = re.search(r"(\d+) passed", proc.stdout)
    # At least the read-back and missing-column tests for each contract
    # (a Parquet one is skipped without pyarrow).
    assert passed and int(passed.group(1)) >= 2 * len(every_contract())
    assert " failed" not in proc.stdout and " error" not in proc.stdout


def test_same_contract_same_bytes(project, tmp_path, monkeypatch):
    monkeypatch.chdir(project)
    paths = sorted(str(p.relative_to(project))
                   for p in (project / "contracts").glob("*.yaml"))
    assert cli.main(["scaffold", *paths, "--out", str(tmp_path / "again")]) \
        == 0
    for path in sorted((project / "pipeline").glob("*.py")):
        again = tmp_path / "again" / path.name
        assert again.read_bytes() == path.read_bytes(), path.name
        assert b"\r" not in again.read_bytes()


def test_a_draft_says_so(tmp_path, monkeypatch, capsys):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.csv").write_text("id,ts\n007,2026-10-01 08:00:00\n"
                                          "008,2026-10-01 09:00:00\n",
                                          encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--from", "d"]) == 0
    capsys.readouterr()
    assert cli.main(["scaffold", "x"]) == 0
    out = capsys.readouterr().out
    assert "(from a draft contract: confirm it, then regenerate)" in out
    text = (tmp_path / "pipeline" / "x_io.py").read_text(encoding="utf-8")
    assert "# DRAFT: contracts/x.yaml is a draft contract" in text
    assert "# Columns still to confirm: ts." in text


def test_a_v1_contract_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "legacy.yaml").write_text(TPC.V1,
                                                        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scaffold", "contracts/legacy.yaml"]) == 2
    err = capsys.readouterr().err
    assert "scaffold needs a version 2 contract (this one is version 1" in err
    assert not (tmp_path / "pipeline").exists()


def test_a_contract_without_columns_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "c.yaml").write_text(
        "version: 2\ndataset: c\ngrain: one row\nkeys: [id]\n",
        encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scaffold", "c.yaml"]) == 2
    assert "lists no columns" in capsys.readouterr().err


def test_an_invalid_contract_is_refused(tmp_path, monkeypatch, capsys):
    (tmp_path / "c.yaml").write_text("version: 2\ndataset: c\n",
                                     encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scaffold", "c.yaml", "missing.yaml"]) == 2
    err = capsys.readouterr().err
    assert "grain" in err and "missing.yaml: no such contract file" in err


def test_files_are_not_written_over_without_force(tmp_path, monkeypatch,
                                                  capsys):
    (tmp_path / "c.yaml").write_text(EXTRA["json_doc"], encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scaffold", "c.yaml"]) == 0
    mine = tmp_path / "pipeline" / "doc_io.py"
    mine.write_text("# my edits\n", encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["scaffold", "c.yaml"]) == 2
    assert "already there, not written over: pipeline/doc_io.py" in \
        capsys.readouterr().err.replace(os.sep, "/")
    assert mine.read_text(encoding="utf-8") == "# my edits\n"
    assert cli.main(["scaffold", "c.yaml", "--force"]) == 0
    assert mine.read_text(encoding="utf-8") != "# my edits\n"


def test_two_contracts_for_one_module_are_refused(tmp_path, monkeypatch,
                                                  capsys):
    for name in ("a", "b"):
        (tmp_path / f"{name}.yaml").write_text(
            EXTRA["json_doc"].replace("dataset: doc", "dataset: Doc-1"
                                      if name == "a" else "dataset: doc 1"),
            encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["scaffold", "a.yaml", "b.yaml"]) == 2
    assert "would both write doc_1_io.py" in capsys.readouterr().err


@pytest.mark.parametrize("name,expected", [
    ("orders", "orders"), ("Order Lines", "order_lines"),
    ("2024-sales", "d_2024_sales"), ("class", "class_"),
    ("schema.table", "schema_table"), ("údaje", "udaje"), ("$$", "dataset"),
])
def test_identifiers(name, expected):
    assert SC.ident(name) == expected


def test_the_reader_keeps_leading_zeros_and_refuses_naive_utc(tmp_path):
    pd = pytest.importorskip("pandas")
    text = TPC.FETCH_LOG
    sp = SC.spec_of(yaml.safe_load(text), "contracts/fetch_log.yaml")
    namespace: dict = {}
    exec(compile(SC.render_module(sp), "fetch_log_io.py", "exec"), namespace)
    path = tmp_path / "f.csv"
    path.write_text("run_id,url,fetched_at,status,local_path\n"
                    "007,u,2026-10-01T08:00:00Z,fetched,a/b.bin\n"
                    "008,NA,2026-10-01T09:00:00+00:00,released,\n",
                    encoding="utf-8")
    df = namespace["read_fetch_log"](path)
    assert list(df["run_id"]) == ["007", "008"]
    assert list(df["url"]) == ["u", "NA"]  # NA stays text
    assert str(df["fetched_at"].dt.tz) == "UTC"
    assert pd.isna(df["local_path"].iloc[1])
    namespace["check_fetch_log"](df)
    path.write_text("run_id,url,fetched_at,status,local_path\n"
                    "007,u,2026-10-01 08:00:00,fetched,a/b.bin\n",
                    encoding="utf-8")
    with pytest.raises(ValueError, match="no UTC marker"):
        namespace["read_fetch_log"](path)


def test_the_check_names_ids_turned_into_floats():
    pd = pytest.importorskip("pandas")
    contract = yaml.safe_load(TCC.FETCH_LOG)
    sp = SC.spec_of(contract, "contracts/fetch_log.yaml")
    namespace: dict = {}
    exec(compile(SC.render_module(sp), "m.py", "exec"), namespace)
    frame = pd.DataFrame({"id": [1.0, None], "path": ["a", "b"],
                          "status": ["fetched", "lost"]})
    with pytest.raises(ValueError) as err:
        namespace["check_fetch_log"](frame)
    message = str(err.value)
    assert "id: dtype float64, the contract says integer" in message
    assert "id: 1 empty value(s)" in message
    assert "status: 1 value(s) not declared in the contract (lost)" in message


def test_a_partition_column_comes_from_the_folder_name(tmp_path):
    pytest.importorskip("pandas")
    sp = SC.spec_of(yaml.safe_load(EXTRA["partitioned"]),
                    "contracts/partitioned.yaml")
    namespace: dict = {}
    exec(compile(SC.render_module(sp), "class__io.py", "exec"), namespace)
    path = tmp_path / "events" / "dt=2026-01-02" / "part.csv"
    path.parent.mkdir(parents=True)
    path.write_text("id;amount;flag\n007;1.5;true\n", encoding="utf-8")
    df = namespace["read_class_"](path)
    assert str(df["dt"].iloc[0].date()) == "2026-01-02"
    assert df["id"].iloc[0] == "007"
    namespace["check_class_"](df)
