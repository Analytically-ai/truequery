"""Contract-free pandas checks: mistakes visible in the code alone.

Reads the file with Python's `ast`; never imports pandas or the code it
reads. Deterministic and offline.

- **Which files:** only a .py file that imports pandas (`import pandas`,
  `import pandas as pd`, `from pandas import ...`).
- **Which variables:** a DataFrame is a name this file assigns from a
  pandas reader or constructor (`pd.read_*`, `pd.DataFrame(...)`,
  `pd.concat`, `pd.merge`, ...), from a method of another known
  DataFrame that returns one, or that is annotated `pd.DataFrame` (a
  parameter, a variable, or a function in this file that returns one).
  Names only, inside one function at a time, with no data flow beyond
  that. When the checker can't tell, it doesn't report.

Each check is backed by a practice record in standard/py/ (its proof
runs pandas in the tests).
"""
from __future__ import annotations

import ast
import hashlib
import re
import warnings

# Cheap pre-filter before parsing: a line that imports pandas.
_IMPORTS_PANDAS = re.compile(r"^[ \t]*(?:import[ \t]+pandas\b|from[ \t]+pandas\b"
                             r"|import[ \t]+[^#\n]*,[ \t]*pandas\b)",
                             re.MULTILINE)

CHAINED = "py/pandas-no-chained-assignment"
ROW_LOOP = "py/pandas-no-row-loop-mutations"
FIRST = "py/pandas-groupby-first-mixes-rows"
DEDUPE = "py/pandas-dedupe-unstable-sort"
MERGE = "py/pandas-merge-validate"
DROPNA = "py/pandas-groupby-dropna"
TO_SQL = "py/pandas-to-sql-replace"

RULES = (CHAINED, ROW_LOOP, FIRST, DEDUPE, MERGE, DROPNA, TO_SQL)

FRAME, SERIES, GROUPBY, GROUPBY_SERIES = "frame", "series", "groupby", \
    "groupby-series"

# pandas functions that return a DataFrame.
_FRAME_FUNCTIONS = frozenset({
    "DataFrame", "concat", "merge", "merge_asof", "merge_ordered",
    "pivot_table", "crosstab", "json_normalize", "get_dummies",
})
# Readers that return something else: a list, any object.
_NOT_FRAME_READERS = frozenset({"read_html", "read_pickle", "read_clipboard"})
# Reader arguments that make it return an iterator (or a dict of sheets).
_ITERATOR_ARGS = frozenset({"chunksize", "iterator"})

# DataFrame methods that return a new DataFrame (with inplace unset).
_FRAME_METHODS = frozenset({
    "abs", "add_prefix", "add_suffix", "assign", "astype", "bfill", "clip",
    "combine_first", "convert_dtypes", "copy", "corr", "cov", "cummax",
    "cummin", "cumprod", "cumsum", "describe", "diff", "drop",
    "drop_duplicates", "dropna", "explode", "ffill", "fillna", "filter",
    "head", "infer_objects", "interpolate", "isna", "isnull", "join", "map",
    "mask", "melt", "merge", "nlargest", "notna", "notnull", "nsmallest",
    "pct_change", "pivot", "pivot_table", "query", "rank", "reindex",
    "rename", "rename_axis", "replace", "reset_index", "round", "sample",
    "select_dtypes", "set_axis", "set_index", "shift", "sort_index",
    "sort_values", "tail", "transpose", "truncate", "where",
})
# GroupBy methods that return a DataFrame on a frame groupby.
_GROUPBY_FRAME_METHODS = frozenset({
    "agg", "aggregate", "count", "cummax", "cummin", "cumsum", "first",
    "head", "last", "max", "mean", "median", "min", "nth", "nunique",
    "prod", "std", "sum", "tail", "transform", "var",
})
# Series methods that return a DataFrame.
_SERIES_TO_FRAME = frozenset({"to_frame", "unstack"})
# Attributes of a DataFrame that are not columns.
_FRAME_ATTRIBUTES = frozenset({
    "T", "at", "attrs", "axes", "columns", "dtypes", "empty", "flags",
    "iat", "iloc", "index", "loc", "ndim", "plot", "shape", "size",
    "sparse", "style", "values",
})
_INDEXERS = frozenset({"loc", "iloc", "at", "iat"})
# Methods that keep row order, between a sort and a drop_duplicates.
_ORDER_KEEPING = frozenset({
    "assign", "astype", "copy", "drop", "dropna", "fillna", "filter",
    "query", "rename", "reset_index",
})
_UNSTABLE_KINDS = frozenset({"quicksort", "heapsort"})


