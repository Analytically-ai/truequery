"""Deterministic offline engine facade.

One code path for the CLI and the hook: discover files, resolve
mode + dialect per file, run the registry-driven analyzer, return
results in a stable order. Zero network, zero LLM in this path.
"""

from __future__ import annotations

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
from truequery.analyzers import project_checks
from truequery.analyzers import sql as analyzer
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
    # "sql"; "project" for .py/.yml/.yaml files; "macro" for a file of
    # dbt macro definitions, which is listed as not checked.
    kind: str = "sql"

    @property
    def verified(self) -> bool:
        return not self.diagnostics


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
        result = FileResult(path=path, rel=rel, mode=file_mode,
                            kind="project")
        # Project files have no statements: ignores match on rule + file
        # + line.
        _apply_ignores(result, _aux_findings(text, path, file_mode, tiers,
                                             settings), [], ignores)
        results.append(result)
    results.sort(key=lambda r: r.rel)
    return results
