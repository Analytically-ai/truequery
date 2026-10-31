"""Preview non-SQL checks (v1, Stop tier, never immediate).

Each check is backed by an existing standard practice (cited in its
``why``); practices with no detector-shaped record yet (Airflow dynamic
``start_date``, pandas ``merge(validate=)``) are NOT implemented here —
they are listed as gaps in docs. Deterministic, offline, file/project
evidence only.
"""
import ast
import os
import re

import yaml

INCREMENTAL = re.compile(r"materialized\s*=\s*['\"]incremental['\"]")
UNIQUE_KEY = re.compile(r"unique_key\s*=")


def _finding(rule, registry, settings, mode, path, line, why, fix,
             snippet=""):
    record = registry.RECORDS.get(rule) if registry else None
    sev = None
    if settings:
        sev = settings.get(rule, {}).get("severity")
    if not sev and record:
        sev = record["severity"].get(mode, "warning")
    return {"rule": rule, "severity": sev or "warning", "mode": mode,
            "evidence": "project", "file": path, "line": line, "col": 1,
            "snippet": snippet[:120], "why": why, "fix": fix}


STRATEGY = re.compile(r"incremental_strategy\s*=\s*['\"]([\w+]+)['\"]")


def _yaml_unique_key(path):
    """True when a properties YAML next to the model sets unique_key in
    the model's config block (dbt allows config there as well as in
    the SQL)."""
    model = os.path.splitext(os.path.basename(path))[0]
    directory = os.path.dirname(os.path.abspath(path))
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return False
    for name in names:
        if not name.endswith((".yml", ".yaml")):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                doc = yaml.safe_load(handle)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        specs = doc.get("models") if isinstance(doc, dict) else None
        for spec in specs or []:
            if (isinstance(spec, dict) and spec.get("name") == model
                    and isinstance(spec.get("config"), dict)
                    and spec["config"].get("unique_key")):
                return True
    return False


def check_dbt_model(text, path, mode="build", registry=None,
                    settings=None):
    """model/unique-key-merge-key: merge/delete+insert incremental
    model with no unique_key (in the SQL config or the model's YAML).
    Microbatch/insert_overwrite strategies key on event_time/partitions
    instead — no unique_key expected."""
    out = []
    m = INCREMENTAL.search(text)
    strat = STRATEGY.search(text)
    merge_like = strat is None or strat.group(1) in (
        "merge", "delete+insert", "delete_insert")
    if (m and merge_like and not UNIQUE_KEY.search(text)
            and not _yaml_unique_key(path)):
        line = text[:m.start()].count("\n") + 1
        out.append(_finding(
            "model/unique-key-merge-key", registry, settings, mode, path,
            line,
            "incremental model declares no unique_key; a merge degrades "
            "to append and duplicates rows on every re-run "
            "(model/unique-key-merge-key)",
            "add unique_key='<key>' to the model config and a "
            "unique + not_null test on the key"))
    return out


def _tests_of(entry):
    tests = set()
    for t in entry.get("tests") or []:
        tests.add(t if isinstance(t, str) else next(iter(t), ""))
    for t in entry.get("data_tests") or []:
        tests.add(t if isinstance(t, str) else next(iter(t), ""))
    return tests


