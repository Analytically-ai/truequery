"""`truequery scaffold`: a pipeline's boundaries, generated from contracts.

For each contract it writes two files of plain pandas:
- `<dataset>_io.py`: `read_<dataset>(path)` (a typed reader: a dtype for
  every column, timestamps parsed in the declared zone, only empty fields
  missing), `coerce_<dataset>(df)` (the same types for a frame read some
  other way) and `check_<dataset>(df)` (keys unique and filled, accepted
  values, types and time zones; it raises ValueError naming every problem);
- `test_<dataset>_io.py`: pytest tests on synthetic rows made from the
  contract, never from real data.

The generated code never imports TrueQuery. It's a template filled from
the contract, so the same contract gives the same bytes. Files are never
written over without `--force`.
"""

from __future__ import annotations

import keyword
import os
import re
import unicodedata

from truequery import contract_v2 as C2
from truequery import project as P

DEFAULT_OUT = "pipeline"

DTYPES = {"string": "string", "path": "string", "integer": "Int64",
          "number": "Float64", "boolean": "boolean", "timestamp": "string",
          "date": "string"}
TYPE_WORDS = {"string": "text", "path": "a path", "integer": "a whole number",
              "number": "a number", "boolean": "true/false",
              "timestamp": "a timestamp", "date": "a date"}


class ScaffoldError(ValueError):
    """A contract that can't be scaffolded, or a file in the way."""


def ident(name: str) -> str:
    """A Python identifier for a dataset name, the same on every OS."""
    ascii_text = unicodedata.normalize("NFKD", str(name)).encode(
        "ascii", "ignore").decode("ascii")
    out = re.sub(r"[^a-z0-9_]+", "_", ascii_text.lower())
    out = re.sub(r"_+", "_", out).strip("_") or "dataset"
    if out[0].isdigit():
        out = "d_" + out
    if keyword.iskeyword(out):
        out += "_"
    return out


def _py(value) -> str:
    """A Python literal for a str, int, float, bool or None."""
    if isinstance(value, str):
        if all(32 <= ord(ch) < 127 for ch in value) and '"' not in value \
                and "\\" not in value:
            return f'"{value}"'
        return repr(value)
    return repr(value)


def _py_list(items) -> str:
    return "[" + ", ".join(_py(i) for i in items) + "]"


def _key_cols(key) -> list[str]:
    return list(key) if isinstance(key, list) else [key]


# -- the contract, as the generator needs it ---------------------------------

def load(path: str) -> dict:
    """The contract at `path`, or ScaffoldError saying why it can't be
    scaffolded."""
    try:
        contract = P.load_contract(path)
    except (OSError, ValueError) as exc:
        raise ScaffoldError(str(exc)) from None
    if contract.get("version") != 2:
        raise ScaffoldError(
            f"{path}: scaffold needs a version 2 contract (this one is "
            "version 1, which has no column types). Add `version: 2` and "
            "`columns:` with a type for each column; see docs/data.md")
    if not contract.get("columns"):
        raise ScaffoldError(
            f"{path}: the contract lists no columns, so there's nothing to "
            "type or check. Add `columns:` with a type for each column")
    return contract


def spec_of(contract: dict, path: str) -> dict:
    name = str(contract.get("dataset") or contract.get("table"))
    columns = contract.get("columns") or {}
    loc = contract.get("location") if isinstance(
        contract.get("location"), dict) else {}
    kind = contract.get("kind") or "table"
    keys = [_key_cols(k) for k in contract.get("keys") or []]
    types, zones, values, nullable_values, not_null = {}, {}, {}, {}, []
    dates, notes = [], {}
    for col, spec in columns.items():
        spec = spec or {}
        ctype = spec.get("type")
        if ctype:
            types[col] = ctype
        if ctype == "timestamp":
            zones[col] = spec.get("time_zone")
        if ctype == "date":
            dates.append(col)
        if spec.get("nullable") is False:
            not_null.append(col)
        vals = spec.get("values")
        if vals:
            items = list(vals) if isinstance(vals, (list, dict)) else []
            if ctype in (None, "string", "path"):
                items = [str(v) for v in items]
            values[col] = items
            nullable_values[col] = spec.get("nullable") is True
        notes[col] = _column_note(spec)
    for key in keys:
        for col in key:
            if col not in not_null:
                not_null.append(col)
    partitions = []
    if kind == "files":
        for part in str(loc.get("path") or "").split("/")[:-1]:
            match = re.match(r"^([^=]+)=\*$", part)
            if match and match.group(1) in columns:
                partitions.append(match.group(1))
    return {
        "name": name, "ident": ident(name), "path": _slash(path),
        "kind": kind, "format": loc.get("format") if kind == "files" else None,
        "location": loc.get("path"), "base": loc.get("base"),
        "delimiter": loc.get("delimiter") or ",",
        "database": loc.get("database"), "relation": loc.get("relation"),
        "columns": list(columns), "types": types, "zones": zones,
        "dates": dates, "keys": keys, "not_null": not_null,
        "values": values, "nullable_values": nullable_values,
        "notes": notes, "partitions": partitions,
        "draft": contract.get("status") == "draft"
        or C2.is_unconfirmed_grain(contract.get("grain")),
        "inferred_from": contract.get("inferred_from"),
        "grain": contract.get("grain"),
        "write_mode": contract.get("write_mode"),
        "draft_columns": [c for c, s in columns.items()
                          if (s or {}).get("status") == "draft"],
        # nullable: true (and not part of a key): a file may lack it.
        "optional": [c for c, s in columns.items()
                     if (s or {}).get("nullable") is True
                     and c not in {k for key in keys for k in key}],
    }


