"""Reporters shared by the CLI and the hook: text, JSON, SARIF, and the
review receipt. All deterministic: sorted input, sorted output,
``sort_keys`` JSON."""

from __future__ import annotations

import json

from truequery import engine as E
from truequery import project as P
from truequery import std_registry
from truequery.schema import STANDARD_VERSION

SEVERITY_RANK = {"error": 3, "warning": 2, "advisory": 1, "info": 0}

MAX_LISTED = 10  # receipt lines name at most this many files

SARIF_LEVEL = {"error": "error", "warning": "warning",
               "advisory": "note", "info": "none"}

# Static recall limits: things no file-level check can see. The receipt
# names each one that applies to the files in front of it, so a pass
# means something and the line isn't boilerplate.
DIVISION_LIMIT = ("integer division between columns: column types aren't "
                  "known from files alone")
PROJECT_LIMIT = ("dbt, incremental-model and Airflow rules beyond the 4 "
                 "project checks")
# The data line when this run didn't read the data. TrueQuery reads local
# data files and database files a contract points to (`review --data`,
# `truequery verify`); it doesn't connect to remote databases yet.
DATA_LIMIT = ("your data: no contract points at a local data file, so none "
              "was read (remote databases: not until 0.3)")
DATA_NOT_READ = ("your data: not read in this run. Run `truequery review "
                 "--data` to check {names} against the contracts")
PANDAS_LIMIT = ("pandas: only DataFrames a file builds or annotates "
                "itself are followed; what a column means (money, units, "
                "time zones) needs contracts")

# How the receipt names a finding's git attribution.
ATTRIBUTION_WORDS = {"new": "new", "existing": "already committed",
                     "attribution unknown": "can't tell if new: no git history"}


def _divides(results) -> bool:
    """True when a checked SQL file divides (a `/` token outside comments
    and strings). Only files the checker read are looked at: a file it
    skipped (over the size limit, unparseable) is listed as not checked
    already, and must not be read here either. A file that can't be
    tokenized counts as dividing."""
    from sqlglot.tokens import Tokenizer, TokenType
    for result in results:
        texts = [f.sql for f in result.fragments if f.checked]
        if result.kind == "sql" and result.verified:
            try:
                with open(result.path, encoding="utf-8") as handle:
                    texts.append(handle.read())
            except Exception:
                return True
        for text in texts:
            try:
                tokens = Tokenizer().tokenize(text)
            except Exception:
                return True
            if any(t.token_type == TokenType.SLASH for t in tokens):
                return True
    return False


def recall_limits(results) -> list[str]:
    limits = []
    if _divides(results):
        limits.append(DIVISION_LIMIT)
    if any(r.kind == "project" for r in results):
        limits.append(PROJECT_LIMIT)
    if any(r.pandas for r in results):
        limits.append(PANDAS_LIMIT)
    return limits


def finding_to_dict(finding, tool_version: str) -> dict:
    return {
        "rule": finding.rule,
        "severity": finding.severity,
        "mode": finding.mode,
        "evidence_level": finding.evidence,
        "location": {"file": finding.file, "line": finding.line,
                     "col": finding.col},
        "snippet": finding.snippet,
        "why": finding.why,
        "fix": finding.fix,
        "standard_ref": f"{finding.rule} (standard-{STANDARD_VERSION})",
        "tool_version": tool_version,
    }


def all_findings(results) -> list:
    out = [f for r in results for f in r.findings]
    out.sort(key=lambda f: (f.file, f.line, f.col, f.rule))
    return out


def all_diagnostics(results) -> list:
    out = [d for r in results for d in r.diagnostics]
    out.sort(key=lambda d: (d.file, d.message))
    return out


def render_text(results, tool_version: str) -> str:
    lines = []
    for finding in all_findings(results):
        lines.append(
            f"{finding.file}:{finding.line}:{finding.col} "
            f"[{finding.severity}] {finding.rule} — {finding.why}")
        lines.append(f"    fix: {finding.fix}")
    return "\n".join(lines)


