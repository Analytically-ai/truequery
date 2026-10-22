"""Project context: discovery, DATA.md, contracts, stack/dialect detection.

Offline and deterministic. DATA.md carries a machine-readable
``tq-config`` fence; everything else in the file is human prose.
"""

from __future__ import annotations

import fnmatch
import os
import re

import sqlglot
import yaml
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect

from truequery.analyzers import sql as analyzer

SKIP_DIRS = ("target", "dbt_packages", "node_modules", ".venv", ".git",
             ".truequery", "__pycache__")
# .sql, plus Jinja-flavoured dbt files the engine strips (case-insensitive).
SQL_SUFFIXES = (".sql", ".jinja", ".dbt")


def is_sql_path(path: str) -> bool:
    return path.lower().endswith(SQL_SUFFIXES)

DEFAULT_MODES = {
    "dbt": (("models/**/*.sql", "build"),
            ("snapshots/**/*.sql", "change"),
            ("analyses/**/*.sql", "explore"),
            ("tests/**/*.sql", "assure"),
            ("data-tests/**/*.sql", "assure"),
            ("macros/**/*.sql", "build")),
    "sqlmesh": (("models/**/*.sql", "build"),
                ("audits/**/*.sql", "assure"),
                ("tests/**/*.sql", "assure")),
    "plain": (("**/*.sql", "build"),),
}

_TQ_FENCE = re.compile(r"```tq-config\n(.*?)\n```", re.DOTALL)


class ContextError(ValueError):
    pass


class ConfigError(ContextError):
    """A config file the user wrote is unreadable. Never ignored silently:
    a typo must not quietly turn rule settings back to defaults."""


# dbt adapter / common names -> sqlglot dialect names.
DIALECT_ALIASES = {"sqlserver": "tsql", "synapse": "tsql", "mssql": "tsql",
                   "postgresql": "postgres", "pg": "postgres",
                   "spark_sql": "spark", "sparksql": "spark"}


def normalize_dialect(name) -> str | None:
    """A sqlglot dialect name for name, or None for "auto". Raises
    ValueError for a name sqlglot doesn't know: parsing every file with
    the wrong (or no) dialect would report them all as not checked."""
    if name is None or (isinstance(name, str) and not name.strip()):
        return None
    low = str(name).strip().lower()
    if low == "auto":
        return None
    low = DIALECT_ALIASES.get(low, low)
    if low not in Dialect.classes:
        known = ", ".join(sorted(k for k in Dialect.classes if k))
        raise ValueError(f"unknown dialect {name!r} (known: {known})")
    return low


def _yaml_problem(exc) -> str:
    mark = getattr(exc, "problem_mark", None)
    where = f" (line {mark.line + 1})" if mark is not None else ""
    problem = getattr(exc, "problem", None) or str(exc).split("\n")[0]
    return f"{problem}{where}"


def discover(paths: list[str]) -> list[str]:
    """Collect .sql files under paths, skipping build/dependency dirs.

    Returns absolute paths, sorted. Missing paths raise ContextError
    (usage error, exit 2); an empty result is not an error.
    """
    collected: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames
                                     if d not in SKIP_DIRS
                                     and not d.startswith("."))
                for name in sorted(filenames):
                    if is_sql_path(name):
                        collected.append(os.path.join(dirpath, name))
        elif os.path.isfile(path):
            # A named non-SQL file (dag.py, schema.yml) is a project
            # file: the engine's project pass reads it, not this one.
            if is_sql_path(path):
                collected.append(os.path.abspath(path))
        else:
            raise ContextError(f"tq: no such file or directory: {path}")
    return sorted(set(collected))


def _rel(path: str, root: str) -> str:
    """Short path for output: relative when under root, absolute when
    the target lives outside the working directory."""
    try:
        rel = os.path.relpath(path, root).replace(os.sep, "/")
    except ValueError:
        return path
    if rel.startswith(".."):
        return path
    return rel


def detect_stack(root: str) -> str:
    """dbt (dbt_project.yml) > sqlmesh (config marker or MODEL block) > plain."""
    if os.path.isfile(os.path.join(root, "dbt_project.yml")):
        return "dbt"
    for name in ("config.py", "config.yaml", "config.yml"):
        probe = os.path.join(root, name)
        try:
            with open(probe, encoding="utf-8") as handle:
                head = handle.read(4000).lower()
        except OSError:
            continue
        if "sqlmesh" in head:
            return "sqlmesh"
    try:
        files = discover([root])
    except ContextError:
        return "plain"
    for path in files[:50]:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                text = handle.read(2000)
        except OSError:
            continue
        if re.match(r"\s*(--[^\n]*\n|\s)*MODEL\s*\(",
                    analyzer.strip_jinja(text), re.IGNORECASE):
            return "sqlmesh"
    return "plain"


