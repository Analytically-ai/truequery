"""Deterministic offline engine facade.

One code path for the CLI and the hook: discover files, resolve
mode + dialect per file, run the registry-driven analyzer, return
results in a stable order. Zero network, zero LLM in this path.
"""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field

from truequery import __version__
from truequery import project as P
from truequery import settings as S
from truequery import std_registry
from truequery.analyzers import pandas_checks, project_checks
from truequery.analyzers import sql as analyzer
from truequery.extractors import python_sql
from truequery.models import Diagnostic, Finding

ENGINE_VERSION = __version__

IGNORES_NAME = "ignores.json"

MAX_FILE_BYTES = 10 * 1024 * 1024  # ~10 MB; bigger files are not checked.


def active_overrides(config: dict | None) -> dict:
    """tier_overrides from DATA.md, restricted to known slugs and tiers.

    Dogfooding only: init never writes this, defaults never ship it.
    Unknown slugs/tiers are dropped (typo safety)."""
    raw = (config or {}).get("tier_overrides") or {}
    if not isinstance(raw, dict):
        return {}
    return {slug: tier for slug, tier in raw.items()
            if slug in std_registry.RECORDS
            and tier in ("immediate", "stop")}


@dataclass
class FileResult:
    path: str
    rel: str
    mode: str
    findings: list = field(default_factory=list)
    diagnostics: list = field(default_factory=list)
    suppressed: list = field(default_factory=list)
    key_lookups: list = field(default_factory=list)
    # "sql"; "project" for DAG/schema .py/.yml/.yaml files; "macro"
    # for a file of dbt macro definitions; "python" for a .py file with
    # at least one checked SQL fragment; "pandas" for a .py file with no
    # checked SQL that the pandas checks read; "embedded" for a .py file
    # whose SQL fragments were all left unchecked (and no pandas checks
    # ran). Macro and embedded files are listed as not checked.
    kind: str = "sql"
    # True when a .py result holds SQL that wasn't checked (built at run
    # time, or doesn't parse). A counted result (a DAG, a Python file
    # with other checked SQL) stays counted and is also listed.
    embedded_sql: bool = False
    # SQL fragments of a .py file (python_sql.Fragment), by position.
    fragments: list = field(default_factory=list)
    # True when the pandas checks ran on this .py file (it imports pandas
    # and at least one pandas rule is on for this pass). Such a file is
    # counted as checked even when it holds no SQL (kind "pandas").
    pandas: bool = False

    @property
    def verified(self) -> bool:
        return not self.diagnostics


def is_embedded(result: "FileResult") -> bool:
    """One source of truth for "this result's SQL is not checked".

    Covers the standalone embedded kind and any counted result (a DAG)
    that also holds embedded SQL, so the receipt, JSON and summary
    can't drift apart.
    """
    return result.kind == "embedded" or result.embedded_sql


def fragment_counts(result: "FileResult") -> dict:
    """{checked, dynamic, unparsed} fragment counts for a .py result."""
    counts = {"checked": 0, "dynamic": 0, "unparsed": 0}
    for frag in result.fragments:
        counts["checked" if frag.checked else frag.status] += 1
    return counts


def unchecked_reasons(result: "FileResult") -> list[str]:
    """Why some of a .py result's SQL wasn't checked, in a fixed order."""
    statuses = {frag.status for frag in result.fragments}
    return [python_sql.REASONS[s] for s in (python_sql.DYNAMIC,
                                            python_sql.UNPARSED)
            if s in statuses]