def _column_note(spec: dict) -> str:
    bits = [TYPE_WORDS.get(spec.get("type"), "no type declared")]
    if spec.get("time_zone"):
        tz = spec["time_zone"]
        bits.append("UTC" if tz == "UTC" else "no time zone" if tz == "naive"
                    else f"local time in {tz.split(':', 1)[1]}")
    if spec.get("unit"):
        unit = spec["unit"]
        if spec.get("currency"):
            unit += f" {spec['currency']}"
        elif spec.get("currency_column"):
            unit += f" (currency in {spec['currency_column']})"
        bits.append(f"unit {unit}")
    if spec.get("relative_to"):
        bits.append(f"relative to base {spec['relative_to']}")
    if spec.get("nullable") is False:
        bits.append("never empty")
    if spec.get("pii"):
        bits.append("personal data")
    if spec.get("status") == "draft":
        bits.append("draft")
    vals = spec.get("values")
    if isinstance(vals, dict):
        means = [f"{v}={(m or {}).get('means')}" for v, m in vals.items()
                 if isinstance(m, dict) and m.get("means")]
        if means:
            bits.append("values mean: " + ", ".join(means))
    return "; ".join(bits)


def _slash(path: str) -> str:
    return path.replace(os.sep, "/")


# -- the boundary module -----------------------------------------------------