def imports_pandas(text: str) -> bool:
    """True when the file imports pandas (checked on the parsed code)."""
    if not _IMPORTS_PANDAS.search(text):
        return False
    tree = _parse(text)
    return tree is not None and _pandas_names(tree)[0] is not None


def _parse(text):
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # e.g. invalid escapes
            return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None


def _pandas_names(tree):
    """(module aliases, {local name: pandas function}) or (None, {}) when
    the file doesn't import pandas."""
    aliases, functions, found = set(), {}, False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "pandas":
                    aliases.add(alias.asname or "pandas")
                    found = True
                elif alias.name.startswith("pandas."):
                    found = True
                    if not alias.asname:
                        aliases.add("pandas")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and (
                node.module == "pandas"
                or (node.module or "").startswith("pandas.")):
            found = True
            if node.module == "pandas":
                for alias in node.names:
                    functions[alias.asname or alias.name] = alias.name
    return (aliases if found else None), functions


def _const(node):
    return node.value if isinstance(node, ast.Constant) else None


def _kw(call, name):
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _has_star_kwargs(call):
    return any(k.arg is None for k in call.keywords)


def _arg(call, index, name):
    """A call argument by keyword, else by position."""
    value = _kw(call, name)
    if value is None and len(call.args) > index:
        value = call.args[index]
    return value


def _inplace(call):
    return _const(_kw(call, "inplace")) is True


class _File:
    """Names this file gives to pandas, and the functions it annotates as
    returning a DataFrame."""

    def __init__(self, tree, aliases, functions):
        self.aliases = aliases
        self.functions = functions
        self.frame_functions = set()
        for node in ast.walk(tree):
            if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.returns is not None
                    and self.is_frame_annotation(node.returns)):
                self.frame_functions.add(node.name)

    def pandas_function(self, func):
        """The pandas function a call target names, or None."""
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in self.aliases):
            return func.attr
        if (isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "DataFrame"
                and isinstance(func.value.value, ast.Name)
                and func.value.value.id in self.aliases
                and func.attr in ("from_dict", "from_records")):
            return "DataFrame"
        if isinstance(func, ast.Name) and func.id in self.functions:
            return self.functions[func.id]
        return None

    def is_frame_annotation(self, node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            text = node.value.strip()
            return text in {f"{a}.DataFrame" for a in self.aliases} or (
                text == "DataFrame" and self.functions.get(
                    "DataFrame") == "DataFrame")
        if isinstance(node, ast.Attribute):
            return (node.attr == "DataFrame"
                    and isinstance(node.value, ast.Name)
                    and node.value.id in self.aliases)
        if isinstance(node, ast.Name):
            return self.functions.get(node.id) == "DataFrame"
        return False


class _Scope:
    """One function (or the module) at a time, statements in order."""

    def __init__(self, file, rules, out, frames=()):
        self.file = file
        self.rules = rules
        self.out = out
        self.env = {name: FRAME for name in frames}
        # name -> the line of a sort_values on one key, default sort
        self.sorted_unstable = {}

    # -- what an expression is ---------------------------------------------
    def kind(self, node):
        if isinstance(node, ast.Name):
            return self.env.get(node.id)
        if isinstance(node, ast.Call):
            return self._call_kind(node)
        if isinstance(node, ast.Subscript):
            return self._subscript_kind(node)
        if isinstance(node, ast.Attribute):
            base = self.kind(node.value)
            if base == FRAME and node.attr == "T":
                return FRAME
            if base == FRAME and node.attr not in _FRAME_ATTRIBUTES:
                return SERIES
            return None
        return None

    def _call_kind(self, call):
        func = call.func
        name = self.file.pandas_function(func)
        if name is not None:
            if name in _FRAME_FUNCTIONS:
                return FRAME
            if (name.startswith("read_") and name not in _NOT_FRAME_READERS
                    and not any(_kw(call, a) is not None
                                for a in _ITERATOR_ARGS)
                    and not _has_star_kwargs(call)):
                sheet = _kw(call, "sheet_name")
                typ = _const(_kw(call, "typ"))
                if sheet is not None and not isinstance(_const(sheet),
                                                        (str, int)):
                    return None
                if typ not in (None, "frame"):
                    return None
                return FRAME
            return None
        if isinstance(func, ast.Name) and func.id in self.file.frame_functions:
            return FRAME
        if not isinstance(func, ast.Attribute):
            return None
        base = self.kind(func.value)
        if base == FRAME:
            if func.attr == "groupby":
                return GROUPBY
            if func.attr in _FRAME_METHODS and not _inplace(call):
                return FRAME
        elif base == GROUPBY:
            if func.attr == "size":
                return SERIES
            if func.attr in _GROUPBY_FRAME_METHODS:
                return FRAME
        elif base == SERIES:
            if func.attr in _SERIES_TO_FRAME:
                return FRAME
            if (func.attr == "reset_index"
                    and _const(_kw(call, "drop")) is not True
                    and not _inplace(call)):
                return FRAME
        return None

    def _subscript_kind(self, node):
        value, index = node.value, node.slice
        if (isinstance(value, ast.Attribute) and value.attr in ("loc", "iloc")
                and self.kind(value.value) == FRAME):
            if isinstance(index, ast.Tuple) and len(index.elts) == 2:
                cols = index.elts[1]
                if isinstance(cols, (ast.List, ast.Slice)):
                    return FRAME
                if isinstance(_const(cols), (str, int)):
                    return SERIES
                return None
            return FRAME if _is_row_selector(index) else None
        base = self.kind(value)
        if base == FRAME:
            if isinstance(_const(index), str):
                return SERIES
            if isinstance(index, ast.List) or _is_row_selector(index):
                return FRAME
            return None
        if base == GROUPBY:
            if isinstance(index, ast.List):
                return GROUPBY
            if isinstance(_const(index), str):
                return GROUPBY_SERIES
        return None

    # -- walking ------------------------------------------------------------
    def run(self, body):
        for stmt in body:
            self.statement(stmt)

    def statement(self, stmt):
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            params = [a for a in (stmt.args.posonlyargs + stmt.args.args
                                  + stmt.args.kwonlyargs)
                      if a.annotation is not None
                      and self.file.is_frame_annotation(a.annotation)]
            _Scope(self.file, self.rules, self.out,
                   [a.arg for a in params]).run(stmt.body)
            return
        if isinstance(stmt, ast.ClassDef):
            _Scope(self.file, self.rules, self.out).run(stmt.body)
            return
        if isinstance(stmt, ast.Assign):
            self.expressions(stmt.value)
            for target in stmt.targets:
                self.check_target(stmt, target)
            kind = self.kind(stmt.value)
            for target in stmt.targets:
                self.bind(target, kind, stmt.value)
            return
        if isinstance(stmt, ast.AnnAssign):
            if stmt.value is not None:
                self.expressions(stmt.value)
            self.check_target(stmt, stmt.target)
            kind = (FRAME if self.file.is_frame_annotation(stmt.annotation)
                    else self.kind(stmt.value) if stmt.value is not None
                    else None)
            if stmt.value is not None or kind == FRAME:
                self.bind(stmt.target, kind, stmt.value)
            return
        if isinstance(stmt, ast.AugAssign):
            self.expressions(stmt.value)
            self.check_target(stmt, stmt.target)
            return
        if isinstance(stmt, (ast.For, ast.AsyncFor)):
            self.expressions(stmt.iter)
            self.row_loop(stmt)
            group = None
            if (self.kind(stmt.iter) == GROUPBY
                    and isinstance(stmt.target, ast.Tuple)
                    and len(stmt.target.elts) == 2):
                group = stmt.target.elts[1]
            self.bind(stmt.target, None, None)
            if isinstance(group, ast.Name):
                self.env[group.id] = FRAME
            self.run(stmt.body)
            self.run(stmt.orelse)
            return
        if isinstance(stmt, (ast.With, ast.AsyncWith)):
            for item in stmt.items:
                self.expressions(item.context_expr)
                if item.optional_vars is not None:
                    self.bind(item.optional_vars, None, None)
            self.run(stmt.body)
            return
        if isinstance(stmt, (ast.If, ast.While)):
            self.expressions(stmt.test)
            self.run(stmt.body)
            self.run(stmt.orelse)
            return
        if isinstance(stmt, ast.Try) or type(stmt).__name__ == "TryStar":
            self.run(stmt.body)
            for handler in stmt.handlers:
                if handler.name:
                    self.env.pop(handler.name, None)
                self.run(handler.body)
            self.run(stmt.orelse)
            self.run(stmt.finalbody)
            return
        if isinstance(stmt, ast.Match):
            self.expressions(stmt.subject)
            for case in stmt.cases:
                self.run(case.body)
            return
        if isinstance(stmt, ast.Expr):
            self.expressions(stmt.value)
            self.inplace_on_selection(stmt)
            return
        if isinstance(stmt, ast.Delete):
            for target in stmt.targets:
                self.bind(target, None, None)
            return
        for child in ast.iter_child_nodes(stmt):
            if isinstance(child, ast.expr):
                self.expressions(child)

    def bind(self, target, kind, value):
        if isinstance(target, ast.Name):
            self.sorted_unstable.pop(target.id, None)
            if kind in (FRAME, GROUPBY):
                self.env[target.id] = kind
            else:
                self.env.pop(target.id, None)
            if value is not None and kind == FRAME:
                line = self._unstable_sort_line(value)
                if line is not None:
                    self.sorted_unstable[target.id] = line
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self.bind(elt, None, None)
        elif isinstance(target, ast.Starred):
            self.bind(target.value, None, None)

    def expressions(self, node):
        """Run the call checks on every call inside one expression, in
        source order (lambdas and comprehensions included)."""
        calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
        calls.sort(key=lambda n: (n.lineno, n.col_offset))
        for call in calls:
            self.call(call)

    # -- the checks -----------------------------------------------------------
    def report(self, rule, node, why, fix):
        if rule in self.rules:
            line, col = _position(node)
            self.out.append((rule, line, col, why, fix))

    def call(self, call):
        func = call.func
        if not isinstance(func, ast.Attribute):
            if self.file.pandas_function(func) == "merge":
                self.merge(call)
            return
        method = func.attr
        base = self.kind(func.value)
        if self.file.pandas_function(func) == "merge":
            self.merge(call)
        elif method in ("merge", "join") and base == FRAME:
            self.merge(call)
        if method == "groupby" and base == FRAME:
            self.groupby(call)
        mixes = base == GROUPBY and not _one_column(func.value)
        if method in ("first", "last") and mixes:
            if _const(_kw(call, "skipna")) is not False:
                self.first(call, method)
        if (method in ("agg", "aggregate") and mixes
                and len(call.args) == 1 and not call.keywords
                and _const(call.args[0]) in ("first", "last")):
            self.first(call, _const(call.args[0]))
        if method == "drop_duplicates" and base == FRAME:
            self.dedupe(call)
        if method == "sort_values" and base == FRAME and _inplace(call) \
                and isinstance(func.value, ast.Name):
            if self._unstable_sort(call):
                self.sorted_unstable[func.value.id] = _position(call)[0]
            else:
                self.sorted_unstable.pop(func.value.id, None)
        if method == "to_sql" and _const(_kw(call, "if_exists")) == "replace":
            self.report(
                TO_SQL, call,
                "to_sql(if_exists=\"replace\") drops the table and creates a "
                "new one with types pandas guesses: its keys, constraints, "
                "indexes, column types and grants are lost without an error",
                "empty the table (DELETE or TRUNCATE) and write with "
                "if_exists=\"append\", so the table's definition stays")

    def merge(self, call):
        if _kw(call, "validate") is not None or _has_star_kwargs(call):
            return
        if _const(_kw(call, "how")) == "cross":
            return
        self.report(
            MERGE, call,
            "merge/join with no validate=: a duplicate key on either side "
            "multiplies rows and inflates every total after it, with no "
            "error",
            "state the expected cardinality: validate=\"many_to_one\" (or "
            "\"one_to_one\", \"one_to_many\"); pandas then raises on a "
            "duplicate key")

    def groupby(self, call):
        if _kw(call, "dropna") is not None or _has_star_kwargs(call):
            return
        self.report(
            DROPNA, call,
            "groupby with no dropna=: rows whose key is NaN/None are "
            "dropped from every group by default",
            "pass dropna=False to keep a group for missing keys, or "
            "dropna=True to say the drop is intended")

    def first(self, call, method):
        self.report(
            FIRST, call,
            f"groupby(...).{method}() takes the {method} non-null value of "
            "each column separately, so one output row can mix values from "
            "different input rows",
            "to keep whole rows: sort, then .nth(0) / .nth(-1) (or "
            "drop_duplicates(subset=keys)); or pass skipna=False")

    def dedupe(self, call):
        if not _dedupe_picks_a_row(call):
            return
        line = self._unstable_sort_line(call.func.value)
        receiver = call.func.value
        if line is None and isinstance(receiver, ast.Name):
            line = self.sorted_unstable.get(receiver.id)
        if line is None:
            return
        self.report(
            DEDUPE, call,
            f"drop_duplicates keeps a row picked by sort_values on line "
            f"{line}: one key, default (unstable) sort, so rows that tie on "
            "the key come out in any order",
            "sort by the key plus a unique tiebreaker column, or pass "
            "kind=\"stable\" if the input order is the intended tiebreaker")

    def _unstable_sort(self, call):
        """sort_values on a single key with the default (unstable) sort."""
        if _has_star_kwargs(call):
            return False
        kind = _kw(call, "kind")
        if kind is not None and _const(kind) not in _UNSTABLE_KINDS:
            return False
        by = _arg(call, 0, "by")
        if isinstance(by, (ast.List, ast.Tuple)):
            return len(by.elts) == 1 and isinstance(_const(by.elts[0]), str)
        return isinstance(_const(by), str)

    def _unstable_sort_line(self, node):
        """Line of an unstable one-key sort_values this frame expression
        was built from, through methods that keep row order; else None."""
        while isinstance(node, ast.Call) and isinstance(node.func,
                                                        ast.Attribute):
            method = node.func.attr
            if method == "sort_values":
                if (self.kind(node.func.value) == FRAME
                        and self._unstable_sort(node)):
                    return _position(node)[0]
                return None
            if method not in _ORDER_KEEPING:
                return None
            node = node.func.value
        return None

    def check_target(self, stmt, target):
        """A write through two steps: `df[a][b] = v`, `df.loc[m][c] = v`,
        `df.query(...)[c] = v`, `df.col[m] = v`."""
        if not isinstance(target, ast.Subscript):
            return
        base = target.value
        if isinstance(base, ast.Attribute) and base.attr in _INDEXERS:
            base = base.value
        if self._is_selection(base):
            self.report(
                CHAINED, stmt,
                "chained assignment: the write lands on a temporary copy, "
                "so the DataFrame never changes",
                "assign in one step on the DataFrame itself: "
                "df.loc[rows, \"col\"] = value")

    def inplace_on_selection(self, stmt):
        """`df["col"].fillna(0, inplace=True)`: an in-place method on a
        column or rows taken from a DataFrame."""
        call = stmt.value
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and _inplace(call) and self._is_selection(call.func.value)):
            self.report(
                CHAINED, stmt,
                f"{call.func.attr}(inplace=True) on a column or rows taken "
                "from a DataFrame changes a temporary copy, so the "
                "DataFrame never changes",
                "assign the result back: df[\"col\"] = "
                f"df[\"col\"].{call.func.attr}(...)")

    def _is_selection(self, node):
        """Rows or a column taken from a known DataFrame (an intermediate
        object, not the DataFrame itself)."""
        if isinstance(node, ast.Subscript):
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr in ("loc",
                                                                  "iloc"):
                return self.kind(value.value) == FRAME
            return self.kind(value) == FRAME
        if isinstance(node, ast.Attribute):
            return (node.attr not in _FRAME_ATTRIBUTES
                    and self.kind(node.value) == FRAME)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            return (node.func.attr in _FRAME_METHODS
                    and not _inplace(node)
                    and self.kind(node.func.value) == FRAME)
        return False

    def row_loop(self, loop):
        """Writes to `row` inside `for i, row in df.iterrows()`, when the
        row is never used as a whole afterwards (passed on, stored,
        returned): such writes are thrown away."""
        it = loop.iter
        if not (isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "iterrows" and not it.args
                and not it.keywords):
            return
        target = loop.target
        if not (isinstance(target, ast.Tuple) and len(target.elts) == 2
                and isinstance(target.elts[1], ast.Name)):
            return
        row = target.elts[1].id
        writes, escapes = [], False
        for node in _walk_body(loop.body):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = (node.targets if isinstance(node, ast.Assign)
                           else [node.target])
                for t in targets:
                    if _names_row(t, row):
                        writes.append(node)
                    elif any(isinstance(n, ast.Name) and n.id == row
                             and isinstance(n.ctx, ast.Store)
                             for n in ast.walk(t)):
                        escapes = True  # row rebound: another object
        for parent, child in _parents(loop.body):
            if not (isinstance(child, ast.Name) and child.id == row
                    and isinstance(child.ctx, ast.Load)):
                continue
            if isinstance(parent, ast.Subscript) and parent.value is child:
                continue  # row["x"]: reads (or writes) one value
            if isinstance(parent, ast.Attribute) and parent.value is child:
                if parent.attr == "get" or not _is_called(parent, loop.body):
                    continue  # row.x, row.get("x"): one value
            escapes = True
        if escapes:
            return
        for node in writes:
            self.report(
                ROW_LOOP, node,
                "write to the row of an iterrows() loop: the row is a copy, "
                "so the change never reaches the DataFrame and is thrown "
                "away",
                "compute the column without the loop (df[\"c\"] = ...), or "
                "write through df.loc[index, \"c\"] = value")


def _position(node):
    """(line, 1-based column) of a finding: a method call is placed at the
    method's name (`.merge` in a long chain), anything else at its start."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        func = node.func
        if func.end_lineno is not None and func.end_col_offset is not None:
            return func.end_lineno, func.end_col_offset - len(func.attr) + 1
    return node.lineno, node.col_offset + 1


def _one_column(node):
    """`g[["v"]]`: a groupby narrowed to one column, which can't mix
    values from different rows (like `g["v"]`)."""
    return (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.List)
            and len(node.slice.elts) == 1
            and not isinstance(node.slice.elts[0], ast.Starred))


def _is_row_selector(index):
    return isinstance(index, (ast.Compare, ast.BoolOp, ast.Slice)) or (
        isinstance(index, ast.UnaryOp) and isinstance(index.op, ast.Invert))


def _dedupe_picks_a_row(call):
    """drop_duplicates on a subset of columns that keeps one row (keep
    first or last): which row survives depends on the order."""
    if _has_star_kwargs(call):
        return False
    subset = _arg(call, 0, "subset")
    if subset is None or (isinstance(subset, ast.Constant)
                          and subset.value is None):
        return False  # whole-row duplicates: any survivor is the same
    keep = _kw(call, "keep")
    if keep is None and len(call.args) > 1:
        keep = call.args[1]
    return keep is None or _const(keep) in ("first", "last")


def _names_row(target, row):
    """`row[...] = v`, `row.x = v`, `row.at[...] = v`."""
    node = target
    if isinstance(node, ast.Subscript):
        node = node.value
        if isinstance(node, ast.Attribute) and node.attr in _INDEXERS:
            node = node.value
    elif isinstance(node, ast.Attribute):
        node = node.value
    else:
        return False
    return isinstance(node, ast.Name) and node.id == row


def _walk_body(body):
    """Every node of a loop body, not entering nested functions/classes."""
    stack = list(reversed(body))
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef, ast.Lambda)):
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _parents(body):
    """(parent, child) pairs over a loop body, nested functions included
    (a closure that captures the row uses it)."""
    for stmt in body:
        for parent in ast.walk(stmt):
            for child in ast.iter_child_nodes(parent):
                yield parent, child


def _is_called(attr, body):
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call) and node.func is attr:
                return True
    return False


def check(text: str, rules) -> tuple[bool, list]:
    """(ran, [(rule, line, col, why, fix)]) for one .py file.

    `rules` is the set of pandas rules to run. `ran` is True only when the
    file imports pandas, parses, and at least one rule ran on it.
    """
    rules = set(rules) & set(RULES)
    if not rules or not _IMPORTS_PANDAS.search(text):
        return False, []
    tree = _parse(text)
    if tree is None:
        return False, []
    aliases, functions = _pandas_names(tree)
    if aliases is None:
        return False, []
    out = []
    _Scope(_File(tree, aliases, functions), rules, out).run(tree.body)
    out = sorted(set(out), key=lambda f: (f[1], f[2], f[0]))
    return True, out


def statement_spans(text: str) -> list[tuple]:
    """(first, last, sha1-12) for each Python statement of a file that
    imports pandas: simple statements whole, compound ones by their
    header. Ignores and git baselines match pandas findings on these,
    so they follow a statement when lines above it move."""
    if not _IMPORTS_PANDAS.search(text):
        return []
    tree = _parse(text)
    if tree is None or _pandas_names(tree)[0] is None:
        return []
    spans = []

    def visit(body, where):
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                visit(stmt.body, f"{where}.{stmt.name}")
                continue
            blocks = [getattr(stmt, f, None) for f in
                      ("body", "orelse", "finalbody")]
            blocks = [b for b in blocks if isinstance(b, list) and b]
            handlers = getattr(stmt, "handlers", None) or []
            cases = getattr(stmt, "cases", None) or []
            if blocks or handlers or cases:
                first_inner = min(
                    [b[0].lineno for b in blocks]
                    + [h.lineno for h in handlers]
                    + [c.pattern.lineno for c in cases])
                last = max(stmt.lineno, first_inner - 1)
                header = "\n".join(text.splitlines()[stmt.lineno - 1:last])
                _add(spans, stmt.lineno, last, where, header)
                for block in blocks:
                    visit(block, where)
                for handler in handlers:
                    visit(handler.body, where)
                for case in cases:
                    visit(case.body, where)
                continue
            source = ast.get_source_segment(text, stmt) or ""
            _add(spans, stmt.lineno, stmt.end_lineno or stmt.lineno, where,
                 source)

    visit(tree.body, "")
    return spans


def _add(spans, first, last, where, source):
    norm = re.sub(r"\s+", " ", source).strip()
    digest = hashlib.sha1(f"{where}\n{norm}".encode("utf-8")).hexdigest()[:12]
    spans.append((first, last, digest))


# slug -> implementing function, for the standard<->checks CI test.
CHECKS = {rule: check for rule in RULES}
