"""pandas checks: a finding where the code is wrong, none where it's fine,
and the engine/receipt wiring (counted only when a check ran)."""
import json

from truequery import engine as E
from truequery import report as RP
from truequery.analyzers import pandas_checks as pc

HEAD = "import pandas as pd\ndf = pd.read_csv('x.csv')\n"


def rules(code, head=HEAD):
    ran, found = pc.check(head + code, pc.RULES)
    return ran, [f[0] for f in found]


def test_no_pandas_import_is_not_in_scope():
    assert pc.check("df[df.a > 1]['b'] = 0\n", pc.RULES) == (False, [])


def test_unknown_names_are_not_reported():
    assert rules("other[other.a > 1]['b'] = 0\n")[1] == []


def test_chained_assignment():
    assert rules("df[df.a > 1]['b'] = 0\n")[1] == [pc.CHAINED]
    assert rules("df['b'].fillna(0, inplace=True)\n")[1] == [pc.CHAINED]
    assert rules("df.query('a > 1')['b'] = 0\n")[1] == [pc.CHAINED]
    assert rules("df.loc[df.a > 1, 'b'] = 0\n")[1] == []
    assert rules("d2 = df[df.a > 1].copy()\nd2['b'] = 0\n")[1] == []


def test_annotated_parameter_is_a_frame():
    code = "def f(x: pd.DataFrame):\n    x[x.a > 1]['b'] = 0\n"
    assert rules(code)[1] == [pc.CHAINED]


def test_row_loop_mutation():
    bad = "for i, row in df.iterrows():\n    row['t'] = row['a'] * 2\n"
    assert rules(bad)[1] == [pc.ROW_LOOP]
    used = bad + "    out.append(row)\n"
    assert rules(used)[1] == []
    good = "for i, row in df.iterrows():\n    df.loc[i, 't'] = row['a'] * 2\n"
    assert rules(good)[1] == []


def test_groupby_first():
    assert pc.FIRST in rules("x = df.groupby('k').first()\n")[1]
    assert pc.FIRST not in rules("x = df.groupby('k')['c'].first()\n")[1]
    assert pc.FIRST not in rules(
        "x = df.groupby('k').first(skipna=False)\n")[1]
    # one column in a list can't mix rows either; two can
    assert pc.FIRST not in rules("x = df.groupby('k')[['v']].first()\n")[1]
    assert pc.FIRST not in rules(
        "x = df.groupby('k')[['v']].agg('last')\n")[1]
    assert pc.FIRST in rules("x = df.groupby('k')[['v', 'w']].first()\n")[1]


def test_why_never_repeats_the_rule_id():
    # the CLI and the hooks print the rule already
    code = ("df[df.a > 1]['b'] = 0\n"
            "df['b'].fillna(0, inplace=True)\n"
            "for i, row in df.iterrows():\n    row['t'] = 1\n"
            "x = df.groupby('k').first()\n"
            "y = df.sort_values('ts').drop_duplicates('id')\n"
            "z = df.merge(df, on='id')\n"
            "df.to_sql('t', con, if_exists='replace')\n")
    _, found = pc.check(HEAD + code, pc.RULES)
    # the contract checks need a project (tests/test_pandas_contracts.py)
    assert {f[0] for f in found} == set(pc.RULES) - set(pc.PC.NEW_RULES)
    for rule, _, _, why, fix in found:
        assert rule not in why and rule not in fix, why


def test_dedupe_unstable_sort():
    bad = "x = df.sort_values('ts').drop_duplicates('id', keep='last')\n"
    assert pc.DEDUPE in rules(bad)[1]
    two = "df = df.sort_values('ts')\ndf = df.drop_duplicates('id')\n"
    assert pc.DEDUPE in rules(two)[1]
    for good in ("x = df.sort_values(['ts', 'n']).drop_duplicates('id')\n",
                 "x = df.sort_values('ts', kind='stable').drop_duplicates('id')\n",
                 "x = df.sort_values('ts').drop_duplicates()\n",
                 "x = df.sort_values('ts').drop_duplicates('id', keep=False)\n"):
        assert pc.DEDUPE not in rules(good)[1], good


def test_merge_validate_groupby_dropna_to_sql():
    assert rules("x = df.merge(df, on='id')\n")[1] == [pc.MERGE]
    assert rules("x = df.merge(df, on='id', validate='m:1')\n")[1] == []
    assert rules("x = df.merge(df, how='cross')\n")[1] == []
    assert rules("x = ', '.join(names)\n")[1] == []
    assert pc.DROPNA in rules("x = df.groupby('k').size()\n")[1]
    assert rules("x = df.groupby('k', dropna=False).size()\n")[1] == []
    assert rules("df.to_sql('t', con, if_exists='replace')\n")[1] == [
        pc.TO_SQL]
    assert rules("df.to_sql('t', con, if_exists='append')\n")[1] == []


def test_method_findings_point_at_the_method():
    code = "x = (df.sort_values('ts')\n     .drop_duplicates('id'))\n"
    _, found = pc.check(HEAD + code, {pc.DEDUPE})
    assert [(f[1], f[2]) for f in found] == [(4, 7)]


