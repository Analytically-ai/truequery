"""Generic SQL analyzer: plain SQL, dbt models, and SQLMesh models.

Pipeline: Jinja/SQLMesh-tolerant preprocessing (line-preserving) ->
per-statement parse isolation -> registry-driven checks -> findings
with per-mode severity. Anything unparsable becomes a diagnostic, never
silence. Positions are anchor-located: the pinned sqlglot version
attaches no node positions, so each rule searches its statement text
for its own anchor (documented approximation, good to the statement
and usually the line).
"""
import re

import sqlglot

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


def strip_comments(text, dialect):
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
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def split_statements(cleaned):
    parts, depth, cur, start, line = [], 0, [], 1, 1
    squote = False
    for ch in cleaned:
        if ch == "'":
            squote = not squote
        if not squote:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
        cur.append(ch)
        if ch == "\n":
            line += 1
        if ch == ";" and depth == 0 and not squote:
            parts.append(("".join(cur), start))
            cur, start = [], line
    tail = "".join(cur)
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
    def __init__(self, stmt_text, start_line):
        self.text = stmt_text
        self.start_line = start_line
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
        pre = self.text[:off]
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
             why, fix):
    return {"rule": slug, "severity": severity, "mode": mode,
            "evidence": "file", "file": path, "line": line, "col": col,
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


def check_text(text, dialect=None, path="<input>", mode="build", registry=None,
               include_disabled=False, tiers=None, settings=None):
    """Run enabled file-level checks. Returns {path, findings,
    diagnostics, verified, key_lookups}. Findings carry rule slug +
    per-mode severity. ``tiers`` restricts to rule tiers (the hook's
    immediate pass); ``settings`` ({slug: {enabled, tier, severity}}
    from truequery.settings.effective) overrides the registry."""
    from truequery import keylookup  # imports project, which imports us
    registry = registry or std_registry
    cleaned = strip_comments(strip_jinja(text), dialect or "generic")
    findings, diagnostics, key_lookups = [], [], []
    chunks = split_statements(cleaned)
    # A file with no statements at all (blank, comments or Jinja only)
    # is verified. Anything else must parse: a typo or prose is "not
    # checked", never "clean".
    active = _active_checks(registry, settings, tiers, include_disabled)
    ctx = checks.CheckContext(dialect=dialect, path=path, mode=mode,
                              keys=keylookup.declared_keys_for(path))
    for chunk, start_line in chunks:
        try:
            stmts = sqlglot.parse(chunk, read=dialect or None)
        except Exception as exc:
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
        for stmt in stmts:
            if stmt is None:
                continue
            loc = Locator(chunk, start_line)
            for slug, check, record, setting in active:
                for node, why, fix in check(stmt, loc, ctx):
                    line, col = loc.locate(slug, node)
                    findings.append(_finding(
                        slug, _severity(record, setting, mode), mode, path,
                        chunk, start_line, line, col, why, fix))
            for suppressed in loc.suppressed:
                if suppressed not in key_lookups:
                    key_lookups.append(suppressed)
    findings.sort(key=lambda f: (f["line"], f["col"], f["rule"]))
    return {"path": path, "findings": findings, "diagnostics": diagnostics,
            "verified": not diagnostics, "key_lookups": key_lookups}
