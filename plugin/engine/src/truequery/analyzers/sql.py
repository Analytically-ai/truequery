"""Generic SQL analyzer: plain SQL, dbt models, and SQLMesh models.

Pipeline: Jinja/SQLMesh-tolerant preprocessing (line-preserving) ->
per-statement parse isolation -> registry-driven checks -> findings
with per-mode severity. Anything unparsable becomes a diagnostic, never
silence. Positions are anchor-located: the pinned sqlglot version
attaches no node positions, so each rule searches its statement text
for its own anchor (documented approximation, good to the statement
and usually the line).
"""
import contextlib
import logging
import re

import sqlglot
from sqlglot import exp as _exp

from truequery import std_registry
from truequery.checks import sql as checks

JINJA_PH = "__tq_jinja"


# -- preprocessing (line-preserving) ------------------------------------------
def _skip_trivia(text, i):
    while i < len(text):
        if text[i].isspace():
            i += 1
        elif text.startswith("--", i):
            end = text.find("\n", i)
            i = len(text) if end == -1 else end + 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = len(text) if end == -1 else end + 2
        else:
            break
    return i


def strip_model_block(text):
    i = _skip_trivia(text, 0)
    m = re.match(r"(MODEL|AUDIT)\s*\(", text[i:], re.IGNORECASE)
    if not m:
        return text
    depth, i, squote = 0, i + m.start(), False
    while i < len(text):
        ch = text[i]
        if ch == "'" and not squote:
            squote = True
        elif ch == "'" and squote:
            squote = False
        elif not squote:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    tail = text[i + 1:]
                    semi = tail.find(";")
                    cut = i + 1 + (semi + 1 if semi != -1 else 0)
                    return "".join("\n" if c == "\n" else " " for c in text[:cut]) + text[cut:]
        i += 1
    return text


_MACRO_CALL = re.compile(r"@[A-Za-z_]\w*\(")


def strip_sqlmesh_macros(text):
    """Blank SQLMesh macro calls (@name(...)) to a `1` placeholder,
    keeping newlines. Linear: jumps from one `@` to the next."""
    out, i = [], 0
    while i < len(text):
        at = text.find("@", i)
        if at == -1:
            out.append(text[i:])
            break
        if not _MACRO_CALL.match(text, at):
            out.append(text[i:at + 1])
            i = at + 1
            continue
        out.append(text[i:at])
        i = at
        depth, j, squote = 0, i, False
        while j < len(text):
            ch = text[j]
            if ch == "'" and not squote:
                squote = True
            elif ch == "'" and squote:
                squote = False
            elif not squote:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        seg = text[i:j + 1]
        out.append("1" + "".join("\n" if c == "\n" else " " for c in seg[1:]))
        i = j + 1
    return "".join(out)


_REF_RE = re.compile(r"\{\{\s*ref\(\s*['\"]([^'\"\n]+)['\"]\s*\)\s*\}\}")
_SOURCE_RE = re.compile(
    r"\{\{\s*source\(\s*['\"]([^'\"\n]+)['\"]\s*,\s*['\"]([^'\"\n]+)['\"]\s*\)\s*\}\}")


def _resolve_dbt_refs(text):
    """Resolve dbt relation builders to plain names before parsing, so
    table references (and contract matching) see real relations:
    {{ ref('orders') }} -> orders, {{ source('shop','orders') }} ->
    shop.orders. Single-line tags only, so line numbers never shift."""

    def sub_source(match):
        return f"{match.group(1)}.{match.group(2)}"

    text = _SOURCE_RE.sub(sub_source, text)
    return _REF_RE.sub(r"\1", text)


_JINJA_CLOSE = {"{{": "}}", "{%": "%}", "{#": "#}"}
_JINJA_MARKERS = ("JINJA_QUERY_BEGIN;", "JINJA_QUERY_BEGIN", "JINJA_END;",
                  "JINJA_END")


def _jinja_spans(text):
    """(start, end, opener) for each Jinja tag. Quotes inside a tag are
    respected, so `post_hook="{{ x }}"` doesn't close the outer tag."""
    spans, i, n = [], 0, len(text)
    while True:
        i = text.find("{", i)
        if i == -1 or i + 1 >= n:
            return spans
        opener = text[i:i + 2]
        if opener not in _JINJA_CLOSE:
            i += 1
            continue
        closer, j, quote = _JINJA_CLOSE[opener], i + 2, None
        while j < n:
            ch = text[j]
            if quote:
                if ch == "\\":
                    j += 1
                elif ch == quote:
                    quote = None
            elif ch in "'\"" and opener != "{#":
                quote = ch
            elif text.startswith(closer, j):
                j += 2
                break
            j += 1
        spans.append((i, min(j, n), opener))
        i = j


def strip_jinja(text):
    """Remove Jinja (and SQLMesh MODEL blocks / macros), keeping every
    newline so line numbers still match the source. A line holding only
    tags becomes blank; an inline `{{ ... }}` becomes a placeholder
    identifier so the SQL around it still parses."""
    text = strip_model_block(text)
    text = strip_sqlmesh_macros(text)
    text = _resolve_dbt_refs(text)
    spans = _jinja_spans(text)
    in_tag = bytearray(len(text))
    for start, end, _ in spans:
        in_tag[start:end] = b"\x01" * (end - start)
    out = list(text)
    # Lines whose only content (besides `;`) is Jinja are blanked whole.
    pos = 0
    for line in text.split("\n"):
        end = pos + len(line)
        rest = "".join(c for k, c in enumerate(line) if not in_tag[pos + k])
        if line.strip() in _JINJA_MARKERS or (
                rest.strip() in ("", ";") and line.strip()):
            for k in range(pos, end):
                out[k] = " "
        pos = end + 1
    for start, end, opener in spans:
        if all(out[k] in " \n" for k in range(start, end)):
            continue  # already blanked with its line
        # An expression tag becomes one placeholder token with no padding,
        # so `is_{{ name }}_model` stays one identifier; statement and
        # comment tags become spaces. Newlines are kept either way.
        fill = "" if opener == "{{" else " "
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = fill
        if opener == "{{":
            out[start] = JINJA_PH
    return "".join(out)


