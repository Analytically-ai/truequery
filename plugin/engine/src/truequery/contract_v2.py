"""Contract Model v2, DATA.md bases, and Metrics (v1) validation.

Offline and deterministic. Sits behind ``project.load_contract`` and
``project.load_contracts_reporting`` so every consumer (engine, context,
review, keylookup, receipt, and ``truequery contract validate``) shares a
single code path.
"""

from __future__ import annotations

import os
import re
import unicodedata

import sqlglot
import yaml
from sqlglot import exp

# Closed vocabularies for Contract v2 and Metric v1.
CONTRACT_V2_KINDS = ("table", "files")
CONTRACT_V2_STATUSES = ("confirmed", "draft")
CONTRACT_V2_FORMATS = ("csv", "jsonl", "json", "parquet")
CONTRACT_V2_WRITE_MODES = ("append", "overwrite_partition", "merge_on_key")
COLUMN_TYPES = ("string", "integer", "number", "boolean", "date",
                "timestamp", "path")

SIZE_UNITS = ("bytes", "KB", "KiB", "MB", "MiB", "GB", "GiB", "TB", "TiB")
DURATION_UNITS = ("ms", "s", "min", "h", "d")
RATIO_UNITS = ("ratio", "percent", "bps")
MONEY_UNITS = ("minor", "major")
ALL_UNITS = SIZE_UNITS + DURATION_UNITS + RATIO_UNITS + MONEY_UNITS

STATUS_MEANS_CONFIRMED = ("present", "absent_expected", "absent_unexpected")
STATUS_MEANS_DRAFT = STATUS_MEANS_CONFIRMED + ("TODO",)

METRIC_KINDS = ("simple", "ratio")
METRIC_AGGS = ("count", "count_distinct", "sum", "avg", "min", "max")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# Validated by regex, never zoneinfo (tzdata may be absent on minimal OS/Windows).
_LOCAL_TZ_RE = re.compile(
    r"^local:[A-Za-z0-9_+-]+(?:/[A-Za-z0-9_+-]+)+$")
_DURATION_RE = re.compile(r"^[1-9]\d*(?:ms|s|min|h|d)$")
# Names in Contract v2 and Metric v1 allow Unicode letters, digits (plus
# combining marks for NFD strings), `_`, spaces, `-`, and `$`, with `.`
# between non-empty parts in `dataset`, `table`, and `location.relation`.
# Quotes, backticks, `;`, parentheses, `*`, and all other punctuation or
# control characters are rejected.
#
# NOTE FOR PR B: Every identifier interpolated into SQL MUST be quoted
# (e.g. `exp.column(col, quoted=True)` / `exp.table_(..., quoted=True)`) and
# metric SQL MUST be built from the parsed sqlglot AST tree, NEVER from the
# raw `where` text.
_ENV_VAR_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TODO_WORD_RE = re.compile(r"\bTODO\b")

CONTRACT_V2_TOP_KEYS = frozenset({
    "version", "dataset", "table", "meaning", "kind", "status",
    "inferred_from", "location", "grain", "keys", "partition_key",
    "freshness_sla", "deprecated", "quirks", "write_mode", "columns",
})

COLUMN_V2_KEYS = frozenset({
    "type", "nullable", "meaning", "description", "unit", "currency",
    "currency_column", "size_of", "time_zone", "relative_to",
    "status_column", "values", "pii",
})

METRIC_V1_TOP_KEYS = frozenset({
    "version", "metric", "meaning", "kind", "dataset", "numerator",
    "denominator", "measure", "grain", "unit", "owner",
})

MEASURE_KEYS = frozenset({"agg", "column", "where"})

# Exact-type allowlist for metric `where` AST nodes (sqlglot 30.22.0).
# Never use isinstance(..., exp.Func) because exp.And, exp.Is, exp.Like, etc.
# subclass exp.Func in sqlglot.
ALLOWED_WHERE_NODES = frozenset({
    exp.Column, exp.Identifier, exp.Literal, exp.Null, exp.Boolean,
    exp.Neg, exp.Paren,
    exp.And, exp.Or, exp.Not,
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.In, exp.Between, exp.Is, exp.Like, exp.ILike,
})

_PREDICATE_NODES = frozenset({
    exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE,
    exp.In, exp.Between, exp.Is, exp.Like, exp.ILike,
})


def _yaml_problem(exc) -> str:
    mark = getattr(exc, "problem_mark", None)
    where = f" (line {mark.line + 1})" if mark is not None else ""
    problem = getattr(exc, "problem", None) or str(exc).split("\n")[0]
    return f"{problem}{where}"


def _non_empty_str(val) -> bool:
    return isinstance(val, str) and bool(val.strip())


def _is_ident_char(ch: str) -> bool:
    if ch in ("_", " ", "-", "$"):
        return True
    cat = unicodedata.category(ch)
    return cat[0] == "L" or cat == "Nd" or cat in ("Mn", "Mc")


