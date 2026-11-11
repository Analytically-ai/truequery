"""Declared-key index: the first consumer of contracts/.

Maps a relation name (lowercased) to its candidate keys. Each candidate
is a frozenset of columns that is unique on its own; a relation may
have several (a surrogate id and a natural key). Sources:

- ``contracts/<table>.yaml``: every ``keys:`` entry is one candidate
  (a nested list such as ``[currency, rate_date]`` is one composite
  candidate), and
- dbt schema YAML under a ``models`` tree: each column with a
  ``unique`` test, and each model-level
  ``unique_combination_of_columns`` test.

Deterministic, cached per project. Offline, file-level evidence only.
A contract that fails validation, is a duplicate (a second file for the
same table), or is still a draft contributes nothing, so its keys can't
hide a finding (the receipt names invalid, duplicate, and draft contracts).
Unreadable YAML contributes nothing either; this index never breaks a check.
"""
import os

import yaml

from truequery.project import (
    SKIP_DIRS,
    ContextError,
    is_draft_contract,
    load_config,
    load_contracts_reporting,
    load_metrics_reporting,
)

_CACHE: dict[tuple, dict[str, list[frozenset]]] = {}
_CONTEXT_CACHE: dict[tuple, dict] = {}


def clear_cache() -> None:
    """Forget what was read: each run reads contracts, metrics and dbt YAML
    once, and a later run in the same process sees later edits."""
    _CACHE.clear()
    _CONTEXT_CACHE.clear()