_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_][A-Za-z_0-9]*)?\$")


def _dollar_end(text, i):
    """End of the dollar-quoted string ($$…$$ or $tag$…$tag$) that opens
    at text[i], or None: not an opening tag (a `$1` parameter, a `$`
    inside a name) or never closed."""
    if i and (text[i - 1].isalnum() or text[i - 1] in "_$"):
        return None
    tag = _DOLLAR_TAG.match(text, i)
    if tag is None:
        return None
    close = text.find(tag.group(0), tag.end())
    return None if close == -1 else close + len(tag.group(0))


def strip_comments(text, dialect):
    """Blank comments, keeping every newline and the text's length.
    Quoted strings and dollar-quoted bodies are kept as they are."""
    out, i, n = [], 0, len(text)
    squote = False
    while i < n:
        ch = text[i]
        two = text[i:i + 2]
        if squote:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == "'":
                squote = False
            i += 1
        elif ch == "'":
            squote = True
            out.append(ch)
            i += 1
        elif two == "--":
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "#" and dialect in ("mysql", "bigquery"):
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
        elif two == "/*":
            end = text.find("*/", i + 2)
            end = n if end == -1 else end + 2
            for c in text[i:end]:
                out.append("\n" if c == "\n" else " ")
            i = end
        elif ch == "$" and (end := _dollar_end(text, i)) is not None:
            out.append(text[i:end])  # a function body: checked on its own
            i = end
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_statements(cleaned):
    """(text, start line) per statement: split at `;` outside quotes,
    brackets and dollar-quoted bodies. The parts are contiguous."""
    parts, depth, start, line = [], 0, 1, 1
    squote, i, cut, n = False, 0, 0, len(cleaned)
    while i < n:
        ch = cleaned[i]
        if ch == "'":
            squote = not squote
        elif not squote:
            if ch == "$" and (end := _dollar_end(cleaned, i)) is not None:
                line += cleaned.count("\n", i, end)
                i = end
                continue
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == ";" and depth == 0:
                parts.append((cleaned[cut:i + 1], start))
                cut, start = i + 1, line
        if ch == "\n":
            line += 1
        i += 1
    tail = cleaned[cut:]
    if tail.strip() and tail.strip().strip(";"):
        parts.append((tail, start))
    return parts


ANCHORS = {
    "sql/not-in-nullable-subquery": [r"\bNOT\s+IN\s*\(", r"\bNOT\s*\("],
    "sql/unguarded-update-delete": [r"\bDELETE\b", r"\bUPDATE\b"],
    "sql/accidental-cross-join": [r"\bJOIN\b", ","],
    "sql/left-join-null-rejecting-where": [r"\bWHERE\b"],
    "sql/null-equality-comparison": [r"=\s*NULL", r"<>\s*NULL", r"!=\s*NULL"],
    # Fallbacks for rules whose node is the whole statement.
    "sql/union-all-default": [r"\bUNION\b"],
    "sql/set-operation-column-position": [r"\bUNION\b", r"\bINTERSECT\b",
                                          r"\bEXCEPT\b"],
    "sql/insert-explicit-column-list": [r"\bINSERT\b"],
    "sql/keyset-pagination": [r"\bOFFSET\b"],
    "sql/limit-requires-order-by": [r"\bLIMIT\b"],
    "sql/group-by-bare-column": [r"\bGROUP\s+BY\b"],
    "sql/row-number-dedupe-tiebreaker": [r"\bROW_NUMBER\s*\("],
}


def _compact_map(text):
    """Lowercased whitespace-stripped text + original offsets per char."""
    chars, idx = [], []
    for i, ch in enumerate(text):
        if not ch.isspace():
            chars.append(ch.lower())
            idx.append(i)
    return "".join(chars), idx


def _usable_needle(needle, compact):
    # Tiny needles match everywhere; a needle that IS the statement
    # carries no position information. Both defer to anchors.
    if len(needle) < 4 and not (
            len(needle) == 3 and re.search(r"[^a-z0-9]", needle)):
        return False
    if needle and len(needle) > 0.8 * len(compact):
        return False
    return bool(needle)