def check_schema_yaml(text, path, mode="build", registry=None,
                      settings=None):
    """dq/uniqueness-key-no-duplicates + ingest/declare-source-freshness
    over a dbt schema YAML (models:/sources:)."""
    out = []
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return out
    if not isinstance(doc, dict):
        return out
    lines = text.splitlines()

    def line_of(needle, after=0):
        for i, row in enumerate(lines[after:], start=after + 1):
            if needle in row:
                return i
        return 1

    for model in doc.get("models") or []:
        if not isinstance(model, dict):
            continue
        name = model.get("name", "")
        cols = model.get("columns") or []
        # A key candidate needs a declared signal: a MetricFlow primary
        # entity, or a unique test (someone claimed uniqueness). A bare
        # *_id column is as often a foreign key, and a column with an
        # accepted_values test is not a key — stay silent there.
        key_cols = []
        for c in cols:
            if not isinstance(c, dict):
                continue
            entity = c.get("entity") or {}
            if entity.get("type") == "primary" or "unique" in _tests_of(c):
                key_cols.append(c)
        if not key_cols:
            continue  # no declared key candidate: cannot infer, stay silent
        col = key_cols[0]
        tests = _tests_of(col)
        combo = any("unique_combination_of_columns" in t
                    for t in _tests_of(model))
        if combo or {"unique", "not_null"} <= tests:
            continue
        out.append(_finding(
            "dq/uniqueness-key-no-duplicates", registry, settings, mode,
            path, line_of(str(col.get("name"))),
            f"model {name}: key column {col.get('name')} lacks a "
            "unique + not_null test pair, so duplicate or null keys "
            "pass silently (dq/uniqueness-key-no-duplicates)",
            f"add unique and not_null tests on {col.get('name')}, or a "
            "unique_combination_of_columns test on the model"))
    for source in doc.get("sources") or []:
        if not isinstance(source, dict):
            continue
        for table in source.get("tables") or []:
            if not isinstance(table, dict):
                continue
            freshness = source.get("freshness") or table.get("freshness")
            loaded = (source.get("loaded_at_field")
                      or table.get("loaded_at_field"))
            # Fire only on intent: the source tracks its load time but
            # never checks it. A source with no loaded_at_field at all
            # has not opted into freshness — stay silent.
            if freshness or not loaded:
                continue
            out.append(_finding(
                "ingest/declare-source-freshness", registry, settings,
                mode, path, line_of(str(table.get("name", ""))),
                f"source table {table.get('name')} tracks loaded_at_field "
                "but declares no freshness check, so a stalled source is "
                "invisible (ingest/declare-source-freshness)",
                "add freshness: {warn_after/error_after} to the source"))
    return out


def _dag_calls(text):
    """Airflow DAG call nodes; the one detection check_python and
    applies_to_file share so counting can't drift from checking."""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        if name not in ("DAG", "dag"):  # DAG(...), models.DAG, @dag(...)
            continue
        out.append(node)
    return out


def _schema_doc(text):
    """Parsed YAML mapping, or None when it isn't one."""
    try:
        doc = yaml.safe_load(text)
    except Exception:
        return None
    return doc if isinstance(doc, dict) else None


def applies_to_file(path, text):
    """True when a preview project check applies to this file.

    A .py file counts only when it's an Airflow DAG; a YAML file
    counts only when it's a dbt schema/sources file or dbt_project.yml.
    Detection mirrors check_python/check_schema_yaml so the receipt
    never counts a file no check reads.
    """
    if path.endswith(".py"):
        return bool(_dag_calls(text))
    if path.endswith((".yml", ".yaml")):
        if os.path.basename(path) == "dbt_project.yml":
            return True
        doc = _schema_doc(text)
        return doc is not None and ("models" in doc or "sources" in doc)
    return False


SQL_KEYWORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE",
                "CREATE", "REPLACE", "DROP")

# Call sites where a string literal is plausibly SQL. Matched by function
# name only (pd.read_sql, conn.execute, spark.sql, sqlalchemy text, ...);
# the string content decides, so a non-SQL use never matches.
SQL_CALL_NAMES = frozenset({
    "execute", "executemany", "executescript", "exec_driver_sql",
    "read_sql", "read_sql_query", "read_sql_table", "text", "sql",
})


def _strip_leading_sql_comments(s):
    i, n = 0, len(s)
    while True:
        while i < n and s[i].isspace():
            i += 1
        if s.startswith("--", i):
            j = s.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if s.startswith("/*", i):
            j = s.find("*/", i + 2)
            if j == -1:
                return ""
            i = j + 2
            continue
        if i < n and s[i] == "#":
            j = s.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        break
    return s[i:]