def is_valid_ident(val) -> bool:
    """True when `val` is a single v2/metric name part (Unicode letters,
    decimal digits, `_`, spaces, `-`, `$`; non-empty and without leading or
    trailing spaces). PR B must quote every identifier when building SQL."""
    if not isinstance(val, str) or not val or val != val.strip():
        return False
    if unicodedata.category(val[0]) in ("Mn", "Mc"):
        return False
    return all(_is_ident_char(ch) for ch in val)


def is_valid_dotted_ident(val) -> bool:
    """True when `val` is one or more `.`-separated `is_valid_ident` parts."""
    if not isinstance(val, str) or not val:
        return False
    parts = val.split(".")
    return bool(parts) and all(is_valid_ident(part) for part in parts)


def is_unconfirmed_grain(grain) -> bool:
    """True only when `grain` contains the word TODO or is 'unknown' (including
    the `unknown  # TODO: ...` form `truequery init` writes)."""
    if not isinstance(grain, str):
        return False
    norm = unicodedata.normalize("NFC", grain.strip())
    return bool(_TODO_WORD_RE.search(norm) or norm.lower() == "unknown")


def _is_safe_rel_path(val: str) -> bool:
    """True when val is a relative path/glob that does not traverse '..' or '~'."""
    if not _non_empty_str(val):
        return False
    norm = val.replace("\\", "/")
    if (norm.startswith("/") or norm.startswith("~")
            or re.match(r"^[A-Za-z]:", norm)):
        return False
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return False
    return ".." not in parts and "~" not in parts


def validate_bases(bases, source: str = "DATA.md tq-config") -> dict[str, dict]:
    """Validate optional `bases:` in DATA.md tq-config.

    Accepts either:
      bases:
        data_root: /abs/or/rel/path
        archive:
          env: ARCHIVE_ROOT
          default: ./data/archive
    Returns normalized {base_name: {"env": str | None, "default": str}}.
    Raises ValueError on invalid format.
    """
    if bases is None:
        return {}
    if not isinstance(bases, dict):
        raise ValueError(f"{source}: bases must be a mapping")
    normalized: dict[str, dict] = {}
    for name, spec in bases.items():
        if not is_valid_ident(name):
            raise ValueError(
                f"{source}: base name {name!r} must be a valid identifier")
        if isinstance(spec, str):
            if not _non_empty_str(spec):
                raise ValueError(
                    f"{source}: bases.{name} must be a non-empty path string")
            normalized[name] = {"env": None, "default": spec}
        elif isinstance(spec, dict):
            unknown = set(spec) - {"env", "default"}
            if unknown:
                raise ValueError(
                    f"{source}: bases.{name} has unknown keys {sorted(unknown)}")
            env_var = spec.get("env")
            default_val = spec.get("default")
            if env_var is not None and (
                    not isinstance(env_var, str)
                    or not _ENV_VAR_RE.match(env_var)):
                raise ValueError(
                    f"{source}: bases.{name}.env must be a valid environment "
                    "variable name")
            if default_val is not None and not _non_empty_str(default_val):
                raise ValueError(
                    f"{source}: bases.{name}.default must be a non-empty string")
            if env_var is None and default_val is None:
                raise ValueError(
                    f"{source}: bases.{name} must set 'default' and/or 'env'")
            normalized[name] = {"env": env_var, "default": default_val}
        else:
            raise ValueError(
                f"{source}: bases.{name} must be a string or mapping")
    return normalized


def validate_keys_identifiers(keys, path: str,
                              columns: dict | None = None) -> None:
    if not isinstance(keys, list):
        raise ValueError(f"{path}: keys must be a list")
    col_set = set(columns.keys()) if isinstance(columns, dict) and columns else None
    for idx, item in enumerate(keys):
        if isinstance(item, str):
            if not is_valid_ident(item):
                raise ValueError(
                    f"{path}: keys[{idx}] ({item!r}) must be a plain column "
                    "identifier")
            if col_set is not None and item not in col_set:
                raise ValueError(
                    f"{path}: keys[{idx}] ({item!r}) is not in columns")
        elif isinstance(item, list):
            if not item:
                raise ValueError(
                    f"{path}: composite key keys[{idx}] must be a non-empty "
                    "list of column names")
            for c_idx, col in enumerate(item):
                if not is_valid_ident(col):
                    raise ValueError(
                        f"{path}: keys[{idx}][{c_idx}] ({col!r}) must be a "
                        "plain column identifier")
                if col_set is not None and col not in col_set:
                    raise ValueError(
                        f"{path}: keys[{idx}][{c_idx}] ({col!r}) is not in "
                        "columns")
        else:
            raise ValueError(
                f"{path}: keys[{idx}] must be a column name or list of "
                "column names")


