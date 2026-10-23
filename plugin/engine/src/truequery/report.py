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
# must name them so a pass means something.
RECALL_LIMITS = (
    "integer division between columns (column types are unknown from "
    "files alone)",
    "project rules beyond the 4 preview checks (dbt YAML, incremental "
    "models, Airflow catchup)",
)


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


def render_json(results, tool_version: str, modes: list[str],
                receipt: dict | None = None) -> str:
    doc = {
        "schema_version": 1,
        "standard": STANDARD_VERSION,
        "tool": "tq",
        "tool_version": tool_version,
        "offline": True,
        "modes": sorted(set(modes)),
        "files": [r.rel for r in results],
        "findings": [finding_to_dict(f, tool_version)
                     for f in all_findings(results)],
        "diagnostics": [{"file": d.file, "message": d.message,
                           "detail": d.detail}
                        for d in all_diagnostics(results)],
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
                "name": "tq",
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
                  invalid_contracts: list[str] | None = None) -> dict:
    findings = all_findings(results)
    counts = {"error": 0, "warning": 0, "advisory": 0, "info": 0}
    attributed: dict[str, dict[str, int]] = {}
    for finding in findings:
        counts[finding.severity] = counts.get(finding.severity, 0) + 1
        tag = getattr(finding, "_attribution", "")
        if tag in ("new", "existing", "attribution unknown"):
            slot = attributed.setdefault(tag, {})
            slot[finding.severity] = slot.get(finding.severity, 0) + 1
    sql = [r for r in results if r.kind == "sql"]
    project = [r for r in results if r.kind == "project"]
    oversize = sorted({r.rel for r in results
                       if any(d.message == E.TOO_LARGE
                              for d in r.diagnostics)})
    unverified = sorted({r.rel for r in results if not r.verified}
                        - set(oversize))
    missing = [t for t in tables_referenced
               if P.match_contract(t, contracts) is None]
    present = [t for t in tables_referenced
               if P.match_contract(t, contracts) is not None]
    consumers = sorted({k for r in results for k in r.key_lookups})
    narrow = sum(1 for r in results for s in r.suppressed if not s["broad"])
    broad = sum(1 for r in results for s in r.suppressed if s["broad"])
    return {
        "modes": sorted(set(modes)),
        "standard": STANDARD_VERSION,
        "files_checked": len(results),
        "sql_files": len(sql),
        "project_files": len(project),
        "evidence": {
            "file": f"{len(sql)} SQL file(s)",
            "project": (f"{len(project)} project file(s), preview checks"
                        if project else "no project files in scope"),
            "contract": f"{len(present)}/{len(tables_referenced)} tables",
            "contract_consumers": consumers,
            "metadata": "no adapter",
            "data": "no adapter",
        },
        "not_checked": {
            "unverified_files": unverified,
            "oversize_files": oversize,
            "invalid_contracts": list(invalid_contracts or []),
            "metadata": "no adapter",
            "data": "no adapter",
            "recall_limits": list(RECALL_LIMITS),
        },
        "findings": counts,
        "contracts_referenced": tables_referenced,
        "contracts_missing": missing,
        "judgment_in_scope": judgment_in_scope(modes),
        "ignores": {"narrow": narrow, "broad": broad},
        "attribution": attributed,
        "tier_overrides": tier_overrides or {},
        "rule_settings": rule_settings,
    }


def render_receipt(receipt: dict) -> str:
    """The receipt: what was checked, what was NOT checked and why, and
    the findings. Column one is a fixed-width label."""
    modes = ",".join(receipt["modes"]) or "build"
    ev = receipt["evidence"]
    nc = receipt["not_checked"]
    lines = [
        f"[truequery@1] receipt · mode={modes} · standard-{receipt['standard']}"]
    if receipt.get("rule_settings"):
        lines.append(f"{'settings':<13}{receipt['rule_settings']}")
    elif receipt.get("tier_overrides"):
        lines.append(f"{'settings':<13}tier overrides active (testing only)")
    checked = [_plural(receipt["sql_files"], "SQL file")]
    if receipt["project_files"]:
        checked.append(_plural(receipt["project_files"], "project file")
                       + " (preview checks)")
    total = len(receipt["contracts_referenced"])
    if total:
        have = total - len(receipt["contracts_missing"])
        contract = f"contracts for {have} of {_plural(total, 'table')}"
        if ev["contract_consumers"]:
            contract += f" (keys used by {', '.join(ev['contract_consumers'])})"
        checked.append(contract)
    lines.append(f"{'checked':<13}" + " · ".join(checked))
    lines.append(f"{'not checked':<13}warehouse metadata and data "
                 "(no warehouse adapter connected)")
    unverified = nc["unverified_files"]
    if unverified:
        shown = ", ".join(unverified[:MAX_LISTED])
        if len(unverified) > MAX_LISTED:
            shown += f" and {len(unverified) - MAX_LISTED} more (--json lists all)"
        lines.append(f"{'not checked':<13}could not parse "
                     f"{_plural(len(unverified), 'file')}: {shown} "
                     "(set `dialect` in the DATA.md tq-config if "
                     "identifiers need one)")
    for problem in nc.get("invalid_contracts", []):
        lines.append(f"{'not checked':<13}invalid contract: {problem}")
    oversized = nc.get("oversize_files", [])
    if oversized:
        shown = ", ".join(oversized[:MAX_LISTED])
        if len(oversized) > MAX_LISTED:
            shown += f" and {len(oversized) - MAX_LISTED} more (--json lists all)"
        lines.append(f"{'not checked':<13}over the 10 MB size limit "
                     f"{_plural(len(oversized), 'file')}: {shown}")
    lines.append(f"{'not checked':<13}" + "; ".join(nc["recall_limits"]))
    counts = receipt["findings"]
    attributed = receipt.get("attribution", {})
    parts = []
    for severity in ("error", "warning", "advisory"):
        part = _plural(counts.get(severity, 0), severity)
        bits = [f"{attributed[tag][severity]} {tag}"
                for tag in ("new", "existing", "attribution unknown")
                if attributed.get(tag, {}).get(severity)]
        parts.append(part + (f" ({'; '.join(bits)})" if bits else ""))
    lines.append(f"{'findings':<13}" + " · ".join(parts))
    ignores = receipt.get("ignores", {})
    if ignores.get("narrow") or ignores.get("broad"):
        lines.append(f"{'ignores':<13}{ignores.get('narrow', 0)} narrow · "
                     f"{ignores.get('broad', 0)} broad "
                     "(broad ignores need a human)")
    if receipt["contracts_missing"]:
        lines.append(f"{'no contract':<13}"
                     + ", ".join(receipt["contracts_missing"]))
    scoped = receipt["judgment_in_scope"]
    if scoped:
        lines.append("judgment-only practices in scope (no check runs them):")
        for entry in scoped:
            lines.append(f"  {entry['rule']} — {entry['title']}")
    return "\n".join(lines)