class Locator:
    def __init__(self, stmt_text, start_line, origin=None):
        self.text = stmt_text
        self.start_line = start_line
        # (chunk, pos) when stmt_text is a query inside a wrapper or a
        # function body: pos[i] is the chunk offset of stmt_text[i], and
        # lines and columns are the chunk's (start_line is its first).
        self.origin = origin
        # Rules append their slug here when a declared-key lookup
        # suppresses a firing; check_text reports them.
        self.suppressed = []
        # Occurrence indexes are keyed per (rule, node text): the nth
        # firing of the same rule on the same node text points at the
        # nth textual occurrence. A per-rule counter misattributes when
        # a clean earlier occurrence exists (e.g. an unjoined count(*)
        # before the firing one).
        self.used = {}
        self._compact, self._index = _compact_map(stmt_text)

    def locate(self, rule, node=None):
        # Primary: the parser's own offset for the firing node. This
        # points at the node itself even when an identical clean text
        # occurs earlier (e.g. an unjoined count(*) before the firing
        # one) — text search alone cannot tell those apart.
        off = self._locate_meta(node)
        if off is None:
            sig = None
            if node is not None:
                try:
                    sig, _ = _compact_map(node.sql())
                except Exception:
                    sig = None
                if sig is not None and not _usable_needle(sig, self._compact):
                    sig = None
            if sig is not None:
                nth = self.used.get((rule, sig), 0)
                off = self._locate_text(sig, nth)
                self.used[(rule, sig)] = nth + 1
            if off is None:
                nth = self.used.get((rule, None), 0)
                off = self._locate_anchor(rule, nth)
                self.used[(rule, None)] = nth + 1
        text = self.text
        if self.origin is not None:
            text, pos = self.origin
            off = pos[min(off, len(pos) - 1)]
        # Offset inside the statement text, for callers that map
        # positions back to another file (SQL inside Python).
        self.last_offset = off
        pre = text[:off]
        return (self.start_line + pre.count("\n"),
                off - (pre.rfind("\n") + 1) + 1)

    def _locate_meta(self, node):
        # sqlglot tags only some nodes (functions, leaves) with offsets;
        # synthesized parents (NOT, predicates) often carry none. The
        # minimum over the node and its descendants is the construct's
        # own start — always inside the firing node, never at an
        # earlier clean occurrence of similar text.
        if node is None:
            return None
        try:
            whole, _ = _compact_map(node.sql())
        except Exception:
            return None
        if not _usable_needle(whole, self._compact):
            # The node IS the statement (or is unrenderable): its
            # offsets carry no position information. Defer to text.
            return None
        best, end = None, None
        try:
            nodes = [node] + list(node.walk())
        except Exception:
            nodes = [node]
        for cand in nodes:
            cand = cand[0] if isinstance(cand, tuple) else cand
            meta = getattr(cand, "meta", None) or {}
            start = meta.get("start")
            stop = meta.get("end", start)
            if isinstance(start, int) and 0 <= start < len(self.text):
                best = start if best is None else min(best, start)
                if isinstance(stop, int):
                    end = stop if end is None else max(end, stop)
        if best is None:
            return None
        # A node spanning the statement (UNION of two branches, a whole
        # DELETE) carries no position information by itself: defer to
        # the text/anchor path, which handles those shapes.
        if end is not None and end - best > 0.8 * len(self.text):
            return None
        return best

    def _locate_text(self, needle, nth):
        occ, start = [], 0
        while True:
            hit = self._compact.find(needle, start)
            if hit == -1:
                break
            occ.append(self._index[hit])
            start = hit + 1
        if not occ:
            return None
        return occ[nth] if nth < len(occ) else occ[-1]

    def _locate_anchor(self, rule, nth):
        pats = ANCHORS.get(rule, [])
        cands = []
        for p in pats:
            cands += [m.start() for m in re.finditer(p, self.text, re.IGNORECASE)]
        cands.sort()
        if nth < len(cands):
            return cands[nth]
        return cands[-1] if cands else 0


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


# -- statements sqlglot reads only as a Command ------------------------------
# For syntax it doesn't support, sqlglot doesn't raise: it keeps the text
# as an opaque exp.Command, and no check can see inside one. Whether that
# leaves something unchecked depends on what the statement is, judged on
# its leading keywords. The lists below are the whole policy.
#
# Statements that hold a query or a data change: every check is about
# these, so reading one only as a Command skipped them. That is a parse
# failure (the file is not checked; in Python the fragment is unparsed).
_DATA_HEADS = frozenset({
    "SELECT", "WITH", "VALUES", "TABLE", "FROM",  # queries (TABLE t, FROM-first)
    "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE", "UPSERT", "COPY",
    "EXPLAIN",   # wraps a query; EXPLAIN ANALYZE also runs it
    "PREPARE",   # PREPARE p AS <query>
    "DECLARE",   # DECLARE c CURSOR FOR <query>
    "DO",        # an anonymous block of statements
    "CACHE",     # Spark CACHE TABLE t AS <query>
})
# CREATE / ALTER of these objects can hold a query: CREATE TABLE … AS,
# a view's query, a function, procedure, rule, trigger or macro body, a
# scheduled task's statement. A plain CREATE TABLE is here too: the
# Command's text can't show that no AS <query> follows.
_DATA_OBJECTS = frozenset({"TABLE", "VIEW", "FUNCTION", "PROCEDURE",
                           "ROUTINE", "RULE", "TRIGGER", "MACRO", "TASK"})
_ALTER_DATA_OBJECTS = frozenset({"VIEW"})  # ALTER VIEW v AS <query>

# Statements no check applies to: session and transaction control,
# permissions, maintenance, and schema objects that hold no query. A
# Command here skipped nothing, so it is no diagnostic.
_NO_CHECK_HEADS = frozenset({
    "SET", "RESET", "SHOW", "USE", "PRAGMA",                 # session
    "BEGIN", "START", "COMMIT", "END", "ROLLBACK", "ABORT",  # transactions
    "SAVEPOINT", "RELEASE",
    "GRANT", "REVOKE", "DENY", "REASSIGN",                   # permissions
    "VACUUM", "ANALYZE", "OPTIMIZE", "REINDEX", "CLUSTER",   # maintenance
    "CHECKPOINT", "DISCARD", "LOCK", "UNLOCK", "REFRESH", "MSCK", "KILL",
    "SYSTEM",
    "COMMENT", "SECURITY",                                   # COMMENT ON, SECURITY LABEL
    "LISTEN", "NOTIFY", "UNLISTEN", "LOAD", "ATTACH", "DETACH",
    "DESCRIBE", "DESC",
    # Run something already defined, or move a cursor: no query text here.
    "CALL", "EXECUTE", "EXEC", "DEALLOCATE", "FETCH", "MOVE", "CLOSE",
    "DROP",      # removes an object; never holds a query
    "TRUNCATE",  # removes every row on purpose; no check is about it
})
_NO_CHECK_OBJECTS = frozenset({
    "INDEX", "STATISTICS", "EXTENSION", "SCHEMA", "DATABASE", "SEQUENCE",
    "TYPE", "DOMAIN", "COLLATION", "CAST", "OPERATOR", "AGGREGATE", "TEXT",
    "ROLE", "USER", "GROUP", "POLICY", "PUBLICATION", "SUBSCRIPTION",
    "SERVER", "FOREIGN", "EVENT", "ACCESS", "CONVERSION", "LANGUAGE",
    "TABLESPACE", "TRANSFORM", "SECRET", "STAGE",
    "VIRTUAL",        # SQLite VIRTUAL TABLE … USING module(...)
    "KEYSPACE", "CUSTOM", "AUTHORIZATION", "MAPPING",  # Cassandra, Teradata, Hazelcast
})
# Words between CREATE / ALTER and the object they name.
_OBJECT_MODIFIERS = frozenset({
    "OR", "REPLACE", "ALTER", "IF", "NOT", "EXISTS", "TEMP", "TEMPORARY",
    "UNLOGGED", "GLOBAL", "LOCAL", "MATERIALIZED", "RECURSIVE", "TRANSIENT",
    "SECURE", "VOLATILE", "EXTERNAL", "DYNAMIC", "ICEBERG", "HYBRID",
    "MULTISET", "UNIQUE", "CONCURRENTLY", "CONSTRAINT", "DEFAULT",
    "TRUSTED", "PROCEDURAL",
})
# Anything not named on either side is treated as holding a query: an
# unknown statement is not checked, never silently clean. (ClickHouse
# CREATE DICTIONARY is left unknown on purpose: its SOURCE can be a query.)