def load_ignores(root: str) -> list[dict]:
    """Entries from .truequery/ignores.json: {rule, path, line?, reason?}.

    Corrupt or absent files mean no ignores (the `ignore add` verb
    validates on write; the hook never breaks on a bad file).
    """
    try:
        with open(os.path.join(root, ".truequery", IGNORES_NAME),
                   encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    entries = data.get("ignores", []) if isinstance(data, dict) else []
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rule, path = entry.get("rule"), entry.get("path")
        if not isinstance(rule, str) or not isinstance(path, str):
            continue
        line = entry.get("line")
        stmt = entry.get("stmt")
        offset = entry.get("offset")
        out.append({"rule": rule, "path": path,
                    "line": line if isinstance(line, int) else None,
                    "stmt": stmt if isinstance(stmt, str) else None,
                    "offset": offset if isinstance(offset, int) else None,
                    "reason": entry.get("reason") or "",
                    "broad": not isinstance(line, int) and not isinstance(
                        stmt, str)})
    return out


def _suppressed_by(finding, rel: str, ignores: list[dict],
                   spans=()) -> dict | None:
    """Match by rule + file + normalized-statement hash + the line's
    offset inside that statement: an ignore covers one line, and can't
    drift onto another statement when lines shift. Entries without a
    hash fall back to rule + file + line."""
    for entry in ignores:
        if not fnmatch.fnmatchcase(finding.rule, entry["rule"]):
            continue
        if not fnmatch.fnmatchcase(rel, entry["path"]):
            continue
        if entry["stmt"] is not None:
            if getattr(finding, "_stmt", None) != entry["stmt"]:
                continue
            if (entry["offset"] is not None
                    and stmt_offset(spans, finding.line) != entry["offset"]):
                continue
        elif entry["line"] is not None and finding.line != entry["line"]:
            continue
        return entry
    return None


def _chunk_hashes(text: str, dialect: str | None) -> list[tuple]:
    """(start_line, end_line, sha1-12) per statement chunk, on the same
    cleaning the analyzer uses, so ignore entries and git baselines
    identify statements independent of line shifts."""
    cleaned = analyzer.strip_comments(analyzer.strip_jinja(text),
                                      dialect or "generic")
    chunks = analyzer.split_statements(cleaned)
    spans = []
    for chunk, start in chunks:
        # First and last content lines of the statement: the
        # splitter's start trails the previous `;`, so blank/comment
        # gap lines between statements belong to no statement.
        rows = chunk.split("\n")
        content = [i for i, row in enumerate(rows) if row.strip()]
        if not content:
            continue
        first, last = start + content[0], start + content[-1]
        norm = re.sub(r"\s+", " ", chunk).strip().rstrip(";")
        digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:12]
        spans.append((first, last, digest))
    return spans


def stmt_hash_at(spans: list[tuple], line: int) -> str | None:
    for start, end, digest in spans:
        if start <= line <= end:
            return digest
    return None


def stmt_offset(spans: list[tuple], line: int) -> int | None:
    """line's offset from the first line of its statement."""
    for start, end, _ in spans:
        if start <= line <= end:
            return line - start
    return None


def discover_aux(paths: list[str]) -> list[str]:
    """Non-SQL files the preview project checks read (.py, .yml/.yaml)."""
    out: list[str] = []
    for path in paths:
        if os.path.isdir(path):
            for dirpath, dirnames, filenames in os.walk(path):
                dirnames[:] = sorted(d for d in dirnames
                                     if d not in P.SKIP_DIRS
                                     and not d.startswith("."))
                for name in sorted(filenames):
                    if name.endswith((".py", ".yml", ".yaml")):
                        out.append(os.path.join(dirpath, name))
        elif os.path.isfile(path) and path.endswith((".py", ".yml", ".yaml")):
            out.append(os.path.abspath(path))
    return sorted(set(out))


def _aux_findings(text, path, mode, tiers, settings):
    """Preview non-SQL checks, honoring enabled/tier like SQL checks."""
    raw = (project_checks.check_model_sql(text, path, mode, std_registry,
                                          settings)
           + project_checks.check_aux_file(text, path, mode, std_registry,
                                           settings))
    kept = []
    for f in raw:
        record = std_registry.RECORDS.get(f["rule"])
        if record is None:
            continue
        setting = (settings or {}).get(f["rule"], {})
        enabled = setting.get("enabled", record["enabled"])
        tier = setting.get("tier", record["tier"])
        if not enabled:
            continue
        if tiers is not None and tier not in tiers:
            continue
        kept.append(Finding(rule=f["rule"], severity=f["severity"],
                            mode=f["mode"], evidence=f["evidence"],
                            file=f["file"], line=f["line"], col=f["col"],
                            snippet=f["snippet"], why=f["why"],
                            fix=f["fix"]))
    kept.sort(key=Finding.sort_key)
    return kept


def python_dialect(dialect: str | None, extraction) -> str | None:
    """--dialect, then DATA.md (both already in `dialect`), then the
    file's imports, then auto."""
    return dialect or extraction.dialect


_FROM = re.compile(r"\bfrom\b", re.IGNORECASE)