def validate_partition_key_identifiers(part_key, path: str,
                                       columns: dict | None = None) -> None:
    if part_key is None:
        return
    col_set = set(columns.keys()) if isinstance(columns, dict) and columns else None
    if isinstance(part_key, str):
        if not is_valid_ident(part_key):
            raise ValueError(
                f"{path}: partition_key ({part_key!r}) must be a plain column "
                "identifier")
        if col_set is not None and part_key not in col_set:
            raise ValueError(
                f"{path}: partition_key ({part_key!r}) is not in columns")
    elif isinstance(part_key, list):
        if not part_key:
            raise ValueError(
                f"{path}: partition_key list must contain column names")
        for idx, col in enumerate(part_key):
            if not is_valid_ident(col):
                raise ValueError(
                    f"{path}: partition_key[{idx}] ({col!r}) must be a plain "
                    "column identifier")
            if col_set is not None and col not in col_set:
                raise ValueError(
                    f"{path}: partition_key[{idx}] ({col!r}) is not in "
                    "columns")
    else:
        raise ValueError(
            f"{path}: partition_key must be a string, list of strings, or null")


def _validate_location_v2(loc, kind: str, path: str) -> None:
    if kind == "files":
        if not isinstance(loc, dict):
            raise ValueError(
                f"{path}: location is required and must be a mapping when "
                "kind is 'files'")
        unknown = set(loc) - {"path", "base", "format"}
        if unknown:
            raise ValueError(
                f"{path}: location has unknown keys {sorted(unknown)}")
        loc_path = loc.get("path")
        if not isinstance(loc_path, str) or not _is_safe_rel_path(loc_path):
            raise ValueError(
                f"{path}: location.path must be a non-empty relative path "
                "without '..' or '~'")
        fmt = loc.get("format")
        if fmt not in CONTRACT_V2_FORMATS:
            raise ValueError(
                f"{path}: location.format must be one of "
                f"{list(CONTRACT_V2_FORMATS)}")
        base = loc.get("base")
        if base is not None and not is_valid_ident(base):
            raise ValueError(
                f"{path}: location.base must be a valid base identifier or null")
    else:
        if loc is None:
            return
        if not isinstance(loc, dict):
            raise ValueError(f"{path}: location must be a mapping or null")
        unknown = set(loc) - {"database", "relation", "base"}
        if unknown:
            raise ValueError(
                f"{path}: location has unknown keys {sorted(unknown)}")
        db = loc.get("database")
        if db is not None and (
                not isinstance(db, str) or not _is_safe_rel_path(db)):
            raise ValueError(
                f"{path}: location.database must be a non-empty relative path "
                "without '..' or '~'")
        rel = loc.get("relation")
        if rel is not None and not is_valid_dotted_ident(rel):
            raise ValueError(
                f"{path}: location.relation ({rel!r}) must be a plain "
                "table or schema.table identifier")
        base = loc.get("base")
        if base is not None and not is_valid_ident(base):
            raise ValueError(
                f"{path}: location.base must be a valid base identifier or null")


def _validate_freshness_v2(freshness, columns: dict, path: str) -> None:
    if freshness is None or isinstance(freshness, str):
        return
    if not isinstance(freshness, dict):
        raise ValueError(
            f"{path}: freshness_sla must be a string, mapping, or null")
    unknown = set(freshness) - {"column", "max_age"}
    if unknown:
        raise ValueError(
            f"{path}: freshness_sla has unknown keys {sorted(unknown)}")
    col = freshness.get("column")
    max_age = freshness.get("max_age")
    if not is_valid_ident(col):
        raise ValueError(
            f"{path}: freshness_sla.column ({col!r}) must be a plain column "
            "identifier")
    if not isinstance(max_age, str) or not _DURATION_RE.match(max_age):
        raise ValueError(
            f"{path}: freshness_sla.max_age must match <int><ms|s|min|h|d> "
            f"(got {max_age!r})")
    if columns:
        if col not in columns:
            raise ValueError(
                f"{path}: freshness_sla.column {col!r} is not in columns")
        col_type = (columns.get(col) or {}).get("type")
        if col_type not in ("timestamp", "date"):
            raise ValueError(
                f"{path}: freshness_sla.column {col!r} must have type "
                f"'timestamp' or 'date' (got {col_type!r})")


def _validate_values_v2(values, col_name: str, is_draft: bool,
                        path: str) -> None:
    allowed_means = STATUS_MEANS_DRAFT if is_draft else STATUS_MEANS_CONFIRMED
    if isinstance(values, list):
        if not values:
            raise ValueError(
                f"{path}: columns.{col_name}.values must not be empty")
        for item in values:
            if not isinstance(item, (str, int, float)) or isinstance(item, bool):
                raise ValueError(
                    f"{path}: columns.{col_name}.values items must be strings "
                    "or numbers")
        return
    if isinstance(values, dict):
        if not values:
            raise ValueError(
                f"{path}: columns.{col_name}.values must not be empty")
        for val_key, meta in values.items():
            if not isinstance(val_key, (str, int, float)) or isinstance(
                    val_key, bool):
                raise ValueError(
                    f"{path}: columns.{col_name}.values keys must be strings "
                    "or numbers")
            if meta is None:
                continue
            if not isinstance(meta, dict):
                raise ValueError(
                    f"{path}: columns.{col_name}.values[{val_key!r}] must be "
                    "a mapping or null")
            unknown = set(meta) - {"means", "meaning"}
            if unknown:
                raise ValueError(
                    f"{path}: columns.{col_name}.values[{val_key!r}] has "
                    f"unknown keys {sorted(unknown)}")
            means = meta.get("means")
            if means is not None and means not in allowed_means:
                if means == "TODO" and not is_draft:
                    raise ValueError(
                        f"{path}: columns.{col_name}.values[{val_key!r}] uses "
                        "means: TODO, which is only allowed when contract "
                        "status is 'draft'")
                raise ValueError(
                    f"{path}: columns.{col_name}.values[{val_key!r}].means "
                    f"must be one of {list(allowed_means)}")
            meaning = meta.get("meaning")
            if meaning is not None and not isinstance(meaning, str):
                raise ValueError(
                    f"{path}: columns.{col_name}.values[{val_key!r}].meaning "
                    "must be a string")
        return
    raise ValueError(
        f"{path}: columns.{col_name}.values must be a non-empty list or mapping")