_WORD = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")


def _command_words(stmt):
    rest = stmt.expression
    if isinstance(rest, _exp.Literal):
        rest = rest.name
    elif isinstance(rest, _exp.Expression):
        rest = rest.sql()
    return [w.upper() for w in _WORD.findall(f"{stmt.this or ''} {rest or ''}")]


def command_holds_query(stmt) -> bool:
    """True when a Command statement is one a check would have looked at
    (see the lists above)."""
    words = _command_words(stmt)
    if not words:
        return False
    head = words[0]
    if head in ("CREATE", "ALTER"):
        obj = next((w for w in words[1:] if w not in _OBJECT_MODIFIERS), "")
        if head == "ALTER":
            return obj in _ALTER_DATA_OBJECTS
        if obj in _DATA_OBJECTS:
            return True
        return obj not in _NO_CHECK_OBJECTS
    if head in _DATA_HEADS:
        return True
    return head not in _NO_CHECK_HEADS


def unread_detail(stmt):
    """Why a statement wasn't checked, or None when it was: a query or
    data change that parsed only as an opaque Command."""
    if not isinstance(stmt, _exp.Command) or not command_holds_query(stmt):
        return None
    words = _command_words(stmt)
    what = " ".join(words[:2]) if words[0] in ("CREATE", "ALTER") \
        else words[0]
    return (f"unsupported syntax: this {what} statement could be read "
            "only as an opaque command, so no check looked inside it")


def _command_span(text, stmt, after):
    """(start, end) of a Command statement in text, searching from
    `after`. sqlglot keeps the Command's text verbatim (its head, then
    the rest), so it is found exactly; None when it isn't."""
    head = str(stmt.this or "")
    rest = stmt.expression
    if isinstance(rest, _exp.Literal):
        rest = rest.name
    elif isinstance(rest, _exp.Expression):
        return None
    rest = str(rest or "").strip()
    if not head:
        return None
    for hit in re.finditer(r"(?<![\w$])" + re.escape(head) + r"(?![\w$])",
                           text[after:], re.IGNORECASE):
        start = after + hit.start()
        i = after + hit.end()
        while i < len(text) and text[i].isspace():
            i += 1
        if text.startswith(rest, i):
            return start, i + len(rest)
    return None


# -- queries inside wrappers and function bodies ------------------------------
# A wrapper holds a whole query: EXPLAIN [ANALYZE] [VERBOSE] [(options)]
# <query>, PREPARE name [(types)] AS <query>, DECLARE name … CURSOR …
# FOR <query>, CREATE TABLE / [MATERIALIZED] VIEW … AS <query>. sqlglot
# reads these only as a Command; the query inside is found in the text,
# parsed and checked like any other. A wrapper whose query isn't found,
# or doesn't parse, stays not checked (see unread_detail).
_EXPLAIN_OPTIONS = frozenset({
    "ANALYZE", "ANALYSE", "VERBOSE",          # Postgres, MySQL, DuckDB
    "QUERY", "PLAN", "FOR",                   # SQLite QUERY PLAN, Oracle PLAN FOR
    "EXTENDED", "PARTITIONS", "FORMAT",       # MySQL (FORMAT=JSON)
    "JSON", "TEXT", "TREE", "TRADITIONAL", "XML", "YAML",
    "USING", "TABULAR",                       # Snowflake EXPLAIN USING TEXT
    "FORMATTED", "COST", "CODEGEN", "LOGICAL",  # Spark
})
# Words a wrapped statement starts with. PREPARE / DECLARE / CREATE …
# AS also accept a parenthesised query.
_INNER_HEADS = frozenset({
    "SELECT", "WITH", "VALUES", "TABLE", "FROM", "INSERT", "UPDATE",
    "DELETE", "MERGE", "REPLACE", "UPSERT",
    "CREATE",    # EXPLAIN CREATE TABLE … AS
    "DECLARE",   # EXPLAIN DECLARE … CURSOR
    "EXECUTE",   # EXPLAIN EXECUTE p, CREATE TABLE t AS EXECUTE p
})
_WRAPPING_OBJECTS = frozenset({"TABLE", "VIEW"})
_NO_DATA = re.compile(r"\s+WITH\s+(?:NO\s+)?DATA\s*$", re.IGNORECASE)