def _known_or_none(name) -> str | None:
    try:
        return normalize_dialect(name)
    except ValueError:
        return None  # unknown adapter: let sqlglot auto-detect per file


def _read_yaml_file(path: str):
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, ValueError, yaml.YAMLError):
        return None


def detect_dialect(root: str, stack: str) -> str | None:
    """Best-effort dialect from project config. None means per-file auto.

    dbt: the profile named in dbt_project.yml, its default target's type
    (profiles.yml in the project, else ~/.dbt). SQLMesh: the first
    gateway's connection type."""
    if stack == "dbt":
        project = _read_yaml_file(os.path.join(root, "dbt_project.yml")) or {}
        wanted = project.get("profile") if isinstance(project, dict) else None
        for path in (os.path.join(root, "profiles.yml"),
                     os.path.expanduser("~/.dbt/profiles.yml")):
            data = _read_yaml_file(path)
            if not isinstance(data, dict):
                continue
            profiles = ([data.get(wanted)] if wanted in data
                        else [p for p in data.values() if isinstance(p, dict)])
            for profile in profiles:
                if not isinstance(profile, dict):
                    continue
                outputs = profile.get("outputs") or {}
                target = outputs.get(profile.get("target")) or next(
                    iter(outputs.values()), None)
                if isinstance(target, dict) and target.get("type"):
                    return _known_or_none(target["type"])
    if stack == "sqlmesh":
        for name in ("config.yaml", "config.yml"):
            data = _read_yaml_file(os.path.join(root, name))
            if isinstance(data, dict):
                for gateway in (data.get("gateways") or {}).values():
                    conn = (gateway or {}).get("connection") or {}
                    if conn.get("type"):
                        return _known_or_none(conn["type"])
    return None


def default_config(stack: str, dialect: str | None) -> dict:
    return {"version": 1, "stack": stack, "dialect": dialect,
            "modes": [{"pattern": pat, "mode": mode}
                      for pat, mode in DEFAULT_MODES[stack]]}


def render_data_md(config: dict) -> str:
    lines = [
        "# DATA.md — TrueQuery project context",
        "",
        f"Stack: {config.get('stack', 'plain')} · "
        f"Dialect: {config.get('dialect') or 'auto'} · "
        "Generated by `tq init` — edit freely, but keep the tq-config block.",
        "",
        "## Owners, layers, SLAs",
        "",
        "<!-- TODO: who owns this data, layer conventions, freshness SLAs. -->",
        "",
        "```tq-config",
        yaml.safe_dump(config, sort_keys=True).rstrip(),
        "```",
        "",
    ]
    return "\n".join(lines)