def python_sql_summary(results) -> dict:
    """Per .py file: fragment counts, and where the unchecked SQL is."""
    out = {}
    for result in results:
        if not result.fragments:
            continue
        entry = E.fragment_counts(result)
        for status in ("static", "templated"):  # how the checked SQL was built
            entry[status] = sum(1 for f in result.fragments
                                if f.status == status)
        entry["not_checked"] = [
            {"line": f.line, "col": f.col, "reason": f.reason}
            for f in result.fragments if not f.checked]
        out[result.rel] = entry
    return out


def render_json(results, tool_version: str, modes: list[str],
                receipt: dict | None = None) -> str:
    doc = {
        "schema_version": 1,
        "standard": STANDARD_VERSION,
        "tool": "truequery",
        "tool_version": tool_version,
        "offline": True,
        "modes": sorted(set(modes)),
        "files": [r.rel for r in results],
        "findings": [finding_to_dict(f, tool_version)
                     for f in all_findings(results)],
        "diagnostics": [{"file": d.file, "message": d.message,
                           "detail": d.detail}
                        for d in all_diagnostics(results)],
        "python_sql": python_sql_summary(results),
        # .py files the pandas checks read (added in 0.1.5).
        "pandas_files": sorted(r.rel for r in results if r.pandas),
    }
    if receipt is not None:
        doc["receipt"] = receipt
    return json.dumps(doc, indent=2, sort_keys=True)


def render_sarif(results, tool_version: str) -> str:
    implemented = {slug: rec for slug, rec in std_registry.RECORDS.items()
                   if rec["implemented"]}
    rules = [{
        "id": slug,
        "name": rec["title"],
        "shortDescription": {"text": rec["title"]},
        "properties": {"tier": rec["tier"], "enabled": rec["enabled"]},
    } for slug, rec in sorted(implemented.items())]
    rules.append({
        "id": "parse-error",
        "name": "Unparsable statement (diagnostic, not a rule)",
        "shortDescription": {
            "text": "A statement could not be parsed, so it was not checked."},
    })
    rules.sort(key=lambda r: r["id"])
    sarif_results = []
    for finding in all_findings(results):
        sarif_results.append({
            "ruleId": finding.rule,
            "level": SARIF_LEVEL.get(finding.severity, "warning"),
            "message": {"text": f"{finding.why} Fix: {finding.fix}"},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": finding.file},
                    "region": {"startLine": finding.line,
                               "startColumn": finding.col},
                }}],
            "properties": {"mode": finding.mode,
                           "evidence_level": finding.evidence},
        })
    for diagnostic in all_diagnostics(results):
        text = diagnostic.message
        if diagnostic.detail:
            text += f" Detail: {diagnostic.detail}"
        sarif_results.append({
            "ruleId": "parse-error",
            "level": "warning",
            "message": {"text": text},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": diagnostic.file},
                }}],
        })
    doc = {
        "version": "2.1.0",
        "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
        "runs": [{
            "tool": {"driver": {
                "name": "truequery",
                "version": tool_version,
                "informationUri": "https://github.com/Analytically-ai/truequery",
                "rules": rules,
            }},
            "results": sarif_results,
        }],
    }
    return json.dumps(doc, indent=2, sort_keys=True)


def judgment_in_scope(modes=None) -> list[dict]:
    """Judgment-only sql records the review teaches but no check runs,
    limited to the review's modes (an explore practice is out of scope
    in a build review)."""
    modes = set(modes or [])
    out = []
    for slug, rec in sorted(std_registry.RECORDS.items()):
        if rec["domain"] != "sql" or rec["enforcement"] != "judgment":
            continue
        if modes and not (set(rec["modes"]) & modes):
            continue
        out.append({"rule": slug, "title": rec["title"]})
    return out


def _plural(count: int, word: str) -> str:
    plural = {"advisory": "advisories"}.get(word, word + "s")
    return f"{count} {word if count == 1 else plural}"