def _top_words(text, lo, hi):
    """(WORD, start, end) for each word of text[lo:hi] outside brackets,
    quoted strings, quoted names and dollar-quoted bodies."""
    depth, i = 0, lo
    while i < hi:
        ch = text[i]
        if ch in "'\"`":
            j = text.find(ch, i + 1)
            while ch == "'" and j != -1 and text.startswith("''", j):
                j = text.find(ch, j + 2)
            i = hi if j == -1 else j + 1
            continue
        if ch == "$" and (end := _dollar_end(text, i)) is not None:
            i = end
            continue
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif depth == 0 and (ch.isalpha() or ch == "_") and not (
                i > lo and (text[i - 1].isalnum() or text[i - 1] in "_$")):
            word = _WORD.match(text, i)
            yield word.group(0).upper(), word.start(), word.end()
            i = word.end()
            continue
        i += 1


def _query_after(text, at, hi):
    """Start of the statement that begins at or after `at`, when it
    begins with one of _INNER_HEADS, in brackets or not; None otherwise
    (`AS ({placeholder})` is no query to check)."""
    while at < hi and text[at].isspace():
        at += 1
    first = at
    while at < hi and (text[at].isspace() or text[at] == "("):
        at += 1
    word = _WORD.match(text, at)
    return first if word and word.group(0).upper() in _INNER_HEADS else None


def _inner_query(text, start, end):
    """(start, end) of the query inside a wrapper Command spanning
    text[start:end], or None when it isn't one of the forms above."""
    words = list(_top_words(text, start, end))
    if not words:
        return None
    head = words[0][0]
    begin = None
    if head == "EXPLAIN":
        for word, at, _ in words[1:]:
            if word in _INNER_HEADS:
                begin = at
                break
            if word not in _EXPLAIN_OPTIONS:
                return None
    elif head == "PREPARE":
        after_as = next((e for w, _, e in words[1:] if w == "AS"), None)
        if after_as is not None:
            begin = _query_after(text, after_as, end)
    elif head == "DECLARE":
        names = [w for w, _, _ in words]
        if "CURSOR" in names:
            cursor = names.index("CURSOR")
            after_for = next((e for w, _, e in words[cursor + 1:]
                              if w == "FOR"), None)
            if after_for is not None:
                begin = _query_after(text, after_for, end)
    elif head == "CREATE":
        obj = next((w for w, _, _ in words[1:]
                    if w not in _OBJECT_MODIFIERS), "")
        after_as = next((e for w, _, e in words[1:] if w == "AS"), None)
        if obj in _WRAPPING_OBJECTS and after_as is not None:
            begin = _query_after(text, after_as, end)
            tail = _NO_DATA.search(text, begin or start, end)
            if tail is not None:
                end = tail.start()
    if begin is None or begin >= end:
        return None
    return begin, end


# Function and procedure bodies. A body in plain SQL (LANGUAGE sql, or no
# language) is parsed as statements and checked; a procedural body is
# not checked, and says so; a body in another language (python,
# javascript, c, …) holds no SQL, so it is left as it was.
_PROCEDURAL_LANGUAGES = {
    "plpgsql": "PL/pgSQL", "plsql": "PL/SQL", "tsql": "T-SQL",
    "sqlscript": "SQLScript", "spl": "SPL",
}
_SQL_LANGUAGES = frozenset({"sql", ""})
_ROUTINES = frozenset({"FUNCTION", "PROCEDURE"})
_BLOCK_HEAD = re.compile(r"\s*(BEGIN|DECLARE)\b(\s+ATOMIC\b)?", re.IGNORECASE)
_LANGUAGE = re.compile(r"\s*'?([A-Za-z_][A-Za-z_0-9]*)'?")
PROCEDURAL = "procedural code in"


def _block_name(dialect):
    if dialect in ("snowflake",):
        return "Snowflake Scripting"
    if dialect in ("tsql", "fabric"):
        return "T-SQL"
    return "a BEGIN … END block"


def _string_bodies(text, lo, hi):
    """(content start, content end, quoted) for each string after a
    top-level AS in text[lo:hi]: `AS $tag$…$tag$` or `AS '…'`."""
    for word, _, after in _top_words(text, lo, hi):
        if word != "AS":
            continue
        at = after
        while at < hi and text[at].isspace():
            at += 1
        if text.startswith("$", at) and (end := _dollar_end(text, at)):
            tag = _DOLLAR_TAG.match(text, at).group(0)
            yield at + len(tag), end - len(tag), False
        elif text.startswith("'", at):
            j = text.find("'", at + 1)
            while j != -1 and text.startswith("''", j):
                j = text.find("'", j + 2)
            if j != -1:
                yield at + 1, j, True


def _language_in(text, lo, hi):
    """The LANGUAGE named in text[lo:hi], lowercased; "" when none."""
    for word, _, after in _top_words(text, lo, hi):
        if word == "LANGUAGE":
            name = _LANGUAGE.match(text, after)
            return name.group(1).lower() if name else ""
    return ""


def _unquote(raw):
    """A single-quoted body's text with `''` read as `'`, and for each of
    its characters (plus the end) the offset in raw."""
    out, idx, i = [], [], 0
    while i < len(raw):
        out.append(raw[i])
        idx.append(i)
        i += 2 if raw.startswith("''", i) else 1
    idx.append(len(raw))
    return "".join(out), idx


def _is_block(text, at=0):
    """A procedural block starts at text[at]: BEGIN … END, or DECLARE …
    BEGIN … END (Snowflake Scripting). Not BEGIN ATOMIC (a SQL-standard
    body of plain statements), and not a lone DECLARE c CURSOR FOR …,
    which is a SQL statement."""
    head = _BLOCK_HEAD.match(text, at)
    if head is None or head.group(2):
        return False
    if head.group(1).upper() == "BEGIN":
        return True
    return re.search(r"\bBEGIN\b", text[head.end():], re.IGNORECASE) \
        is not None