def _reads_as_prose(sql: str, dialect: str | None) -> bool:
    """True for text that parses only by accident: a lone SELECT of bare
    words, no FROM and no other clause ("Select an option" is SELECT an
    AS option). Columns with no table can't run, so it isn't SQL."""
    if _FROM.search(sql):
        return False
    import sqlglot
    from sqlglot import exp
    try:
        stmts = sqlglot.parse(analyzer.strip_comments(sql, "generic"),
                              read=dialect or None)
    except Exception:
        return False
    placeholders = (python_sql.PLACEHOLDER, analyzer.JINJA_PH)
    for stmt in stmts:
        if not isinstance(stmt, exp.Select) or any(
                stmt.args.get(k) for k in ("from", "where", "group",
                                           "having", "order", "limit")):
            continue
        cols = []
        for item in stmt.expressions:
            item = item.this if isinstance(item, exp.Alias) else item
            if not isinstance(item, exp.Column):
                break
            cols.append(item)
        else:
            if cols and not any(c.name.startswith(placeholders)
                                for c in cols):
                return True
    return False


# Rules that report something missing (a condition, a tiebreaker). Where
# a template placeholder stands for that whole part (`WHERE {cond}`,
# `ON {join}`, `OVER (ORDER BY {order})`), the real text is unknown, so
# these can't say it's missing.
_CONDITION_RULES = frozenset({"sql/unguarded-update-delete",
                              "sql/accidental-cross-join"})
_ORDER_RULES = frozenset({"sql/row-number-dedupe-tiebreaker"})


_TRAILING_PLACEHOLDER = re.compile(
    re.escape(python_sql.PLACEHOLDER) + r"\d+(?=[\s;]*$)")


def _is_placeholder(node) -> bool:
    from sqlglot import exp
    return (isinstance(node, exp.Column)
            and node.name.startswith(python_sql.PLACEHOLDER))


def _unknown_parts(sql: str, dialect: str | None) -> dict:
    """{rule: [(first, last) SQL lines]} of statements where a placeholder
    stands for the part that rule looks for."""
    import sqlglot
    from sqlglot import exp
    cleaned = analyzer.strip_comments(analyzer.strip_jinja(sql),
                                      dialect or "generic")
    out: dict[str, list] = {}
    for chunk, start in analyzer.split_statements(cleaned):
        try:
            stmts = sqlglot.parse(chunk, read=dialect or None)
        except Exception:
            continue
        span = (start, start + chunk.count("\n"))
        for stmt in stmts:
            if stmt is None:
                continue
            # A placeholder after the target, standing for a whole clause
            # (`DELETE FROM t {where}`): it parses as a table alias, or it
            # is the statement's last token without being a table name.
            # A trailing placeholder that is a table name (`DELETE FROM
            # {t}`) or a value (`SET a = {v}`) is not a clause.
            tail = _TRAILING_PLACEHOLDER.search(chunk)
            if tail:
                name = tail.group(0)
                if name in {t.name for t in stmt.find_all(exp.Table)} or any(
                        c.name == name and isinstance(
                            c.parent, (exp.Binary, exp.Func, exp.Tuple))
                        for c in stmt.find_all(exp.Column)):
                    tail = None
            if tail or any(a.name.startswith(python_sql.PLACEHOLDER)
                           for a in stmt.find_all(exp.TableAlias)):
                for rule in _CONDITION_RULES:
                    out.setdefault(rule, []).append(span)
            for col in stmt.find_all(exp.Column):
                if not _is_placeholder(col):
                    continue
                parent = col.parent
                while isinstance(parent, exp.Paren):
                    parent = parent.parent
                if (isinstance(parent, (exp.Where, exp.Having, exp.And,
                                        exp.Or, exp.Not))
                        or (isinstance(parent, exp.Join)
                            and parent.args.get("on") is col)):
                    for rule in _CONDITION_RULES:
                        out.setdefault(rule, []).append(span)
                if isinstance(parent, exp.Ordered) and isinstance(
                        parent.parent, exp.Order) and isinstance(
                        parent.parent.parent, exp.Window):
                    for rule in _ORDER_RULES:
                        out.setdefault(rule, []).append(span)
    return out