def load_config(root: str) -> dict | None:
    """Parse the tq-config fence from DATA.md. None when absent; raises
    ConfigError when the fence is there but is not a YAML mapping."""
    try:
        with open(os.path.join(root, "DATA.md"), encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    match = _TQ_FENCE.search(text)
    if not match:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ConfigError("DATA.md: the tq-config block is not valid YAML: "
                          f"{_yaml_problem(exc)}") from None
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ConfigError("DATA.md: the tq-config block must be a mapping "
                          "(key: value lines)")
    try:
        data["dialect"] = normalize_dialect(data.get("dialect"))
    except ValueError as exc:
        raise ConfigError(f"DATA.md tq-config: {exc}") from None
    return data


def resolve_mode(relpath: str, config: dict | None,
                 override: str | None) -> str:
    if override:
        return override
    if config:
        for entry in config.get("modes", []):
            if _glob_match(relpath, entry.get("pattern", "")):
                return entry.get("mode", "build")
    return "build"


def _glob_match(relpath: str, pattern: str) -> bool:
    """fnmatch plus one glob truth: `**/` spans zero or more dirs, so
    models/**/*.sql also matches models/top-level.sql. Case-sensitive
    (fnmatchcase) for identical results on every OS."""
    if fnmatch.fnmatchcase(relpath, pattern):
        return True
    if "**/" in pattern:
        return fnmatch.fnmatchcase(relpath, pattern.replace("**/", ""))
    return False


# -- contracts ---------------------------------------------------------------
CONTRACT_REQUIRED = ("table", "grain", "keys")
CONTRACT_OPTIONAL = ("partition_key", "pii", "freshness_sla",
                     "allowed_values", "deprecated", "quirks",
                     "columns", "units")


class ContractError(ValueError):
    pass


def load_contract(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ContractError(f"{path}: not valid YAML: {_yaml_problem(exc)}") \
            from None
    if not isinstance(raw, dict):
        raise ContractError(f"{path}: top level must be a mapping")
    unknown = set(raw) - set(CONTRACT_REQUIRED) - set(CONTRACT_OPTIONAL)
    if unknown:
        raise ContractError(f"{path}: unknown keys {sorted(unknown)}")
    missing = [k for k in CONTRACT_REQUIRED if k not in raw]
    if missing:
        raise ContractError(f"{path}: missing keys {missing}")
    if not isinstance(raw["table"], str) or not raw["table"]:
        raise ContractError(f"{path}: table must be a non-empty string")
    if not isinstance(raw["keys"], list):
        raise ContractError(f"{path}: keys must be a list")
    return raw


def _format_keys(keys: list) -> str:
    """Render a keys list with composite keys as (a,b)."""
    parts = []
    for key in keys or []:
        if isinstance(key, list):
            parts.append("(" + ",".join(str(k) for k in key) + ")")
        else:
            parts.append(str(key))
    return ",".join(parts)


def match_contract(name: str, contracts: dict[str, dict]) -> str | None:
    """Key of the contract covering a referenced relation, or None.

    A model may read `raw.fx_rates` (or `{{ source('raw','fx_rates') }}`)
    while the contract declares `table: fx_rates`, so match the exact
    name first, then the unqualified name on both sides.
    """
    low = name.lower()
    if low in contracts:
        return low
    last = low.split(".")[-1]
    if last in contracts:
        return last
    for key in contracts:
        if key.split(".")[-1] in (low, last):
            return key
    return None


def load_contracts(root: str) -> dict[str, dict]:
    """{lower(table): contract} for every contract that loads."""
    return load_contracts_reporting(root)[0]


def load_contracts_reporting(root: str) -> tuple[dict[str, dict], list[str]]:
    """({lower(table): contract}, [problem, ...]). A contract that fails
    strict load is left out and named in the problems, which the
    receipt prints under "not checked"."""
    out: dict[str, dict] = {}
    problems: list[str] = []
    cdir = os.path.join(root, "contracts")
    if not os.path.isdir(cdir):
        return out, problems
    for name in sorted(os.listdir(cdir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        try:
            contract = load_contract(os.path.join(cdir, name))
        except (OSError, ValueError) as exc:
            problems.append(str(exc).replace(cdir + os.sep, "contracts/"))
            continue
        out[contract["table"].lower()] = contract
    return out, problems


def _dbt_built_refs(text: str) -> set[str]:
    """Names built by dbt relation builders in the raw text. A ref()
    always names a real model — even when a CTE shadows the same name
    (a model's CTE often reuses an upstream's name)."""
    real = {m.group(1).lower() for m in analyzer._REF_RE.finditer(text)}
    for m in analyzer._SOURCE_RE.finditer(text):
        real.add(f"{m.group(1)}.{m.group(2)}".lower())
    return real


def referenced_tables(text: str, dialect: str | None = None) -> list[str]:
    """Upstream relations a SQL document reads, best effort.

    CTE names defined in the same statement are not tables, and dbt
    {{ ref()/source() }} builders are resolved to real relations by
    the Jinja pass — so neither may surface as CONTRACT_MISSING.
    """
    real = _dbt_built_refs(text)
    refs: list[str] = []
    cleaned = analyzer.strip_comments(analyzer.strip_jinja(text),
                                      dialect or "generic")
    for chunk, _ in analyzer.split_statements(cleaned):
        try:
            stmts = sqlglot.parse(chunk, read=dialect or None)
        except Exception:
            continue
        ctes: set[str] = set()
        for stmt in stmts:
            if stmt is None:
                continue
            for cte in stmt.find_all(exp.CTE):
                name = cte.alias_or_name
                if name:
                    ctes.add(name.lower())
        for stmt in stmts:
            if stmt is None:
                continue
            for table in stmt.find_all(exp.Table):
                parts = [p for p in
                         (table.catalog, table.db, table.name) if p]
                if not parts:
                    continue
                # Skip CTE self-references — unless a dbt builder names
                # the same relation, which always means a real model.
                dotted = ".".join(parts)
                if parts[-1].lower() in ctes and dotted.lower() not in real:
                    continue
                refs.append(dotted)
    seen = set()
    ordered = []
    for ref in refs:
        # Jinja placeholders are template artifacts, not tables.
        if analyzer.JINJA_PH in ref.lower():
            continue
        if ref.lower() not in seen:
            seen.add(ref.lower())
            ordered.append(ref)
    return ordered


def tables_in_files(paths: list[str], dialect: str | None = None) -> list[str]:
    """Union of referenced tables across files, order-stable, deduped."""
    refs: list[str] = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        except (OSError, UnicodeDecodeError):
            continue  # the engine already lists it as not checked
        refs.extend(referenced_tables(text, dialect))
    seen, ordered = set(), []
    for ref in refs:
        if ref.lower() not in seen:
            seen.add(ref.lower())
            ordered.append(ref)
    return ordered


def _test_name(entry) -> tuple[str, dict]:
    """Normalize one dbt test entry to (name, args)."""
    if isinstance(entry, str):
        return entry, {}
    if isinstance(entry, dict) and len(entry) == 1:
        name, args = next(iter(entry.items()))
        return str(name), args if isinstance(args, dict) else {}
    return "", {}


def _dbt_models(data: dict) -> list[dict]:
    """Model specs from a dbt schema YAML doc: name, grain hints, keys,
    allowed values — derived only from declared tests, never invented."""
    specs = []
    for model in data.get("models", []) or []:
        if not isinstance(model, dict) or not model.get("name"):
            continue
        columns = {}
        for col in model.get("columns", []) or []:
            if isinstance(col, dict) and col.get("name"):
                # dbt >= 1.8 renamed tests: to data_tests:
                tests = col.get("tests", col.get("data_tests", [])) or []
                columns[col["name"]] = tests
        specs.append({"name": model["name"],
                      "tests": (model.get("tests",
                                          model.get("data_tests", []))
                                or []),
                      "columns": columns})
    return specs


def _contract_for_spec(spec: dict, source: str) -> str:
    name = spec["name"]
    keys: list[str] = []
    grain = "unknown  # TODO: one row per <what>"
    allowed: dict[str, list] = {}
    unique_combo: list[str] = []
    for entry in spec["tests"]:
        test, args = _test_name(entry)
        base = test.split(".")[-1]
        if base == "unique_combination_of_columns":
            unique_combo = list(args.get("combination_of_columns", []))
        elif base == "unique" and isinstance(args.get("column_name"), str):
            keys.append(args["column_name"])
    for col, tests in spec["columns"].items():
        flags = set()
        for entry in tests:
            test, args = _test_name(entry)
            base = test.split(".")[-1]
            if base in ("unique", "not_null"):
                flags.add(base)
            if base == "accepted_values":
                values = args.get("values")
                if isinstance(values, list):
                    allowed[col] = values
        if "unique" in flags:
            keys.append(col)
        if flags == {"unique", "not_null"} and grain.startswith("unknown"):
            grain = f"one row per {col}"
    if unique_combo:
        grain = f"one row per ({', '.join(unique_combo)})"
        keys.extend(c for c in unique_combo if c not in keys)
    lines = [f"# Seeded by `tq init` from {source} — grain/keys come from",
             "# declared tests only; everything else is TODO.",
             f"table: {name}",
             f"grain: {grain}",
             f"keys: [{', '.join(keys)}]",
             "partition_key: null  # TODO",
             "pii: []  # TODO: [{column: <name>, masking: hash}]",
             "freshness_sla: null  # TODO"]
    if allowed:
        dumped = yaml.safe_dump({"allowed_values": allowed},
                                sort_keys=True).rstrip()
        lines.append(dumped)
    else:
        lines.append("allowed_values: {}  # TODO")
    lines += ["deprecated: false",
              "quirks: null  # TODO"]
    return "\n".join(lines) + "\n"


def seed_contracts(root: str) -> list[str]:
    """Seed contracts/ from dbt schema YAML (models: - name: ...).

    Grain/keys/allowed_values come from declared uniqueness and
    accepted-values tests; everything else is an explicit TODO, never
    invented. Existing contracts are always kept. Returns created.
    """
    created: list[str] = []
    specs: list[tuple[dict, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            # dbt schema docs are schema.yml by convention but often
            # <model>.yml — accept any YAML carrying a
            # models: list (_dbt_models ignores the rest).
            if not name.lower().endswith((".yml", ".yaml")):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8") as handle:
                    data = yaml.safe_load(handle)
            except (OSError, ValueError, yaml.YAMLError):
                continue
            if isinstance(data, dict):
                for spec in _dbt_models(data):
                    specs.append((spec, os.path.relpath(
                        path, root).replace(os.sep, "/")))
    cdir = os.path.join(root, "contracts")
    os.makedirs(cdir, exist_ok=True)
    for spec, source in sorted(specs, key=lambda s: s[0]["name"]):
        dest = os.path.join(cdir, f"{spec['name']}.yaml")
        if os.path.exists(dest):
            continue
        with open(dest, "w", encoding="utf-8") as handle:
            handle.write(_contract_for_spec(spec, source))
        created.append(dest)
    return created