def _routine_body(stmt):
    """For a CREATE FUNCTION / PROCEDURE whose body sqlglot keeps as
    text: (body text, language). (None, language) for a BEGIN … END
    block sqlglot parsed into the tree (T-SQL, or a SQL-standard BEGIN
    ATOMIC), None when the body is otherwise part of the tree (BigQuery,
    a SQL `RETURN expr`) or there is none."""
    if not isinstance(stmt, _exp.Create) or str(
            stmt.args.get("kind") or "").upper() not in _ROUTINES:
        return None
    body = stmt.expression
    props = stmt.args.get("properties")
    lang = next((p.name.lower() for p in (props.expressions if props else [])
                 if isinstance(p, _exp.LanguageProperty)), "")
    if isinstance(body, _exp.Block) and stmt.args.get("begin"):
        return None, lang
    if isinstance(body, _exp.Block) and len(body.expressions) == 1 \
            and isinstance(body.expressions[0], _exp.RawString):
        body = body.expressions[0]
    if isinstance(body, (_exp.Heredoc, _exp.RawString)) or (
            isinstance(body, _exp.Literal) and body.is_string):
        return body.name, lang
    return None


class _Pass:
    """The checks over one chunk's statements, and over the queries
    inside its wrappers and SQL function bodies. A piece of text being
    checked has a `pos` map (pos[i] = chunk offset of text[i]); None
    for the chunk itself."""

    def __init__(self, active, ctx, path, mode, dialect):
        self.active, self.ctx, self.path, self.mode = active, ctx, path, mode
        self.dialect = dialect
        self.findings, self.diagnostics, self.key_lookups = [], [], []

    def chunk(self, chunk, start_line, base, stmts):
        self.text, self.start_line, self.base = chunk, start_line, base
        self._statements(stmts, chunk, None)

    def wrapped_chunk(self, chunk, start_line, base):
        """A wrapper the dialect's parser rejects outright instead of
        keeping it as a Command (under auto, `DECLARE … CURSOR` raises):
        True when the query inside is found, parses and was checked."""
        self.text, self.start_line, self.base = chunk, start_line, base
        lead = len(chunk) - len(chunk.lstrip())
        inner = _inner_query(chunk, lead, len(chunk))
        return inner is not None and \
            self._parse_piece(chunk, None, *inner) is None

    # -- positions --
    def _off(self, pos, i):
        return i if pos is None else pos[min(i, len(pos) - 1)]

    def _line(self, off):
        return self.start_line + self.text.count("\n", 0, off)

    def _first_line(self, text, pos):
        lead = len(text) - len(text.lstrip())
        return self._line(self._off(pos, lead))

    def _sub(self, pos, lo, hi, idx=None):
        """pos map for text[lo:hi] (decoded through idx when given)."""
        rel = idx if idx is not None else range(hi - lo + 1)
        return [self._off(pos, lo + k) for k in rel]

    def _diagnose(self, line, detail, what="could not parse"):
        self.diagnostics.append({
            "message": f"line {line}: {what} (not checked)",
            "detail": detail})

    # -- statements --
    def _statements(self, stmts, text, pos):
        searched = 0
        for stmt in stmts:
            if stmt is None:
                continue
            if isinstance(stmt, _exp.Command):
                searched = self._command(stmt, text, pos, searched)
                continue
            body = _routine_body(stmt)
            if body is not None:
                searched = self._routine(stmt, body, text, pos, searched)
                continue
            self._check(stmt, text, pos)

    def _parse_piece(self, text, pos, lo, hi):
        """Parse text[lo:hi] and check it. The parse error when it
        doesn't parse (nothing checked), else None."""
        piece = text[lo:hi]
        try:
            stmts = sqlglot.parse(piece, read=self.dialect or None)
        except Exception as exc:  # noqa: BLE001 - any parse error
            return exc
        self._statements(stmts, piece, self._sub(pos, lo, hi))
        return None

    def _command(self, stmt, text, pos, searched):
        if not command_holds_query(stmt):
            return searched
        span = _command_span(text, stmt, searched)
        if span is None:
            self._diagnose(self._first_line(text[searched:], self._sub(
                pos, searched, len(text))), unread_detail(stmt))
            return searched
        start, end = span
        line = self._line(self._off(pos, start))
        words = _command_words(stmt)
        if words[0] == "DO":
            lang = _language_in(text, start, end) or "plpgsql"
            if lang in _PROCEDURAL_LANGUAGES:
                body = next(((at, e) for at in range(start, end)
                             if text[at] == "$"
                             and (e := _dollar_end(text, at)) is not None),
                            None)
                if body is not None:
                    tag = len(_DOLLAR_TAG.match(text, body[0]).group(0))
                    body = (text, pos, body[0] + tag, body[1] - tag, False)
                self._procedural(line, _PROCEDURAL_LANGUAGES[lang],
                                 "DO block", body)
            else:
                self._diagnose(line, unread_detail(stmt))
            return end
        obj = next((w for w in words[1:] if w not in _OBJECT_MODIFIERS), "")
        if words[0] == "CREATE" and obj in _ROUTINES:
            self._routine_text(stmt, text, pos, start, end, line)
            return end
        inner = _inner_query(text, start, end)
        if inner is None or self._parse_piece(text, pos, *inner) is not None:
            self._diagnose(line, unread_detail(stmt))
        return end

    def _routine_text(self, stmt, text, pos, start, end, line):
        """A CREATE FUNCTION / PROCEDURE sqlglot read only as a Command."""
        lang = _language_in(text, start, end)
        bodies = list(_string_bodies(text, start, end))
        body = (text, pos, *bodies[0]) if bodies else None
        block = next((e for w, _, e in _top_words(text, start, end)
                      if w == "AS" and _is_block(text[:end], e)), None)
        if lang in _PROCEDURAL_LANGUAGES:
            self._procedural(line, _PROCEDURAL_LANGUAGES[lang], body=body)
        elif lang in _SQL_LANGUAGES and body:
            self._sql_body(*body, line)
        elif lang in _SQL_LANGUAGES and block is not None:
            self._procedural(line, _block_name(self.dialect),
                             body=(text, pos, block, end, False))
        else:
            # Another language, or a body not found: as in 0.1.3.
            self._diagnose(line, unread_detail(stmt))

    def _routine(self, stmt, body, text, pos, searched):
        """A CREATE FUNCTION / PROCEDURE whose body sqlglot kept as text:
        find that text in the source, then check it as SQL."""
        content, lang = body
        create = re.compile(r"(?<![\w$])CREATE(?![\w$])", re.IGNORECASE)
        hit = create.search(text, searched)
        line = self._line(self._off(pos, hit.start() if hit else searched))
        if lang not in _SQL_LANGUAGES and lang not in _PROCEDURAL_LANGUAGES:
            self._check(stmt, text, pos)  # another language: as before
            return searched
        if content is None:
            # A BEGIN … END block sqlglot parsed (loosely, for T-SQL):
            # what it read is still checked, as before. A SQL-standard
            # BEGIN ATOMIC body is plain SQL; T-SQL is procedural code.
            self._check(stmt, text, pos)
            if not re.compile(r"\bBEGIN\s+ATOMIC\b", re.IGNORECASE).search(
                    text, searched):
                self._procedural(line, _block_name(self.dialect))
            return searched
        want = " ".join(content.split())
        for lo, hi, quoted in _string_bodies(text, searched, len(text)):
            raw = text[lo:hi]
            got = _unquote(raw)[0] if quoted else raw
            if " ".join(got.split()) != want:
                continue
            if lang in _PROCEDURAL_LANGUAGES:
                self._procedural(line, _PROCEDURAL_LANGUAGES[lang],
                                 body=(text, pos, lo, hi, quoted))
            else:
                self._sql_body(text, pos, lo, hi, quoted, line)
            return hi
        if lang in _PROCEDURAL_LANGUAGES:
            self._procedural(line, _PROCEDURAL_LANGUAGES[lang])
        else:
            self._diagnose(line, "the body of this function could not be "
                           "found in the source, so no check looked inside "
                           "it")
        return searched

    def _procedural(self, line, language, what="function body", body=None):
        """Procedural code is not checked. Its statements that parse on
        their own as plain SQL still are, and their findings are real;
        the rest (control flow, variables) is not understood."""
        if body is not None:
            keep = len(self.diagnostics)
            self._pieces(*body)
            del self.diagnostics[keep:]  # the body is reported once, below
        self._diagnose(
            line, f"TrueQuery doesn't look inside procedural code yet: the "
            f"control flow in this {what} ({language}) was not checked; "
            f"only its statements that parse on their own as SQL were",
            what=f"{PROCEDURAL} {language}")

    def _sql_body(self, text, pos, lo, hi, quoted, line):
        """Check a plain-SQL body, statement by statement, at its true
        lines and columns. A body that starts a BEGIN … END block is
        procedural code."""
        raw = text[lo:hi]
        if _is_block(_unquote(raw)[0] if quoted else raw):
            self._procedural(line, _block_name(self.dialect),
                             body=(text, pos, lo, hi, quoted))
            return
        self._pieces(text, pos, lo, hi, quoted)

    def _pieces(self, text, pos, lo, hi, quoted):
        """Parse and check each statement of a body; one that doesn't
        parse is a diagnostic at its own line."""
        raw = text[lo:hi]
        body, idx = _unquote(raw) if quoted else (raw, None)
        bpos = self._sub(pos, lo, hi, idx)
        cleaned = strip_comments(body, self.dialect or "generic")
        at = 0
        for part, _ in split_statements(cleaned):
            lo2, at = at, at + len(part)
            if not part.strip().strip(";").strip():
                continue
            err = self._parse_piece(cleaned, bpos, lo2, at)
            if err is not None:
                detail = _ANSI.sub("", str(err)).replace("\n", " ")
                self._diagnose(self._first_line(part, bpos[lo2:]),
                               re.sub(r"\s+", " ", detail).strip())

    # -- the checks --
    def _check(self, stmt, text, pos):
        origin = None if pos is None else (self.text, pos)
        loc = Locator(text, self.start_line, origin)
        for slug, check, record, setting in self.active:
            for node, why, fix in check(stmt, loc, self.ctx):
                line, col = loc.locate(slug, node)
                self.findings.append(_finding(
                    slug, _severity(record, setting, self.mode), self.mode,
                    self.path, self.text, self.start_line, line, col, why,
                    fix, evidence=record.get("evidence", "file")))
                self.findings[-1]["offset"] = self.base + loc.last_offset
        for suppressed in loc.suppressed:
            if suppressed not in self.key_lookups:
                self.key_lookups.append(suppressed)