def _original_offset(sql: str, cleaned: str, offset: int) -> int:
    """Map an offset in the analyzer's cleaned text back to the fragment's
    own text. Cleaning keeps every newline, so the line is exact; within
    the line, Jinja (`{{ ds }}` -> one placeholder, `{{ ref('x') }}` ->
    `x`) changes lengths, so the column is aligned on the unchanged text
    around it. Inside a replaced tag, the tag's start."""
    if len(sql) == len(cleaned) and "{" not in sql:
        return offset  # nothing changed length
    offset = min(max(offset, 0), len(cleaned))
    row = cleaned.count("\n", 0, offset)
    c_start = cleaned.rfind("\n", 0, offset) + 1
    starts = [0] + [i + 1 for i, ch in enumerate(sql) if ch == "\n"]
    o_start = starts[min(row, len(starts) - 1)]
    c_line = cleaned[c_start:].split("\n", 1)[0]
    o_line = sql[o_start:].split("\n", 1)[0]
    col = offset - c_start
    if c_line == o_line:
        return o_start + col
    matcher = difflib.SequenceMatcher(None, c_line, o_line, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if i1 <= col < i2 or (col == i2 == len(c_line)):
            return o_start + (j1 + (col - i1) if tag == "equal" else j1)
    return o_start


@analyzer.quiet_sqlglot()
def check_python_source(text: str, path: str, dialect: str | None = None,
                        mode: str = "build", tiers=None, settings=None,
                        extraction=None):
    """(findings, fragments, spans) for the SQL inside one .py file.

    Each static/templated fragment goes through the same SQL checks as a
    .sql file; one that doesn't parse becomes `unparsed` (listed, never
    a parse failure: detection is a heuristic). Findings point at the
    Python line and column where the SQL is written; spans are per
    statement, in Python lines, so ignores stay attached when lines
    above move. File-level dbt checks don't apply to fragments.
    """
    if extraction is None:
        extraction = python_sql.extract(text)
    dialect = python_dialect(dialect, extraction)
    findings, spans = [], []
    for frag in extraction.fragments:
        if not frag.checked:
            continue
        res = analyzer.check_text(frag.sql, dialect=dialect, path=path,
                                  mode=mode, tiers=tiers, settings=settings)
        prose = not res["diagnostics"] and _reads_as_prose(frag.sql, dialect)
        if prose:
            res["findings"] = []  # not SQL: nothing it says is a finding
        if res["diagnostics"] or prose:
            # Not checked (in full). Findings from statements of it that
            # did parse are still real and still reported.
            frag.status = python_sql.UNPARSED
            frag.reason = python_sql.REASONS[python_sql.UNPARSED]
        if frag.status == python_sql.TEMPLATED and any(
                f["rule"] in _CONDITION_RULES | _ORDER_RULES
                for f in res["findings"]):
            unknown = _unknown_parts(frag.sql, dialect)
            res["findings"] = [
                f for f in res["findings"]
                if not any(a <= f["line"] <= b
                           for a, b in unknown.get(f["rule"], ()))]
        sql_spans = _chunk_hashes(frag.sql, dialect)
        for first, last, digest in sql_spans:
            spans.append((*frag.python_lines(first, last), digest))
        cleaned = analyzer.strip_comments(analyzer.strip_jinja(frag.sql),
                                          dialect or "generic")
        for f in res["findings"]:
            if "offset" in f:
                line, col = frag.at_offset(
                    _original_offset(frag.sql, cleaned, f["offset"]))
            else:
                line, col = frag.at(f["line"], f["col"])
            finding = Finding(rule=f["rule"], severity=f["severity"],
                              mode=f["mode"], evidence=f["evidence"],
                              file=path, line=line, col=col,
                              snippet=frag.show(f["snippet"]),
                              why=frag.show(f["why"]),
                              fix=frag.show(f["fix"]))
            finding._stmt = stmt_hash_at(sql_spans, f["line"])
            findings.append(finding)
    findings.sort(key=Finding.sort_key)
    return findings, extraction.fragments, spans


def pandas_rules(tiers=None, settings=None) -> set:
    """The pandas rules on for this pass (enabled, and in `tiers`)."""
    rules = set()
    for slug in pandas_checks.RULES:
        record = std_registry.RECORDS.get(slug)
        if record is None or not record["implemented"]:
            continue
        setting = (settings or {}).get(slug, {})
        if not setting.get("enabled", record["enabled"]):
            continue
        if tiers is not None and setting.get("tier",
                                             record["tier"]) not in tiers:
            continue
        rules.add(slug)
    return rules


def check_pandas_source(text: str, path: str, mode: str = "build",
                        tiers=None, settings=None) -> tuple[bool, list]:
    """(ran, findings) for the pandas checks on one .py file. `ran` is
    True only when the file imports pandas and a pandas check ran on it."""
    ran, raw = pandas_checks.check(text, pandas_rules(tiers, settings))
    lines = text.splitlines()
    findings = []
    for rule, line, col, why, fix in raw:
        record = std_registry.RECORDS[rule]
        severity = ((settings or {}).get(rule, {}).get("severity")
                    or record["severity"].get(mode, "warning"))
        snippet = lines[line - 1].strip() if line <= len(lines) else ""
        findings.append(Finding(rule=rule, severity=severity, mode=mode,
                                evidence=record["evidence"], file=path,
                                line=line, col=col, snippet=snippet[:120],
                                why=why, fix=fix))
    return ran, findings


def check_python_file(text: str, path: str, dialect: str | None = None,
                      mode: str = "build", tiers=None, settings=None):
    """(findings, fragments, spans, pandas_ran) for one .py file: the SQL
    inside it and its pandas code. Spans are the SQL statements first,
    then the file's Python statements when it imports pandas; a pandas
    finding is keyed to the first span holding its line, the same lookup
    `ignore add` and git baselines use."""
    findings, fragments, spans = check_python_source(
        text, path, dialect, mode, tiers, settings)
    spans = spans + pandas_checks.statement_spans(text)
    ran, pandas = check_pandas_source(text, path, mode, tiers, settings)
    for finding in pandas:
        finding._stmt = stmt_hash_at(spans, finding.line)
    findings = sorted(findings + pandas, key=Finding.sort_key)
    return findings, fragments, spans, ran


@analyzer.quiet_sqlglot()
def statement_spans(path: str, text: str, dialect: str | None) -> list:
    """Statement spans for ignores and baselines: per SQL statement in a
    SQL file; in a .py file, per statement of each SQL fragment, then per
    Python statement when the file imports pandas."""
    if path.endswith(".py"):
        return (check_python_source(text, path, dialect)[2]
                + pandas_checks.statement_spans(text))
    return _chunk_hashes(text, dialect)


@analyzer.quiet_sqlglot()
def check_file(path: str, dialect: str | None = None,
               mode: str = "build", tiers=None,
               settings=None, text: str | None = None
               ) -> tuple[list[Finding], list[Diagnostic], list[str]]:
    """Check one file from disk. The CLI and hook use run()."""
    if text is None:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    res = analyzer.check_text(text, dialect=dialect, path=path, mode=mode,
                              tiers=tiers,
                              settings=settings)
    findings = [Finding(rule=f["rule"], severity=f["severity"],
                        mode=f["mode"], evidence=f["evidence"],
                        file=f["file"], line=f["line"], col=f["col"],
                        snippet=f["snippet"], why=f["why"], fix=f["fix"])
                for f in res["findings"]]
    diagnostics = [Diagnostic(file=path, message=d["message"],
                               detail=d.get("detail", ""))
                   for d in res["diagnostics"]]
    findings.sort(key=Finding.sort_key)
    diagnostics.sort(key=Diagnostic.sort_key)
    return findings, diagnostics, sorted(set(res.get("key_lookups", [])))


NOT_UTF8 = "not UTF-8 text (not checked)"

# dbt definition blocks: templates that render into other files' SQL.
_DEFINITION_BLOCK = re.compile(
    r"{%-?\s*(macro|test|materialization|docs)\b.*?{%-?\s*end\1\s*-?%}",
    re.DOTALL)
_JINJA_COMMENT = re.compile(r"{#.*?#}", re.DOTALL)


def is_macro_file(text: str) -> bool:
    """True when a file holds only dbt macro/test/materialization/docs
    definitions (plus comments): templates, not queries. A file that also
    has a query outside those blocks is checked as usual."""
    if "{%" not in text:
        return False
    rest, blocks = _DEFINITION_BLOCK.subn("", _JINJA_COMMENT.sub("", text))
    if not blocks:
        return False
    return not analyzer.strip_comments(rest, "generic").strip()

TOO_LARGE = "file over the 10 MB size limit (not checked)"


def is_procedural(diagnostic) -> bool:
    """A function body in procedural code (PL/pgSQL, a BEGIN … END
    block): it parsed, but TrueQuery doesn't look inside procedural
    code yet, so it is not checked."""
    return f": {analyzer.PROCEDURAL} " in diagnostic.message


def parse_failed(diagnostic) -> bool:
    """Not checked because it didn't parse (not size, not procedural)."""
    return diagnostic.message != TOO_LARGE and not is_procedural(diagnostic)


def _read(path: str) -> str | None:
    """File text; None when unreadable. NOT_UTF8 and TOO_LARGE are
    sentinels handled by run(): the file is reported as not checked."""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            return TOO_LARGE
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        return NOT_UTF8
    except OSError:
        return None


def _apply_ignores(result: FileResult, findings: list, spans, ignores) -> None:
    """Tag each finding with its statement hash, then split kept from
    suppressed by .truequery/ignores.json."""
    for finding in findings:
        finding.file = result.rel
        if getattr(finding, "_stmt", None) is None:
            finding._stmt = stmt_hash_at(spans, finding.line)
        entry = _suppressed_by(finding, result.rel, ignores, spans)
        if entry is None:
            result.findings.append(finding)
        else:
            result.suppressed.append({"rule": finding.rule,
                                      "line": finding.line,
                                      "broad": entry["broad"],
                                      "reason": entry["reason"]})
    result.findings.sort(key=Finding.sort_key)


@analyzer.quiet_sqlglot()
def run(paths: list[str], root: str, dialect: str | None = None,
        mode: str | None = None, tiers=None, settings=None,
        config: dict | None = None) -> list[FileResult]:
    """Run enabled checks over discovered files. Deterministic order.

    SQL files get the SQL checks plus the preview dbt-model check;
    .py/.yml/.yaml files under the given paths get the preview project
    checks. ``config`` is the parsed DATA.md tq-config (read from root
    when None). ``settings`` is an Effective rules map; without
    one it is resolved from that config plus the user's config.

    Applies .truequery/ignores.json to every finding; suppressed ones
    land on FileResult.suppressed (with a broad/narrow flag) so
    reporters can say what an ignore hid.
    """
    sql_files = P.discover(paths)
    if config is None:
        config = P.load_config(root)
    base_dialect = dialect or (config or {}).get("dialect")
    if settings is None:
        settings = S.effective(project=config).check_map()
    ignores = load_ignores(root)
    results: list[FileResult] = []
    for path in sql_files:
        text = _read(path)
        if text is None:
            continue
        rel = P._rel(path, root)
        file_mode = P.resolve_mode(rel, config, mode)
        if text is NOT_UTF8 or text is TOO_LARGE:
            # Never silent: an undecodable or oversize file is listed
            # as not checked, with the reason.
            results.append(FileResult(path=path, rel=rel, mode=file_mode,
                                      diagnostics=[Diagnostic(rel, text)]))
            continue
        if is_macro_file(text):
            # Macro definitions aren't queries: listed as not checked
            # (kind "macro"), never as a parse failure, never as clean.
            results.append(FileResult(path=path, rel=rel, mode=file_mode,
                                      kind="macro"))
            continue
        findings, diagnostics, key_lookups = check_file(
            path, dialect=base_dialect, mode=file_mode, tiers=tiers,
            settings=settings, text=text)
        # Preview dbt-model check (incremental without unique_key).
        findings += _aux_findings(text, path, file_mode, tiers, settings)
        for diagnostic in diagnostics:
            diagnostic.file = rel
        result = FileResult(path=path, rel=rel, mode=file_mode,
                            diagnostics=diagnostics, key_lookups=key_lookups)
        _apply_ignores(result, findings, _chunk_hashes(text, base_dialect),
                       ignores)
        results.append(result)
    for path in discover_aux(paths):
        text = _read(path)
        if text is None or text is NOT_UTF8 or text is TOO_LARGE:
            continue
        rel = P._rel(path, root)
        file_mode = P.resolve_mode(rel, config, mode)
        findings, fragments, spans, pandas = [], [], [], False
        if path.endswith(".py"):
            findings, fragments, spans, pandas = check_python_file(
                text, path, base_dialect, file_mode, tiers, settings)
        unchecked = any(not f.checked for f in fragments)
        if project_checks.applies_to_file(path, text):
            # A DAG stays a project file (the project check really ran
            # on it); its SQL is checked like any Python SQL, and SQL it
            # couldn't check is also listed.
            kind = "project"
            findings += _aux_findings(text, path, file_mode, tiers, settings)
        elif any(f.checked for f in fragments):
            kind = "python"
        elif pandas:
            # The pandas checks read it: counted, and any SQL it holds
            # that wasn't checked is still listed.
            kind = "pandas"
        elif fragments:
            # Every fragment unchecked: listed as not checked, not counted.
            kind = "embedded"
        else:
            continue  # other .py and YAML files apply to no check
        result = FileResult(path=path, rel=rel, mode=file_mode, kind=kind,
                            embedded_sql=unchecked, fragments=fragments,
                            pandas=pandas)
        # Python SQL matches ignores per statement; project findings
        # (outside any statement) on rule + file + line.
        _apply_ignores(result, findings, spans, ignores)
        results.append(result)
    results.sort(key=lambda r: r.rel)
    return results