def _validate_columns_v2(columns, is_draft: bool, path: str) -> dict:
    if columns is None:
        return {}
    if not isinstance(columns, dict):
        raise ValueError(f"{path}: columns must be a mapping")
    for col_name, spec in columns.items():
        if not is_valid_ident(col_name):
            raise ValueError(
                f"{path}: column name {col_name!r} must be a plain column "
                "identifier")
        if not isinstance(spec, dict):
            raise ValueError(
                f"{path}: columns.{col_name} must be a mapping")
        unknown = set(spec) - COLUMN_V2_KEYS
        if unknown:
            raise ValueError(
                f"{path}: columns.{col_name} has unknown keys "
                f"{sorted(unknown)}")
        ctype = spec.get("type")
        if ctype is not None and ctype not in COLUMN_TYPES:
            raise ValueError(
                f"{path}: columns.{col_name}.type must be one of "
                f"{list(COLUMN_TYPES)}")
        nullable = spec.get("nullable")
        if nullable is not None and not isinstance(nullable, bool):
            raise ValueError(
                f"{path}: columns.{col_name}.nullable must be a boolean")
        for text_key in ("meaning", "description"):
            val = spec.get(text_key)
            if val is not None and not isinstance(val, str):
                raise ValueError(
                    f"{path}: columns.{col_name}.{text_key} must be a string")
        unit = spec.get("unit")
        if unit is not None:
            if unit not in ALL_UNITS:
                raise ValueError(
                    f"{path}: columns.{col_name}.unit must be one of "
                    f"{list(ALL_UNITS)}")
            if ctype is not None and ctype not in ("integer", "number"):
                raise ValueError(
                    f"{path}: columns.{col_name}.unit {unit!r} requires "
                    f"numeric type 'integer' or 'number' (got {ctype!r})")
        currency = spec.get("currency")
        currency_col = spec.get("currency_column")
        if currency is not None and (
                not isinstance(currency, str)
                or not _CURRENCY_RE.match(currency)):
            raise ValueError(
                f"{path}: columns.{col_name}.currency must be a 3-letter "
                "uppercase ISO-4217 code (e.g. USD)")
        if currency_col is not None and not is_valid_ident(currency_col):
            raise ValueError(
                f"{path}: columns.{col_name}.currency_column must be a "
                "plain column identifier")
        if currency is not None and currency_col is not None:
            raise ValueError(
                f"{path}: columns.{col_name} cannot set both currency and "
                "currency_column")
        if unit in MONEY_UNITS and currency is None and currency_col is None:
            raise ValueError(
                f"{path}: columns.{col_name} with unit {unit!r} requires "
                "currency or currency_column")
        if (currency is not None or currency_col is not None) and (
                unit not in MONEY_UNITS):
            raise ValueError(
                f"{path}: columns.{col_name} with currency/currency_column "
                "requires unit 'minor' or 'major'")
        tz = spec.get("time_zone")
        if tz is not None:
            if not isinstance(tz, str) or (
                    tz not in ("UTC", "naive")
                    and not _LOCAL_TZ_RE.match(tz)):
                raise ValueError(
                    f"{path}: columns.{col_name}.time_zone must be 'UTC', "
                    "'naive', or 'local:<Area>/<Location>'")
            if ctype != "timestamp":
                raise ValueError(
                    f"{path}: columns.{col_name}.time_zone requires "
                    f"type: timestamp (got {ctype!r})")
        rel_to = spec.get("relative_to")
        if rel_to is not None:
            if not is_valid_ident(rel_to):
                raise ValueError(
                    f"{path}: columns.{col_name}.relative_to must be a "
                    "valid base identifier")
            if ctype != "path":
                raise ValueError(
                    f"{path}: columns.{col_name}.relative_to requires "
                    "type: path")
        size_of = spec.get("size_of")
        if size_of is not None:
            if not is_valid_ident(size_of):
                raise ValueError(
                    f"{path}: columns.{col_name}.size_of must be a plain "
                    "column identifier")
            if unit not in SIZE_UNITS:
                raise ValueError(
                    f"{path}: columns.{col_name}.size_of requires a size "
                    f"unit {list(SIZE_UNITS)} (got {unit!r})")
            if ctype is not None and ctype not in ("integer", "number"):
                raise ValueError(
                    f"{path}: columns.{col_name}.size_of requires numeric "
                    "type 'integer' or 'number'")
        status_col = spec.get("status_column")
        if status_col is not None:
            if not is_valid_ident(status_col):
                raise ValueError(
                    f"{path}: columns.{col_name}.status_column must be a "
                    "plain column identifier")
            if ctype != "path":
                raise ValueError(
                    f"{path}: columns.{col_name}.status_column requires "
                    f"type: path (got {ctype!r})")
        if "values" in spec and spec["values"] is not None:
            _validate_values_v2(spec["values"], col_name, is_draft, path)
        pii = spec.get("pii")
        if pii is not None and not isinstance(pii, bool):
            raise ValueError(
                f"{path}: columns.{col_name}.pii must be a boolean")

    # Cross-column checks within the same contract:
    for col_name, spec in columns.items():
        currency_col = spec.get("currency_column")
        if currency_col is not None and currency_col not in columns:
            raise ValueError(
                f"{path}: columns.{col_name}.currency_column references "
                f"unknown column {currency_col!r}")
        size_of = spec.get("size_of")
        if size_of is not None:
            target = columns.get(size_of)
            if not isinstance(target, dict) or target.get("type") != "path":
                raise ValueError(
                    f"{path}: columns.{col_name}.size_of references "
                    f"{size_of!r}, which must be a column with type: path")
        status_col = spec.get("status_column")
        if status_col is not None:
            target = columns.get(status_col)
            t_values = target.get("values") if isinstance(target, dict) else None
            if not isinstance(t_values, dict) or not t_values:
                raise ValueError(
                    f"{path}: columns.{col_name}.status_column references "
                    f"{status_col!r}, which must be a column with values "
                    "mapping each status to `means`")
            missing_means = [
                k for k, v in t_values.items()
                if not isinstance(v, dict) or not v.get("means")
            ]
            if missing_means:
                raise ValueError(
                    f"{path}: columns.{col_name}.status_column references "
                    f"{status_col!r}, whose values {missing_means} are "
                    "missing `means`")
    return columns