@contextlib.contextmanager
def quiet_sqlglot():
    """Hold sqlglot's logger at ERROR while checking. It warns 'contains
    unsupported syntax' on stderr for every Command fallback, and the
    checker's output must be only TrueQuery's. The level is restored
    afterwards, so a host application's logging is left as it was."""
    logger = logging.getLogger("sqlglot")
    before = logger.level
    if logger.getEffectiveLevel() < logging.ERROR:
        logger.setLevel(logging.ERROR)
    try:
        yield
    finally:
        logger.setLevel(before)


def _parse_diagnostic(start_line, chunk, exc):
    """One clean agent-facing line + full detail for --json/--verbose.

    The line names no internals and no statement body; the path is
    attached by the caller (which knows the display path).
    """
    detail = _ANSI.sub("", str(exc)).replace("\n", " ")
    detail = re.sub(r"\s+", " ", detail).strip()
    message = f"line {start_line}: could not parse (not checked)"
    if "`" in chunk:
        message += (" - set `dialect` in the DATA.md tq-config "
                    "if identifiers need one")
    return {"message": message, "detail": detail}


def _intdiv_outside_strings(chunk):
    """Locate `//` outside single-quoted strings (comments and Jinja are
    already stripped). `//` is integer division in every dialect that
    accepts it, and a syntax error elsewhere — either way it needs a
    human, so it reports as a finding, not silence."""
    squote, i = False, 0
    while i < len(chunk):
        ch = chunk[i]
        if ch == "'":
            squote = not squote
        elif not squote and ch == "$" and (
                end := _dollar_end(chunk, i)) is not None:
            i = end  # a body in another language (`//` starts a comment)
            continue
        elif not squote and chunk[i:i + 2] == "//":
            pre = chunk[:i]
            return (1 + pre.count("\n"), i - (pre.rfind("\n") + 1) + 1)
        i += 1
    return None


