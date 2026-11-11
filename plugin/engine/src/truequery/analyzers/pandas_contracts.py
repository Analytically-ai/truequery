"""pandas checks that read the project's contracts (0.2, part C2).

The contract-free pandas checks live in `pandas_checks.py`; this module
adds what they can't see without a contract: which file or table a
DataFrame came from, and what its contract declares about each column.

**The link.** A DataFrame is linked to a contract only when the code says
exactly where it came from, so the link is deterministic:

- `pd.read_csv` / `read_table` / `read_fwf` / `read_json` / `read_parquet`
  of a path that is a string literal, a same-file constant, `Path()` of
  one, or `os.path.join` of literals, matching one contract's
  `location.path` glob (relative to the project root, or to the declared
  base's default when the contract names a base);
- `pd.read_sql` / `read_sql_query` of a literal query whose only table has
  a contract, or `read_sql_table` of a contracted table;
- then through methods that keep rows and columns as they are (a row
  filter, `head`, `sort_values`, `drop_duplicates`, `copy`, ...).

Anything else gives no link and no finding: a path built with a variable
or an f-string, a column renamed or replaced (`rename`, `assign`,
`df["c"] = ...`, `fillna`, `astype`), `merge`, `concat`. Writes are linked
by their target alone (`to_sql`'s table name, `to_csv`'s path).

Names only, inside one function (or the module) at a time, like the
contract-free checks. Never imports pandas or the code it reads.
"""
from __future__ import annotations

import ast
import os
import posixpath
import re
from dataclasses import dataclass, field, replace

READ_DTYPE = "py/pandas-read-csv-contract-dtype"
FILLNA_MONEY = "py/pandas-fillna-zero-on-money"
WRITE_MODE = "py/pandas-write-mode-violation"
UNDECLARED = "dq/undeclared-status-value"
ABSENCE = "dq/expected-absence-counted-as-missing"
UNIT = "sem/unit-mismatch"
TZ = "sem/timezone-mismatch"
PATH = "sem/relative-path-base-mismatch"

# Checks with a pandas proof, implemented only here.
NEW_RULES = (READ_DTYPE, FILLNA_MONEY, WRITE_MODE)
# C1's SQL contract checks, extended to the same mistakes in pandas.
EXTENDED_RULES = (UNDECLARED, ABSENCE, UNIT, TZ, PATH)
RULES = NEW_RULES + EXTENDED_RULES

_PATH_READERS = frozenset({"read_csv", "read_table", "read_fwf", "read_json",
                           "read_parquet"})
_TEXT_READERS = frozenset({"read_csv", "read_table", "read_fwf"})
_SQL_READERS = frozenset({"read_sql", "read_sql_query"})
# Methods whose result keeps the receiver's rows' values and column names.
_KEEPS_COLUMNS = frozenset({
    "copy", "head", "tail", "sample", "sort_values", "sort_index",
    "drop_duplicates", "dropna", "reset_index", "drop", "set_index",
    "nlargest", "nsmallest", "query", "filter", "infer_objects",
})
# In-place methods that leave every value and name as it was.
_INPLACE_HARMLESS = frozenset({"sort_values", "sort_index", "drop_duplicates",
                               "dropna", "reset_index", "drop", "set_index",
                               "query"})
# Reductions a value passes through unscaled (C1's SUM/AVG/MIN/MAX,
# COALESCE and CAST).
_PASSTHROUGH = frozenset({"sum", "mean", "min", "max", "median", "fillna",
                          "astype", "abs"})
_STRING_DTYPES = frozenset({"str", "string", "object", "O", "string[python]",
                            "string[pyarrow]"})
_MONEY_UNITS = frozenset({"minor", "major"})
_COMPARE_METHODS = {"eq": ast.Eq, "ne": ast.NotEq, "lt": ast.Lt,
                    "le": ast.LtE, "gt": ast.Gt, "ge": ast.GtE}
_ARITH_METHODS = {"add": ast.Add, "sub": ast.Sub, "radd": ast.Add,
                  "rsub": ast.Sub}
_FRAME_ATTRS = frozenset({
    "T", "at", "attrs", "axes", "columns", "dtypes", "empty", "flags",
    "iat", "iloc", "index", "loc", "ndim", "plot", "shape", "size",
    "sparse", "style", "values", "str", "dt", "cat",
})