def build_receipt(results, modes: list[str], contracts: dict,
                  tables_referenced: list[str],
                  tier_overrides: dict | None = None,
                  rule_settings: str | None = None,
                  invalid_contracts: list[str] | None = None,
                  invalid_metrics: list[str] | None = None,
                  contracts_changed: list[str] | None = None,
                  data: dict | None = None,
                  local_data: list[str] | None = None) -> dict:
    """`data` is this run's `review --data` result (None: data not read);
    `local_data` names the contracts whose data could be read here."""
    findings = all_findings(results)
    counts = {"error": 0, "warning": 0, "advisory": 0, "info": 0}
    attributed: dict[str, dict[str, int]] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        tag = getattr(finding, "_attribution", "")
        if tag in ("new", "existing", "attribution unknown"):
            slot = attributed.setdefault(tag, {})
            slot[finding.severity] = slot.get(finding.severity, 0) + 1
    # "Checked" counts only files the checker actually read: a file that
    # didn't parse, didn't decode or was over the size limit is listed
    # under "Not checked", never counted here too.
    sql = [r for r in results if r.kind == "sql" and r.verified]
    project = [r for r in results if r.kind == "project"]
    python = [r for r in results if E.fragment_counts(r)["checked"]]
    queries = sum(E.fragment_counts(r)["checked"] for r in python)
    pandas = [r for r in results if r.pandas]
    oversize = sorted({r.rel for r in results
                       if any(d.message == E.TOO_LARGE
                              for d in r.diagnostics)})
    unverified = sorted({r.rel for r in results if not r.verified}
                        - set(oversize))
    unparsed = sorted({r.rel for r in results
                       if any(E.parse_failed(d) for d in r.diagnostics)})
    procedural = sorted({r.rel for r in results
                         if any(E.is_procedural(d) for d in r.diagnostics)})
    missing = [t for t in tables_referenced
               if P.match_contract(t, contracts) is None]
    present = [t for t in tables_referenced
               if P.match_contract(t, contracts) is not None]
    drafts = [t for t in present
              if P.is_draft_contract(contracts[P.match_contract(t, contracts)])]
    consumers = sorted({k for r in results for k in r.key_lookups})
    narrow = sum(1 for r in results for s in r.suppressed if not s["broad"])
    broad = sum(1 for r in results for s in r.suppressed if s["broad"])
    contract_ev = f"{len(present)}/{len(tables_referenced)} tables"
    if drafts:
        contract_ev += f" ({len(drafts)} draft)"
    data_lines = None
    if data is not None:
        from truequery import dataverify  # only when the data was read
        data_lines = dataverify.receipt_lines(data)
        data_ev = dataverify.summary(data)
        data = dataverify.public_doc(data)
    elif local_data:
        data_ev = "not read in this run"
    else:
        data_ev = "no contract points at local data"
    return {
        "modes": sorted(set(modes)),
        "standard": STANDARD_VERSION,
        "files_checked": sum(1 for r in results
                             if r.kind not in ("macro", "embedded")
                             and r.verified),
        "sql_files": len(sql),
        "project_files": len(project),
        "python_files": len(python),
        "python_queries": queries,
        "pandas_files": len(pandas),
        "evidence": {
            "file": f"{len(sql)} SQL file(s)",
            "python": (f"SQL in {len(python)} Python file(s), "
                       f"{queries} quer{'y' if queries == 1 else 'ies'}"
                       if python else "no SQL in Python files checked"),
            "project": (f"{len(project)} project file(s), preview checks"
                        if project else "no project files in scope"),
            "pandas": (f"pandas in {len(pandas)} Python file(s)"
                       if pandas else "no pandas code checked"),
            "contract": contract_ev,
            "contract_consumers": consumers,
            "metadata": "no adapter",
            "data": data_ev,
        },
        "not_checked": {
            "unverified_files": unverified,
            # The same files by reason (a file can have both).
            "unparsed_files": unparsed,
            "procedural_files": procedural,
            "oversize_files": oversize,
            "macro_files": sorted(r.rel for r in results if r.kind == "macro"),
            "embedded_sql_files": sorted(
                r.rel for r in results if E.is_embedded(r)),
            "embedded_sql_reasons": {
                r.rel: E.unchecked_reasons(r) or ["not checked"]
                for r in results if E.is_embedded(r)},
            "invalid_contracts": list(invalid_contracts or []),
            "invalid_metrics": list(invalid_metrics or []),
            "metadata": "no adapter",
            "data": data_ev,
            "recall_limits": recall_limits(results),
        },
        "data": data,
        "data_lines": data_lines,
        "local_data": list(local_data or []),
        "findings": counts,
        "contracts_referenced": tables_referenced,
        "contracts_missing": missing,
        "contracts_draft": drafts,
        "contracts_changed": list(contracts_changed or []),
        "judgment_in_scope": judgment_in_scope(modes),
        "ignores": {"narrow": narrow, "broad": broad},
        "attribution": attributed,
        "tier_overrides": tier_overrides or {},
        "rule_settings": rule_settings,
    }