def validate_contract_v2(raw: dict, path: str,
                         error_cls: type[Exception] = ValueError) -> dict:
    """Validate a version: 2 contract dict and normalize dataset/table."""
    try:
        return _validate_contract_v2_inner(raw, path)
    except ValueError as exc:
        if error_cls is ValueError:
            raise
        raise error_cls(str(exc)) from None


def _validate_contract_v2_inner(raw: dict, path: str) -> dict:
    unknown = set(raw) - CONTRACT_V2_TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    ver = raw.get("version")
    if isinstance(ver, bool) or ver != 2:
        raise ValueError(f"{path}: version must be 2")

    dataset = raw.get("dataset")
    table = raw.get("table")
    if dataset is not None and table is not None and dataset != table:
        raise ValueError(
            f"{path}: dataset ({dataset!r}) and table ({table!r}) must match "
            "when both are given")
    name = dataset if dataset is not None else table
    if not is_valid_dotted_ident(name):
        raise ValueError(
            f"{path}: dataset (or table) is required and must be a plain "
            "table or schema.table identifier")

    kind = raw.get("kind", "table")
    if kind not in CONTRACT_V2_KINDS:
        raise ValueError(
            f"{path}: kind must be one of {list(CONTRACT_V2_KINDS)}")

    status = raw.get("status", "confirmed")
    if status not in CONTRACT_V2_STATUSES:
        raise ValueError(
            f"{path}: status must be one of {list(CONTRACT_V2_STATUSES)}")
    inferred_from = raw.get("inferred_from")
    if status == "draft":
        if not _non_empty_str(inferred_from):
            raise ValueError(
                f"{path}: inferred_from is required when status is 'draft'")
    elif inferred_from is not None and not isinstance(inferred_from, str):
        raise ValueError(f"{path}: inferred_from must be a string or null")

    grain = raw.get("grain")
    if "grain" not in raw or not _non_empty_str(grain):
        raise ValueError(f"{path}: grain must be a non-empty string")
    if status != "draft" and is_unconfirmed_grain(grain):
        raise ValueError(
            f"{path}: grain {grain!r} is unconfirmed (contains TODO or is "
            "'unknown'); confirm the grain or set status: draft")

    if "keys" not in raw:
        raise ValueError(f"{path}: missing keys ['keys']")

    for opt_str in ("meaning", "quirks"):
        val = raw.get(opt_str)
        if val is not None and not isinstance(val, str):
            raise ValueError(f"{path}: {opt_str} must be a string or null")

    deprecated = raw.get("deprecated", False)
    if not isinstance(deprecated, bool):
        raise ValueError(f"{path}: deprecated must be a boolean")

    write_mode = raw.get("write_mode")
    if write_mode is not None and write_mode not in CONTRACT_V2_WRITE_MODES:
        raise ValueError(
            f"{path}: write_mode must be one of "
            f"{list(CONTRACT_V2_WRITE_MODES)}")

    _validate_location_v2(raw.get("location"), kind, path)
    columns = _validate_columns_v2(
        raw.get("columns"), status == "draft", path)
    validate_keys_identifiers(raw["keys"], path, columns=columns)
    validate_partition_key_identifiers(
        raw.get("partition_key"), path, columns=columns)
    _validate_freshness_v2(raw.get("freshness_sla"), columns, path)

    out = dict(raw)
    out["version"] = 2
    out["dataset"] = name
    out["table"] = name
    out["kind"] = kind
    out["status"] = status
    out["deprecated"] = deprecated
    return out