# -- small AST helpers (pandas_checks imports this module) --------------------
def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _kw(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _arg(call, index, name):
    value = _kw(call, name)
    if value is None and len(call.args) > index:
        value = call.args[index]
    return value


def _star(call):
    return any(k.arg is None for k in call.keywords) or any(
        isinstance(a, ast.Starred) for a in call.args)


def _str_list(node):
    """A list/tuple/set of string literals, or None."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_const(e) for e in node.elts]
        if all(isinstance(v, str) for v in vals):
            return vals
    return None


def _literal_values(node):
    """Literal values compared against: strings and numbers (not bools)."""
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_one_literal(e) for e in node.elts]
        return vals if vals and all(v is not None for v in vals) else None
    one = _one_literal(node)
    return [one] if one is not None else None


def _one_literal(node):
    value = _const(node)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        return str(value)
    return None


def _position(node):
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.end_lineno is not None and func.end_col_offset is not None:
            return func.end_lineno, func.end_col_offset - len(func.attr) + 1
    return node.lineno, node.col_offset + 1


def _shown(node) -> str:
    try:
        text = " ".join(ast.unparse(node).split())
    except Exception:  # pragma: no cover - unparse handles every parsed node
        return "this filter"
    return text if len(text) <= 80 else text[:77] + "..."


# -- the project's contracts ---------------------------------------------------
def _glob_re(pattern: str):
    """A location.path glob as a regex: `*` stays inside one folder, `**`
    crosses folders (as `glob` reads it)."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        char = pattern[i]
        if char == "*":
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and "]" in pattern[i + 2:]:
            end = pattern.index("]", i + 2)
            body = pattern[i + 1:end]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
            continue
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _norm(path: str) -> str:
    path = posixpath.normpath(path.replace("\\", "/"))
    return path[2:] if path.startswith("./") else path


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


class Contracts:
    """The contracts, bases and root one check run reads (built once per
    project and cached by keylookup)."""

    def __init__(self, ctx: dict):
        self.index = ctx.get("contracts") or {}
        self.bases = ctx.get("bases") or {}
        self.root = ctx.get("root")
        self._files = None

    def __bool__(self):
        return bool(self.index)

    def _file_patterns(self):
        if self._files is None:
            seen, files = set(), []
            for contract in self.index.values():
                if id(contract) in seen:
                    continue
                seen.add(id(contract))
                loc = contract.get("location")
                if not isinstance(loc, dict) or not loc.get("path"):
                    continue
                pattern = str(loc["path"])
                base = loc.get("base")
                if base:
                    spec = self.bases.get(base)
                    default = spec.get("default") if isinstance(spec, dict) \
                        else None
                    if not default:
                        continue  # the base is only known at run time
                    pattern = posixpath.join(str(default).replace("\\", "/"),
                                             pattern)
                files.append((_glob_re(_norm(pattern)), contract,
                              not _is_glob(str(loc["path"]))))
            self._files = files
        return self._files

    def for_path(self, literal: str):
        """(contract, names one file) for a path the code reads or writes;
        None unless exactly one contract's location matches."""
        candidates = [_norm(literal)]
        if os.path.isabs(literal) and self.root:
            root = os.path.realpath(self.root)
            real = os.path.realpath(literal)
            if os.path.commonpath([root, real]) == root:
                candidates.append(_norm(os.path.relpath(real, root)))
        hits = {}
        for regex, contract, single in self._file_patterns():
            if any(regex.match(c) for c in candidates):
                hits[id(contract)] = (contract, single)
        return next(iter(hits.values())) if len(hits) == 1 else None

    def for_table(self, name: str, schema: str | None = None):
        """The contract for a table name, with C1's rule: a contract that
        names only the table matches it in any schema; one that names its
        schema doesn't match another schema."""
        from truequery.checks.sql import _declared_relations
        name_low = name.lower()
        if schema:
            full = f"{schema}.{name}".lower()
            if full in self.index:
                return self.index[full]
        contract = self.index.get(name_low)
        if contract is None or not schema:
            return contract
        schemas = {rel.split(".")[-2] for rel in _declared_relations(contract)
                   if "." in rel and rel.split(".")[-1] == name_low}
        if schemas and schema.lower() not in schemas:
            return None
        return contract


_COLUMNS: dict[int, tuple] = {}  # id(contract) -> (contract, columns), per run


def _columns(contract: dict) -> dict[str, tuple[str, dict]]:
    """lowercased name -> (declared name, spec), v1 allowed_values too."""
    hit = _COLUMNS.get(id(contract))
    if hit is not None and hit[0] is contract:
        return hit[1]
    from truequery.checks.sql import _contract_col_map
    cols = {k: (v[1], v[2]) for k, v in _contract_col_map(contract).items()}
    _COLUMNS[id(contract)] = (contract, cols)
    return cols


def clear_cache() -> None:
    _COLUMNS.clear()


def _ds(contract: dict) -> str:
    return str(contract.get("dataset") or contract.get("table") or "unknown")


def _draft(*contracts) -> str:
    from truequery.checks.sql import _draft_note
    return _draft_note(*contracts)


# -- what a DataFrame is linked to --------------------------------------------
@dataclass(frozen=True)
class Link:
    contract: dict
    # columns the frame holds, when the read says (usecols, a SELECT list)
    only: frozenset | None = None
    # columns replaced since the read: they no longer hold the contract's data
    changed: frozenset = frozenset()
    # columns still as read text (read_csv without parse_dates for them)
    raw: frozenset = frozenset()
    # status values ruled out upstream, per column
    excluded: tuple = ()
    # negated tests on a status column that this frame was filtered by
    tests: tuple = ()

    def column(self, name):
        """(declared name, spec) for a column the frame holds as read."""
        low = str(name).lower()
        if low in self.changed or (self.only is not None
                                   and low not in self.only):
            return None
        return _columns(self.contract).get(low)

    def excluded_for(self, low: str) -> frozenset:
        return next((vals for col, vals in self.excluded if col == low),
                    frozenset())


@dataclass
class Col:
    """A column of a linked frame, as an expression uses it."""
    link: Link
    name: str
    spec: dict
    frame: ast.AST = field(repr=False, default=None)

    @property
    def ref(self) -> str:
        return f"{_ds(self.link.contract)}.{self.name}"

    @property
    def low(self) -> str:
        return self.name.lower()


# -- the pass ------------------------------------------------------------------
class ContractPass:
    """The contract checks for one file. A `_Scope` from pandas_checks
    calls into it as it walks: on each binding, write, call and
    expression."""

    def __init__(self, file, rules, out, contracts: Contracts, tree):
        self.file = file
        self.rules = set(rules) & set(RULES)
        self.out = out
        self.contracts = contracts
        self.module_consts = _constants(tree.body)

    # -- reporting -------------------------------------------------------
    def report(self, rule, node, why, fix):
        if rule in self.rules:
            line, col = _position(node)
            self.out.append((rule, line, col, why, fix))

    # -- literal paths and names -------------------------------------------
    def literal(self, node, scope, depth=0):
        """The string a path or name expression always is, or None."""
        if depth > 8 or node is None:
            return None
        value = _const(node)
        if isinstance(value, str):
            return value
        if isinstance(node, ast.Name):
            target = scope.consts.get(node.id)
            if target is None and node.id not in scope.assigned:
                target = self.module_consts.get(node.id)
            return self.literal(target, scope, depth + 1)
        if isinstance(node, ast.BinOp):
            left = self.literal(node.left, scope, depth + 1)
            right = self.literal(node.right, scope, depth + 1)
            if left is None or right is None:
                return None
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Div) and self._is_path(node.left, scope):
                return posixpath.join(left, right)
            return None
        if isinstance(node, ast.Call) and not node.keywords:
            func = node.func
            fname = (func.attr if isinstance(func, ast.Attribute)
                     else func.id if isinstance(func, ast.Name) else None)
            if fname in ("Path", "PurePath", "PurePosixPath") \
                    and len(node.args) == 1:
                return self.literal(node.args[0], scope, depth + 1)
            if fname == "join" and isinstance(func, ast.Attribute) \
                    and isinstance(func.value, ast.Attribute) \
                    and func.value.attr == "path" and node.args:
                parts = [self.literal(a, scope, depth + 1) for a in node.args]
                if all(p is not None for p in parts):
                    return posixpath.join(*parts)
        return None

    def resolved(self, node, scope):
        """A same-file constant's value node (`VALID = ["paid"]`), else
        the node itself."""
        if isinstance(node, ast.Name):
            target = scope.consts.get(node.id)
            if target is None and node.id not in scope.assigned:
                target = self.module_consts.get(node.id)
            if isinstance(target, (ast.Constant, ast.List, ast.Tuple, ast.Set)):
                return target
        return node

    def _is_path(self, node, scope):
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            node = node.left
        if isinstance(node, ast.Name):
            node = scope.consts.get(node.id) or self.module_consts.get(node.id)
        return isinstance(node, ast.Call) and (
            getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        ) in ("Path", "PurePath", "PurePosixPath")

    # -- links -----------------------------------------------------------
    def link_of(self, node, scope):
        """The Link of a DataFrame expression, or None."""
        if isinstance(node, ast.Name):
            return scope.links.get(node.id)
        if isinstance(node, ast.Call):
            name = self.file.pandas_function(node.func)
            if name in _PATH_READERS:
                return self._read_link(node, name, scope)
            if name in _SQL_READERS or name == "read_sql_table":
                return self._sql_link(node, name, scope)
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _KEEPS_COLUMNS \
                    and not _star(node) and _const(_kw(node, "inplace")) is not True:
                link = self.link_of(func.value, scope)
                if link is None:
                    return None
                if func.attr == "query":
                    return self._filtered(link, self._query_mask(node, scope),
                                          scope, query=True)
                if func.attr == "filter":
                    items = _str_list(_arg(node, 0, "items"))
                    return replace(link, only=frozenset(i.lower() for i in items)) \
                        if items is not None else None
                return link
            return None
        if isinstance(node, ast.Subscript):
            value, index = node.value, node.slice
            if isinstance(value, ast.Attribute) and value.attr == "loc":
                link = self.link_of(value.value, scope)
                if link is None:
                    return None
                if isinstance(index, ast.Tuple) and len(index.elts) == 2:
                    rows, cols = index.elts
                    names = _str_list(cols)
                    if names is not None:
                        link = replace(link, only=frozenset(n.lower() for n in names))
                    elif not (isinstance(cols, ast.Slice) and cols.lower is None
                              and cols.upper is None):
                        return None
                    return self._filtered(link, rows, scope)
                return self._filtered(link, index, scope)
            link = self.link_of(value, scope)
            if link is None:
                return None
            names = _str_list(index)
            if names is not None:
                return replace(link, only=frozenset(n.lower() for n in names))
            if _const(index) is not None:
                return None  # one column: a Series
            return self._filtered(link, index, scope)
        return None

    def _read_link(self, call, reader, scope):
        if _star(call):
            return None
        path = self.literal(_arg(call, 0, "filepath_or_buffer")
                            or _kw(call, "path") or _kw(call, "path_or_buf"),
                            scope)
        if path is None or self.contracts is None:
            return None
        hit = self.contracts.for_path(path)
        if hit is None:
            return None
        contract = hit[0]
        if _kw(call, "names") is not None or _kw(call, "header") is not None \
                and _const(_kw(call, "header")) not in (0, "infer"):
            return None  # columns named by the code, not the file
        only = None
        usecols = _kw(call, "usecols") if reader != "read_parquet" \
            else _kw(call, "columns")
        if usecols is not None:
            names = _str_list(usecols)
            if names is None:
                return None
            only = frozenset(n.lower() for n in names)
        raw = frozenset()
        if reader in _TEXT_READERS:
            parsed = _kw(call, "parse_dates")
            parsed_names = _str_list(parsed) if parsed is not None else []
            if parsed_names is not None and _kw(call, "converters") is None:
                raw = frozenset(c for c in _columns(contract)
                                if c not in {p.lower() for p in parsed_names})
        return Link(contract, only=only, raw=raw)

    def _sql_link(self, call, reader, scope):
        if _star(call):
            return None
        if reader == "read_sql_table":
            name = self.literal(_arg(call, 0, "table_name"), scope)
            if name is None:
                return None
            contract = self.contracts.for_table(
                name, self.literal(_kw(call, "schema"), scope))
            cols = _str_list(_kw(call, "columns"))
            return Link(contract, only=frozenset(c.lower() for c in cols)
                        if cols is not None else None) if contract else None
        text = self.literal(_arg(call, 0, "sql"), scope)
        return _query_link(text, self.contracts) if text else None

    def _filtered(self, link, mask, scope, query=False):
        """The link after a row filter: what it rules out on each status
        column, and the negated tests it applies."""
        if mask is None:
            return link if query else None
        if not (query or _is_mask(mask)):
            if isinstance(mask, ast.Slice):
                return link
            if isinstance(mask, ast.Name) and mask.id in scope.masks:
                mask = scope.masks[mask.id]
            else:
                return None
        excluded = dict(link.excluded)
        tests = list(link.tests)
        for part in _and_parts(mask):
            test = self.value_test(part, scope, frame=link if query else None)
            if test is None:
                continue
            col, vals, negated = test
            if col.link.contract is not link.contract:
                continue
            spec_vals = _declared(col.spec) or []
            ruled = set(vals) if negated else set(spec_vals) - set(vals)
            excluded[col.low] = frozenset(excluded.get(col.low, frozenset())
                                          | ruled)
        for part in _and_parts(mask):
            test = self.value_test(part, scope, frame=link if query else None)
            if test is not None and test[2] \
                    and test[0].link.contract is link.contract:
                tests.append((part, test[0].name, tuple(test[1])))
        return replace(link, excluded=tuple(sorted(excluded.items())),
                       tests=tuple(tests))

    def _query_mask(self, call, scope):
        text = self.literal(_arg(call, 0, "expr"), scope)
        if text is None:
            return None
        try:
            return ast.parse(text, mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            return None

    # -- columns -------------------------------------------------------------
    def column(self, node, scope, frame=None):
        """A Col for `df["c"]`, `df.c`, `df.loc[:, "c"]`; with `frame`, a bare
        name in a query string."""
        if frame is not None and isinstance(node, ast.Name):
            found = frame.column(node.id)
            return Col(frame, found[0], found[1]) if found else None
        if isinstance(node, ast.Subscript):
            value, index = node.value, node.slice
            if isinstance(value, ast.Attribute) and value.attr == "loc" \
                    and isinstance(index, ast.Tuple) and len(index.elts) == 2:
                value, index = value.value, index.elts[1]
            name = _const(index)
            if not isinstance(name, str):
                return None
        elif isinstance(node, ast.Attribute) and node.attr not in _FRAME_ATTRS:
            value, name = node.value, node.attr
        else:
            return None
        link = self.link_of(value, scope)
        if link is None:
            return None
        found = link.column(name)
        return Col(link, found[0], found[1], value) if found else None

    def value_test(self, node, scope, frame=None):
        """(Col, values, negated) for a test of one status column against
        literals: ==, !=, .eq, .ne, .isin, ~(...), `in`/`not in` (query)."""
        negated = False
        while isinstance(node, ast.UnaryOp) and isinstance(
                node.op, (ast.Invert, ast.Not)):
            negated, node = not negated, node.operand
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            left, right, op = node.left, node.comparators[0], node.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                col = self.column(left, scope, frame)
                vals = _literal_values(self.resolved(right, scope))
                if col is None:
                    col, vals = self.column(right, scope, frame), \
                        _literal_values(self.resolved(left, scope))
                if col is None or vals is None or len(vals) != 1:
                    return None
                return col, vals, negated != isinstance(op, ast.NotEq)
            if isinstance(op, (ast.In, ast.NotIn)) and frame is not None:
                col, vals = self.column(left, scope, frame), _literal_values(right)
                if col is None or vals is None or not isinstance(
                        right, (ast.List, ast.Tuple, ast.Set)):
                    return None
                return col, vals, negated != isinstance(op, ast.NotIn)
            return None
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and len(node.args) == 1 and not node.keywords:
            method = node.func.attr
            if method in ("eq", "ne", "isin"):
                col = self.column(node.func.value, scope, frame)
                vals = _literal_values(self.resolved(node.args[0], scope))
                if col is None or vals is None:
                    return None
                if method != "isin" and len(vals) != 1:
                    return None
                return col, vals, negated != (method == "ne")
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr) \
                and not negated:
            parts = [self.value_test(p, scope, frame) for p in _or_parts(node)]
            if parts and all(p is not None and not p[2] for p in parts) \
                    and len({(id(p[0].link.contract), p[0].low)
                             for p in parts}) == 1:
                return parts[0][0], [v for p in parts for v in p[1]], False
        return None

    # -- bindings and writes ------------------------------------------------
    def bind(self, scope, target, value):
        if not isinstance(target, ast.Name):
            return
        link = self.link_of(value, scope) if value is not None else None
        if link is not None:
            scope.links[target.id] = link
        else:
            scope.links.pop(target.id, None)
        if value is not None and _is_mask(value):
            scope.masks[target.id] = value
        else:
            scope.masks.pop(target.id, None)

    def track_write(self, scope, target):
        """`df["c"] = ...`, `df.c = ...`, `df.loc[m, "c"] = ...`,
        `df.columns = ...`: the column (or every name) no longer holds
        the contract's data."""
        node, names = target, None
        if isinstance(node, ast.Subscript):
            value, index = node.value, node.slice
            if isinstance(value, ast.Attribute) and value.attr in ("loc", "at"):
                value = value.value
                index = index.elts[1] if isinstance(index, ast.Tuple) \
                    and len(index.elts) == 2 else None
            if isinstance(value, ast.Name) and value.id in scope.links:
                single = _const(index) if index is not None else None
                names = [single] if isinstance(single, str) else _str_list(index)
                if names is None:
                    scope.links.pop(value.id)
                    return
                link = scope.links[value.id]
                scope.links[value.id] = replace(
                    link, changed=link.changed | {n.lower() for n in names})
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in scope.links:
            if node.attr in ("columns", "index"):
                scope.links.pop(node.value.id)
            elif node.attr not in _FRAME_ATTRS:
                link = scope.links[node.value.id]
                scope.links[node.value.id] = replace(
                    link, changed=link.changed | {node.attr.lower()})

    def track_inplace(self, scope, call):
        func = call.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
                and func.value.id in scope.links
                and _const(_kw(call, "inplace")) is True
                and func.attr not in _INPLACE_HARMLESS):
            scope.links.pop(func.value.id)

    # -- calls ---------------------------------------------------------------
    def call(self, scope, call, parents):
        name = self.file.pandas_function(call.func)
        if name in _TEXT_READERS and READ_DTYPE in self.rules:
            self.read_dtype(call, scope)
        func = call.func
        if not isinstance(func, ast.Attribute):
            if name == "merge" and len(call.args) >= 2:
                self.merge_paths(call, call.args[0], call.args[1], scope)
            if isinstance(func, ast.Name) and func.id == "len" \
                    and len(call.args) == 1:
                self.counted_frame(call.args[0], call, scope)
            return
        method = func.attr
        if method == "fillna":
            self.fillna(call, scope, parents)
        elif method in ("to_sql", "to_csv", "to_json", "to_parquet"):
            self.write_mode(call, method, scope)
        elif method in ("merge", "join") and call.args:
            self.merge_paths(call, func.value, call.args[0], scope)
        elif method == "count" and not call.args:
            self.counted_frame(func.value, call, scope)
        elif method in ("sum", "mean") and not call.args:
            self.counted_mask(func.value, call, scope)
        elif method in _COMPARE_METHODS and len(call.args) == 1 \
                and not call.keywords:
            self.compare(call, func.value, call.args[0],
                         _COMPARE_METHODS[method](), scope)
        elif method in _ARITH_METHODS and len(call.args) == 1:
            self.arith([func.value, call.args[0]], call, scope)
        elif method in ("apply", "map") and len(call.args) == 1:
            self.path_lambda(call, scope)
        if method in ("isin", "eq", "ne") and len(call.args) == 1:
            self.isin(call, scope)
        if method == "query":
            link = self.link_of(func.value, scope)
            mask = self._query_mask(call, scope)
            if link is not None and mask is not None:
                for node in ast.walk(mask):
                    if not isinstance(node, (ast.Compare, ast.Call)):
                        continue
                    test = self.value_test(node, scope, frame=link)
                    if test is not None:
                        self.undeclared(test, call, op_ne=test[2])

    # -- py/pandas-read-csv-contract-dtype ------------------------------------
    def read_dtype(self, call, scope):
        if _star(call) or self.contracts is None:
            return
        path = self.literal(_arg(call, 0, "filepath_or_buffer"), scope)
        if path is None:
            return
        hit = self.contracts.for_path(path)
        if hit is None:
            return
        contract = hit[0]
        if contract.get("version") != 2:
            return
        if _kw(call, "header") is not None and _kw(call, "names") is None \
                and _const(_kw(call, "header")) not in (0, "infer"):
            return
        cols = _columns(contract)
        keys = []
        for entry in contract.get("keys") or []:
            keys += entry if isinstance(entry, list) else [entry]
        string_keys = []
        for key in keys:
            found = cols.get(str(key).lower())
            if found and found[1].get("type") == "string" \
                    and found[0] not in string_keys:
                string_keys.append(found[0])
        for limit in ("usecols", "names"):
            given = _kw(call, limit)
            if given is None:
                continue
            names = _str_list(given)
            if names is None:
                return  # columns the checker can't read
            used = {n.lower() for n in names}
            string_keys = [k for k in string_keys if k.lower() in used]
        if not string_keys:
            return
        dtype = _kw(call, "dtype")
        if dtype is not None:
            if _string_dtype(dtype, self.file):
                return
            if not isinstance(dtype, ast.Dict) or any(k is None for k in dtype.keys):
                return  # a dtype the checker can't read
            given = {str(_const(k)).lower(): v for k, v in
                     zip(dtype.keys, dtype.values)}
            if any(not isinstance(_const(k), str) for k in dtype.keys):
                return
            string_keys = [k for k in string_keys if k.lower() not in given]
        converters = _kw(call, "converters")
        if converters is not None:
            if not isinstance(converters, ast.Dict) or any(
                    not isinstance(_const(k), str) for k in converters.keys):
                return
            done = {_const(k).lower() for k in converters.keys}
            string_keys = [k for k in string_keys if k.lower() not in done]
        if not string_keys:
            return
        names = _join([repr(k) for k in string_keys])
        which = "key" if len(string_keys) == 1 else "keys"
        dtypes = ", ".join(f'"{k}": "string"' for k in string_keys)
        self.report(
            READ_DTYPE, call,
            f"{path} is read with no dtype for {names}, the string {which} "
            f"of the {_ds(contract)} contract: an ID that looks like a number "
            f"is read as one and loses its leading zeros ('007' becomes 7)"
            f"{_draft(contract)}",
            f"pass dtype={{{dtypes}}} so the {which} stay{'s' if len(string_keys) == 1 else ''} text")

    # -- py/pandas-fillna-zero-on-money ---------------------------------------
    def fillna(self, call, scope, parents):
        if FILLNA_MONEY not in self.rules or _star(call):
            return
        value = _arg(call, 0, "value")
        parent = parents.get(id(call))
        if isinstance(parent, ast.Attribute) and parent.attr == "sum":
            grand = parents.get(id(parent))
            if isinstance(grand, ast.Call) and grand.func is parent:
                return  # .fillna(0).sum() is .sum()
        receiver = call.func.value
        targets = []
        col = self.column(receiver, scope)
        if col is not None:
            if _is_zero(value):
                targets = [col]
        else:
            link = self.link_of(receiver, scope)
            if link is None:
                return
            if _is_zero(value):
                targets = [Col(link, name, spec)
                           for low, (name, spec) in sorted(_columns(link.contract).items())
                           if link.column(low)]
            elif isinstance(value, ast.Dict):
                for key, val in zip(value.keys, value.values):
                    if isinstance(_const(key), str) and _is_zero(val):
                        found = link.column(_const(key))
                        if found:
                            targets.append(Col(link, found[0], found[1]))
        money = [c for c in targets
                 if c.link.contract.get("version") == 2
                 and c.spec.get("unit") in _MONEY_UNITS
                 and c.spec.get("nullable") is not False]
        if not money:
            return
        from truequery.checks.sql import _unit_words
        contract = money[0].link.contract
        if len(money) == 1:
            what = (f"{money[0].ref} is a money amount in "
                    f"{_unit_words(money[0].spec)} that may be missing")
        else:
            what = (f"{_join([c.ref for c in money])} are money amounts that "
                    "may be missing")
        self.report(
            FILLNA_MONEY, call,
            f"{what}; fillna(0) turns each missing amount into a real 0, so "
            f"mean(), count(), min() and the like change{_draft(contract)}",
            "leave missing amounts missing (mean() and count() skip them), or "
            "ask the user whether a missing amount really means 0")

    # -- py/pandas-write-mode-violation ---------------------------------------
    def write_mode(self, call, method, scope):
        if WRITE_MODE not in self.rules or _star(call) or self.contracts is None:
            return
        if method == "to_sql":
            name = self.literal(_arg(call, 0, "name"), scope)
            if name is None:
                return
            contract = self.contracts.for_table(
                name, self.literal(_kw(call, "schema"), scope))
            single = True
            how = _kw(call, "if_exists")
            how = "fail" if how is None else _const(how)
            target = f'to_sql("{name}", if_exists="{how}")'
        else:
            path = self.literal(_arg(call, 0, "path_or_buf")
                                if method != "to_parquet"
                                else _arg(call, 0, "path"), scope)
            if path is None:
                return
            hit = self.contracts.for_path(path)
            if hit is None:
                return
            contract, single = hit
            if method == "to_parquet":
                how = "parquet"
                target = f'to_parquet("{path}")'
            else:
                how = _kw(call, "mode")
                how = "w" if how is None else _const(how)
                target = f'{method}("{path}", mode="{how}")'
        if contract is None or contract.get("version") != 2:
            return
        mode = contract.get("write_mode")
        if not isinstance(how, str) or mode not in (
                "append", "merge_on_key", "overwrite_partition"):
            return
        ds, draft = _ds(contract), _draft(contract)
        why = fix = None
        if mode == "append":
            if how == "replace" or (how in ("w", "parquet") and single):
                why = (f"{ds} is appended to, as its contract says, but "
                       f"{target} replaces it with this DataFrame, so every "
                       f"earlier load is lost{draft}")
                how_to = {"to_sql": 'write with if_exists="append"',
                          "to_parquet": "write each load to a new file"}.get(
                    method, 'write with mode="a", or each load to a new file')
                fix = (f"{how_to}, or ask the user whether the contract's "
                       "write mode is right")
        elif mode == "merge_on_key":
            if how in ("append", "a"):
                why = (f"{ds} is merged on its key, as its contract says, but "
                       f"{target} appends, so a rerun adds a second copy of "
                       f"every key{draft}")
                fix = ("write to a staging table and merge (upsert) on the key, "
                       "or ask the user whether the contract's write mode is "
                       "right")
        elif mode == "overwrite_partition":
            partitioned = _kw(call, "partition_cols") is not None
            if how == "replace" or (how in ("w", "parquet") and single
                                    and not partitioned):
                why = (f"{ds} is rewritten one partition at a time, as its "
                       f"contract says, but {target} replaces all of it, so "
                       f"every other partition is lost{draft}")
                fix = ("delete and rewrite only this partition's rows (or write "
                       "with partition_cols), or ask the user whether the "
                       "contract's write mode is right")
        if why:
            self.report(WRITE_MODE, call, why, fix)

    # -- dq/undeclared-status-value -------------------------------------------
    def undeclared(self, test, anchor, op_ne=False):
        if UNDECLARED not in self.rules:
            return
        col, vals, negated = test
        allowed = _declared(col.spec)
        if not allowed:
            return
        bad = [v for v in vals if v not in allowed]
        if not bad:
            return
        listed = ", ".join(repr(v) for v in allowed)
        shown = ", ".join(repr(v) for v in bad)
        result = ("is true for every row" if negated
                  else "matches no rows")
        self.report(
            UNDECLARED, anchor,
            f"{col.ref} is compared to {shown}, which its contract doesn't "
            f"list ({listed}), so the comparison {result}"
            f"{_draft(col.link.contract)}",
            f"use one of the listed values for {col.ref}, or ask the user "
            f"whether the contract should list {shown}")

    def isin(self, call, scope):
        test = self.value_test(call, scope)
        if test is not None:
            self.undeclared(test, call)

    # -- dq/expected-absence-counted-as-missing -------------------------------
    def _absence(self, col, vals, excluded, anchor, shown):
        if ABSENCE not in self.rules:
            return
        contract = col.link.contract
        if contract.get("version") != 2:
            return
        values = col.spec.get("values")
        if not isinstance(values, dict):
            return
        means = {str(k): v.get("means") for k, v in values.items()
                 if isinstance(v, dict)}
        present = [k for k, m in means.items() if m == "present"]
        absent_exp = [k for k, m in means.items() if m == "absent_expected"]
        absent_unexp = [k for k, m in means.items() if m == "absent_unexpected"]
        if not present or not absent_exp or not set(vals) <= set(present):
            return
        left = [v for v in absent_exp if v not in set(excluded)]
        if not left:
            return
        from truequery.checks.sql import _and_list
        gone = _and_list(left)
        if absent_unexp:
            only = (f'{col.name} == {absent_unexp[0]!r}' if len(absent_unexp) == 1
                    else f"{col.name} in {absent_unexp!r}")
            fix = (f"count only the values that mean missing ({only}), or "
                   f"leave out {gone} explicitly")
        else:
            fix = f"leave out {gone} explicitly"
        self.report(
            ABSENCE, anchor,
            f"{gone} in {col.ref} {'is' if len(left) == 1 else 'are'} absent "
            f"on purpose; `{shown}` counts {'it' if len(left) == 1 else 'them'} "
            f"as missing or failed{_draft(contract)}",
            fix)

    def counted_mask(self, node, call, scope):
        """`(df.status != "fetched").sum()`: a negated test, counted."""
        if isinstance(node, ast.Name) and node.id in scope.masks:
            node = scope.masks[node.id]
        parts = _and_parts(node)
        for part in parts:
            test = self.value_test(part, scope)
            if test is None or not test[2]:
                continue
            col, vals, _ = test
            excluded = set(col.link.excluded_for(col.low))
            for other in parts:
                if other is part:
                    continue
                more = self.value_test(other, scope)
                if more is not None and more[0].low == col.low \
                        and more[0].link.contract is col.link.contract:
                    declared = _declared(col.spec) or []
                    excluded |= set(more[1]) if more[2] \
                        else set(declared) - set(more[1])
            self._absence(col, vals, excluded, part, _shown(part))

    def counted_frame(self, node, call, scope):
        """`len(df[df.status != "fetched"])`, `.shape[0]`, `.count()`: a
        frame filtered by a negated test, counted."""
        link = self.link_of(node, scope)
        if link is None and isinstance(node, (ast.Subscript, ast.Attribute)):
            # a column of the filtered frame (df[m]["id"].count()), or its
            # index (len(df[m].index))
            link = self.link_of(node.value, scope)
        if link is None or not link.tests:
            return
        for part, name, vals in link.tests:
            found = link.column(name)
            if not found:
                continue
            col = Col(link, found[0], found[1])
            self._absence(col, list(vals), link.excluded_for(col.low), part,
                          _shown(part))

    # -- sem/unit-mismatch, sem/timezone-mismatch, path comparisons -----------
    def expression(self, scope, node, parents):
        """Compare and BinOp nodes (calls are handled in `call`)."""
        if isinstance(node, ast.Compare) and len(node.ops) == 1:
            op = node.ops[0]
            if isinstance(op, (ast.Eq, ast.NotEq)):
                test = self.value_test(node, scope)
                if test is not None:
                    self.undeclared(test, node)
            self.compare(node, node.left, node.comparators[0], op, scope)
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
            parent = parents.get(id(node))
            if not (isinstance(parent, ast.BinOp)
                    and isinstance(parent.op, (ast.Add, ast.Sub))):
                self.arith(_arith_leaves(node), node, scope)
            if isinstance(node.op, ast.Add):
                self.path_prefix(node, node.left, node.right, scope)
        elif isinstance(node, ast.Subscript) and _const(node.slice) == 0 \
                and isinstance(node.value, ast.Attribute) \
                and node.value.attr == "shape":
            self.counted_frame(node.value.value, node, scope)

    def compare(self, anchor, left, right, op, scope):
        if isinstance(op, (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)):
            self.arith([left, right], anchor, scope, verb="compared")
            self.tz_compare(anchor, left, right, scope)
        if isinstance(op, (ast.Eq, ast.NotEq)):
            self.path_compare(anchor, left, right, scope)

    def _unscaled(self, node, scope):
        """A contract column with a unit, used as is (or through sum(),
        mean(), fillna(), astype() ...)."""
        while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr in _PASSTHROUGH:
            node = node.func.value
        col = self.column(node, scope)
        if col is None or col.link.contract.get("version") != 2:
            return None
        if not (col.spec.get("unit") or col.spec.get("currency")):
            return None
        return col

    def arith(self, operands, anchor, scope, verb="added or subtracted"):
        if UNIT not in self.rules:
            return
        from truequery.checks.sql import _unit_words, _units_differ
        cols = [c for c in (self._unscaled(o, scope) for o in operands) if c]
        pair = next(((a, b) for i, a in enumerate(cols) for b in cols[i + 1:]
                     if _units_differ(a.spec, b.spec)), None)
        if pair is None:
            return
        a, b = pair
        same = a.link.contract is b.link.contract
        ref_a = a.name if same else a.ref
        ref_b = b.name if same else b.ref
        self.report(
            UNIT, anchor,
            f"{ref_a} is in {_unit_words(a.spec)} and {ref_b} is in "
            f"{_unit_words(b.spec)}; they're {verb} without converting one "
            f"to the other{_draft(a.link.contract, b.link.contract)}",
            f"convert {ref_a} or {ref_b} so both are in the same unit first")

    def tz_compare(self, anchor, left, right, scope):
        """Two timestamp columns still as read text, in different declared
        zones, compared: text comparison ignores the zone. (Parsed with
        pd.to_datetime, pandas compares instants or raises, so it isn't
        flagged.)"""
        if TZ not in self.rules:
            return
        sides = [self.column(s, scope) for s in (left, right)]
        if not all(sides):
            return
        a, b = sides
        if any(c.link.contract.get("version") != 2 or c.low not in c.link.raw
               for c in sides):
            return
        tz_a, tz_b = a.spec.get("time_zone"), b.spec.get("time_zone")
        if not tz_a or not tz_b or tz_a == tz_b:
            return
        from truequery.checks.sql import _tz_words
        self.report(
            TZ, anchor,
            f"{a.ref} is in {_tz_words(str(tz_a))} and {b.ref} is in "
            f"{_tz_words(str(tz_b))}; they're compared as text, without a "
            f"conversion, which shifts every boundary by the difference"
            f"{_draft(a.link.contract, b.link.contract)}",
            f"parse both with pd.to_datetime and convert one "
            f"(.dt.tz_localize / .dt.tz_convert) to the other's zone before "
            f"comparing {a.ref} and {b.ref}")

    # -- sem/relative-path-base-mismatch --------------------------------------
    def _path_col(self, node, scope):
        col = self.column(node, scope)
        if col is None or col.link.contract.get("version") != 2 \
                or col.spec.get("type") != "path":
            return None
        return col

    def _declared_default(self, base):
        spec = self.contracts.bases.get(base) if base else None
        if not isinstance(spec, dict) or spec.get("env") \
                or not spec.get("default"):
            return None
        return str(spec["default"])

    def _wrong_prefix(self, literal, col):
        """True when `literal` in front of `col` isn't its declared base."""
        from truequery.checks.sql import _same_dir
        default = self._declared_default(col.spec.get("relative_to"))
        if default is None or _same_dir(literal, default):
            return False
        if literal.startswith(("/", "~")) or re.match(r"^[A-Za-z]:", literal):
            if not default.startswith(("/", "~")):
                return False  # an absolute path may be where the base is
        return True

    def _report_prefix(self, anchor, literal, col):
        base = col.spec.get("relative_to")
        default = self._declared_default(base)
        self.report(
            PATH, anchor,
            f"the code puts {literal!r} in front of {col.ref}, but {col.ref} "
            f"is relative to {base} ({default}), so the paths it builds point "
            f"into the wrong folder{_draft(col.link.contract)}",
            f"put the declared base ({default}) in front of {col.ref}, or ask "
            f"the user whether the contract's base is right")

    def path_prefix(self, node, left, right, scope):
        if PATH not in self.rules:
            return
        literal = self.literal(left, scope)
        if literal is None:
            return
        col = self._path_col(right, scope)
        if col is not None and self._wrong_prefix(literal, col):
            self._report_prefix(node, literal, col)

    def path_lambda(self, call, scope):
        """`df["path"].apply(lambda p: os.path.join("/mnt/x", p))` (or
        `"/mnt/x/" + p`, `Path("/mnt/x") / p`)."""
        if PATH not in self.rules:
            return
        col = self._path_col(call.func.value, scope)
        fn = call.args[0]
        if col is None or not isinstance(fn, ast.Lambda) \
                or len(fn.args.args) != 1:
            return
        param, body = fn.args.args[0].arg, fn.body
        literal = None
        if isinstance(body, ast.BinOp) and isinstance(body.op, (ast.Add, ast.Div)) \
                and isinstance(body.right, ast.Name) and body.right.id == param:
            if isinstance(body.op, ast.Add) or self._is_path(body.left, scope):
                literal = self.literal(body.left, scope)
        elif isinstance(body, ast.Call) and len(body.args) == 2 \
                and isinstance(body.args[1], ast.Name) \
                and body.args[1].id == param \
                and isinstance(body.func, ast.Attribute) \
                and body.func.attr == "join":
            literal = self.literal(body.args[0], scope)
        if literal is not None and self._wrong_prefix(literal, col):
            self._report_prefix(call, literal, col)

    def path_compare(self, anchor, left, right, scope):
        if PATH not in self.rules:
            return
        a, b = self._path_col(left, scope), self._path_col(right, scope)
        if a is not None and b is not None:
            self._path_pair(anchor, a, b)

    def _path_pair(self, anchor, a, b):
        base_a, base_b = a.spec.get("relative_to"), b.spec.get("relative_to")
        if base_a == base_b:
            return

        def describe(base):
            if not base:
                return "a full path"
            default = self._declared_default(base)
            return f"relative to {base} ({default})" if default \
                else f"relative to {base}"

        default = next((self._declared_default(x).rstrip("/") + "/"
                        for x in (base_a, base_b)
                        if x and self._declared_default(x)), None)
        example = f" (for example '{default}' + <relative path>)" \
            if default else ""
        self.report(
            PATH, anchor,
            f"{a.ref} is {describe(base_a)} and {b.ref} is {describe(base_b)}; "
            f"compared directly, they don't match"
            f"{_draft(a.link.contract, b.link.contract)}",
            f"add or strip the declared base{example} before comparing "
            f"{a.ref} and {b.ref}")

    def merge_paths(self, call, left_frame, right_frame, scope):
        """A merge on path columns with different bases."""
        if PATH not in self.rules or _star(call):
            return
        left, right = self.link_of(left_frame, scope), \
            self.link_of(right_frame, scope)
        if left is None or right is None:
            return
        on = _const(_kw(call, "on"))
        l_on, r_on = _const(_kw(call, "left_on")), _const(_kw(call, "right_on"))
        if isinstance(on, str):
            l_on = r_on = on
        if not (isinstance(l_on, str) and isinstance(r_on, str)):
            return
        found_a, found_b = left.column(l_on), right.column(r_on)
        if not (found_a and found_b):
            return
        a, b = Col(left, *found_a), Col(right, *found_b)
        if all(c.link.contract.get("version") == 2
               and c.spec.get("type") == "path" for c in (a, b)):
            self._path_pair(call, a, b)