def _listed(names: list[str]) -> str:
    shown = ", ".join(names[:MAX_LISTED])
    if len(names) > MAX_LISTED:
        shown += f" and {len(names) - MAX_LISTED} more (--json lists all)"
    return shown


def render_receipt(receipt: dict) -> str:
    """The receipt, in plain words: what was checked, what it found, and
    what was NOT checked and why. Column one is a fixed-width label."""
    ev = receipt["evidence"]
    nc = receipt["not_checked"]
    lines = ["TrueQuery receipt"]
    if receipt.get("rule_settings"):
        lines.append(f"{'Settings':<13}{receipt['rule_settings']}")
    elif receipt.get("tier_overrides"):
        lines.append(f"{'Settings':<13}tier overrides active (testing only)")
    checked = [_plural(receipt["sql_files"], "SQL file")]
    if receipt.get("python_files"):
        queries = receipt["python_queries"]
        checked.append(f"SQL in {_plural(receipt['python_files'], 'Python file')}"
                       f" ({queries} quer{'y' if queries == 1 else 'ies'})")
    if receipt.get("pandas_files"):
        checked.append(f"pandas in "
                       f"{_plural(receipt['pandas_files'], 'Python file')}")
    if receipt["project_files"]:
        checked.append(_plural(receipt["project_files"], "project file"))
    total = len(receipt["contracts_referenced"])
    if total:
        have = total - len(receipt["contracts_missing"])
        drafts = len(receipt.get("contracts_draft", []))
        contract = f"contracts for {have} of {_plural(total, 'table')}"
        if drafts:
            contract += f" ({drafts} draft)"
        if ev["contract_consumers"] and have:
            contract += f" (keys used by {', '.join(ev['contract_consumers'])})"
        checked.append(contract)
    lines.append(f"{'Checked':<13}" + " · ".join(checked))
    data_lines = receipt.get("data_lines")
    for i, text in enumerate((data_lines or {}).get("verified", [])):
        lines.append(f"{'Verified':<13}against the data: {text}" if i == 0
                     else f"{'':<13}{text}")
    counts = receipt["findings"]
    attributed = receipt.get("attribution", {})
    parts = []
    for severity in ("error", "warning", "advisory"):
        if not counts.get(severity):
            continue
        bits = [f"{attributed[tag][severity]} {ATTRIBUTION_WORDS[tag]}"
                for tag in ("new", "existing", "attribution unknown")
                if attributed.get(tag, {}).get(severity)]
        parts.append(_plural(counts[severity], severity)
                     + (f" ({'; '.join(bits)})" if bits else ""))
    data_found = (data_lines or {}).get("found", [])
    if data_found:
        parts.append(_plural(len(data_found), "data failure"))
        lines.append(f"{'Found':<13}" + " · ".join(parts) + ":")
        lines.extend(f"{'':<13}{text}" for text in data_found)
    else:
        lines.append(f"{'Found':<13}" + (" · ".join(parts) or "no problems"))
    for text in (data_lines or {}).get("not_verified", []):
        lines.append(f"{'Not verified':<13}{text}")
    unverified = nc.get("unparsed_files", nc["unverified_files"])
    if unverified:
        lines.append(f"{'Not checked':<13}{_plural(len(unverified), 'file')} "
                     f"that didn't parse: {_listed(unverified)} (set "
                     "`dialect` in the DATA.md tq-config if they need one)")
    procedural = nc.get("procedural_files", [])
    if procedural:
        lines.append(f"{'Not checked':<13}{_plural(len(procedural), 'file')} "
                     f"with procedural code (PL/pgSQL, BEGIN … END), not "
                     f"looked inside yet: {_listed(procedural)}")
    oversized = nc.get("oversize_files", [])
    if oversized:
        lines.append(f"{'Not checked':<13}{_plural(len(oversized), 'file')} "
                     f"over the 10 MB limit: {_listed(oversized)}")
    macros = nc.get("macro_files", [])
    if macros:
        lines.append(f"{'Not checked':<13}{_plural(len(macros), 'dbt macro file')} "
                     f"(macro definitions are templates, not queries): "
                     f"{_listed(macros)}")
    embedded = nc.get("embedded_sql_files", [])
    reasons = nc.get("embedded_sql_reasons", {})
    for reason, words in (("built at run time", "SQL built at run time in"),
                          ("doesn't parse as SQL", "SQL that doesn't parse in")):
        files = [f for f in embedded if reason in reasons.get(f, [])]
        if files:
            lines.append(f"{'Not checked':<13}{words} "
                         f"{_plural(len(files), 'Python file')}: "
                         f"{_listed(files)}")
    other = [f for f in embedded if not set(reasons.get(f, [])) & {
        "built at run time", "doesn't parse as SQL"}]
    if other:
        lines.append(f"{'Not checked':<13}SQL inside "
                     f"{_plural(len(other), 'Python file')}: {_listed(other)}")
    for problem in nc.get("invalid_contracts", []):
        lines.append(f"{'Not checked':<13}invalid contract: {problem}")
    for problem in nc.get("invalid_metrics", []):
        lines.append(f"{'Not checked':<13}invalid metric: {problem}")
    if data_lines is not None:
        data_nc = data_lines.get("not_checked", [])
        data = receipt.get("data") or {}
        if not (data.get("datasets") or data_nc):
            data_nc = [DATA_LIMIT]
        for text in data_nc:
            lines.append(f"{'Not checked':<13}{text}")
    elif receipt.get("local_data"):
        names = receipt["local_data"]
        shown = ", ".join(names[:3]) + (f" and {len(names) - 3} more"
                                        if len(names) > 3 else "")
        lines.append(f"{'Not checked':<13}"
                     + DATA_NOT_READ.format(names=shown))
    else:
        lines.append(f"{'Not checked':<13}{DATA_LIMIT}")
    for limit in nc["recall_limits"]:
        lines.append(f"{'Not checked':<13}{limit}")
    ignores = receipt.get("ignores", {})
    if ignores.get("narrow") or ignores.get("broad"):
        lines.append(f"{'Ignored':<13}{ignores.get('narrow', 0)} line · "
                     f"{ignores.get('broad', 0)} file-wide "
                     "(file-wide ignores need a human)")
    if receipt["contracts_missing"]:
        lines.append(f"{'No contract':<13}"
                     + ", ".join(receipt["contracts_missing"]))
    if receipt.get("contracts_changed"):
        changed = ", ".join(receipt["contracts_changed"])
        lines.append(f"{'Session':<13}Contracts changed in the last agent "
                     f"session: {changed} (confirm with the user)")
    scoped = receipt["judgment_in_scope"]
    if scoped:
        lines.append("Judgment calls (no check can decide these):")
        for entry in scoped:
            lines.append(f"  {entry['rule']}: {entry['title']}")
    return "\n".join(lines)