# -- Metrics (metrics/*.yaml) -------------------------------------------------

def _check_boolean_structure(node: exp.Expression, path: str,
                             field_name: str) -> None:
    """Verify `node` is a valid boolean filter tree where every leaf is an
    comparison/predicate (never a bare Column, bare Boolean, or bare Literal)."""
    ntype = type(node)
    if ntype is exp.Paren:
        _check_boolean_structure(node.this, path, field_name)
        return
    if ntype is exp.Not:
        _check_boolean_structure(node.this, path, field_name)
        return
    if ntype in (exp.And, exp.Or):
        _check_boolean_structure(node.this, path, field_name)
        _check_boolean_structure(node.expression, path, field_name)
        return
    if ntype in _PREDICATE_NODES:
        return
    if ntype is exp.Column:
        raise ValueError(
            f"{path}: {field_name}.where bare column {node.sql()!r} is not "
            "allowed as a condition; use an explicit comparison (e.g. "
            f"`{node.sql()} = TRUE` or `{node.sql()} IS TRUE`)")
    if ntype is exp.Boolean:
        raise ValueError(
            f"{path}: {field_name}.where boolean literal {node.sql()!r} is "
            "only allowed on the right side of a comparison or IS")
    raise ValueError(
        f"{path}: {field_name}.where must be a filter predicate, not "
        f"{ntype.__name__}")


def validate_metric_where(where_str: str, path: str, field_name: str,
                          allowed_columns: set[str] | None = None) -> list[str]:
    """Validate a metric `where` clause against the exact-type AST allowlist.

    Returns the sorted list of column names referenced in `where_str`.
    Raises ValueError on any disallowed syntax, function call, subquery,
    table qualifier, SQL comment, bare column/boolean, or unknown column.

    NOTE FOR PR B: Metric SQL must be built from the parsed sqlglot AST tree
    with every identifier quoted, never by splicing the raw `where_str` text.
    """
    if not _non_empty_str(where_str):
        raise ValueError(f"{path}: {field_name}.where must be a non-empty string")
    try:
        stmts = [s for s in sqlglot.parse(where_str) if s is not None]
    except Exception as exc:
        raise ValueError(
            f"{path}: {field_name}.where could not parse as a filter "
            f"condition: {exc}") from None
    if len(stmts) != 1:
        raise ValueError(
            f"{path}: {field_name}.where must be a single filter condition "
            "(no ';' or multiple statements)")
    try:
        node = sqlglot.parse_one(where_str, into=exp.Condition)
    except Exception as exc:
        raise ValueError(
            f"{path}: {field_name}.where could not parse as a filter "
            f"condition: {exc}") from None
    if node is None:
        raise ValueError(
            f"{path}: {field_name}.where could not parse as a filter condition")

    nodes = list(node.walk())
    for sub in nodes:
        ntype = type(sub)
        if getattr(sub, "comments", None):
            raise ValueError(
                f"{path}: {field_name}.where must not contain SQL comments")
        if ntype is exp.In and (
                sub.args.get("query") is not None
                or sub.args.get("field") is not None):
            raise ValueError(
                f"{path}: {field_name}.where IN (...) must use a literal "
                "list, not a subquery")
        if ntype not in ALLOWED_WHERE_NODES:
            raise ValueError(
                f"{path}: {field_name}.where contains disallowed expression "
                f"{ntype.__name__} (only comparisons, AND/OR/NOT, IN, "
                "BETWEEN, IS NULL, and LIKE are allowed)")
        if ntype is exp.Neg:
            if (type(sub.this) is not exp.Literal
                    or sub.this.is_string):
                raise ValueError(
                    f"{path}: {field_name}.where unary '-' is only allowed "
                    "on numeric literals")

    _check_boolean_structure(node, path, field_name)

    referenced_cols: set[str] = set()
    for sub in nodes:
        ntype = type(sub)
        if ntype is exp.Column:
            if sub.table or sub.db or sub.catalog:
                raise ValueError(
                    f"{path}: {field_name}.where column {sub.sql()!r} must "
                    "not be table-qualified")
            col_name = sub.name
            if not is_valid_ident(col_name):
                raise ValueError(
                    f"{path}: {field_name}.where column {col_name!r} must "
                    "be a plain column identifier")
            referenced_cols.add(col_name)
        elif ntype is exp.Boolean:
            parent = sub.parent
            if not (type(parent) in (exp.EQ, exp.NEQ, exp.Is)
                    and parent.expression is sub
                    and type(parent.this) is exp.Column):
                raise ValueError(
                    f"{path}: {field_name}.where boolean literal "
                    f"{sub.sql()!r} is only allowed as the right side of a "
                    "column comparison or IS")
        elif ntype in (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE):
            if (type(sub.this) is not exp.Column
                    and type(sub.expression) is not exp.Column):
                raise ValueError(
                    f"{path}: {field_name}.where comparison {sub.sql()!r} "
                    "must reference a column")
        elif ntype is exp.Between:
            if type(sub.this) is not exp.Column:
                raise ValueError(
                    f"{path}: {field_name}.where BETWEEN left side must be "
                    "a column")
        elif ntype is exp.Is:
            if type(sub.this) is not exp.Column:
                raise ValueError(
                    f"{path}: {field_name}.where IS left side must be "
                    "a column")
            if type(sub.expression) not in (exp.Null, exp.Boolean):
                raise ValueError(
                    f"{path}: {field_name}.where IS right side must be "
                    "NULL, TRUE, or FALSE")
        elif ntype is exp.In:
            if type(sub.this) is not exp.Column:
                raise ValueError(
                    f"{path}: {field_name}.where IN left side must be "
                    "a column")
            exprs = sub.expressions
            if not exprs:
                raise ValueError(
                    f"{path}: {field_name}.where IN (...) list must not be empty")
            for item in exprs:
                if type(item) is exp.Literal:
                    continue
                if (type(item) is exp.Neg
                        and type(item.this) is exp.Literal
                        and not item.this.is_string):
                    continue
                raise ValueError(
                    f"{path}: {field_name}.where IN (...) items must be "
                    f"literals (got {type(item).__name__})")
        elif ntype in (exp.Like, exp.ILike):
            if type(sub.this) is not exp.Column:
                raise ValueError(
                    f"{path}: {field_name}.where LIKE left side must be a "
                    "column")
            if (type(sub.expression) is not exp.Literal
                    or not sub.expression.is_string):
                raise ValueError(
                    f"{path}: {field_name}.where LIKE pattern must be a "
                    "string literal")
            esc = sub.args.get("escape")
            if esc is not None and (
                    type(esc) is not exp.Literal or not esc.is_string):
                raise ValueError(
                    f"{path}: {field_name}.where LIKE ESCAPE must be a "
                    "string literal")

    if not referenced_cols:
        raise ValueError(
            f"{path}: {field_name}.where must reference at least one column")
    if allowed_columns is not None:
        unknown = sorted(c for c in referenced_cols if c not in allowed_columns)
        if unknown:
            raise ValueError(
                f"{path}: {field_name}.where references unknown column(s) "
                f"{unknown}")
    return sorted(referenced_cols)