# -- module helpers --------------------------------------------------------------
def _constants(body) -> dict:
    """Names assigned exactly once in a body, by a plain `NAME = value`
    (nested functions and classes aside): their value node."""
    counts, values = {}, {}
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            counts[node.id] = counts.get(node.id, 0) + 1
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name):
            values[node.targets[0].id] = node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and isinstance(node.target, ast.Name):
            values[node.target.id] = node.value
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            counts[node.target.id] = counts.get(node.target.id, 0) + 1
        stack.extend(ast.iter_child_nodes(node))
    return {name: value for name, value in values.items()
            if counts.get(name) == 1}


def assigned_names(body) -> set:
    """Every name a body assigns (so a module constant it shadows isn't
    used)."""
    names = set()
    stack = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
        stack.extend(ast.iter_child_nodes(node))
    return names


def _query_link(text: str, contracts: Contracts):
    """Link for `pd.read_sql("SELECT ...")`: one contracted table, the
    columns selected as they are, and what its WHERE rules out."""
    import sqlglot
    from sqlglot import exp

    from truequery.checks.sql import _conjuncts, _value_test
    try:
        stmts = [s for s in sqlglot.parse(text) if s is not None]
    except Exception:
        return None
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Select):
        return None
    sel = stmts[0]
    ctes = {c.alias_or_name.lower() for c in sel.find_all(exp.CTE)}
    tables = [t for t in sel.find_all(exp.Table)
              if (t.name or "").lower() not in ctes]
    if len(tables) != 1 or ctes or sel.args.get("joins"):
        return None
    contract = contracts.for_table(tables[0].name, tables[0].db or None)
    if contract is None:
        return None
    only = set()
    for proj in sel.expressions:
        if isinstance(proj, exp.Star):
            only = None
            break
        if isinstance(proj, exp.Column) and not isinstance(proj.this, exp.Star):
            only.add(proj.name.lower())
        elif isinstance(proj, exp.Column):
            only = None
            break
        # an alias or an expression: that output isn't a contract column
    excluded: dict[str, set] = {}
    cols = _columns(contract)
    where = sel.args.get("where")
    for cond in _conjuncts(where.this) if where is not None else []:
        test = _value_test(cond)
        if test is None:
            continue
        low = (test[0].name or "").lower()
        if low not in cols:
            continue
        declared = _declared(cols[low][1]) or []
        ruled = set(test[1]) if test[2] else set(declared) - set(test[1])
        excluded.setdefault(low, set()).update(ruled)
    return Link(contract, only=frozenset(only) if only is not None else None,
                excluded=tuple(sorted((k, frozenset(v))
                                      for k, v in excluded.items())))