def test_pandas_only_file_is_counted_and_receipted(tmp_path):
    (tmp_path / "etl.py").write_text(
        HEAD + "df[df.a > 1]['b'] = 0\n", encoding="utf-8")
    (tmp_path / "plain.py").write_text("x = 1\n", encoding="utf-8")
    results = E.run([str(tmp_path)], str(tmp_path), config={})
    assert [(r.rel, r.kind, r.pandas) for r in results] == [
        ("etl.py", "pandas", True)]
    assert [f.rule for f in results[0].findings] == [pc.CHAINED]
    receipt = RP.build_receipt(results, ["build"], {}, [])
    assert receipt["pandas_files"] == 1
    assert "pandas in 1 Python file" in RP.render_receipt(receipt)
    doc = json.loads(RP.render_json(results, "x", ["build"]))
    assert doc["schema_version"] == 1 and doc["pandas_files"] == ["etl.py"]


def test_no_pandas_rule_on_means_not_counted(tmp_path):
    (tmp_path / "etl.py").write_text(HEAD, encoding="utf-8")
    off = {slug: {"enabled": False} for slug in pc.RULES}
    assert E.run([str(tmp_path)], str(tmp_path), config={},
                 settings=off) == []


def test_ignore_spans_cover_pandas_lines():
    text = HEAD + "\n\ndf[df.a > 1]['b'] = 0\n"
    spans = E.statement_spans("etl.py", text, None)
    assert E.stmt_hash_at(spans, 5) is not None
    shifted = "# moved\n" + text
    assert (E.stmt_hash_at(E.statement_spans("etl.py", shifted, None), 6)
            == E.stmt_hash_at(spans, 5))


# -- hooks and the CLI ------------------------------------------------------
import io  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402

import truequery.hook as hook  # noqa: E402


class _Args:
    harness, event, log_file, scope = "claude", "auto", None, "project"


def _hook(payload, monkeypatch):
    monkeypatch.setattr(hook, "read_stdin",
                        lambda timeout=5.0: json.dumps(payload))
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)
    assert hook.main(_Args()) == 0
    text = out.getvalue()
    return (json.loads(text)["hookSpecificOutput"]["additionalContext"]
            if text else None)


def _edit(path, event="PostToolUse"):
    return {"hook_event_name": event, "tool_name": "Write",
            "tool_input": {"file_path": str(path)}, "cwd": str(path.parent)}


def test_edit_runs_chained_assignment_at_once(tmp_path, monkeypatch):
    target = tmp_path / "etl.py"
    target.write_text(HEAD + "df[df.a > 1]['b'] = 0\n"
                      "x = df.groupby('k').first()\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    text = _hook(_edit(target), monkeypatch)
    assert "etl.py:3 [py/pandas-no-chained-assignment]" in text
    assert "groupby-first" not in text  # end-of-turn rule
    stop = _hook(_edit(target, "Stop"), monkeypatch)
    assert "etl.py:4 [py/pandas-groupby-first-mixes-rows]" in stop


def test_python_file_without_pandas_or_sql_is_silent(tmp_path, monkeypatch):
    target = tmp_path / "util.py"
    target.write_text("df = load()\ndf[df.a > 1]['b'] = 0\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert _hook(_edit(target), monkeypatch) is None


_HOME = tempfile.mkdtemp(prefix="tq-test-home-")
_ENV = dict(os.environ, HOME=_HOME, USERPROFILE=_HOME,
            PYTHONPATH=os.path.join(os.path.dirname(__file__), "..",
                                    "plugin", "engine", "src"))


def _cli(*argv, cwd):
    proc = subprocess.run([sys.executable, "-m", "truequery.cli", *argv],
                          cwd=cwd, capture_output=True, text=True,
                          env=_ENV, timeout=120)
    return proc.returncode, proc.stdout, proc.stderr


def test_ignore_add_and_rules_set_for_pandas_rules(tmp_path):
    (tmp_path / "etl.py").write_text(
        HEAD + "df[df.a > 1]['b'] = 0\nx = df.merge(df, on='id')\n",
        encoding="utf-8")
    code, out, _ = _cli("detect", "--json", "etl.py", cwd=tmp_path)
    doc = json.loads(out)
    assert [f["rule"] for f in doc["findings"]] == [pc.CHAINED]  # merge off
    code, _, err = _cli("ignore", "add", pc.CHAINED, "etl.py:3",
                        "--reason", "test: owner accepted", cwd=tmp_path)
    assert code == 0, err
    (tmp_path / "etl.py").write_text(
        "# a new first line\n" + HEAD + "df[df.a > 1]['b'] = 0\n"
        "x = df.merge(df, on='id')\n", encoding="utf-8")
    code, out, _ = _cli("detect", "--json", "etl.py", cwd=tmp_path)
    assert json.loads(out)["findings"] == []  # the ignore moved with it
    code, _, err = _cli("init", cwd=tmp_path)
    assert code == 0, err
    code, _, err = _cli("rules", "set", pc.MERGE, "--enabled", "on",
                        cwd=tmp_path)
    assert code == 0, err
    code, out, _ = _cli("detect", "--json", "etl.py", cwd=tmp_path)
    assert [f["rule"] for f in json.loads(out)["findings"]] == [pc.MERGE]