def _validate_measure_block(block, field_name: str,
                            allowed_columns: set[str] | None,
                            path: str) -> list[str]:
    if not isinstance(block, dict):
        raise ValueError(f"{path}: {field_name} must be a mapping")
    unknown = set(block) - MEASURE_KEYS
    if unknown:
        raise ValueError(
            f"{path}: {field_name} has unknown keys {sorted(unknown)}")
    agg = block.get("agg")
    if agg not in METRIC_AGGS:
        raise ValueError(
            f"{path}: {field_name}.agg must be one of {list(METRIC_AGGS)}")
    col = block.get("column")
    if not _non_empty_str(col):
        raise ValueError(
            f"{path}: {field_name}.column must be a non-empty string")
    if col == "*":
        if agg != "count":
            raise ValueError(
                f"{path}: {field_name}.column '*' is only valid when agg is "
                "'count'")
    elif not is_valid_ident(col):
        raise ValueError(
            f"{path}: {field_name}.column ({col!r}) must be a plain column "
            "identifier")
    cols_used: set[str] = set()
    if col != "*":
        cols_used.add(col)
        if allowed_columns is not None and col not in allowed_columns:
            raise ValueError(
                f"{path}: {field_name}.column {col!r} is not in dataset columns")
    where_str = block.get("where")
    if where_str is not None:
        where_cols = validate_metric_where(
            where_str, path, field_name, allowed_columns=allowed_columns)
        cols_used.update(where_cols)
    return sorted(cols_used)