def _starts_with_sql_keyword(s):
    rest = _strip_leading_sql_comments(s)
    upper = rest.upper()
    for kw in SQL_KEYWORDS:
        if upper.startswith(kw):
            after = rest[len(kw):len(kw) + 1]
            if not after or not (after.isalnum() or after == "_"):
                return True
    return False


def _lead_text(node):
    """Static leading text of a string expression, or None.

    Constants give their value; f-strings give the literal prefix before
    the first placeholder; `+` concatenation gives the left prefix (or the
    right when the left has none). Anything dynamic yields None.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(
                    value.value, str):
                parts.append(value.value)
            else:
                break
        return "".join(parts) if parts else ""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _lead_text(node.left)
        if left:
            return left
        return _lead_text(node.right)
    return None


def _node_is_sql_string(node):
    lead = _lead_text(node)
    return bool(lead) and _starts_with_sql_keyword(lead)


def _assigned_values(tree):
    """Name -> value nodes assigned to it in this file.

    Only plain `name = value` and `name: type = value` with a single
    `Name` target: same-file, by-name lookup. No data flow, no imports.
    """
    out: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                out.setdefault(target.id, []).append(node.value)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.value is not None):
            out.setdefault(node.target.id, []).append(node.value)
    return out


def _resolves_to_sql(node, assigned, seen=frozenset()):
    """True when a call-site argument is (or names) a SQL string.

    A bare `Name` resolves to the string assigned to it in the same
    file, then gets the same keyword test. Anything else (including a
    docstring, which is never assigned or passed) never matches.
    """
    if isinstance(node, ast.Name):
        if node.id in seen:
            return False
        return any(_resolves_to_sql(value, assigned, seen | {node.id})
                   for value in assigned.get(node.id, []))
    return _node_is_sql_string(node)


def has_embedded_sql(text):
    """True when a .py file holds SQL strings at known call sites.

    Detection only: a string counts as SQL when it starts (after
    whitespace and SQL comments) with a SQL keyword. A bare name at a
    call site resolves to the string assigned to it in the same file.
    The SQL inside is never parsed or checked here; callers list such
    files as not checked.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    assigned = _assigned_values(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute) else "")
        args = list(node.args)
        for kw in node.keywords:
            if kw.arg == "sql":
                args.append(kw.value)
        if not args:
            continue
        if name in SQL_CALL_NAMES or any(
                kw.arg == "sql" for kw in node.keywords):
            for arg in args:
                if _resolves_to_sql(arg, assigned):
                    return True
    return False


def check_python(text, path, mode="build", registry=None, settings=None):
    """orch/explicit-catchup-policy: Airflow DAG without catchup."""
    out = []
    for node in _dag_calls(text):
        kws = {k.arg for k in node.keywords if k.arg}
        if "catchup" in kws:
            continue
        out.append(_finding(
            "orch/explicit-catchup-policy", registry, settings, mode,
            path, node.lineno,
            "Airflow DAG declares no explicit catchup; the default "
            "backfills every missed interval on first deploy "
            "(orch/explicit-catchup-policy)",
            "set catchup=False (or True deliberately) on the DAG"))
    return out


def check_aux_file(text, path, mode="build", registry=None, settings=None):
    """Dispatch by file kind. Returns finding dicts (engine shape)."""
    if path.endswith(".py"):
        return check_python(text, path, mode, registry, settings)
    if path.endswith((".yml", ".yaml")):
        return check_schema_yaml(text, path, mode, registry, settings)
    return []


def check_model_sql(text, path, mode="build", registry=None, settings=None):
    if path.endswith(".sql"):
        return check_dbt_model(text, path, mode, registry, settings)
    return []


# slug -> implementing function, for the standard<->checks CI test.
PREVIEW_CHECKS = {
    "model/unique-key-merge-key": check_dbt_model,
    "dq/uniqueness-key-no-duplicates": check_schema_yaml,
    "ingest/declare-source-freshness": check_schema_yaml,
    "orch/explicit-catchup-policy": check_python,
}