def _source_snippet(chunk, start_line, line):
    """The user's own source line (placeholders named), not normalized SQL."""
    rows = chunk.split("\n")
    idx = line - start_line
    text = rows[idx].strip() if 0 <= idx < len(rows) else ""
    return text.replace(JINJA_PH, "<jinja>")[:160]


def _severity(record, setting, mode):
    return setting.get("severity") or record["severity"].get(mode, "warning")


def _finding(slug, severity, mode, path, chunk, start_line, line, col,
             why, fix, evidence="file"):
    return {"rule": slug, "severity": severity, "mode": mode,
            "evidence": evidence, "file": path, "line": line, "col": col,
            "snippet": _source_snippet(chunk, start_line, line),
            "why": why, "fix": fix}


def _active_checks(registry, settings, tiers, include_disabled):
    """(slug, check, record, setting) for every check that runs."""
    active = []
    for slug, check in checks.CHECKS.items():
        record = registry.RECORDS.get(slug)
        if record is None or not record["implemented"]:
            continue
        setting = (settings or {}).get(slug, {})
        if not include_disabled and not setting.get("enabled",
                                                    record["enabled"]):
            continue
        tier = setting.get("tier", record["tier"])
        if tiers is not None and tier not in tiers:
            continue
        active.append((slug, check, record, setting))
    return active


INTDIV = "sql/integer-division-truncation"


@quiet_sqlglot()
def check_text(text, dialect=None, path="<input>", mode="build", registry=None,
               include_disabled=False, tiers=None, settings=None, root=None):
    """Run enabled file-level checks. Returns {path, findings,
    diagnostics, verified, key_lookups}. Findings carry rule slug +
    per-mode severity. ``tiers`` restricts to rule tiers (the hook's
    immediate pass); ``settings`` ({slug: {enabled, tier, severity}}
    from truequery.settings.effective) overrides the registry."""
    from truequery import keylookup  # imports project, which imports us
    registry = registry or std_registry
    cleaned = strip_comments(strip_jinja(text), dialect or "generic")
    chunks = split_statements(cleaned)
    # A file with no statements at all (blank, comments or Jinja only)
    # is verified. Anything else must parse: a typo or prose is "not
    # checked", never "clean".
    active = _active_checks(registry, settings, tiers, include_disabled)
    # Contracts and metric files are read only when a check that reads them
    # is on: a project pays nothing for checks it hasn't switched on.
    reads_contracts = any(record.get("evidence") == "contract"
                          for _, _, record, _ in active)
    proj_ctx = (keylookup.declared_context_for(path, root=root)
                if reads_contracts
                else {"contracts": {}, "metrics": {}, "bases": {}})
    ctx = checks.CheckContext(
        dialect=dialect, path=path, mode=mode,
        keys=keylookup.declared_keys_for(path, root=root),
        contracts=proj_ctx["contracts"],
        metrics=proj_ctx["metrics"],
        bases=proj_ctx["bases"],
    )
    run = _Pass(active, ctx, path, mode, dialect)
    findings, diagnostics = run.findings, run.diagnostics
    base = 0  # offset of the chunk in the cleaned text (chunks are contiguous)
    for chunk, start_line in chunks:
        chunk_base, base = base, base + len(chunk)
        try:
            stmts = sqlglot.parse(chunk, read=dialect or None)
        except Exception as exc:
            if run.wrapped_chunk(chunk, start_line, chunk_base):
                continue
            # A parse failure is never a silent pass: the chunk was not
            # checked, so the file is unverified even when a `//`
            # finding is salvaged from it.
            intdiv = _intdiv_outside_strings(chunk)
            salvage = [a for a in active if a[0] == INTDIV]
            if intdiv is not None and salvage:
                _, _, record, setting = salvage[0]
                line = start_line + intdiv[0] - 1
                findings.append(_finding(
                    INTDIV, _severity(record, setting, mode), mode, path,
                    chunk, start_line, line, intdiv[1],
                    "integer division truncates (5 // 2 = 2)",
                    "use / with a decimal operand if a fraction is meant"))
            rows = chunk.split("\n")
            first = next((i for i, row in enumerate(rows) if row.strip()), 0)
            diagnostics.append(_parse_diagnostic(start_line + first, chunk,
                                                 exc))
            continue
        # Each statement is checked; a query sqlglot read only as an
        # opaque Command is unwrapped and checked when it can be, and a
        # function body is checked as SQL. What can't be is not checked,
        # like any parse failure, and the chunk's other statements still
        # are.
        run.chunk(chunk, start_line, chunk_base, stmts)
    key_lookups = run.key_lookups
    findings.sort(key=lambda f: (f["line"], f["col"], f["rule"]))
    return {"path": path, "findings": findings, "diagnostics": diagnostics,
            "verified": not diagnostics, "key_lookups": key_lookups}