def validate_metric(raw: dict, path: str,
                    allowed_columns: set[str] | None = None) -> dict:
    """Validate a metric v1 dictionary."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: top level must be a mapping")
    unknown = set(raw) - METRIC_V1_TOP_KEYS
    if unknown:
        raise ValueError(f"{path}: unknown keys {sorted(unknown)}")
    version = raw.get("version", 1)
    if isinstance(version, bool) or version != 1:
        raise ValueError(f"{path}: version must be 1")
    metric_name = raw.get("metric")
    if not is_valid_ident(metric_name):
        raise ValueError(
            f"{path}: metric ({metric_name!r}) must be a plain identifier")
    dataset = raw.get("dataset")
    if not is_valid_dotted_ident(dataset):
        raise ValueError(
            f"{path}: dataset ({dataset!r}) must be a plain table or "
            "schema.table identifier")
    kind = raw.get("kind")
    if kind not in METRIC_KINDS:
        raise ValueError(
            f"{path}: kind must be one of {list(METRIC_KINDS)}")

    for opt_str in ("meaning", "owner"):
        val = raw.get(opt_str)
        if val is not None and not isinstance(val, str):
            raise ValueError(f"{path}: {opt_str} must be a string or null")

    unit = raw.get("unit")
    if unit is not None and unit not in ALL_UNITS:
        raise ValueError(
            f"{path}: unit must be one of {list(ALL_UNITS)}")

    grain = raw.get("grain")
    if grain is not None:
        if isinstance(grain, str):
            if not grain.strip():
                raise ValueError(f"{path}: grain must not be an empty string")
        elif isinstance(grain, list):
            if not grain or not all(is_valid_ident(g) for g in grain):
                raise ValueError(
                    f"{path}: grain list must contain plain column identifiers")
        else:
            raise ValueError(
                f"{path}: grain must be a string, list of column identifiers, "
                "or null")

    if kind == "ratio":
        if "measure" in raw:
            raise ValueError(
                f"{path}: kind 'ratio' uses numerator and denominator, "
                "not measure")
        if "numerator" not in raw or "denominator" not in raw:
            raise ValueError(
                f"{path}: kind 'ratio' requires both numerator and denominator")
        _validate_measure_block(
            raw["numerator"], "numerator", allowed_columns, path)
        _validate_measure_block(
            raw["denominator"], "denominator", allowed_columns, path)
    else:
        if "numerator" in raw or "denominator" in raw:
            raise ValueError(
                f"{path}: kind 'simple' uses measure, not numerator/"
                "denominator")
        if "measure" not in raw:
            raise ValueError(f"{path}: kind 'simple' requires measure")
        _validate_measure_block(
            raw["measure"], "measure", allowed_columns, path)

    out = dict(raw)
    out["version"] = 1
    return out


def load_metric(path: str,
                allowed_columns: set[str] | None = None) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: not valid YAML: {_yaml_problem(exc)}") \
            from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"{path}: cannot read file: {exc}") from None
    return validate_metric(raw, path, allowed_columns=allowed_columns)


def check_contract_bases(contract: dict, declared_bases: dict,
                         rel_path: str) -> list[str]:
    """Verify every `location.base` and `columns.<col>.relative_to` in a v2
    contract exists in `declared_bases` (from DATA.md tq-config `bases:`)."""
    problems: list[str] = []
    if contract.get("version") != 2:
        return problems
    loc = contract.get("location")
    if isinstance(loc, dict):
        base = loc.get("base")
        if base and base not in declared_bases:
            problems.append(
                f"{rel_path}: location.base references undeclared base "
                f"{base!r} (declare it under `bases:` in DATA.md tq-config)")
    for col_name, spec in (contract.get("columns") or {}).items():
        if not isinstance(spec, dict):
            continue
        rel_to = spec.get("relative_to")
        if rel_to and rel_to not in declared_bases:
            problems.append(
                f"{rel_path}: columns.{col_name}.relative_to references "
                f"undeclared base {rel_to!r} (declare it under `bases:` "
                "in DATA.md tq-config)")
    return problems


def load_metrics_reporting(
    root: str,
    contracts: dict[str, dict] | None = None,
    match_fn=None,
) -> tuple[dict[str, dict], list[str]]:
    """({lower(metric): metric_dict}, [problem, ...]) for metrics/*.yaml."""
    out: dict[str, dict] = {}
    seen_files: dict[str, str] = {}
    problems: list[str] = []
    mdir = os.path.join(root, "metrics")
    if not os.path.isdir(mdir):
        return out, problems
    for name in sorted(os.listdir(mdir)):
        if not name.endswith((".yaml", ".yml")):
            continue
        full_path = os.path.join(mdir, name)
        rel_path = f"metrics/{name}"
        try:
            metric = load_metric(full_path)
        except (OSError, ValueError) as exc:
            problems.append(str(exc).replace(mdir + os.sep, "metrics/"))
            continue
        if contracts is not None:
            m_problems = check_metric_against_contracts(
                metric, contracts, rel_path, match_fn=match_fn)
            if m_problems:
                problems.extend(m_problems)
                continue
        m_key = metric["metric"].lower()
        if m_key in seen_files:
            prev = seen_files[m_key]
            problems.append(
                f"{rel_path}: duplicate metric {metric['metric']!r} "
                f"(already defined in {prev})")
            out.pop(m_key, None)
            continue
        seen_files[m_key] = rel_path
        out[m_key] = metric
    return out, problems


def check_metric_against_contracts(
    metric: dict,
    contracts: dict[str, dict],
    rel_path: str,
    match_fn=None,
) -> list[str]:
    """Cross-file validation of one loaded metric against project contracts."""
    problems: list[str] = []
    ds = metric["dataset"]
    key = (match_fn(ds, contracts) if match_fn is not None
           else (ds.lower() if ds.lower() in contracts else None))
    if key is None:
        problems.append(
            f"{rel_path}: dataset {ds!r} has no contract in contracts/")
        return problems
    contract = contracts[key]
    cols = contract.get("columns")
    if isinstance(cols, dict) and cols:
        allowed = set(cols.keys())
        try:
            validate_metric(
                {k: v for k, v in metric.items() if not k.startswith("_")},
                rel_path,
                allowed_columns=allowed,
            )
        except ValueError as exc:
            problems.append(str(exc))
    return problems