def _read_yaml(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except (OSError, ValueError, yaml.YAMLError):
        return None


def _add(index, table, columns):
    key = frozenset(str(c).lower() for c in columns if isinstance(c, str))
    if key and key not in index.setdefault(table, []):
        index[table].append(key)


def _test_entries(spec):
    """(name, args) for each test on a model or column (dbt >= 1.8
    spells `tests:` as `data_tests:`)."""
    for test in (spec.get("tests") or []) + (spec.get("data_tests") or []):
        if isinstance(test, str):
            yield test, {}
        elif isinstance(test, dict):
            for name, args in test.items():
                yield str(name), args if isinstance(args, dict) else {}


def _from_contracts(root, index):
    # Share load_contracts_reporting so one rule holds everywhere: the
    # first valid contract for a table (sorted by file name) is the
    # contract, while invalid, duplicate, and draft contracts add no keys.
    contracts, _ = load_contracts_reporting(root)
    for table, contract in sorted(contracts.items()):
        if is_draft_contract(contract):
            continue
        for key in contract["keys"]:
            _add(index, table, key if isinstance(key, list) else [key])


def _from_dbt_yaml(path, index):
    doc = _read_yaml(path)
    if not isinstance(doc, dict):
        return
    for model in doc.get("models") or []:
        if not isinstance(model, dict) or not model.get("name"):
            continue
        table = str(model["name"]).lower()
        for name, args in _test_entries(model):
            # A model-level unique test without a column list pins nothing.
            if name.split(".")[-1] == "unique_combination_of_columns":
                _add(index, table, args.get("combination_of_columns") or [])
        for col in model.get("columns") or []:
            if not isinstance(col, dict) or not col.get("name"):
                continue
            if any(name.split(".")[-1] == "unique"
                   for name, _ in _test_entries(col)):
                _add(index, table, [col["name"]])


def _nearest(path, test):
    """Nearest ancestor directory of path for which test(dir) holds."""
    directory = os.path.dirname(os.path.abspath(path))
    while True:
        if test(directory):
            return directory
        parent = os.path.dirname(directory)
        if parent == directory:
            return None
        directory = parent


def _project_root(path):
    """Nearest ancestor with a project marker, or None: without one we
    don't walk the disk looking for keys (they'd belong to someone else)."""
    if not path or path == "<input>":
        return None
    markers = ("contracts", "dbt_project.yml", "DATA.md")
    return _nearest(path, lambda d: any(
        os.path.exists(os.path.join(d, m)) for m in markers))


def _models_ymls(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        # A nested dbt project is a different project: its keys aren't ours.
        dirnames[:] = sorted(
            d for d in dirnames
            if d not in SKIP_DIRS and not d.startswith(".")
            and not os.path.isfile(os.path.join(dirpath, d, "dbt_project.yml")))
        parts = os.path.relpath(dirpath, root).split(os.sep)
        if dirpath != root and "models" not in parts:
            continue
        found += [os.path.join(dirpath, name) for name in sorted(filenames)
                  if name.endswith((".yml", ".yaml"))]
    return found


def declared_keys_for(path, root=None):
    """{relation name -> [candidate key, ...]} for the project of path.

    contracts/ may sit above the dbt project (examples/shop/contracts
    next to dbt_repo/), so it is looked up on its own."""
    proj_root = _project_root(path)
    cdir = (_nearest(path, lambda d: os.path.isdir(os.path.join(d, "contracts")))
            if path and path != "<input>" else None)
    if cdir is None and root:
        cdir = _nearest(os.path.join(os.path.abspath(root), "_"),
                        lambda d: os.path.isdir(os.path.join(d, "contracts")))
    if proj_root is None and root:
        proj_root = _project_root(os.path.join(os.path.abspath(root), "_"))
    cache_key = (proj_root, cdir)
    if cache_key not in _CACHE:
        index: dict[str, list[frozenset]] = {}
        if cdir is not None:
            _from_contracts(cdir, index)
        for yml in _models_ymls(proj_root) if proj_root is not None else []:
            _from_dbt_yaml(yml, index)
        _CACHE[cache_key] = index
    return _CACHE[cache_key]


def declared_context_for(path, root=None) -> dict:
    """Return {contracts, metrics, bases} for the project of path (or root).

    Valid contracts (including status: draft contracts, whose findings carry
    draft provenance) and valid metrics are indexed by lowercased name.
    Invalid or duplicate files are ignored so they never poison checks.
    """
    markers = ("contracts", "metrics", "DATA.md")
    ctx_dir = (_nearest(path, lambda d: any(
        os.path.exists(os.path.join(d, m)) for m in markers))
        if path and path != "<input>" else None)
    if ctx_dir is None and root:
        ctx_dir = _nearest(
            os.path.join(os.path.abspath(root), "_"),
            lambda d: any(os.path.exists(os.path.join(d, m)) for m in markers))
    if ctx_dir is None:
        return {"contracts": {}, "metrics": {}, "bases": {}}
    cdir = (_nearest(path, lambda d: os.path.isdir(os.path.join(d, "contracts")))
            if path and path != "<input>" else None) or ctx_dir
    mdir = (_nearest(path, lambda d: os.path.isdir(os.path.join(d, "metrics")))
            if path and path != "<input>" else None) or ctx_dir
    ddir = (_nearest(path, lambda d: os.path.isfile(os.path.join(d, "DATA.md")))
            if path and path != "<input>" else None) or ctx_dir
    cache_key = (cdir, mdir, ddir)
    if cache_key not in _CONTEXT_CACHE:
        try:
            cfg = load_config(ddir)
        except ContextError:
            cfg = {}
        bases = (cfg or {}).get("bases") or {}
        raw_contracts, _ = load_contracts_reporting(cdir)
        raw_metrics, _ = load_metrics_reporting(mdir, contracts=raw_contracts)
        contracts_idx: dict[str, dict] = {}
        for key, contract in sorted(raw_contracts.items()):
            k_low = key.lower()
            contracts_idx[k_low] = contract
            short = k_low.split(".")[-1]
            contracts_idx.setdefault(short, contract)
            loc = contract.get("location")
            if isinstance(loc, dict) and loc.get("relation"):
                rel = str(loc["relation"]).lower()
                contracts_idx.setdefault(rel, contract)
                contracts_idx.setdefault(rel.split(".")[-1], contract)
        metrics_idx = {k.lower(): v for k, v in sorted(raw_metrics.items())}
        _CONTEXT_CACHE[cache_key] = {
            "root": cdir,
            "contracts": contracts_idx,
            "metrics": metrics_idx,
            "bases": bases if isinstance(bases, dict) else {},
        }
    return _CONTEXT_CACHE[cache_key]
