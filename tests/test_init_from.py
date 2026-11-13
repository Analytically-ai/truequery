"""`truequery init --from` (0.2 part E): draft v2 contracts from data files.

One code path (stdlib for CSV and JSONL), deterministic, never over an
existing contract, every draft valid, no values from personal-data
columns, and nothing read from outside the project or a declared base."""
import json
import os
import shutil
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from truequery import cli
from truequery import contract_v2 as C2
from truequery import initfrom as IF
from truequery import project as P
from truequery import verify as V

sys.path.insert(0, os.path.dirname(__file__))
import r2_projects as R2  # noqa: E402

SCHEMA = Path(C2.__file__).parent / "schemas" / "contract-v2.schema.json"


def run(root, *args, capsys=None):
    code = cli.main(["init", str(root), "--from", *[str(a) for a in args]])
    out = capsys.readouterr() if capsys else None
    return code, out


def write(path: Path, text, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        with open(path, mode, encoding="utf-8", newline="") as handle:
            handle.write(text)
    return path


def drafts(root: Path) -> dict:
    out = {}
    for path in sorted((root / "contracts").glob("*.yaml")):
        out[path.stem] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return out


def col(contract, name):
    return contract["columns"][name]


@pytest.fixture
def chdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# -- Reviewer 2's fixtures ---------------------------------------------------

def _r2(tmp_path, case, broken):
    root = R2.build(tmp_path / f"{case}-{broken}", case, broken)
    shutil.rmtree(root / "contracts")
    return root


@pytest.mark.parametrize("case", R2.CASES)
@pytest.mark.parametrize("broken", [True, False])
def test_reviewer2_fixtures_give_valid_drafts(tmp_path, monkeypatch, capsys,
                                              case, broken):
    root = _r2(tmp_path, case, broken)
    monkeypatch.chdir(root)
    code, _ = run(root, "logs", capsys=capsys)
    assert code == 0
    found = drafts(root)
    assert len(found) == 1
    contract = next(iter(found.values()))
    assert contract["status"] == "draft"
    assert contract["inferred_from"].startswith("truequery init --from:")
    assert C2.is_unconfirmed_grain(contract["grain"])
    assert cli.main(["contract", "validate"]) == 0


def test_reviewer2_wrong_base_is_named(tmp_path, monkeypatch, capsys):
    root = _r2(tmp_path, "base", True)
    monkeypatch.chdir(root)
    code, out = run(root, "logs", capsys=capsys)
    assert "the base folder's own name" in out.out
    contract = drafts(root)["fetch"]
    assert "relative_to" not in col(contract, "local_path")
    assert "Every path starts with data/, the base folder's own name" in \
        out.out


def test_reviewer2_right_base_is_written_as_a_draft(tmp_path, monkeypatch,
                                                    capsys):
    root = _r2(tmp_path, "base", False)
    monkeypatch.chdir(root)
    run(root, "logs", capsys=capsys)
    spec = col(drafts(root)["fetch"], "local_path")
    assert spec["type"] == "path"
    assert spec["relative_to"] == "data_root"
    assert spec["status"] == "draft"


def test_reviewer2_sizes_written_with_decimals_ask_for_the_unit(tmp_path):
    root = _r2(tmp_path, "sizes", True)
    plan = IF.plan(str(root), [str(root / "logs")])
    spec = plan["drafts"][0]["contract"]["columns"]["bytes_logged"]
    assert "unit" not in spec and spec["status"] == "draft"
    assert any("bytes_logged: the name says bytes, but values are written "
               "with a decimal point" in q
               for q in plan["drafts"][0]["questions"])


def test_reviewer2_mixed_zones_give_no_zone_and_counts(tmp_path):
    root = _r2(tmp_path, "tz", True)
    plan = IF.plan(str(root), [str(root / "logs")])
    contract = plan["drafts"][0]["contract"]
    assert "time_zone" not in col(contract, "fetched_at")
    assert "2 UTC (Z or +00:00), 1 with no zone, 1 with another offset" in \
        contract["quirks"]
    fixed = _r2(tmp_path, "tz", False)
    plan = IF.plan(str(fixed), [str(fixed / "logs")])
    assert col(plan["drafts"][0]["contract"], "fetched_at")["time_zone"] == \
        "UTC"


def test_reviewer2_statuses_get_means_todo_and_link_the_path(tmp_path):
    root = _r2(tmp_path, "lifecycle", True)
    plan = IF.plan(str(root), [str(root / "logs")])
    contract = plan["drafts"][0]["contract"]
    status = col(contract, "status")
    assert set(status["values"]) == {"fetched", "removed_on_purpose",
                                     "released", "refetchable", "failed"}
    assert all(v == {"means": "TODO"} for v in status["values"].values())
    assert col(contract, "local_path")["status_column"] == "status"


@pytest.mark.skipif(not V.duckdb_status()["installed"],
                    reason="the data reader isn't installed")
def test_answering_the_interview_lets_verify_catch_reviewer2s_errors(
        tmp_path, monkeypatch, capsys):
    """The draft, with the answers the interview would record, catches the
    missing files Reviewer 2 described, and the draft alone doesn't
    flag the files that are absent on purpose."""
    root = _r2(tmp_path, "lifecycle", True)
    monkeypatch.chdir(root)
    run(root, "logs", capsys=capsys)
    assert cli.main(["verify"]) == 3  # meanings TODO: not judged, no failure
    out = capsys.readouterr().out
    assert "not judged: what the status means is still TODO" in out
    path = root / "contracts" / "fetch.yaml"
    text = path.read_text(encoding="utf-8")
    for value, means in (("fetched", "present"),
                         ("removed_on_purpose", "absent_expected"),
                         ("released", "absent_expected"),
                         ("refetchable", "absent_expected"),
                         ("failed", "absent_unexpected")):
        text = text.replace(f"{value}: {{means: TODO}}",
                            f"{value}: {{means: {means}}}")
    path.write_text(text, encoding="utf-8")
    assert cli.main(["verify"]) == 1
    out = capsys.readouterr().out
    assert "1 file missing where the status says present (" in out
    # `failed` means a failure: no file is what the status says, so it's
    # counted, not failed
    assert ("1 row whose status means a failure (failed): no file, as the "
            "status says") in out


# -- the inputs the prompt lists ---------------------------------------------

def test_bom_is_not_part_of_the_first_column(tmp_path):
    write(tmp_path / "d" / "x.csv", b"\xef\xbb\xbfid,v\n1,a\n2,b\n")
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert list(contract["columns"]) == ["id", "v"]


@pytest.mark.skipif(not V.duckdb_status()["installed"],
                    reason="the data reader isn't installed")
def test_semicolon_delimiter_is_recorded_and_verify_uses_it(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    write(tmp_path / "d" / "x.csv",
          "id;amount;city\n001;1,5;Praha\n002;2,5;Brno\n003;3;Praha\n")
    monkeypatch.chdir(tmp_path)
    run(tmp_path, "d", capsys=capsys)
    contract = drafts(tmp_path)["x"]
    assert contract["location"]["delimiter"] == ";"
    assert col(contract, "id")["type"] == "string"  # leading zeros
    assert col(contract, "amount")["type"] == "string"  # decimal comma
    assert cli.main(["verify"]) == 0
    assert "key id unique" in capsys.readouterr().out


def test_tsv_is_tab_delimited(tmp_path):
    write(tmp_path / "d" / "x.tsv", "a\tb\n1\t2\n3\t4\n")
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert contract["location"]["delimiter"] == "\t"
    assert list(contract["columns"]) == ["a", "b"]


def test_quoted_newlines_stay_inside_one_row(tmp_path):
    write(tmp_path / "d" / "x.csv",
          'id,note\n1,"line one\nline two"\n2,plain\n')
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert entry["rows"] == 2
    assert col(entry["contract"], "id")["type"] == "integer"


def test_empty_file_is_not_inferred(tmp_path):
    write(tmp_path / "d" / "x.csv", "")
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    assert result["drafts"] == []
    assert result["not_inferred"] == [{"path": "d/x.csv",
                                       "reason": "it's empty"}]


def test_header_only_file_gives_columns_without_types(tmp_path):
    write(tmp_path / "d" / "x.csv", "a,b,c\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert entry["contract"]["columns"] == {"a": {}, "b": {}, "c": {}}
    assert entry["contract"]["keys"] == []
    assert "too few rows" in entry["contract"]["grain"]


def test_ragged_rows_are_counted(tmp_path):
    write(tmp_path / "d" / "x.csv", "a,b\n1,2\n3\n4,5,6\n7,8\n")
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert "2 of 4 sampled rows have a different number of fields" in \
        contract["quirks"]


@pytest.mark.skipif(os.name == "nt", reason="sparse files")
def test_a_2_gb_file_reads_only_the_sample(tmp_path):
    """Time and memory stay bounded: only the sample is read."""
    path = write(tmp_path / "d" / "big.csv", "id,v\n" + "".join(
        f"{i},x{i}\n" for i in range(3000)))
    with open(path, "r+b") as handle:
        os.truncate(handle.fileno(), 2 * 1024 ** 3)  # sparse past the rows
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert entry["rows"] == IF.SAMPLE_ROWS
    assert "the first 2,000 rows" in entry["contract"]["grain"]
    nul = tmp_path / "e" / "nul.csv"
    nul.parent.mkdir()
    with open(nul, "wb") as handle:
        os.truncate(handle.fileno(), 2 * 1024 ** 3)  # no line break at all
    result = IF.plan(str(tmp_path), [str(tmp_path / "e")])
    assert result["not_inferred"][0]["reason"].startswith(
        "its first lines are longer than 1 MB")


def test_jsonl_with_mixed_keys(tmp_path):
    write(tmp_path / "d" / "x.jsonl",
          '{"id": "007", "v": 1}\n{"id": "008", "w": true, "v": 2.5}\n'
          '{"id": "009", "nested": {"a": 1}}\nnot json\n[1, 2]\n')
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert list(contract["columns"]) == ["id", "v", "w", "nested"]
    assert col(contract, "id")["type"] == "string"
    assert col(contract, "v")["type"] == "number"
    assert col(contract, "w") == {"type": "boolean", "nullable": True,
                                  "status": "draft"}
    assert contract["columns"]["nested"] == {"nullable": True,
                                             "status": "draft"}
    assert col(contract, "id")["nullable"] is False  # the key
    for note in ("1 line in the sample is not valid JSON",
                 "1 line in the sample is not a JSON object",
                 "w is in 1 of 3 sampled rows"):
        assert note in contract["quirks"]


def test_jsonl_with_no_objects_is_not_inferred(tmp_path):
    write(tmp_path / "d" / "x.jsonl", "[1]\n2\n")
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    assert result["drafts"] == []
    assert "no JSON object read" in result["not_inferred"][0]["reason"]


def test_parquet_without_the_data_reader_is_listed(tmp_path, monkeypatch):
    write(tmp_path / "d" / "x.parquet", b"PAR1 not really")
    monkeypatch.setattr(IF, "_duckdb_available", lambda: False)
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    assert result["drafts"] == []
    assert result["not_inferred"] == [{
        "path": "d/x.parquet",
        "reason": "Parquet needs the data reader. Run: truequery setup data"}]


@pytest.mark.skipif(not V.duckdb_status()["installed"],
                    reason="the data reader isn't installed")
def test_parquet_with_the_data_reader(tmp_path):
    import duckdb
    folder = tmp_path / "d" / "region=eu"
    folder.mkdir(parents=True)
    con = duckdb.connect()
    target = str(folder / "p.parquet").replace("'", "''")
    con.execute(
        "COPY (SELECT i AS shard_id, (i * 1.5)::DECIMAL(10,2) AS size_gib, "
        "TIMESTAMPTZ '2026-10-01 08:00:00+00' AS created_at, "
        "TIMESTAMP '2026-10-01 08:00:00' AS seen_at, "
        "'2026-10-01T08:00:00Z' AS txt_ts FROM range(5) t(i)) "
        f"TO '{target}' (FORMAT parquet)")
    con.close()
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    contract = entry["contract"]
    assert contract["location"]["path"] == "d/region=*/p.parquet"
    assert contract["partition_key"] == "region"
    assert col(contract, "shard_id") == {"type": "integer", "nullable": False}
    assert col(contract, "created_at")["time_zone"] == "UTC"
    assert col(contract, "seen_at")["time_zone"] == "naive"
    assert col(contract, "txt_ts") == {"type": "timestamp",
                                       "time_zone": "UTC"}
    assert col(contract, "size_gib")["unit"] == "GiB"
    assert col(contract, "region")["type"] == "string"
    assert entry["rows"] == 5


def test_mixed_z_and_naive_timestamps(tmp_path):
    write(tmp_path / "d" / "x.csv",
          "id,ts\n1,2026-10-01T08:00:00Z\n2,2026-10-01 09:00:00\n"
          "3,2026-10-01T09:00:00+00:00\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    spec = col(entry["contract"], "ts")
    assert spec == {"type": "timestamp", "status": "draft"}
    assert "ts mixes time zones: 2 UTC (Z or +00:00), 1 with no zone" in \
        entry["contract"]["quirks"]


def test_naive_timestamps_become_a_question(tmp_path):
    write(tmp_path / "d" / "x.csv", "id,ts\n1,2026-10-01 08:00:00\n"
          "2,2026-10-01T09:00\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert col(entry["contract"], "ts") == {"type": "timestamp",
                                            "time_zone": "naive",
                                            "status": "draft"}
    assert any(q.startswith("ts: no value has a time zone")
               for q in entry["questions"])


def test_ids_with_leading_zeros_stay_text(tmp_path):
    write(tmp_path / "d" / "x.csv",
          "id,n,big,f\n007,1,1234567890123456789012,0.5\n"
          "008,2,1,1.0\n")
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert col(contract, "id")["type"] == "string"
    assert col(contract, "n")["type"] == "integer"
    assert col(contract, "big")["type"] == "string"
    assert col(contract, "f")["type"] == "number"
    assert contract["keys"] == ["id"]


@pytest.mark.parametrize("text,kind", [
    ("0", ("int", None)), ("-0", ("int", None)), ("+5", ("int", None)),
    ("007", ("text", "leading_zero")), ("0.5", ("num", None)),
    ("00.5", ("text", "leading_zero")), (".5", ("num", None)),
    ("1e5", ("num", None)), ("True", ("bool", None)),
    ("2026-02-30", ("text", None)), ("2026-10-01", ("date", None)),
    ("2026-10-01T08:00:00Z", ("ts", "utc")),
    ("2026-10-01 08:00:00+0000", ("ts", "utc")),
    ("2026-10-01T08:00:00-00:00", ("ts", "utc")),
    ("2026-10-01T08:00:00+02:00", ("ts", "offset")),
    ("2026-10-01T08:00:00.123456", ("ts", "naive")),
    ("2026-10-01T25:00:00", ("text", None)), ("NA", ("text", None)),
    ("1,234", ("text", None)),
])
def test_classify(text, kind):
    assert IF.classify(text) == kind


def test_numbers_with_na_tokens_ask_instead_of_guessing(tmp_path):
    write(tmp_path / "d" / "x.csv", "id,amount\n1,5\n2,NA\n3,7.5\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert col(entry["contract"], "amount") == {"status": "draft"}
    assert any("except 1 value 'NA'" in q for q in entry["questions"])


# -- folder layout -----------------------------------------------------------

def test_files_in_a_folder_become_one_glob(tmp_path):
    for day in ("2026-10-01", "2026-10-02", "2026-10-03"):
        write(tmp_path / "logs" / "fetch" / f"{day}.csv",
              f"run_id,url\nr-{day},https://x/{day}\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "logs")])["drafts"][0]
    assert entry["name"] == "fetch"
    assert entry["contract"]["location"]["path"] == "logs/fetch/*.csv"
    assert "(3 of 3 files)" in entry["contract"]["inferred_from"]


def test_one_date_named_file_is_the_first_of_a_series(tmp_path):
    write(tmp_path / "logs" / "fetch" / "2026-10-01.csv", "a\n1\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "logs")])["drafts"][0]
    assert entry["name"] == "fetch"
    assert entry["contract"]["location"]["path"] == "logs/fetch/*.csv"


def test_different_headers_in_one_folder_are_different_datasets(tmp_path):
    write(tmp_path / "d" / "orders_1.csv", "order_id,amount\n1,2\n")
    write(tmp_path / "d" / "orders_2.csv", "order_id,amount\n3,4\n")
    write(tmp_path / "d" / "customers.csv", "customer_id,region\n1,EU\n")
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    paths = sorted(e["contract"]["location"]["path"] for e in result["drafts"])
    assert paths == ["d/customers.csv", "d/orders_*.csv"]


def test_files_whose_columns_drift_are_one_dataset(tmp_path):
    for name, extra in (("a", ""), ("b", ""), ("c", ",boosters")):
        write(tmp_path / "d" / f"{name}.csv",
              f"location,date,total{extra}\nX,2026-10-0{len(name)},1"
              + (",2" if extra else "") + "\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    contract = entry["contract"]
    assert contract["location"]["path"] == "d/*.csv"
    assert list(contract["columns"]) == ["location", "date", "total",
                                         "boosters"]
    assert "the files' columns differ: boosters is in 1 of 3 files" in \
        contract["quirks"]


def test_partition_folders_become_a_glob_and_a_column(tmp_path):
    for day in ("2026-10-01", "2026-10-02"):
        write(tmp_path / "events" / f"dt={day}" / "part-0.csv",
              f"id,v\n{day[-1]},a\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "events")])["drafts"][0]
    contract = entry["contract"]
    assert entry["name"] == "events"
    assert contract["location"]["path"] == "events/dt=*/part-0.csv"
    assert contract["partition_key"] == "dt"
    assert col(contract, "dt") == {"type": "date"}


def test_names_never_collide(tmp_path):
    write(tmp_path / "a" / "logs" / "x.csv", "a\n1\n")
    write(tmp_path / "b" / "logs" / "x.csv", "b\n1\n")
    result = IF.plan(str(tmp_path), [str(tmp_path / "a"), str(tmp_path / "b")])
    names = [e["name"] for e in result["drafts"]]
    assert len(set(names)) == 2


# -- safety ------------------------------------------------------------------

def test_never_overwrites_an_existing_contract(tmp_path, monkeypatch, capsys):
    write(tmp_path / "logs" / "fetch" / "a.csv", "id\n1\n2\n")
    write(tmp_path / "logs" / "fetch" / "b.csv", "id\n3\n")
    existing = write(tmp_path / "contracts" / "fetch.yaml",
                     "table: fetch\ngrain: one row per id\nkeys: [id]\n")
    before = existing.read_bytes()
    monkeypatch.chdir(tmp_path)
    code, out = run(tmp_path, "logs", capsys=capsys)
    assert code == 0
    assert existing.read_bytes() == before
    assert "contracts/fetch.yaml or a contract for a dataset named fetch " \
        "already exists" in out.out


def test_a_location_already_described_is_kept(tmp_path):
    write(tmp_path / "logs" / "x.csv", "id\n1\n")
    write(tmp_path / "contracts" / "mine.yaml",
          "version: 2\ndataset: mine\nkind: files\n"
          "location: {path: logs/x.csv, format: csv}\n"
          "grain: one row per id\nkeys: [id]\n")
    result = IF.plan(str(tmp_path), [str(tmp_path / "logs")])
    assert result["drafts"] == []
    assert result["kept"][0]["reason"] == "already has a contract"


def test_write_never_replaces_a_file_that_appeared(tmp_path):
    write(tmp_path / "d" / "x.csv", "id\n1\n")
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    late = write(tmp_path / "contracts" / "x.yaml", "# someone else's\n")
    assert IF.write(str(tmp_path), result) == []
    assert late.read_text(encoding="utf-8") == "# someone else's\n"


def test_outside_the_project_is_refused_and_nothing_is_written(
        tmp_path, monkeypatch, capsys):
    project = tmp_path / "project"
    project.mkdir()
    outside = write(tmp_path / "elsewhere" / "x.csv", "id\n1\n")
    monkeypatch.chdir(project)
    code, out = run(project, outside.parent, capsys=capsys)
    assert code == 2
    assert "outside the project folder and every base declared in DATA.md" \
        in out.err
    assert not (project / "DATA.md").exists()
    assert not (project / "contracts").exists()


def test_a_declared_base_outside_the_project_is_allowed(tmp_path):
    project = tmp_path / "project"
    outside = write(tmp_path / "store" / "logs" / "x.csv", "id\n1\n2\n")
    write(project / "DATA.md",
          "# Data\n\n```tq-config\nbases:\n  store: "
          + str(outside.parent.parent).replace("\\", "/") + "\n```\n")
    entry = IF.plan(str(project), [str(outside.parent)])["drafts"][0]
    assert entry["contract"]["location"] == {"path": "logs/x.csv",
                                             "base": "store", "format": "csv"}
    assert entry["source"] == "store:logs/x.csv"


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_link_out_of_the_project_is_not_read(tmp_path):
    secret = write(tmp_path / "secret" / "s.csv", "token\nabc\n")
    data = tmp_path / "project" / "data"
    data.mkdir(parents=True)
    os.symlink(secret, data / "s.csv")
    result = IF.plan(str(tmp_path / "project"), [str(data)])
    assert result["drafts"] == []
    assert "points outside the project folder (a link)" in \
        result["not_inferred"][0]["reason"]


def test_personal_data_values_are_never_written(tmp_path, monkeypatch, capsys):
    write(tmp_path / "d" / "p.csv",
          "user_id,email,contact,full_name,product_name,state\n"
          "1,ann@x.org,ann@x.org,Ann Lee,Pen,CA\n"
          "2,bo@x.org,bo@x.org,Bo Li,Pen,CA\n"
          "3,ann@x.org,ann@x.org,Ann Lee,Ink,NY\n"
          "4,bo@x.org,bo@x.org,Bo Li,Ink,NY\n")
    monkeypatch.chdir(tmp_path)
    code, out = run(tmp_path, "d", capsys=capsys)
    assert code == 0
    text = (tmp_path / "contracts" / "p.yaml").read_text(encoding="utf-8")
    contract = yaml.safe_load(text)
    for name in ("email", "contact", "full_name"):
        assert col(contract, name)["pii"] is True
        assert "values" not in col(contract, name)
    for secret in ("ann@x.org", "bo@x.org", "Ann Lee", "Bo Li"):
        assert secret not in text and secret not in out.out
    assert col(contract, "state")["values"] == ["CA", "NY"]


@pytest.mark.parametrize("name,pii", [
    ("email", True), ("customer_email", True), ("phone", True),
    ("first_name", True), ("lastName", True), ("name", True),
    ("customer_name", True), ("date_of_birth", True), ("ip_address", True),
    ("product_name", False), ("file_name", False), ("table_name", False),
    ("status", False), ("event_name", False),
])
def test_pii_names(name, pii):
    assert IF.is_pii_name(name) is pii


def test_units_from_names_only(tmp_path):
    write(tmp_path / "d" / "u.csv",
          "k,size_gib,amount_cents,currency,duration_ms,size,total_gb\n"
          "1,1.5,100,USD,20,1073741824,3\n2,2,250,EUR,30,5,4\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    contract = entry["contract"]
    assert col(contract, "size_gib")["unit"] == "GiB"
    assert col(contract, "total_gb")["unit"] == "GB"
    assert col(contract, "amount_cents")["unit"] == "minor"
    assert col(contract, "amount_cents")["currency_column"] == "currency"
    assert col(contract, "duration_ms")["unit"] == "ms"
    assert "unit" not in col(contract, "size")  # never from magnitudes
    for name in ("size_gib", "total_gb", "amount_cents", "duration_ms"):
        assert col(contract, name)["status"] == "draft"
    assert any("total_gb: unit from the name: GB (10^9 bytes), not GiB?" in q
               for q in entry["questions"])


def test_cents_without_a_currency_column_ask(tmp_path):
    write(tmp_path / "d" / "u.csv", "k,amount_cents\n1,100\n2,200\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    assert col(entry["contract"], "amount_cents") == {"type": "integer",
                                                      "status": "draft"}
    assert any("of which currency" in q for q in entry["questions"])


# -- determinism and the written text ----------------------------------------

def _messy_project(root: Path):
    write(root / "logs" / "fetch" / "2026-10-02.csv",
          "run_id,url,status,local_path,fetched_at\n"
          "r2,https://x/2,released,2026/b.bin,2026-10-02 08:00:00\n")
    write(root / "logs" / "fetch" / "2026-10-01.csv",
          "run_id,url,status,local_path,fetched_at\n"
          "r1,https://x/1,fetched,2026/a.bin,2026-10-01 08:00:00\n")
    write(root / "data" / "2026" / "a.bin", "x")
    write(root / "events.jsonl", '{"id": "1", "ts": "2026-10-01T08:00:00Z"}\n'
          '{"id": "2", "ts": "2026-10-01T09:00:00Z"}\n')
    write(root / "DATA.md", "# Data\n\n```tq-config\nbases:\n"
          "  data_root: ./data\n```\n")


def test_the_same_files_give_byte_identical_contracts(tmp_path, monkeypatch,
                                                      capsys):
    texts = []
    for name in ("one", "two"):
        root = tmp_path / name
        _messy_project(root)
        monkeypatch.chdir(root)
        run(root, "logs", "events.jsonl", capsys=capsys)
        texts.append({p.name: p.read_bytes()
                      for p in sorted((root / "contracts").glob("*.yaml"))})
    assert texts[0] == texts[1]
    for blob in texts[0].values():
        assert b"\r" not in blob and b"\\" not in blob.replace(b'\\"', b"")
        assert str(tmp_path).encode() not in blob


def test_every_draft_passes_validate_and_the_json_schema(tmp_path,
                                                         monkeypatch, capsys):
    _messy_project(tmp_path)
    write(tmp_path / "d" / "weird.csv",
          'id,"yes","on","a b","x-y",status\n'
          '1,"say ""hi""",:,#,%,yes\n2,\\,\x7f,"two\nlines",null,no\n')
    monkeypatch.chdir(tmp_path)
    run(tmp_path, ".", capsys=capsys)
    assert cli.main(["contract", "validate"]) == 0
    validator = jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text()))
    found = drafts(tmp_path)
    assert len(found) == 3
    for contract in found.values():
        validator.validate(contract)
    weird = found["weird"]
    assert list(weird["columns"]) == ["id", "yes", "on", "a b", "x-y",
                                      "status"]
    assert col(weird, "status")["values"] == ["no", "yes"]


def test_render_reads_back_exactly():
    contract = {"version": 2, "dataset": "y", "status": "draft",
                "inferred_from": 'a "quoted": \\ \x7f  é 😀',
                "kind": "files",
                "location": {"path": "d/*.csv", "format": "csv",
                             "delimiter": "\t"},
                "grain": "TODO: x", "keys": [["a", "on"]],
                "columns": {"a": {"type": "string",
                                  "values": ["yes", "1", "null", "~", "-x",
                                             "[", "#c", "é"]},
                            "on": {"type": "string", "values": {
                                "no": {"means": "TODO"}}}}}
    text = IF.render(contract, {("a", "type"): "a\nnote"}, ["head"])
    assert yaml.safe_load(text) == contract


def test_cli_json_output(tmp_path, monkeypatch, capsys):
    write(tmp_path / "d" / "x.csv", "id,ts\n1,2026-10-01 08:00:00\n"
          "2,2026-10-01 09:00:00\n")
    monkeypatch.chdir(tmp_path)
    code = cli.main(["init", "--json", "--from", "d"])
    doc = json.loads(capsys.readouterr().out)
    assert code == 0
    entry = doc["from"]["drafts"][0]
    assert entry["file"] == "contracts/x.yaml" and entry["written"]
    assert any(q.startswith("ts: ") for q in entry["questions"])


def test_hooks_never_load_init_from_or_scaffold():
    folder = os.path.dirname(P.__file__)
    for name in ("hook.py", "engine.py", "report.py"):
        with open(os.path.join(folder, name), encoding="utf-8") as handle:
            text = handle.read()
        assert "initfrom" not in text and "scaffold" not in text, name


# -- review findings (REVIEW-0.2-E-RESULT.md) --------------------------------

@pytest.mark.skipif(not V.duckdb_status()["installed"],
                    reason="the data reader isn't installed")
def test_parquet_columns_are_matched_by_name_in_each_file(tmp_path):
    """Finding 1: files of one dataset with the columns in another order
    (or one more column) never put one column's values in another."""
    import duckdb
    folder = tmp_path / "data" / "cust"
    folder.mkdir(parents=True)
    con = duckdb.connect()
    names = ["Ann Lee", "Bo Li", "Carol White", "Dan Brown"]
    for name, order in (("a", "customer_id, status, customer_name"),
                        ("b", "customer_name, customer_id, status, extra")):
        rows = " UNION ALL ".join(
            f"SELECT '{name}{i}' AS customer_id, "
            f"'{['active', 'closed'][i % 2]}' AS status, "
            f"'{names[i]}' AS customer_name, {i} AS extra"
            for i in range(4))
        target = str(folder / f"{name}.parquet").replace("'", "''")
        con.execute(f"COPY (SELECT {order} FROM ({rows})) TO '{target}' "
                    "(FORMAT parquet)")
    con.close()
    entry = IF.plan(str(tmp_path), [str(tmp_path / "data")])["drafts"][0]
    contract = entry["contract"]
    assert col(contract, "status")["values"] == ["active", "closed"]
    assert col(contract, "customer_name")["pii"] is True
    assert contract["keys"] == ["customer_id"]
    text = IF.render(contract, entry["comments"], [])
    for name in names:
        assert name not in text


@pytest.mark.parametrize("header,rows,column,reason", [
    ("ticket_id,assignee", ["1,Alice Smith", "2,Bob Jones", "3,Alice Smith",
                            "4,Bob Jones"], "assignee", "by its name"),
    ("ticket_id,created_by", ["1,ann", "2,bo", "3,ann", "4,bo"],
     "created_by", "by its name"),
    ("ticket_id,cell", ["1,+1 415 555 0100", "2,+1 415 555 0101",
                        "3,+1 415 555 0100", "4,+1 415 555 0101"], "cell",
     "by its name"),
    ("ticket_id,on_call", ["1,+1 415 555 0100", "2,(415) 555-0101",
                           "3,+1 415 555 0100", "4,(415) 555-0101"],
     "on_call", "its values look like phone numbers"),
    ("ticket_id,handled", ["1,Carol White", "2,Dan Brown", "3,Carol White",
                           "4,Dan Brown"], "handled",
     "its values look like people's names"),
])
def test_names_and_phone_numbers_are_personal_data(tmp_path, header, rows,
                                                   column, reason):
    """Finding 3: names and phone numbers never reach `values`."""
    write(tmp_path / "d" / "t.csv", header + "\n" + "\n".join(rows) + "\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    spec = col(entry["contract"], column)
    assert spec["pii"] is True and "values" not in spec
    assert any(f"looks like personal data ({reason})" in q
               for q in entry["questions"])
    text = IF.render(entry["contract"], entry["comments"], [])
    for row in rows:
        assert row.split(",", 1)[1] not in text


@pytest.mark.parametrize("name,ctype,values,pii", [
    ("email_sent", "boolean", [True], False),
    ("has_email", "boolean", [False], False),
    ("email_opened_count", "integer", [3], False),
    ("is_phone_verified", "boolean", [True], False),
    ("address_verified", "boolean", [True], False),
    ("mail_count", "integer", [1], False),
    ("street_type", "string", ["Ave", "St"], False),
    ("zip_file", "string", ["a.zip"], False),
    ("customer_id", "string", ["c1", "c2"], False),
    ("user_agent", "string", ["Mozilla/5.0"], False),
    ("city", "string", ["New York", "Los Angeles"], False),
    ("subregion1_name", "string", ["New South Wales", "South Australia"],
     False),
    ("handler", "string", ["Carol White", "Dan Brown"], True),
    ("customer", "string", ["acme"], True),
    ("contact", "string", ["a@x.org", "b@y.org"], True),
    ("zip_code", "string", ["10001"], True),
])
def test_personal_data_rules(name, ctype, values, pii):
    """Findings 3 and 9: by name only for text columns; never for an id."""
    assert (IF.personal_data_reason(name, ctype, values) is not None) is pii


def test_drifting_columns_are_optional_and_both_consumers_read_them(
        tmp_path, monkeypatch, capsys):
    """Finding 4: a column some files lack is nullable (a question), the
    scaffolded reader reads those files, and `verify` reads them all."""
    pd = pytest.importorskip("pandas")
    for name, extra in (("a", ""), ("b", ""), ("c", ",boosters")):
        write(tmp_path / "d" / f"{name}.csv",
              f"location,date,total{extra}\nX{name},2026-10-01,1"
              + (",2" if extra else "") + "\n")
    monkeypatch.chdir(tmp_path)
    run(tmp_path, "d", capsys=capsys)
    contract = drafts(tmp_path)["d"]
    assert col(contract, "boosters")["nullable"] is True
    assert col(contract, "boosters")["status"] == "draft"
    assert cli.main(["scaffold", "d"]) == 0
    from truequery import scaffold as SC
    sp = SC.spec_of(P.load_contract(str(tmp_path / "contracts" / "d.yaml")),
                    "contracts/d.yaml")
    namespace: dict = {}
    exec(compile(SC.render_module(sp), "d_io.py", "exec"), namespace)
    frame = namespace["read_d"](tmp_path / "d" / "a.csv")
    assert pd.isna(frame["boosters"].iloc[0])
    if V.duckdb_status()["installed"]:
        capsys.readouterr()
        cli.main(["verify"])
        out = capsys.readouterr().out
        assert "Verified     d (3 files, 3 rows)" in out


def test_init_force_with_from_is_refused(tmp_path, monkeypatch, capsys):
    """Finding 5: --force would drop the bases the drafts point at."""
    write(tmp_path / "DATA.md", "# Data\n\n```tq-config\nbases:\n"
          "  data_root: ./data\n```\n")
    write(tmp_path / "data" / "x.csv", "id\n1\n2\n")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["init", "--force", "--from", "data"]) == 2
    assert "--force and --from can't be used together" in \
        capsys.readouterr().err
    assert "data_root" in (tmp_path / "DATA.md").read_text(encoding="utf-8")
    assert not (tmp_path / "contracts").exists()


def test_sample_values_are_drafts_with_a_question(tmp_path):
    """Finding 7: a `values` list from a sample isn't confirmed by default."""
    write(tmp_path / "d" / "x.csv", "id,currency\n1,EUR\n2,USD\n3,EUR\n"
          "4,USD\n")
    entry = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0]
    spec = col(entry["contract"], "currency")
    assert spec["values"] == ["EUR", "USD"] and spec["status"] == "draft"
    assert any(q.startswith("currency: values seen in the sample: EUR, USD."
                            " Are these all of them?")
               for q in entry["questions"])


@pytest.mark.parametrize("name,unit", [
    ("kb_article_id", None), ("tb_count", None), ("ms_count", None),
    ("revenue_gb", None), ("sales_gb", None), ("size_gb", "GB"),
    ("bytes_saved", "bytes"), ("total_gib", "GiB"), ("latency_ms", "ms"),
])
def test_units_skip_ids_counts_and_money(name, unit):
    """Finding 10."""
    assert IF._unit_from_name(name) == unit


def test_an_outside_base_is_named_not_its_path(tmp_path):
    """Finding 11: no absolute path in what's written."""
    project = tmp_path / "project"
    store = tmp_path / "store"
    write(store / "files" / "a.bin", "x")
    write(store / "logs" / "x.csv", "id,local_path\n1,files/a.bin\n"
          "2,files/b.bin\n")
    write(project / "DATA.md", "# Data\n\n```tq-config\nbases:\n  store: "
          + str(store).replace("\\", "/") + "\n```\n")
    entry = IF.plan(str(project), [str(store / "logs")])["drafts"][0]
    assert str(tmp_path) not in entry["text"]
    assert "exist under store: is that their base?" in entry["text"]


def test_from_paths_are_looked_up_in_the_project_first(tmp_path, monkeypatch,
                                                        capsys):
    """Finding 12: `truequery init proj --from data`."""
    write(tmp_path / "proj" / "data" / "x.csv", "id\n1\n2\n")
    monkeypatch.chdir(tmp_path)
    code, out = run("proj", "data", capsys=capsys)
    assert code == 0
    assert (tmp_path / "proj" / "contracts" / "x.yaml").exists()


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
def test_a_linked_folder_is_named_not_skipped_silently(tmp_path):
    """Finding 12."""
    write(tmp_path / "other" / "y.csv", "id\n1\n")
    write(tmp_path / "d" / "x.csv", "id\n1\n2\n")
    os.symlink(tmp_path / "other", tmp_path / "d" / "linked")
    result = IF.plan(str(tmp_path), [str(tmp_path / "d")])
    assert {"path": f"{tmp_path / 'd'}/linked",
            "reason": "it's a link to a folder, which isn't followed"} in \
        result["not_inferred"]


def test_jsonl_values_include_numbers(tmp_path):
    """Finding 12: `"007"` and `5` in one column: both are listed."""
    write(tmp_path / "d" / "x.jsonl", "".join(
        f'{{"id": {i}, "code": {v}}}\n'
        for i, v in enumerate(['"007"', "5", '"007"', "5"])))
    contract = IF.plan(str(tmp_path), [str(tmp_path / "d")])["drafts"][0][
        "contract"]
    assert col(contract, "code")["values"] == ["007", "5"]