def _declared(spec: dict):
    from truequery.checks.sql import _declared_values
    return _declared_values(spec)


def _join(items) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _is_zero(node) -> bool:
    value = _const(node)
    return not isinstance(value, bool) and value in (0, 0.0)


def _string_dtype(node, file) -> bool:
    """str, "string", object, pd.StringDtype(): every column read as text."""
    value = _const(node)
    if isinstance(value, str):
        return value in _STRING_DTYPES
    if isinstance(node, ast.Name):
        return node.id in ("str", "object")
    if isinstance(node, ast.Call) and not node.args:
        return file.pandas_function(node.func) == "StringDtype"
    return False


def _is_mask(node) -> bool:
    return isinstance(node, (ast.Compare, ast.BoolOp)) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Invert)) or (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, (ast.BitAnd, ast.BitOr))) or (
        isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("isin", "eq", "ne", "notna", "isna", "between"))


def _and_parts(node) -> list:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitAnd):
        return _and_parts(node.left) + _and_parts(node.right)
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out = []
        for value in node.values:
            out += _and_parts(value)
        return out
    return [node]


def _or_parts(node) -> list:
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        return _or_parts(node.left) + _or_parts(node.right)
    return [node]


def _arith_leaves(node) -> list:
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        return _arith_leaves(node.left) + _arith_leaves(node.right)
    return [node]


def parent_map(node) -> dict:
    """id(child) -> parent, for one expression."""
    parents = {}
    for parent in ast.walk(node):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents
