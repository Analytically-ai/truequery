"""SQL fragments inside Python files.

Finds the SQL a .py file passes to known call sites (the detection 0.1.1
used to list such files), rebuilds each fragment's text, and maps every
character of it back to where it is written in the Python source, so a
finding inside the SQL points at the Python file, line and column.

Fragment statuses:

- ``static``: a string constant; implicit or ``+`` concatenation of
  constants; a bare name assigned one of these in the same file.
- ``templated``: an f-string with placeholders, ``.format()`` or ``%``
  applied to a constant, or ``+`` with a non-constant operand. Each
  placeholder becomes a neutral identifier (``__tq_p1``, ``__tq_p2``...).
- ``dynamic``: SQL the program builds at run time (in a loop, returned
  by a function, imported from another module, a name with no
  assignment in this file). Never checked; listed with its reason.
- ``unparsed``: static or templated text that doesn't parse as SQL. The
  engine sets this status after trying to parse it.

Stdlib only (``ast``), offline, deterministic: fragments come back
sorted by their position in the file.
"""

from __future__ import annotations

import ast
import bisect
import re
import unicodedata
from dataclasses import dataclass, field

STATIC, TEMPLATED, DYNAMIC, UNPARSED = (
    "static", "templated", "dynamic", "unparsed")
CHECKED = (STATIC, TEMPLATED)
REASONS = {DYNAMIC: "built at run time", UNPARSED: "doesn't parse as SQL"}
PLACEHOLDER = "__tq_p"
_PLACEHOLDER_RE = re.compile(PLACEHOLDER + r"(\d+)")

SQL_KEYWORDS = ("SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE",
                "CREATE", "REPLACE", "DROP")

# Call sites where a string is plausibly SQL. Matched by function name
# only (pd.read_sql, conn.execute, spark.sql, sqlalchemy text, ...), plus
# any `sql=` keyword; the string content decides, so a non-SQL string
# at one of these sites never matches.
SQL_CALL_NAMES = frozenset({
    "execute", "executemany", "executescript", "exec_driver_sql",
    "read_sql", "read_sql_query", "read_sql_table", "text", "sql",
})

# Dialect inferred from a file's imports: the one table. A module matches
# an entry when it is the entry or a submodule of it. Imports pointing at
# two different dialects mean auto.
IMPORT_DIALECTS = (
    ("sqlite3", "sqlite"),
    ("duckdb", "duckdb"),
    ("psycopg", "postgres"),
    ("psycopg2", "postgres"),
    ("asyncpg", "postgres"),
    ("pymysql", "mysql"),
    ("mysql.connector", "mysql"),
    ("MySQLdb", "mysql"),
    ("pyspark", "spark"),
    ("snowflake.connector", "snowflake"),
    ("snowflake.snowpark", "snowflake"),
    ("google.cloud.bigquery", "bigquery"),
)

# Libraries whose import says "this file talks to a database". Only used
# to decide whether an argument we can't see (a parameter, a function's
# return value) at a SQL call site is SQL built at run time.
SQL_LIBRARIES = tuple(module for module, _ in IMPORT_DIALECTS) + (
    "sqlalchemy", "sqlmodel", "pandas", "polars", "pyodbc", "pymssql",
    "oracledb", "cx_Oracle", "MySQLdb", "aiosqlite", "aiomysql", "aiopg",
    "pg8000", "databricks", "redshift_connector", "trino", "prestodb",
    "pyhive", "clickhouse_driver", "clickhouse_connect", "django.db",
    "peewee", "snowflake", "airflow.providers", "teradatasql",
    "vertica_python", "ibm_db", "jaydebeapi", "adbc_driver_manager",
)

# Names that say "this holds SQL" (a parameter called `query`, an
# attribute `self.sql`, a function `build_query()`).
_SQL_NAME = re.compile(r"sql|query|queries|stmt|statement", re.IGNORECASE)
_SQL_CALLEE = re.compile(
    r"sql|^(build|get|make|render|create|compose|generate|format)_?\w*"
    r"(query|queries|stmt|statement)$", re.IGNORECASE)
# SQL words deeper in a string that doesn't start with a keyword.
_SQL_WORDS = re.compile(
    r"\bSELECT\b.*\bFROM\b|\bINSERT\s+INTO\b|\bDELETE\s+FROM\b"
    r"|\bUPDATE\b.*\bSET\b|\bFROM\b.*\bWHERE\b",
    re.IGNORECASE | re.DOTALL)

# Query-builder calls (SQLAlchemy Core/ORM and friends): they build a
# query object, not SQL text, so there is no string to check or list.
_BUILDERS = frozenset({
    "select", "insert", "update", "delete", "union", "union_all",
    "intersect", "except_", "exists", "join", "outerjoin", "where",
    "filter", "filter_by", "order_by", "group_by", "having", "limit",
    "offset", "values", "returning", "options", "query", "select_from",
    "subquery", "cte", "alias", "scalar_subquery", "with_only_columns",
    "on_conflict_do_update", "on_conflict_do_nothing", "bindparams",
    "columns", "execution_options", "table", "Table", "literal_column",
    "distinct", "correlate", "prefix_with", "suffix_with", "from_select",
    # Django querysets
    "exclude", "annotate", "values_list", "only", "defer", "all",
    "select_related", "prefetch_related", "aggregate",
})
# Sites that only ever take SQL: an argument we can't see there is SQL
# built at run time, whatever the receiver.
_SQL_ONLY_SITES = frozenset({"read_sql", "read_sql_query", "exec_driver_sql",
                             "executescript", "text"})
# A receiver that looks like a database handle (cursor, conn, session,
# db, engine, spark, ...), matched per identifier in the chain. Alembic's
# `op` counts only when imported from alembic (an Airflow `op` is an
# operator).
_DB_HANDLE = re.compile(
    r"(?:^|_)(?:cur|cursor|conn|connection|con|cx|cnx|db|database|session|"
    r"sess|engine|spark|client|backend|hook|warehouse|bq|pg|ch|duck|"
    r"duckdb|sqlite|snowflake|redshift|trino|presto|hive|athena|dbapi)"
    r"(?:_|\d|$)|sql", re.IGNORECASE)
# Calls that only change whitespace: the SQL inside is checked as written.
_TRANSPARENT_METHODS = frozenset({"strip", "lstrip", "rstrip"})
_TRANSPARENT_FUNCS = frozenset({"dedent", "cleandoc"})

# Cheap pre-filter: a file with none of these can't hold a fragment.
_MAYBE = re.compile(r"execute|exec_driver_sql|read_sql|text\s*\(|"
                    r"\bsql\s*[(=]")


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
    """Static leading text of a string expression, or None (the 0.1.1
    detection: constants give their value; f-strings the literal prefix
    before the first placeholder; `+` the left prefix, or the right
    when the left has none)."""
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


def _assigned_values(tree):
    """Name -> value nodes assigned to it in this file.

    Plain `name = value` (one or several `Name` targets) and
    `name: type = value`: same-file, by-name lookup. No data flow, no
    imports.
    """
    out: dict[str, list] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    out.setdefault(target.id, []).append(node.value)
        elif (isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)
              and node.value is not None):
            out.setdefault(node.target.id, []).append(node.value)
    return out


def _is_composable(node):
    """psycopg's sql.SQL("...") (a SQL template object)."""
    return (isinstance(node, ast.Call) and _callee(node.func) == "SQL"
            and len(node.args) == 1 and not node.keywords)


def _callee(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _root_name(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.func if isinstance(node, ast.Call) else node.value
    return node.id if isinstance(node, ast.Name) else ""


def imported_modules(tree) -> list[str]:
    """Every absolute module an import statement names (for `from a.b
    import c`, both a.b and a.b.c: c may be a submodule)."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.append(node.module)
            out.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return out


def _matches_module(module, prefix):
    return module == prefix or module.startswith(prefix + ".")


def dialect_for_imports(modules) -> str | None:
    """The dialect the IMPORT_DIALECTS table gives these modules; None
    (auto) when none match or two different dialects do."""
    found = {dialect for module in modules
             for prefix, dialect in IMPORT_DIALECTS
             if _matches_module(module, prefix)}
    return found.pop() if len(found) == 1 else None


# -- source positions -----------------------------------------------------------
class _Source:
    """Char-index <-> (line, col) for the Python text; ast columns are
    UTF-8 byte offsets, findings use 1-based character columns."""

    def __init__(self, text):
        self.text = text
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]

    def index(self, lineno, col_bytes):
        start = self.starts[lineno - 1]
        end = (self.starts[lineno] if lineno < len(self.starts)
               else len(self.text))
        raw = self.text[start:end].encode("utf-8")[:col_bytes]
        return start + len(raw.decode("utf-8", errors="ignore"))

    def pos(self, idx):
        line = bisect.bisect_right(self.starts, idx)
        return line, idx - self.starts[line - 1] + 1


# -- string literal scanning ------------------------------------------------------
# A cell is (char, source index, display): char is one SQL character, or
# None for a placeholder whose source text is `display`.

_SIMPLE_ESCAPES = {"\\": "\\", "'": "'", '"': '"', "a": "\a", "b": "\b",
                   "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_PREFIX_CHARS = "rRbBuUfFtT"


def _skip_gap(text, i, end):
    """Skip what may sit between implicitly concatenated literals."""
    while i < end:
        ch = text[i]
        if ch in " \t\n\r\f":
            i += 1
        elif ch == "\\" and text.startswith("\n", i + 1):
            i += 2
        elif ch == "#":
            j = text.find("\n", i)
            i = end if j == -1 else j
        else:
            break
    return i


def _string_end(text, i):
    """Index after the string literal whose quote (or prefix) is at i."""
    j = i
    while j < len(text) and text[j] in _PREFIX_CHARS:
        j += 1
    if j >= len(text) or text[j] not in "'\"":
        return None
    token = _scan_token(text, i)
    return token[3] if token else None


def _field_end(text, i):
    """Index after the `}` closing the f-string field opened at i."""
    depth, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch in "'\"":
            end = _string_end(text, i)
            if end is None:
                return None
            i = end
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _scan_token(text, i):
    """(prefix, body start, body end, token end) of the literal at i."""
    j = i
    while j < len(text) and text[j] in _PREFIX_CHARS:
        j += 1
    if j >= len(text) or text[j] not in "'\"":
        return None
    prefix = text[i:j].lower()
    quote = text[j] * 3 if text.startswith(text[j] * 3, j) else text[j]
    raw, fmt = "r" in prefix, "f" in prefix
    body = k = j + len(quote)
    n = len(text)
    while k < n:
        ch = text[k]
        if ch == "\\":
            nxt = text[k + 1:k + 2]
            if not raw and nxt == "N" and text.startswith("{", k + 2):
                close = text.find("}", k + 3)
                if close == -1:
                    return None
                k = close + 1
            elif nxt in ("\\", "'", '"', "\n") or not raw:
                k += 2
            else:
                k += 1
            continue
        if fmt and ch == "{":
            if text.startswith("{{", k):
                k += 2
                continue
            k = _field_end(text, k)
            if k is None:
                return None
            continue
        if text.startswith(quote, k):
            return prefix, body, k, k + len(quote)
        if ch == "\n" and len(quote) == 1:
            return None
        k += 1
    return None


def _decode(text, prefix, body, end, cells):
    """Append the cells of one literal body: escapes decoded, each SQL
    character pointing at where it is written (an escape at its
    backslash), f-string fields as placeholders."""
    raw, fmt = "r" in prefix, "f" in prefix
    i = body
    while i < end:
        ch = text[i]
        if fmt and ch == "{":
            if text.startswith("{{", i):
                cells.append(("{", i, None))
                i += 2
                continue
            close = _field_end(text, i)
            if close is None or close > end:
                return False
            cells.append((None, i, text[i:close]))
            i = close
            continue
        if fmt and ch == "}" and text.startswith("}}", i):
            cells.append(("}", i, None))
            i += 2
            continue
        if ch == "\\" and not raw and i + 1 < end:
            nxt = text[i + 1]
            if nxt == "\n":
                i += 2
                continue
            if nxt in _SIMPLE_ESCAPES:
                cells.append((_SIMPLE_ESCAPES[nxt], i, None))
                i += 2
                continue
            if nxt in "01234567":
                j = i + 1
                while j < min(i + 4, end) and text[j] in "01234567":
                    j += 1
                cells.append((chr(int(text[i + 1:j], 8)), i, None))
                i = j
                continue
            width = {"x": 2, "u": 4, "U": 8}.get(nxt)
            if width:
                digits = text[i + 2:i + 2 + width]
                try:
                    cells.append((chr(int(digits, 16)), i, None))
                except ValueError:
                    return False
                i += 2 + width
                continue
            if nxt == "N" and text.startswith("{", i + 2):
                close = text.find("}", i + 3)
                try:
                    cells.append((unicodedata.lookup(text[i + 3:close]), i,
                                  None))
                except (KeyError, ValueError):
                    return False
                i = close + 1
                continue
            cells.append(("\\", i, None))  # unknown escape: kept as is
            i += 1
            continue
        cells.append((ch, i, None))
        i += 1
    return True


def _shape(cells):
    """Literal runs and placeholders, for comparing with the AST."""
    out, run = [], []
    for ch, _, _ in cells:
        if ch is None:
            if run:
                out.append("".join(run))
                run = []
            out.append(None)
        else:
            run.append(ch)
    if run:
        out.append("".join(run))
    return out


def _ast_shape(node):
    if isinstance(node, ast.Constant):
        return [node.value] if node.value else []
    out = []
    for value in node.values:
        if isinstance(value, ast.Constant):
            if out and isinstance(out[-1], str):
                out[-1] += value.value
            elif value.value:
                out.append(value.value)
        else:
            out.append(None)
    return out


# -- fragments ------------------------------------------------------------------------
@dataclass
class Fragment:
    """One SQL fragment: its text, where it sits, and how it was built."""
    status: str
    line: int
    col: int
    sql: str = ""
    reason: str = ""
    # Source char index (in the Python text) of every SQL character.
    index: list = field(default_factory=list, repr=False)
    # "__tq_p1" -> the source text it stands for ("{table}").
    placeholders: dict = field(default_factory=dict)
    source: _Source | None = field(default=None, repr=False, compare=False)

    @property
    def checked(self) -> bool:
        return self.status in CHECKED

    def _sql_line_starts(self):
        return [0] + [i + 1 for i, ch in enumerate(self.sql) if ch == "\n"]

    def at_offset(self, offset):
        """(line, col) in the Python file of SQL character `offset`."""
        if not self.index:
            return self.line, self.col
        offset = min(max(offset, 0), len(self.index) - 1)
        return self.source.pos(self.index[offset])

    def at(self, sql_line, sql_col):
        """(line, col) in the Python file of a SQL line/column."""
        starts = self._sql_line_starts()
        row = min(max(sql_line, 1), len(starts)) - 1
        stop = starts[row + 1] - 1 if row + 1 < len(starts) else len(self.sql)
        offset = min(starts[row] + max(sql_col, 1) - 1, max(stop - 1,
                                                              starts[row]))
        return self.at_offset(offset)

    def python_lines(self, first, last):
        """First and last Python line holding SQL lines first..last."""
        starts = self._sql_line_starts()
        lo = starts[min(max(first, 1), len(starts)) - 1]
        hi = (starts[last] if last < len(starts) else len(self.sql))
        rows = [self.source.pos(self.index[i])[0] for i in range(lo, hi)
                if not self.sql[i].isspace()]
        if not rows:
            line = self.at_offset(lo)[0]
            return line, line
        return min(rows), max(rows)

    def show(self, text):
        """text with placeholders back to their Python source."""
        return _PLACEHOLDER_RE.sub(
            lambda m: self.placeholders.get(m.group(0), m.group(0)), text)


@dataclass
class Extraction:
    fragments: list
    # Dialect from the file's imports (None: none, or conflicting).
    dialect: str | None = None


class _Context:
    def __init__(self, text, tree):
        self.text = text
        self.src = _Source(text)
        self.assigned = _assigned_values(tree)
        self.augmented: dict[str, list] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AugAssign) and isinstance(
                    node.target, ast.Name):
                self.augmented.setdefault(node.target.id, []).append(
                    node.value)
        modules = imported_modules(tree)
        self.modules = modules
        self.uses_sql = any(
            _matches_module(m, lib) for m in modules for lib in SQL_LIBRARIES)
        self.alembic_op = any(
            isinstance(node, ast.ImportFrom) and node.module == "alembic"
            and any(a.name == "op" and (a.asname or "op") == "op"
                    for a in node.names)
            for node in ast.walk(tree))
        self.sa_text, self.sa_modules = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and (
                    _matches_module(node.module, "sqlalchemy")):
                for alias in node.names:
                    if alias.name == "text":
                        self.sa_text.add(alias.asname or "text")
                    elif alias.name in ("sql", "expression"):
                        self.sa_modules.add(alias.asname or alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if _matches_module(alias.name, "sqlalchemy"):
                        self.sa_modules.add(alias.asname
                                            or alias.name.split(".")[0])
        self.found: dict[tuple, Fragment] = {}

    # -- call sites --
    def site(self, call):
        name = _callee(call.func)
        if isinstance(call.func, ast.Name) and call.func.id in self.sa_text:
            name = "text"  # from sqlalchemy import text as sql_text
        sql_kw = [kw.value for kw in call.keywords if kw.arg == "sql"]
        args = []
        if name in SQL_CALL_NAMES and call.args:
            args.append(call.args[0])
        args += sql_kw
        if not args:
            return
        if name == "text" and not sql_kw and not self._sa_text(call.func):
            return  # st.text("Delete from your history") is UI text, not SQL
        dynamic_ok = self._dynamic_ok(call, name)
        strong = (name in _SQL_ONLY_SITES or bool(sql_kw)
                  or self._db_receiver(call.func))
        # execute("INSERT ... VALUES", rows): the driver appends the rows
        # (clickhouse-driver's bulk insert form).
        rows_later = name in ("execute", "executemany") and len(call.args) > 1
        for arg in args:
            frags, opaque = self.expr(arg, frozenset(),
                                      lists=any(arg is v for v in sql_kw))
            for frag in frags:
                if rows_later and arg is call.args[0]:
                    _rows_from_driver(frag)
                self._add(frag)
            if not dynamic_ok:
                continue
            for node, named in opaque:
                # SQL we can't see: say so only with evidence it is SQL
                # (a SQL-only site or a database handle, plus a name that
                # says SQL, a DB library, or SQL text in the same name).
                if strong and (named or self.uses_sql or frags):
                    self._add(self._dynamic(node))

    def _db_receiver(self, func):
        """True when the call has no receiver (`execute(sql)`) or its
        receiver chain names a database handle."""
        if not isinstance(func, ast.Attribute):
            return True
        node, names = func.value, []
        while True:
            if isinstance(node, ast.Attribute):
                names.append(node.attr)
                node = node.value
            elif isinstance(node, ast.Call):
                node = node.func
            elif isinstance(node, ast.Subscript):
                node = node.value
            elif isinstance(node, ast.Name):
                names.append(node.id)
                break
            else:
                break
        if names == ["op"] and self.alembic_op:
            return True
        return any(_DB_HANDLE.search(n) for n in names)

    def _sa_text(self, func):
        """SQLAlchemy's text(): imported from sqlalchemy, or reached
        through a sqlalchemy module alias (sa.text, sqlalchemy.sql.text).
        `text` is too common a name to mean SQL otherwise."""
        if isinstance(func, ast.Name):
            return func.id in self.sa_text
        return _root_name(func) in self.sa_modules

    def _dynamic_ok(self, call, name):
        if name == "read_sql_table":
            return False  # takes a table name, not SQL
        if name == "text":
            return self._sa_text(call.func)
        return True

    # -- expressions --
    def expr(self, node, seen, lists=False):
        """(fragments, opaque) for one expression: opaque is a list of
        (node, names_sql) for parts that may be SQL we can't see."""
        if isinstance(node, ast.Constant):
            if not isinstance(node.value, str):
                return [], []
            return self._literal(node), []
        if isinstance(node, ast.JoinedStr):
            frags = self._literal(node)
            if frags:
                return frags, []
            return [], self._words(node)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            cells = self._concat(node, seen)
            frag = self._fragment(cells, node)
            if frag:
                return [frag], []
            lead = _lead_text(node)
            if (lead and _starts_with_sql_keyword(lead)) or _SQL_WORDS.search(
                    "".join(c for c, _, _ in cells if c)):
                return [], [(node, True)]
            return [], []
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            return self._template(node.left, seen, node, "%")
        if isinstance(node, ast.Call):
            return self._call(node, seen)
        if isinstance(node, ast.Name):
            return self._name(node, seen)
        if isinstance(node, ast.IfExp):
            return self._combine([self.expr(node.body, seen, lists),
                                  self.expr(node.orelse, seen, lists)])
        if isinstance(node, ast.BoolOp):
            return self._combine([self.expr(v, seen, lists)
                                  for v in node.values])
        if lists and isinstance(node, (ast.List, ast.Tuple)):
            return self._combine([self.expr(v, seen) for v in node.elts])
        if isinstance(node, ast.Attribute):
            return [], [(node, bool(_SQL_NAME.search(node.attr)))]
        if isinstance(node, ast.Subscript):
            return [], [(node, bool(_SQL_NAME.search(_root_name(node))))]
        if isinstance(node, ast.Starred):
            return [], [(node, False)]
        return [], []

    @staticmethod
    def _combine(results):
        frags, opaque = [], []
        for f, o in results:
            frags += f
            opaque += o
        return frags, opaque

    def _call(self, node, seen):
        callee = _callee(node.func)
        if (callee in _TRANSPARENT_METHODS and isinstance(
                node.func, ast.Attribute) and not node.args):
            return self.expr(node.func.value, seen)
        if callee in _TRANSPARENT_FUNCS and len(node.args) == 1:
            return self.expr(node.args[0], seen)
        if _is_composable(node):  # psycopg sql.SQL("..."): the text inside
            return self.expr(node.args[0], seen)
        if callee == "format" and isinstance(node.func, ast.Attribute):
            return self._template(node.func.value, seen, node, "format")
        if (callee in _BUILDERS or callee in SQL_CALL_NAMES
                or (isinstance(node.func, ast.Name)
                    and node.func.id in self.sa_text)):
            return [], []  # a query object, or a site of its own
        return [], [(node, bool(_SQL_CALLEE.search(callee)))]

    def _name(self, node, seen):
        ident = node.id
        if ident in seen:
            return [], []
        values = self.assigned.get(ident, [])
        inner = seen | {ident}
        if ident in self.augmented:
            # Built up piece by piece (`q += ...`): the text that runs is
            # not any one string in the file.
            frags, opaque = self._combine(
                [self.expr(v, inner) for v in values
                 + self.augmented[ident]])
            named = (bool(frags) or bool(_SQL_NAME.search(ident))
                     or any(n for _, n in opaque) or any(
                         _SQL_WORDS.search(v.value) for v in
                         self.augmented[ident] if isinstance(v, ast.Constant)
                         and isinstance(v.value, str)))
            return [], [(node, named)]
        if not values:
            return [], [(node, bool(_SQL_NAME.search(ident)))]
        return self._combine([self.expr(v, inner) for v in values])

    def _words(self, node):
        literal = "".join(v.value for v in node.values
                          if isinstance(v, ast.Constant))
        return [(node, True)] if _SQL_WORDS.search(literal) else []

    # -- building text --
    def _cells(self, node):
        """Cells of a Constant/JoinedStr, mapped exactly when our own
        decoding agrees with Python's; otherwise line by line."""
        text = self.text
        start = self.src.index(node.lineno, node.col_offset)
        end = self.src.index(node.end_lineno, node.end_col_offset)
        cells, i, ok = [], start, True
        while ok:
            i = _skip_gap(text, i, end)
            if i >= end:
                break
            token = _scan_token(text, i)
            if token is None or token[3] > end:
                ok = False
                break
            prefix, body, body_end, i = token
            if "b" in prefix:
                return None
            ok = _decode(text, prefix, body, body_end, cells)
        if ok and _shape(cells) == _ast_shape(node):
            return cells
        return self._coarse_cells(node, start, end)

    def _coarse_cells(self, node, start, end):
        """Fallback mapping: each line of the value at the start of that
        line in the source when the line counts agree, else at the
        string's start. Never a wrong place, just a less precise one."""
        pieces = _ast_shape(node)
        literal = "".join(p for p in pieces if p is not None)
        raw = self.text[start:end]
        per_line = literal.count("\n") == raw.count("\n")
        line0 = node.lineno
        cells, row = [], 0
        for piece in pieces:
            if piece is None:
                cells.append((None, start, "{…}"))
                continue
            for ch in piece:
                if per_line and row:
                    idx = self.src.starts[min(line0 - 1 + row,
                                              len(self.src.starts) - 1)]
                else:
                    idx = start
                cells.append((ch, idx, None))
                if ch == "\n":
                    row += 1
        return cells

    def _single_string(self, node, seen):
        """The one string a name is assigned, when that is all it is."""
        if not isinstance(node, ast.Name) or node.id in seen:
            return None
        values = self.assigned.get(node.id, [])
        if (len(values) == 1 and node.id not in self.augmented
                and (isinstance(values[0], ast.JoinedStr)
                     or (isinstance(values[0], ast.Constant)
                         and isinstance(values[0].value, str)))):
            return values[0]
        return None

    def _concat(self, node, seen):
        parts, stack = [], [node]
        while stack:
            part = stack.pop()
            if isinstance(part, ast.BinOp) and isinstance(part.op, ast.Add):
                stack += [part.right, part.left]
            else:
                parts.append(part)
        cells = []
        for part in parts:
            target = part
            if not isinstance(part, (ast.Constant, ast.JoinedStr)):
                target = self._single_string(part, seen)
            if isinstance(target, ast.JoinedStr) or (
                    isinstance(target, ast.Constant)
                    and isinstance(target.value, str)):
                more = self._cells(target)
                if more is not None:
                    cells += more
                    continue
            segment = ast.get_source_segment(self.text, part) or "…"
            idx = self.src.index(part.lineno, part.col_offset)
            cells.append((None, idx, "{" + segment[:40] + "}"))
        return cells

    def _literal(self, node):
        cells = self._cells(node)
        frag = self._fragment(cells, node) if cells else None
        return [frag] if frag else []

    def _template(self, base, seen, whole, how):
        """`.format()` or `%` applied to a string (or a name assigned
        strings): Python's own fields become placeholders."""
        if _is_composable(base):
            base = base.args[0]
        bases = [base]
        if isinstance(base, ast.Name):
            if base.id in seen or base.id in self.augmented:
                return [], [(whole, True)]
            bases = self.assigned.get(base.id, [])
            if not bases:
                return [], [(whole, bool(_SQL_NAME.search(base.id)))]
        frags, opaque = [], []
        for item in bases:
            if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Add):
                cells = self._concat(item, seen)
            elif isinstance(item, ast.JoinedStr) or (
                    isinstance(item, ast.Constant)
                    and isinstance(item.value, str)):
                cells = self._cells(item)
            else:
                opaque.append((whole, False))
                continue
            if cells is None:
                continue
            cells = (_apply_format(cells) if how == "format"
                     else _apply_percent(cells))
            frag = self._fragment(cells, item, force=TEMPLATED)
            if frag:
                frags.append(frag)
            elif _SQL_WORDS.search("".join(c for c, _, _ in cells if c)):
                opaque.append((whole, True))
        return frags, opaque

    def _fragment(self, cells, node, force=None):
        lead = []
        for ch, _, _ in cells:
            if ch is None:
                break
            lead.append(ch)
        if not _starts_with_sql_keyword("".join(lead)):
            return None
        cells = _bracket_list_params(_normalize_markers(cells))
        status = force or (TEMPLATED if any(c is None for c, _, _ in cells)
                           else STATIC)
        chars, index, holders = [], [], {}
        for ch, idx, display in cells:
            if ch is None:
                name = f"{PLACEHOLDER}{len(holders) + 1}"
                holders[name] = display
                chars.append(name)
                index.extend([idx] * len(name))
            else:
                chars.append(ch)
                index.append(idx)
        line, col = self.src.pos(self.src.index(node.lineno, node.col_offset))
        return Fragment(status=status, line=line, col=col,
                        sql="".join(chars), index=index,
                        placeholders=holders, source=self.src)

    def _dynamic(self, node):
        line, col = self.src.pos(self.src.index(node.lineno, node.col_offset))
        return Fragment(status=DYNAMIC, line=line, col=col,
                        reason=REASONS[DYNAMIC], source=self.src)

    def _add(self, frag):
        self.found.setdefault((frag.line, frag.col, frag.status), frag)


def _apply_format(cells):
    """str.format fields -> placeholders; {{ and }} -> braces."""
    out, i, n = [], 0, len(cells)
    while i < n:
        ch = cells[i][0]
        nxt = cells[i + 1][0] if i + 1 < n else None
        if ch == "{" and nxt == "{":
            out.append(("{", cells[i][1], None))
            i += 2
        elif ch == "}" and nxt == "}":
            out.append(("}", cells[i][1], None))
            i += 2
        elif ch == "{":
            depth, j = 0, i
            while j < n:
                if cells[j][0] == "{":
                    depth += 1
                elif cells[j][0] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                return out + cells[i:]
            display = "".join(c if c is not None else d
                              for c, _, d in cells[i:j + 1])
            out.append((None, cells[i][1], display))
            i = j + 1
        else:
            out.append(cells[i])
            i += 1
    return out


_PERCENT_SPEC = re.compile(
    r"%(?:%|(?:\([^()\x00]*\))?[#0\- +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?"
    r"[hlL]?[diouxXeEfFgGcrsab])")


def _apply_percent(cells):
    """printf-style fields -> placeholders; %% -> %."""
    flat = "".join(c if c is not None else "\x00" for c, _, _ in cells)
    out, last = [], 0
    for m in _PERCENT_SPEC.finditer(flat):
        out += cells[last:m.start()]
        if m.group(0) == "%%":
            out.append(("%", cells[m.start()][1], None))
        else:
            out.append((None, cells[m.start()][1], m.group(0)))
        last = m.end()
    return out + cells[last:]


_PYFORMAT = re.compile(r"%(?:\(([A-Za-z_]\w*)\))?s(?![A-Za-z0-9_])")


def _normalize_markers(cells):
    """Driver parameter markers %s and %(name)s -> ? and :name, outside
    SQL string literals, so they parse. Only those exact forms: a bare %
    is modulo. When the fragment uses them, %% is an escaped %."""
    flat = "".join(c if c is not None else "\x00" for c, _, _ in cells)
    if "%" not in flat:
        return cells
    hits, quoted, i = [], False, 0
    while i < len(flat):
        ch = flat[i]
        if ch == "'":
            quoted = not quoted
        elif ch == "%" and not quoted:
            if flat.startswith("%%", i):
                hits.append((i, i + 2, "%%"))
                i += 2
                continue
            m = _PYFORMAT.match(flat, i)
            if m:
                hits.append((i, m.end(), m.group(1)))
                i = m.end()
                continue
        i += 1
    if not any(kind != "%%" for _, _, kind in hits):
        return cells
    out, last = [], 0
    for start, end, kind in hits:
        out += cells[last:start]
        if kind == "%%":
            out.append(("%", cells[start][1], None))
        elif kind is None:
            out.append(("?", cells[start][1], None))
        else:  # %(name)s -> :name, each letter at its own position
            out.append((":", cells[start][1], None))
            out += cells[start + 2:start + 2 + len(kind)]
        last = end
    return out + cells[last:]


_TRAILING_VALUES = re.compile(r"\bVALUES\s*$", re.IGNORECASE)


def _rows_from_driver(frag):
    """`INSERT ... VALUES` whose rows the driver supplies: give it one
    row of parameters so it parses; the row maps to the VALUES keyword."""
    match = _TRAILING_VALUES.search(frag.sql)
    if not match or not frag.index:
        return
    at = frag.index[match.start()]
    frag.sql += " (?)"
    frag.index.extend([at] * 4)


_LIST_PARAM = re.compile(r"\bIN\s+(\?|:[A-Za-z_]\w*|\$\d+)(?![\w(])",
                         re.IGNORECASE)


def _bracket_list_params(cells):
    """`IN :ids` / `IN ?` (a list parameter the driver expands) ->
    `IN (:ids)`, so it parses. Runs after %s / %(name)s are normalized."""
    flat = "".join(c if c is not None else "\x00" for c, _, _ in cells)
    out, last = [], 0
    for m in _LIST_PARAM.finditer(flat):
        if flat[:m.start()].count("'") % 2:
            continue  # inside a SQL string
        start, end = m.start(1), m.end(1)
        out += cells[last:start]
        out.append(("(", cells[start][1], None))
        out += cells[start:end]
        out.append((")", cells[end - 1][1], None))
        last = end
    return out + cells[last:]


def extract(text: str) -> Extraction:
    """Every SQL fragment the Python source passes to a known call site,
    sorted by position, plus the dialect its imports point at."""
    if not _MAYBE.search(text):
        return Extraction([])
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return Extraction([])
    ctx = _Context(text, tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            ctx.site(node)
    fragments = sorted(ctx.found.values(),
                       key=lambda f: (f.line, f.col, f.status))
    return Extraction(fragments, dialect_for_imports(ctx.modules))


def has_embedded_sql(text: str) -> bool:
    """True when a .py file holds SQL at known call sites."""
    return bool(extract(text).fragments)