def _wrap(text: str, width: int = 74, prefix: str = "# ") -> list[str]:
    words, lines, cur = text.split(), [], ""
    for word in words:
        if cur and len(cur) + 1 + len(word) > width:
            lines.append(prefix + cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        lines.append(prefix + cur)
    return lines


def _safe_text(text) -> str:
    return " ".join("".join(ch if ch.isprintable() else " " for ch in
                            str(text)).split())


def _doc_path(path) -> str:
    """A contract path for a docstring or comment: printable, `/` only,
    and no quote or backslash, so it can never close the string."""
    return _safe_text(str(path).replace("\\", "/").replace('"', "'"))


def _entry(lead: str, text: str, width: int = 79) -> list[str]:
    """`lead` + a string literal + ",", split across lines when long."""
    one = f"{lead}{_py(text)},"
    if len(one) <= width:
        return [one]
    pad = " " * 8
    words, chunks, cur = text.split(" "), [], ""
    for word in words:
        piece = f"{cur} {word}" if cur else word
        if cur and len(pad) + len(_py(piece + " ")) > width:
            chunks.append(cur + " ")
            cur = word
        else:
            cur = piece
    chunks.append(cur)
    return ([lead + "("] + [pad + _py(c) for c in chunks]
            + [" " * (len(lead) - len(lead.lstrip())) + "),"])


def _row_lines(row: list, width: int = 79) -> list[str]:
    one = "    " + _py_list(row) + ","
    if len(one) <= width:
        return [one]
    return ["    ["] + [f"        {_py(v)}," for v in row] + ["    ],"]


def _dict_block(name: str, items: list[tuple[str, str]]) -> list[str]:
    if not items:
        return [f"{name}: dict = {{}}"]
    out = [f"{name} = {{"]
    for key, value in items:
        line = f"    {_py(key)}: {value},"
        if len(line) > 79 and value.startswith("[") and value.endswith("]"):
            out.append(f"    {_py(key)}: [")
            out += [f"        {item.strip()}," for item in
                    _split_list(value[1:-1])]
            out.append("    ],")
        else:
            out.append(line)
    return out + ["}"]


def _split_list(inner: str) -> list[str]:
    """Split a rendered list body on its top-level commas."""
    import ast as _ast
    return [_py(v) for v in _ast.literal_eval("[" + inner + "]")]


def render_module(sp: dict) -> str:
    out: list[str] = []
    out += _wrap(f'"""Boundaries for the {_safe_text(sp["name"])} dataset, '
                 f"generated by `truequery scaffold` from {_doc_path(sp['path'])}.",
                 prefix="")
    out.append("")
    out.append("Plain pandas: this file never imports TrueQuery. Regenerate it "
               "when the")
    out.append("contract changes (`truequery scaffold <contract> --force`), and "
               "write the")
    out.append("transform in your own module, between the read and the check.")
    out.append('"""')
    if sp["draft"]:
        src = _safe_text(sp["inferred_from"] or "its grain is unconfirmed")
        out += _wrap(f"DRAFT: {_doc_path(sp['path'])} is a draft contract "
                     f"({src}). Its "
                     "facts aren't confirmed yet, so neither is this code: "
                     "confirm them, then regenerate.")
        if sp["draft_columns"]:
            out += _wrap("Columns still to confirm: "
                         + ", ".join(sp["draft_columns"]) + ".")
    out.append("")
    out.append("from __future__ import annotations")
    out.append("")
    stdlib = []
    if sp["partitions"]:
        stdlib.append("import os")
    if sp["kind"] == "files" and sp["format"] == "csv":
        stdlib.append("import warnings")
    if stdlib:
        out += stdlib + [""]
    out.append("import pandas as pd")
    out.append("")
    out.append(f"DATASET = {_py(sp['name'])}")
    if sp["kind"] == "files":
        where = (f"under base {sp['base']} (see bases: in DATA.md)"
                 if sp["base"] else "relative to the project folder")
        out.append(f"LOCATION = {_py(sp['location'])}  # {where}")
    out.append("")
    out.append("# The contract's columns: type; zone, unit and meaning as "
               "declared.")
    out.append("COLUMNS = {")
    for col in sp["columns"]:
        out += _entry(f"    {_py(col)}: ", sp["notes"][col])
    out.append("}")
    out.append("# Read every column as these dtypes. Timestamps and dates "
               "are read as")
    out.append("# text, then parsed in their declared zone by "
               "_parse_timestamps.")
    out.append("DTYPES = {")
    for col in sp["columns"]:
        out.append(f"    {_py(col)}: "
                   f"{_py(DTYPES.get(sp['types'].get(col), 'string'))},")
    out.append("}")
    out += _dict_block("TYPES", [(col, _py(kind))
                                 for col, kind in sp["types"].items()])
    out.append("KEYS = [" + ", ".join(_py_list(k) for k in sp["keys"]) + "]")
    out.append(f"NOT_NULL = {_py_list(sp['not_null'])}")
    out += _dict_block("ACCEPTED_VALUES", [
        (col, _py_list(vals)) for col, vals in sp["values"].items()])
    out += _dict_block("EMPTY_ALLOWED", [
        (col, repr(ok)) for col, ok in sp["nullable_values"].items()])
    out += _dict_block("TIME_ZONES", [
        (col, _py(zone)) for col, zone in sp["zones"].items()])
    out.append(f"DATES = {_py_list(sp['dates'])}")
    if sp["partitions"]:
        out.append(f"PARTITIONS = {_py_list(sp['partitions'])}")
    if sp["kind"] == "files" and sp["optional"]:
        out.append("# Declared nullable: true; a file without the column "
                   "reads as empty.")
        out.append(f"OPTIONAL = {_py_list(sp['optional'])}")
    out.append("")
    out.append(HELPERS.rstrip("\n"))
    if sp["kind"] == "files":
        out += _reader(sp)
    out += _coerce(sp)
    out += _check(sp)
    return "\n".join(out).rstrip("\n") + "\n"


HELPERS = '''_UTC_MARK = r"(?:[Zz]|[+-]00(?::?00)?)$"
_ANY_MARK = r"(?:[Zz]|[+-]\\d{2}(?::?\\d{2})?)$"


def _parse_timestamps(values: pd.Series, column: str, zone) -> pd.Series:
    """Parse `values` in the contract's zone, or raise ValueError when a
    value says another zone (a naive value in a UTC column is never read
    as UTC)."""
    if pd.api.types.is_datetime64_any_dtype(values.dtype):
        aware = getattr(values.dtype, "tz", None) is not None
        if zone == "UTC" and not aware:
            raise ValueError(f"{column}: timestamps have no time zone, but "
                             "the contract says UTC")
        if zone == "naive" and aware:
            raise ValueError(f"{column}: timestamps have a time zone, but "
                             "the contract says they have none")
        if zone == "UTC":
            return values.dt.tz_convert("UTC")
        if zone and zone.startswith("local:"):
            local = zone.split(":", 1)[1]
            return (values.dt.tz_convert(local) if aware
                    else values.dt.tz_localize(local))
        return values
    # `z` is UTC as well as `Z`; pandas' ISO8601 parser takes only `Z`.
    text = values.astype("string").str.strip().str.replace(
        r"z$", "Z", regex=True)
    # The marker is after the date (YYYY-MM-DD), so a date's own dashes
    # never count as an offset.
    tail = text.str.slice(10).dropna()
    marked = tail.str.contains(_ANY_MARK, regex=True)
    if zone == "UTC":
        wrong = int((~tail.str.contains(_UTC_MARK, regex=True)).sum())
        if wrong:
            raise ValueError(f"{column}: {wrong} value(s) have no UTC marker "
                             "(Z or +00:00), but the contract says UTC")
        return pd.to_datetime(text, utc=True, format="ISO8601")
    if zone == "naive":
        if int(marked.sum()):
            raise ValueError(f"{column}: {int(marked.sum())} value(s) carry "
                             "a time zone, but the contract says none")
        return pd.to_datetime(text, format="ISO8601")
    if zone and zone.startswith("local:"):
        local = zone.split(":", 1)[1]
        utc_marked = int(tail.str.contains(r"[Zz]$", regex=True).sum())
        if utc_marked:
            raise ValueError(f"{column}: {utc_marked} value(s) are marked "
                             f"UTC, but the contract says local time in "
                             f"{local}")
        if int(marked.sum()) == 0:
            return pd.to_datetime(text, format="ISO8601").dt.tz_localize(
                local, ambiguous="raise", nonexistent="raise")
        if int(marked.sum()) != len(tail):
            raise ValueError(f"{column}: some values carry an offset and "
                             "some don't; the contract says local time in "
                             f"{local}")
        return pd.to_datetime(text, utc=True,
                              format="ISO8601").dt.tz_convert(local)
    # No zone declared: parse as written (mixed zones raise).
    return pd.to_datetime(text, format="ISO8601")


def _has_type(values: pd.Series, kind: str) -> bool:
    types = pd.api.types
    if kind in ("string", "path"):
        return types.is_string_dtype(values)
    if kind == "integer":
        return types.is_integer_dtype(values.dtype)
    if kind == "number":
        return (types.is_numeric_dtype(values.dtype)
                and not types.is_bool_dtype(values.dtype))
    if kind == "boolean":
        return types.is_bool_dtype(values.dtype)
    if kind in ("timestamp", "date"):
        return types.is_datetime64_any_dtype(values.dtype)
    return True


def _zone_problem(values: pd.Series, column: str, zone) -> str | None:
    if not pd.api.types.is_datetime64_any_dtype(values.dtype):
        return None  # reported as a type problem
    tz = getattr(values.dtype, "tz", None)
    if zone == "UTC" and (tz is None or str(tz) != "UTC"):
        return f"{column}: time zone {tz}, the contract says UTC"
    if zone == "naive" and tz is not None:
        return f"{column}: time zone {tz}, the contract says none"
    if zone and zone.startswith("local:"):
        local = zone.split(":", 1)[1]
        if tz is None or str(tz) != local:
            return f"{column}: time zone {tz}, the contract says {local}"
    return None
'''


def _reader(sp: dict) -> list[str]:
    n = sp["ident"]
    fmt = sp["format"]
    out = ["", ""]
    out.append(f"def read_{n}(path) -> pd.DataFrame:")
    kind = fmt.upper() if fmt != "jsonl" else "JSONL"
    out += _wrap(f'"""Read one {kind} file of {_safe_text(sp["name"])} '
                 "with the contract's types.", prefix="    ")
    text = []
    if fmt == "csv":
        text.append("Every column gets a dtype, so an ID such as 007 keeps "
                    "its zeros. Only an empty field is missing: NA, null and "
                    "None stay text (keep_default_na=False). A row with more "
                    "fields than the header raises instead of shifting the "
                    "columns.")
    elif fmt in ("jsonl", "json"):
        text.append("JSON types are kept as written (dtype=False: no number "
                    "or date guessing), then given the contract's types. A "
                    "missing key or a null is missing.")
    else:
        text.append("Parquet carries its own types; they're then given the "
                    "contract's types.")
    if sp["zones"] or sp["dates"]:
        text.append("Timestamps are parsed in the declared zone; a value in "
                    "another zone raises.")
    if sp["optional"]:
        text.append("A file without one of the OPTIONAL columns reads it as "
                    "empty.")
    if sp["partitions"]:
        text.append("Partition columns (" + ", ".join(sp["partitions"])
                    + ") come from the folder names (key=value) when the "
                    "file lacks them.")
    out.append("")
    out += _wrap(" ".join(text), prefix="    ")
    out.append('    """')
    if fmt == "csv":
        out.append("    with warnings.catch_warnings():")
        out.append("        # A row with more fields than the header raises; "
                   "it never")
        out.append("        # shifts every column left (index_col=False).")
        out.append('        warnings.simplefilter("error", '
                   "pd.errors.ParserWarning)")
        out.append(f"        df = pd.read_csv(path, sep={_py(sp['delimiter'])}, "
                   "dtype=DTYPES,")
        out.append("                         keep_default_na=False, "
                   'na_values=[""],')
        out.append('                         encoding="utf-8", '
                   "index_col=False)")
    elif fmt == "jsonl":
        out.append("    df = pd.read_json(path, lines=True, dtype=False)")
    elif fmt == "json":
        out.append("    df = pd.read_json(path, dtype=False)")
    else:
        out.append("    df = pd.read_parquet(path)")
    if sp["partitions"]:
        out.append("    parts = dict(")
        out.append('        part.split("=", 1)')
        out.append("        for part in os.path.normpath(os.fspath(path))"
                   ".split(os.sep)[:-1]")
        out.append('        if "=" in part')
        out.append("    )")
        out.append("    for column in PARTITIONS:")
        out.append("        if column not in df.columns and column in parts:")
        out.append("            df[column] = parts[column]")
    if sp["optional"]:
        out.append("    for column in OPTIONAL:  # may be absent from a file")
        out.append("        if column not in df.columns:")
        out.append("            df[column] = pd.NA")
    out.append(f"    return coerce_{n}(df)")
    return out


def _coerce(sp: dict) -> list[str]:
    n = sp["ident"]
    return [
        "", "",
        f"def coerce_{n}(df: pd.DataFrame) -> pd.DataFrame:",
        *_wrap(f'"""Give a frame of {_safe_text(sp["name"])} the '
               "contract's types (a copy). Raises ValueError when a column "
               "is missing or a timestamp is in the wrong zone.",
               prefix="    "),
        '    """',
        "    missing = [c for c in COLUMNS if c not in df.columns]",
        "    if missing:",
        "        raise ValueError(f\"{DATASET}: missing column(s) "
        "{', '.join(missing)}\")",
        "    df = df.astype(DTYPES)",
        "    for column, zone in TIME_ZONES.items():",
        "        df[column] = _parse_timestamps(df[column], column, zone)",
        "    for column in DATES:",
        "        df[column] = pd.to_datetime(df[column], format=\"%Y-%m-%d\")",
        "    return df",
    ]


def _check(sp: dict) -> list[str]:
    n = sp["ident"]
    out = ["", ""]
    out.append(f"def check_{n}(df: pd.DataFrame) -> pd.DataFrame:")
    out += _wrap(f'"""Raise ValueError naming every way `df` breaks the '
                 f"{_safe_text(sp['name'])} contract (columns, types, keys, "
                 "empty values, accepted values, time zones); return `df` "
                 "when it holds. Run it on the output before you write it.",
                 prefix="    ")
    out.append("")
    out.append("    Units can't be checked from a frame: keep the declared "
               "unit (see COLUMNS).")
    if sp["write_mode"]:
        out.append(f"    Write mode: {sp['write_mode']} (see the contract), "
                   "never a plain replace.")
    out.append('    """')
    out += [
        "    problems = []",
        "    missing = [c for c in COLUMNS if c not in df.columns]",
        "    if missing:",
        "        problems.append(f\"missing column(s): {', '.join(missing)}\")",
        "    for column, kind in TYPES.items():",
        "        if column in df.columns and not _has_type(df[column], kind):",
        "            problems.append(f\"{column}: dtype {df[column].dtype}, \"",
        "                            f\"the contract says {kind}\")",
        "    for column in NOT_NULL:",
        "        if column in df.columns:",
        "            empty = int(df[column].isna().sum())",
        "            if empty:",
        "                problems.append(f\"{column}: {empty} empty value(s), \"",
        "                                \"the contract says never empty\")",
        "    for key in KEYS:",
        "        if all(c in df.columns for c in key):",
        "            shared = int(df.duplicated(subset=key, keep=False).sum())",
        "            if shared:",
        "                problems.append(f\"key ({', '.join(key)}): {shared} \"",
        "                                \"rows share a key value\")",
        "    for column, allowed in ACCEPTED_VALUES.items():",
        "        if column not in df.columns:",
        "            continue",
        "        values = df[column]",
        "        filled = values.dropna()",
        "        undeclared = filled[~filled.isin(allowed)]",
        "        if len(undeclared):",
        "            shown = \", \".join(sorted({str(v) for v in "
        "undeclared})[:5])",
        "            problems.append(f\"{column}: {len(undeclared)} value(s) \"",
        "                            f\"not declared in the contract \"",
        "                            f\"({shown})\")",
        "        empty = int(values.isna().sum())",
        "        if empty and not EMPTY_ALLOWED.get(column, False):",
        "            problems.append(f\"{column}: {empty} empty value(s), \"",
        "                            \"the contract lists no empty value\")",
        "    for column, zone in TIME_ZONES.items():",
        "        if column in df.columns:",
        "            problem = _zone_problem(df[column], column, zone)",
        "            if problem:",
        "                problems.append(problem)",
        "    if problems:",
        "        raise ValueError(f\"{DATASET} breaks its contract:\\n- \"",
        "                         + \"\\n- \".join(problems))",
        "    return df",
    ]
    return out


# -- the tests ---------------------------------------------------------------

def _fixture_value(sp: dict, col: str, i: int, key_cols: set):
    kind = sp["types"].get(col)
    if col in sp["values"] and sp["values"][col]:
        vals = sp["values"][col]
        return vals[i % len(vals)]
    if kind == "integer":
        return i + 1 if col in key_cols else (i + 1) * 10
    if kind == "number":
        return 1.5 + i
    if kind == "boolean":
        return i % 2 == 0
    if kind == "timestamp":
        zone = sp["zones"].get(col)
        stamp = f"2026-01-0{i + 1}T08:00:00"
        return stamp + "Z" if zone == "UTC" else stamp
    if kind == "date":
        return f"2026-01-0{i + 1}"
    if kind == "path":
        return f"folder/file_{i + 1}.txt"
    if col in key_cols:
        return f"{i + 1:04d}"  # leading zeros: proves the dtype keeps them
    return f"text {i + 1}"


def fixture_rows(sp: dict) -> list[list]:
    key_cols = {c for k in sp["keys"] for c in k}
    for count in (3, 2, 1):
        rows = [[_fixture_value(sp, c, i, key_cols) for c in sp["columns"]]
                for i in range(count)]
        ok = True
        for key in sp["keys"]:
            idx = [sp["columns"].index(c) for c in key if c in sp["columns"]]
            seen = {tuple(r[j] for j in idx) for r in rows}
            if len(seen) != len(rows):
                ok = False
        if ok:
            return rows
    return rows


def _string_key(sp: dict) -> str | None:
    for key in sp["keys"]:
        for col in key:
            if sp["types"].get(col) in ("string", None) and \
                    col not in sp["values"]:
                return col
    return None


def _plain_text_col(sp: dict) -> str | None:
    key_cols = {c for k in sp["keys"] for c in k}
    for col in sp["columns"]:
        if sp["types"].get(col) == "string" and col not in key_cols \
                and col not in sp["values"]:
            return col
    return None


def render_tests(sp: dict) -> str:
    n = sp["ident"]
    fmt = sp["format"]
    rows = fixture_rows(sp)
    out: list[str] = []
    out += _wrap(f'"""Tests for the {_safe_text(sp["name"])} boundaries, '
                 f"generated by `truequery scaffold` from {_doc_path(sp['path'])}.",
                 prefix="")
    out.append("")
    out.append("The rows are synthetic, made from the contract: no real data.")
    out.append('"""')
    out.append("")
    imports = ["import sys"]
    if fmt == "csv":
        imports = ["import csv", "import sys"]
    elif fmt in ("jsonl", "json"):
        imports = ["import json", "import sys"]
    out += imports
    out.append("from pathlib import Path")
    out.append("")
    if sp["kind"] != "files" or fmt == "parquet":
        out.append("import pandas as pd")
    out.append("import pytest")
    out.append("")
    out.append("sys.path.insert(0, str(Path(__file__).resolve().parent))")
    names = ["COLUMNS", f"check_{n}",
             f"read_{n}" if sp["kind"] == "files" else f"coerce_{n}"]
    out.append(f"from {n}_io import (  # noqa: E402")
    out.append("    " + ", ".join(names) + ",")
    out.append(")")
    out.append("")
    if fmt == "parquet":
        out.append('pytest.importorskip("pyarrow")  # pandas writes the '
                   "Parquet fixture with it")
        out.append("")
    out.append("# One list per row, in COLUMNS order.")
    out.append("ROWS = [")
    for row in rows:
        out += _row_lines(row)
    out.append("]")
    out += _frame_helper(sp)
    out += _test_bodies(sp)
    return "\n".join(out).rstrip("\n") + "\n"


def _frame_helper(sp: dict) -> list[str]:
    n = sp["ident"]
    fmt = sp["format"]
    out = ["", ""]
    if sp["kind"] != "files":
        out += [
            "def load(tmp_path, rows):",
            '    """The rows as a typed frame (this contract names a table, '
            'not files)."""',
            f"    return coerce_{n}(pd.DataFrame(rows, columns=list(COLUMNS)))",
        ]
        return out
    out.append("def load(tmp_path, rows):")
    out.append('    """Write the rows as a file in the contract\'s format, '
               'then read it back."""')
    if fmt == "csv":
        out += [
            f"    path = tmp_path / \"{n}.csv\"",
            "    with open(path, \"w\", newline=\"\", encoding=\"utf-8\") "
            "as handle:",
            f"        writer = csv.writer(handle, delimiter="
            f"{_py(sp['delimiter'])})",
            "        writer.writerow(COLUMNS)",
            "        writer.writerows(rows)",
        ]
    elif fmt == "jsonl":
        out += [
            f"    path = tmp_path / \"{n}.jsonl\"",
            "    lines = [json.dumps(dict(zip(COLUMNS, row))) for row in rows]",
            "    path.write_text(\"\\n\".join(lines) + \"\\n\", "
            "encoding=\"utf-8\")",
        ]
    elif fmt == "json":
        out += [
            f"    path = tmp_path / \"{n}.json\"",
            "    records = [dict(zip(COLUMNS, row)) for row in rows]",
            "    path.write_text(json.dumps(records), encoding=\"utf-8\")",
        ]
    else:
        out += [
            f"    path = tmp_path / \"{n}.parquet\"",
            "    frame = pd.DataFrame(rows, columns=list(COLUMNS))",
            "    frame.to_parquet(path, index=False)",
        ]
    out.append(f"    return read_{n}(path)")
    return out


def _test_bodies(sp: dict) -> list[str]:
    n = sp["ident"]
    fmt = sp["format"]
    reads = sp["kind"] == "files"
    out: list[str] = []

    def test(name, body):
        out.extend(["", "", f"def {name}(tmp_path):"] + body)

    body = ["    df = load(tmp_path, ROWS)", f"    check_{n}(df)",
            "    assert len(df) == len(ROWS)"]
    skey = _string_key(sp)
    rows = fixture_rows(sp)
    if skey is not None:
        i = sp["columns"].index(skey)
        body.append(f"    # Leading zeros kept: {_py(rows[0][i])} stays "
                    "text.")
        body.append(f"    assert list(df[{_py(skey)}]) == "
                    f"[row[{i}] for row in ROWS]")
    test("test_the_rows_read_back_and_pass_the_check", body)

    text_col = _plain_text_col(sp)
    if reads and fmt == "csv" and text_col is not None:
        i = sp["columns"].index(text_col)
        test("test_only_an_empty_field_is_missing", [
            "    rows = [list(row) for row in ROWS]",
            f"    rows[0][{i}] = \"NA\"",
            "    df = load(tmp_path, rows)",
            f"    assert df[{_py(text_col)}].iloc[0] == \"NA\"",
        ])
    for key in sp["keys"]:
        label = "_".join(ident(c) for c in key)
        test(f"test_a_repeated_key_fails_{label}", [
            "    df = load(tmp_path, ROWS + [ROWS[0]])",
            "    with pytest.raises(ValueError, match=\"share a key value\"):",
            f"        check_{n}(df)",
        ])
    for col in sp["not_null"]:
        i = sp["columns"].index(col)
        empty = '""' if reads and fmt == "csv" else "None"
        test(f"test_an_empty_{ident(col)}_fails", [
            "    rows = [list(row) for row in ROWS]",
            f"    rows[0][{i}] = {empty}",
            "    df = load(tmp_path, rows)",
            "    with pytest.raises(ValueError, match=\"empty value\"):",
            f"        check_{n}(df)",
        ])
    for col in sp["values"]:
        i = sp["columns"].index(col)
        if sp["types"].get(col) not in (None, "string", "path"):
            continue
        test(f"test_an_undeclared_{ident(col)}_fails", [
            "    rows = [list(row) for row in ROWS]",
            f"    rows[0][{i}] = \"not a declared value\"",
            "    df = load(tmp_path, rows)",
            "    with pytest.raises(ValueError, match=\"not declared\"):",
            f"        check_{n}(df)",
        ])
    for col, zone in sp["zones"].items():
        i = sp["columns"].index(col)
        if zone == "UTC":
            bad = '"2026-01-01T08:00:00"'
            what = "no UTC marker"
        elif zone == "naive" or (zone or "").startswith("local:"):
            bad = '"2026-01-01T08:00:00Z"'
            what = "UTC|time zone"
        else:
            continue
        test(f"test_a_{ident(col)}_in_another_zone_is_refused", [
            "    rows = [list(row) for row in ROWS]",
            f"    rows[0][{i}] = {bad}",
            f"    with pytest.raises(ValueError, match=\"{what}\"):",
            "        load(tmp_path, rows)",
        ])
    test("test_a_missing_column_fails", [
        "    df = load(tmp_path, ROWS)",
        f"    df = df.drop(columns=[{_py(sp['columns'][0])}])",
        "    with pytest.raises(ValueError, match=\"missing column\"):",
        f"        check_{n}(df)",
    ])
    return out


# -- the run -----------------------------------------------------------------

def plan(paths: list[str], out_dir: str, root: str) -> list[dict]:
    """[{"contract", "module", "tests", "module_text", "tests_text"}]."""
    specs, problems = [], []
    for path in paths:
        target = path
        if not os.path.isfile(target):
            candidate = os.path.join(root, "contracts", f"{path}.yaml")
            if os.path.isfile(candidate):
                target = candidate
            else:
                problems.append(f"{path}: no such contract file (give a path, "
                                "or a dataset name with contracts/<name>.yaml)")
                continue
        try:
            contract = load(target)
        except ScaffoldError as exc:
            problems.append(str(exc))
            continue
        shown = os.path.relpath(os.path.abspath(target), root) if os.path.abspath(
            target).startswith(os.path.abspath(root) + os.sep) else target
        specs.append(spec_of(contract, shown))
    seen: dict[str, str] = {}
    for sp in specs:
        if sp["ident"] in seen:
            problems.append(f"{sp['path']} and {seen[sp['ident']]} would both "
                            f"write {sp['ident']}_io.py; scaffold them into "
                            "different --out folders")
        seen[sp["ident"]] = sp["path"]
    if problems:
        raise ScaffoldError("; ".join(problems))
    result = []
    for sp in specs:
        module = os.path.join(out_dir, f"{sp['ident']}_io.py")
        tests = os.path.join(out_dir, f"test_{sp['ident']}_io.py")
        result.append({"contract": sp["path"], "dataset": sp["name"],
                       "module": module, "tests": tests, "draft": sp["draft"],
                       "module_text": render_module(sp),
                       "tests_text": render_tests(sp)})
    return result


def write(result: list[dict], force: bool) -> list[str]:
    if not force:
        exists = [p for r in result for p in (r["module"], r["tests"])
                  if os.path.exists(p)]
        if exists:
            raise ScaffoldError(
                "already there, not written over: " + ", ".join(
                    _slash(p) for p in exists) + " (use --force to replace "
                "them; nothing was written)")
    written = []
    for item in result:
        os.makedirs(os.path.dirname(item["module"]) or ".", exist_ok=True)
        for key in ("module", "tests"):
            with open(item[key], "w", encoding="utf-8", newline="\n") as handle:
                handle.write(item[f"{key}_text"])
            written.append(item[key])
    return written
