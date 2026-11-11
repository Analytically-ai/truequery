"""truequery: deterministic SQL checks for the agent edit loop. Offline, no LLM."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

from truequery import engine as E
from truequery import hook
from truequery import project as P
from truequery import report as RP
from truequery import schema
from truequery import settings as S
from truequery import std_registry
from truequery.analyzers import sql as analyzer
from truequery.engine import ENGINE_VERSION, active_overrides, run

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2
EXIT_UNVERIFIED = 3

EXIT_HELP = ("Exit codes: 0 = everything checked, no findings at or above "
             "--fail-on (default error); 1 = findings at or above it; "
             "2 = usage or config error; 3 = no such findings, but some "
             "files could not be checked (parse failures, undecodable "
             "text, or files over the size limit, "
             "listed on stderr). --allow-unverified turns 3 into 0.")

VERIFY_EXIT_HELP = (
    "Exit codes: 0 = every dataset with local data was read and matches its "
    "contract; 1 = the data breaks a contract; 2 = usage or config error "
    "(a bad --now, a dataset with no contract, an unreadable DATA.md); "
    "3 = no failures, but some data couldn't be verified (files missing or "
    "unreadable, no rows, checked only in part, an invalid contract). "
    "--allow-unverified turns 3 into 0. When the data reader isn't "
    "installed, datasets that need it are listed as not checked and the "
    "exit code ignores them, unless --require data, which makes that 3 even "
    "with --allow-unverified.")

FAIL_LEVELS = ("error", "warning", "advisory")
SCAFFOLD_OUT = "pipeline"  # truequery scaffold --out default


def _dump(args, text: str, json_doc: str, sarif_doc: str) -> None:
    if args.sarif:
        print(sarif_doc)
    elif args.json:
        print(json_doc)
    elif text:
        print(text)


def _cli_layer(args) -> dict | None:
    """CLI rule flags (the highest-precedence settings layer)."""
    rules: dict[str, dict] = {}
    for item in getattr(args, "rule_tier", None) or []:
        slug, sep, tier = item.partition(":")
        if not sep or tier not in ("immediate", "stop"):
            return None
        rules.setdefault(slug, {})["tier"] = tier
    for slug in getattr(args, "enable_rule", None) or []:
        rules.setdefault(slug, {})["enabled"] = True
    for slug in getattr(args, "disable_rule", None) or []:
        rules.setdefault(slug, {})["enabled"] = False
    return {"rules": rules} if rules else None


def _add_rule_flags(parser) -> None:
    parser.add_argument("--rule-tier", action="append",
                        metavar="SLUG:TIER",
                        help="override a rule's tier for this run "
                             "(highest precedence)")
    parser.add_argument("--enable-rule", action="append", metavar="SLUG",
                        help="enable a rule for this run")
    parser.add_argument("--disable-rule", action="append", metavar="SLUG",
                        help="disable a rule for this run")


def _effective_for(args, root):
    """(Effective, settings map) for CLI verbs; None layer on bad flags."""
    layer = _cli_layer(args)
    if layer is None and (getattr(args, "rule_tier", None)
                          or getattr(args, "enable_rule", None)
                          or getattr(args, "disable_rule", None)):
        raise ValueError("bad --rule-* flags (want SLUG[:TIER])")
    eff = S.effective(root=root, cli=layer)
    return eff, eff.check_map()


def _fail_level(args) -> int:
    return RP.SEVERITY_RANK[args.fail_on]


def _exit_for(results, args) -> int:
    threshold = _fail_level(args)
    for finding in RP.all_findings(results):
        if RP.SEVERITY_RANK.get(finding.severity, 0) >= threshold:
            return EXIT_FINDINGS
    if not args.allow_unverified and any(not r.verified for r in results):
        return EXIT_UNVERIFIED  # a CI gate must not pass on unchecked SQL
    return EXIT_CLEAN


def _stderr_diagnostics(results, verbose: bool = False) -> None:
    for diagnostic in RP.all_diagnostics(results):
        print(f"{diagnostic.file}: {diagnostic.message}", file=sys.stderr)
        if verbose and diagnostic.detail:
            print(f"  detail: {diagnostic.detail}", file=sys.stderr)


def _unverified_reason(results) -> str:
    """Why files went unchecked: parse failures, procedural code, the
    size limit."""
    diags = [d for r in results if not r.verified for d in r.diagnostics]
    reasons = []
    if any(E.parse_failed(d) for d in diags):
        reasons.append("could not parse")
    if any(E.is_procedural(d) for d in diags):
        reasons.append("procedural code")
    if any(d.message == E.TOO_LARGE for d in diags):
        reasons.append("over the size limit")
    return " / ".join(reasons) or "could not parse"


def _summary_line(results, findings) -> str:
    """Never print 'clean' unless every file was verified."""
    macros = sum(1 for r in results if r.kind == "macro")
    embedded = sum(1 for r in results if E.is_embedded(r))
    results = [r for r in results if r.kind not in ("macro", "embedded")]
    verified = sum(1 for r in results if r.verified)
    unverified = len(results) - verified
    checked = RP._plural(verified, "file")
    hidden = sum(len(r.suppressed) for r in results)
    ignored = f" ({hidden} hidden by ignores)" if hidden else ""
    reason = _unverified_reason(results)
    skipped = (f" · {RP._plural(macros, 'dbt macro file')} not checked "
               "(macro definitions)") if macros else ""
    embedded_note = (f" · SQL inside {RP._plural(embedded, 'Python file')} "
                     "not checked") if embedded else ""
    if not findings:
        if unverified:
            return (f"no findings in {checked} checked{ignored} · "
                    f"{unverified} not checked ({reason})"
                    f"{skipped}{embedded_note}")
        if skipped or embedded_note:
            return (f"no findings in {checked} checked offline{ignored}"
                    f"{skipped}{embedded_note}")
        if hidden:
            return f"no findings in {checked} checked offline{ignored}"
        return f"clean: {checked} checked offline, no findings"
    counts = [RP._plural(sum(1 for f in findings if f.severity == sev), sev)
              for sev in ("error", "warning")]
    advisories = sum(1 for f in findings if f.severity == "advisory")
    if advisories:
        counts.append(RP._plural(advisories, "advisory"))
    line = f"{', '.join(counts)} in {checked} checked offline{ignored}"
    if unverified:
        line += f" ({unverified} not checked: {reason})"
    return line + skipped + embedded_note


def _dialect_arg(value: str) -> str | None:
    try:
        return P.normalize_dialect(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None


def _add_output(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--json", action="store_true",
                       help="machine-readable report")
    group.add_argument("--sarif", action="store_true",
                       help="SARIF 2.1.0 report")
    parser.add_argument("--dialect", default=None, type=_dialect_arg,
                        help="sqlglot dialect; else DATA.md tq-config; else auto")
    parser.add_argument("--mode", default=None,
                        choices=["explore", "build", "change", "assure"],
                        help="override DATA.md mode globs")
    parser.add_argument("--fail-on", default="error", choices=FAIL_LEVELS,
                        help="lowest severity that fails (default error)")
    parser.add_argument("--verbose", action="store_true",
                        help="print full parse-error detail on stderr")
    parser.add_argument("--allow-unverified", action="store_true",
                        help="exit 0 even when some files could not be "
                             "checked")


def cmd_detect(args: argparse.Namespace) -> int:
    root = os.path.abspath(".")
    try:
        _, settings_map = _effective_for(args, root)
        results = run(args.paths, root, dialect=args.dialect,
                      mode=args.mode, settings=settings_map)
    except (P.ContextError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    modes = [r.mode for r in results]
    findings = RP.all_findings(results)
    if not results:
        note = "truequery detect: no SQL files found"
        if args.sarif:
            print(RP.render_sarif(results, ENGINE_VERSION))
        elif args.json:
            print(RP.render_json(results, ENGINE_VERSION, modes))
        else:
            print(note)
        return EXIT_CLEAN
    _dump(args,
          RP.render_text(results, ENGINE_VERSION)
          or _summary_line(results, findings),
          RP.render_json(results, ENGINE_VERSION, modes),
          RP.render_sarif(results, ENGINE_VERSION))
    _stderr_diagnostics(results, verbose=args.verbose)
    if not (args.json or args.sarif) and findings:
        print(_summary_line(results, findings))
    return _exit_for(results, args)


def _review_tables(results, dialect) -> list[str]:
    # Only files the checker read: a skipped file (over the size limit,
    # unparseable) is never opened again for the receipt.
    tables = P.tables_in_files([r.path for r in results
                                if r.kind == "sql" and r.verified], dialect)
    seen = {t.lower() for t in tables}
    for result in results:  # the SQL checked inside Python files
        for frag in result.fragments:
            if not frag.checked:
                continue
            for table in P.referenced_tables(frag.sql, dialect):
                if table.lower() not in seen:
                    seen.add(table.lower())
                    tables.append(table)
    return tables


def cmd_review(args: argparse.Namespace) -> int:
    from truequery.verify import local_data_contracts  # not loaded by hooks
    root = os.path.abspath(".")
    paths = args.paths or [root]
    require_data = getattr(args, "require", None) == "data"
    read_data = bool(getattr(args, "data", False)) or require_data
    DV = None
    if read_data:  # the data module loads only when the data is read
        from truequery import dataverify as DV
    try:
        now = DV.parse_now(args.now) if DV else None
    except ValueError as exc:
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR
    try:
        eff, settings_map = _effective_for(args, root)
        results = run(paths, root, dialect=args.dialect, mode=args.mode,
                      settings=settings_map)
    except (P.ContextError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    modes = [r.mode for r in results]
    contracts, invalid_contracts = P.load_contracts_reporting(root)
    data_doc = DV.run(root, now=now) if DV else None
    _, invalid_metrics = P.load_metrics_reporting(
        root, contracts=contracts)
    tables = _review_tables(results, args.dialect)
    receipt = RP.build_receipt(results, modes, contracts, tables,
                               tier_overrides=active_overrides(
                                   P.load_config(root)),
                               rule_settings=eff.settings_line(),
                               invalid_contracts=invalid_contracts,
                               invalid_metrics=invalid_metrics,
                               contracts_changed=hook.session_contracts_changed(
                                   root),
                               data=data_doc,
                               local_data=local_data_contracts(contracts))
    if args.sarif:
        print(RP.render_sarif(results, ENGINE_VERSION))
    elif args.json:
        print(RP.render_json(results, ENGINE_VERSION, modes,
                             receipt=receipt))
    else:
        text = RP.render_text(results, ENGINE_VERSION)
        if text:
            print(text)
            print()
        else:
            line = _summary_line(results, RP.all_findings(results))
            if data_doc is not None and line.startswith("clean: "):
                # "clean" is about the code; the data's verdict is in the
                # receipt, and may be partial. Never let one read as both.
                line = ("no findings in the code: "
                        + line[len("clean: "):].replace(", no findings", "")
                        + " (the data is in the receipt)")
            print(line)
            print()
        print(RP.render_receipt(receipt))
    _stderr_diagnostics(results, verbose=args.verbose)
    code = _exit_for(results, args)
    if data_doc is not None and code != EXIT_FINDINGS:
        # Code findings rank first; then the data: a failure is a finding,
        # data that couldn't be read is unverified.
        data_code = DV.exit_code(data_doc, args.allow_unverified,
                                 require_data, include_invalid=False)
        if data_code == EXIT_FINDINGS or (data_code == EXIT_UNVERIFIED
                                          and code == EXIT_CLEAN):
            code = data_code
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    from truequery import dataverify as DV  # not loaded by hooks
    root = os.path.abspath(".")
    require_data = args.require == "data"
    try:
        now = DV.parse_now(args.now)
        doc = DV.run(root, datasets=args.datasets or None, now=now)
    except (P.ContextError, ValueError) as exc:
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if doc["unknown"]:
        contracts, _ = P.load_contracts_reporting(root)
        known = sorted(str(c.get("dataset") or c.get("table"))
                       for c in contracts.values())
        print(f"truequery: no valid contract for {', '.join(doc['unknown'])}"
              f" (contracts here: {', '.join(known) or 'none'})",
              file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        out = DV.public_doc(doc)
        out["exit_code"] = DV.exit_code(doc, args.allow_unverified,
                                        require_data)
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print(DV.render_text(doc))
        if args.verbose:
            for name, detail in sorted(doc["not_checked_detail"].items()):
                print(f"{name}: {detail}", file=sys.stderr)
    return DV.exit_code(doc, args.allow_unverified, require_data)


def cmd_setup(args: argparse.Namespace) -> int:
    from truequery import datasetup
    if args.yes and args.no:
        print("truequery: choose --yes or --no, not both", file=sys.stderr)
        return EXIT_ERROR
    return datasetup.run_setup(args, os.path.abspath("."))


def cmd_init(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.path or ".")
    drafts = None
    if args.from_paths:
        from truequery import initfrom  # not loaded by hooks
        try:
            # Every source is checked before anything is written.
            drafts = initfrom.plan(root, args.from_paths)
        except (initfrom.InitFromError, P.ContextError, ValueError) as exc:
            print(f"truequery: init --from: {exc}", file=sys.stderr)
            return EXIT_ERROR
    stack = P.detect_stack(root)
    dialect = P.detect_dialect(root, stack)
    config = P.default_config(stack, dialect)
    wrote, kept = [], []
    data_md = os.path.join(root, "DATA.md")
    if not os.path.exists(data_md) or args.force:
        with open(data_md, "w", encoding="utf-8") as handle:
            handle.write(P.render_data_md(config))
        wrote.append(data_md)
    else:
        kept.append(data_md)
    contracts_dir = os.path.join(root, "contracts")
    os.makedirs(contracts_dir, exist_ok=True)
    # Contracts hold the team's confirmed answers: seeded when missing,
    # never overwritten, not even with --force.
    for path in P.seed_contracts(root):
        wrote.append(path)
    summary = {"stack": stack, "dialect": dialect, "wrote": wrote,
               "kept": kept}
    drafted: list[str] = []
    if drafts is not None:
        drafted = initfrom.write(root, drafts)
        summary["from"] = initfrom.public(drafts, drafted)
    if args.sarif:
        print(RP.render_sarif([], ENGINE_VERSION))
    elif args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        for path in wrote:
            print(f"wrote {path}")
        for path in kept:
            print(f"kept existing {path} (use --force to overwrite)")
        print(f"init: stack={stack} dialect={dialect or 'auto'} "
              f"({len(wrote)} wrote, {len(kept)} kept)")
        if drafts is not None:
            for line in initfrom.render_text(drafts, drafted):
                print(line)
    return EXIT_CLEAN


def cmd_scaffold(args: argparse.Namespace) -> int:
    from truequery import scaffold as SC  # not loaded by hooks
    root = os.path.abspath(".")
    try:
        result = SC.plan(args.contracts, args.out, root)
        written = SC.write(result, args.force)
    except SC.ScaffoldError as exc:
        print(f"truequery: scaffold: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps({"written": [P._rel(os.path.abspath(p), root)
                                      for p in written],
                          "contracts": [{"contract": r["contract"],
                                         "dataset": r["dataset"],
                                         "draft": r["draft"]}
                                        for r in result]},
                         indent=2, sort_keys=True))
        return EXIT_CLEAN
    for item in result:
        note = " (from a draft contract: confirm it, then regenerate)" \
            if item["draft"] else ""
        for key in ("module", "tests"):
            print(f"wrote {P._rel(os.path.abspath(item[key]), root)}{note}")
    print(f"scaffold: {RP._plural(len(result), 'contract')}. Write the "
          "transform between read_<dataset>() and check_<dataset>(); run "
          "the tests with pytest")
    return EXIT_CLEAN


def cmd_context(args: argparse.Namespace) -> int:
    root = os.path.abspath(".")
    config = P.load_config(root)
    stale = config is None
    if stale:
        config = P.default_config(P.detect_stack(root), None)
    dialect = (config or {}).get("dialect")
    rel, target_text = None, ""
    if args.target and os.path.isdir(args.target):
        # A folder: every SQL file in it. One MODE when they agree, else
        # "mixed" with the modes listed.
        rel = P._rel(os.path.abspath(args.target), root)
        files = P.discover([args.target])
        modes = sorted({P.resolve_mode(P._rel(f, root), config, None)
                        for f in files}) or [P.resolve_mode("", config, None)]
        mode = modes[0] if len(modes) == 1 else "mixed (" + ", ".join(modes) + ")"
        tables = P.tables_in_files(files, dialect)
    else:
        if args.target:
            rel = P._rel(os.path.abspath(args.target), root)
            try:
                with open(args.target, encoding="utf-8") as handle:
                    target_text = handle.read()
            except OSError:
                print(f"truequery: no such file or folder: {args.target}",
                      file=sys.stderr)
                return EXIT_ERROR
        mode = P.resolve_mode(rel or "", config, None)
        tables = P.referenced_tables(target_text, dialect)
    contracts = P.load_contracts(root)
    metrics = P.load_metrics(root)
    scoped_metrics = (P.metrics_for_tables(tables, contracts, metrics)
                      if args.target else {m["metric"]: m
                                           for _, m in sorted(metrics.items())})
    directives = [f"MODE: {mode}", "STANDARD: 0.1",
                  "EVIDENCE_AVAILABLE: file"]
    for table in tables:
        key = P.match_contract(table, contracts)
        if key is None:
            directives.append(f"CONTRACT_MISSING: {table}")
        elif contracts[key].get("status") == "draft":
            src = contracts[key].get("inferred_from") or "unconfirmed"
            directives.append(f"CONTRACT_DRAFT: {table} ({src})")
    if stale:
        directives.append("CONTEXT_STALE")
    doc = {"mode": mode, "standard": "0.1",
           "evidence_available": ["file"],
           "config": config, "target": rel,
           "tables_referenced": tables,
           "contracts": {t: contracts[P.match_contract(t, contracts)]
                         for t in tables
                         if P.match_contract(t, contracts) is not None},
           "metrics": scoped_metrics,
           "directives": directives,
           "stale": stale}
    if args.sarif:
        print(RP.render_sarif([], ENGINE_VERSION))
    elif args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(f"# truequery context (project root: {root})")
        print(f"stack={config.get('stack')} "
              f"dialect={config.get('dialect') or 'auto'}")
        if rel:
            print(f"target={rel}")
        for table in tables:
            key = P.match_contract(table, contracts)
            contract = contracts.get(key) if key is not None else None
            if contract is not None:
                line = (f"contract {table}: grain={contract['grain']} "
                        f"keys={P._format_keys(contract['keys']) or '—'}")
                if contract.get("version") == 2:
                    line += f" kind={contract.get('kind', 'table')}"
                    if contract.get("status") == "draft":
                        line += " status=draft"
                    loc_str = _format_location_short(contract.get("location"))
                    if loc_str:
                        line += f" location={loc_str}"
                    sla_str = _format_freshness_short(
                        contract.get("freshness_sla"))
                    if sla_str:
                        line += f" freshness_sla={sla_str}"
                print(line)
                for cline in _format_v2_contract_details(table, contract):
                    print(cline)
        for mname, metric in sorted(scoped_metrics.items()):
            print(_format_metric_context_line(mname, metric))
        if not tables:
            print("(target references no tables)")
        for directive in directives:
            print(directive)
    return EXIT_CLEAN


def _format_location_short(loc) -> str:
    if not isinstance(loc, dict):
        return ""
    bits: list[str] = []
    if loc.get("path"):
        bits.append(str(loc["path"]))
    if loc.get("format"):
        bits.append(f"format={loc['format']}")
    if loc.get("database"):
        bits.append(f"db={loc['database']}")
    if loc.get("relation"):
        bits.append(f"relation={loc['relation']}")
    if loc.get("base"):
        bits.append(f"base={loc['base']}")
    return "(" + ", ".join(bits) + ")" if bits else ""


def _format_freshness_short(sla) -> str:
    if isinstance(sla, str) and sla.strip():
        return sla.strip()
    if isinstance(sla, dict) and sla.get("column") and sla.get("max_age"):
        return f"{sla['column']}<={sla['max_age']}"
    return ""


def _format_measure_short(block) -> str:
    if not isinstance(block, dict):
        return ""
    expr = f"{block.get('agg')}({block.get('column')})"
    if block.get("where"):
        expr += f" where {block['where']}"
    return expr


def _format_metric_context_line(mname: str, metric: dict) -> str:
    parts = [f"metric {mname}: kind={metric['kind']}",
             f"dataset={metric['dataset']}"]
    if metric.get("meaning"):
        parts.append(f"meaning={metric['meaning']!r}")
    if metric["kind"] == "ratio":
        parts.append(
            f"numerator={_format_measure_short(metric.get('numerator'))}")
        parts.append(
            f"denominator={_format_measure_short(metric.get('denominator'))}")
    elif metric.get("measure"):
        parts.append(
            f"measure={_format_measure_short(metric.get('measure'))}")
    grain = metric.get("grain")
    if grain:
        g_str = (",".join(str(g) for g in grain)
                 if isinstance(grain, list) else str(grain))
        parts.append(f"grain={g_str}")
    if metric.get("unit"):
        parts.append(f"unit={metric['unit']}")
    return " ".join(parts)


def _format_v2_contract_details(table: str, contract: dict) -> list[str]:
    if contract.get("version") != 2:
        return []
    lines: list[str] = []
    cols = contract.get("columns") or {}
    if isinstance(cols, dict):
        col_bits: list[str] = []
        for cname, spec in cols.items():
            if not isinstance(spec, dict):
                continue
            tags: list[str] = []
            if spec.get("type"):
                tags.append(str(spec["type"]))
            if spec.get("nullable") is False:
                tags.append("nullable=false")
            elif spec.get("nullable") is True:
                tags.append("nullable=true")
            if spec.get("unit"):
                u = str(spec["unit"])
                if spec.get("currency"):
                    u += f" {spec['currency']}"
                elif spec.get("currency_column"):
                    u += f" @{spec['currency_column']}"
                tags.append(f"unit={u}")
            if spec.get("size_of"):
                tags.append(f"size_of={spec['size_of']}")
            if spec.get("time_zone"):
                tags.append(f"tz={spec['time_zone']}")
            if spec.get("relative_to"):
                tags.append(f"base={spec['relative_to']}")
            if spec.get("status_column"):
                tags.append(f"status_column={spec['status_column']}")
            if isinstance(spec.get("values"), dict):
                vals = ",".join(
                    f"{k}:{v['means']}" if isinstance(v, dict) and v.get("means")
                    else str(k)
                    for k, v in spec["values"].items()
                )
                tags.append(f"values=[{vals}]")
            elif isinstance(spec.get("values"), list):
                tags.append("values=[" + ",".join(
                    str(v) for v in spec["values"]) + "]")
            if spec.get("pii") is True:
                tags.append("pii=true")
            if tags:
                col_bits.append(f"{cname}({', '.join(tags)})")
        if col_bits:
            lines.append(f"  columns {table}: " + "; ".join(col_bits))
    return lines


def cmd_contract_validate(args: argparse.Namespace) -> int:
    root = os.path.abspath(".")
    try:
        report = P.validate_contracts_and_metrics(root, args.paths or None)
    except (P.ContextError, ValueError) as exc:
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR
    problems = report["problems"]
    nc = len(report["contracts_checked"])
    nm = len(report["metrics_checked"])
    if args.sarif:
        sarif_results = [{
            "ruleId": f"{p['kind']}-schema",
            "level": "error",
            "message": {"text": p["message"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": p["file"]},
                }}],
        } for p in problems]
        doc = {
            "version": "2.1.0",
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "runs": [{
                "tool": {"driver": {
                    "name": "truequery",
                    "version": ENGINE_VERSION,
                    "informationUri":
                        "https://github.com/Analytically-ai/truequery",
                    "rules": [
                        {"id": "contract-schema",
                         "name": "Dataset contract validation",
                         "shortDescription": {
                             "text": "Dataset contract schema or cross-file "
                                     "reference error."}},
                        {"id": "metric-schema",
                         "name": "Metric definition validation",
                         "shortDescription": {
                             "text": "Metric schema, filter allowlist, or "
                                     "dataset/column reference error."}},
                    ],
                }},
                "results": sarif_results,
            }],
        }
        print(json.dumps(doc, indent=2, sort_keys=True))
    elif args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for p in problems:
            print(f"{p['file']}: [error] {p['kind']}: {p['message']}")
        checked_parts = []
        if nc or not nm:
            checked_parts.append(RP._plural(nc, "contract"))
        if nm:
            checked_parts.append(RP._plural(nm, "metric"))
        checked_str = " and ".join(checked_parts)
        if not problems:
            if nc == 0 and nm == 0:
                print("truequery contract validate: no contract or metric "
                      "files found")
            else:
                print(f"clean: {checked_str} validated, no errors")
        else:
            print(f"{RP._plural(len(problems), 'error')} in "
                  f"{checked_str} checked")
    return EXIT_FINDINGS if problems else EXIT_CLEAN


def _rules_set(args: argparse.Namespace, root) -> int:
    """`tq rules set` — a HUMAN command: writes rule settings to
    the project (DATA.md tq-config) or user config. Agents never run
    this; SKILL.md says so."""
    slug = args.target
    updates = {}
    if args.enabled is not None:
        updates["enabled"] = args.enabled == "on"
    if args.tier is not None:
        updates["tier"] = args.tier
    if args.severity is not None:
        updates["severity"] = args.severity
    problem = S.guard(slug, updates, std_registry.RECORDS)
    if problem:
        print(problem, file=sys.stderr)
        return EXIT_ERROR
    try:
        if args.scope == "user":
            where = S.write_user_rule(slug, updates)
        else:
            data_md = os.path.join(root, "DATA.md")
            try:
                with open(data_md, encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                print("truequery: no DATA.md here; run `truequery init` first "
                      "(or use --scope user)", file=sys.stderr)
                return EXIT_ERROR
            with open(data_md, "w", encoding="utf-8") as handle:
                handle.write(S.data_md_with_rule(text, slug, updates))
            where = data_md
    except ValueError as exc:
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR
    eff = S.effective(root=root).rules[slug]
    state = ("on" if eff["enabled"] else "off") + f" tier={eff['tier']}"
    if eff["severity"]:
        state += f" severity={eff['severity']}"
    print(f"wrote {where}")
    print(f"{slug} is now {state} (effective, from the {eff['source']} "
          "settings)")
    if args.scope == "user" and eff["source"] == "project":
        print("note: DATA.md in this project sets this rule too, and a "
              "project setting overrides your user setting here.")
    return EXIT_CLEAN


def cmd_rules(args: argparse.Namespace) -> int:
    records = std_registry.RECORDS
    if getattr(args, "slug", None) == "set":
        return _rules_set(args, os.path.abspath("."))
    if args.slug:
        slug = args.slug
        if slug not in records:
            print(f"truequery: unknown rule: {slug}", file=sys.stderr)
            return EXIT_ERROR
        try:
            full = schema.load_all()[slug]
        except (schema.SchemaError, KeyError, OSError) as exc:
            print(f"truequery: cannot load {slug}: {exc}", file=sys.stderr)
            return EXIT_ERROR
        if args.sarif:
            print(RP.render_sarif([], ENGINE_VERSION))
        elif args.json:
            print(json.dumps({
                "rule": full.id, "title": full.title,
                "domain": full.domain, "modes": list(full.modes),
                "enforcement": full.enforcement,
                "evidence": full.evidence,
                "severity": full.severity, "tier": full.tier,
                "implemented": full.implemented,
                "enabled": full.enabled,
                "statement": full.raw.get("statement"),
                "rationale": full.raw.get("rationale"),
                "fix": (full.raw.get("examples") or {}).get("good"),
                "proof": ({
                    "setup": full.proof.setup, "bad": full.proof.bad,
                    "bad_result": full.proof.bad_result,
                    "good": full.proof.good,
                    "good_result": full.proof.good_result,
                    "note": full.proof.note} if full.proof else None),
            }, indent=2, sort_keys=True))
        else:
            print(f"{full.id} [{full.tier}] "
                  f"(implemented={full.implemented} "
                  f"enabled={full.enabled})")
            print(f"title: {full.title}")
            print(f"statement: {full.raw.get('statement')}")
            print(f"rationale: {full.raw.get('rationale')}")
            good = (full.raw.get("examples") or {}).get("good")
            if good:
                print(f"fix:\n{good}")
            if full.proof:
                print(f"proof bad:  {full.proof.bad}  "
                      f"-> {full.proof.bad_result}")
                print(f"proof good: {full.proof.good}  "
                      f"-> {full.proof.good_result}")
        return EXIT_CLEAN
    rows = [(slug, rec) for slug, rec in sorted(records.items())
            if args.all or rec["implemented"]]
    if args.sarif:
        print(RP.render_sarif([], ENGINE_VERSION))
        return EXIT_CLEAN
    try:
        eff, _ = _effective_for(args, os.path.abspath("."))
    except ValueError as exc:
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps(
            [{"rule": slug, "title": rec["title"],
              "implemented": rec["implemented"],
              "enabled": eff.rules[slug]["enabled"],
              "tier": eff.rules[slug]["tier"],
              "severity_override": eff.rules[slug]["severity"],
              "source": eff.rules[slug]["source"],
              "evidence": rec["evidence"],
              "enforcement": rec["enforcement"],
              "severity": rec["severity"]} for slug, rec in rows],
            indent=2, sort_keys=True))
    else:
        for slug, rec in rows:
            state = eff.rules[slug]
            head = f"{slug} [{'on' if state['enabled'] else 'off'} " \
                   f"{state['tier']}]"
            detail = (f"(implemented={rec['implemented']}, "
                      f"source: {state['source']})")
            if state["severity"]:
                detail = (f"(implemented={rec['implemented']}, "
                          f"severity: {state['severity']}, "
                          f"source: {state['source']})")
            print(f"{head} {detail} {rec['title']}")
        if not args.all:
            hidden = sum(1 for rec in records.values()
                         if not rec["implemented"])
            print(f"({hidden} unimplemented records hidden; "
                  f"use --all)")
    return EXIT_CLEAN


def cmd_hook(args: argparse.Namespace) -> int:
    return hook.main(args)


def cmd_install(args: argparse.Namespace) -> int:
    return hook.install(args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    return hook.uninstall(args)


def cmd_status(args: argparse.Namespace) -> int:
    code = hook.status(args)
    from truequery import datasetup
    try:
        lines = datasetup.status_lines(os.path.abspath("."))
    except (P.ContextError, ValueError) as exc:
        lines = [f"unknown ({exc})"]
    print(f"truequery status: data reader: {lines[0]}")
    for line in lines[1:]:
        print(f"  {line}")
    return code


def cmd_ignore_add(args: argparse.Namespace) -> int:
    """Narrowest-ignore with a reason. Line scope is required: the agent
    may self-serve only one line; broader ignores are the user's call
    (hand-edit .truequery/ignores.json)."""
    if args.rule not in std_registry.RECORDS:
        print(f"truequery: unknown rule: {args.rule}", file=sys.stderr)
        return EXIT_ERROR
    if not args.reason or not args.reason.strip():
        print("truequery: --reason is required (who and what evidence)",
              file=sys.stderr)
        return EXIT_ERROR
    match = re.match(r"^(.*):(\d+)$", args.target)
    if not match:
        print("truequery: target must be path:line — narrowest scope only; "
              "file-level ignores need a human", file=sys.stderr)
        return EXIT_ERROR
    raw_path, line = match.group(1), int(match.group(2))
    root = os.path.abspath(".")
    # Store the path the engine reports: relative to the project, with
    # forward slashes (`./models\\a.sql` -> `models/a.sql`).
    path = P._rel(os.path.abspath(raw_path), root)
    config = P.load_config(root)
    try:
        with open(raw_path if os.path.isabs(raw_path)
                   else os.path.join(root, raw_path),
                   encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        print(f"truequery: no such file: {raw_path}", file=sys.stderr)
        return EXIT_ERROR
    spans = E.statement_spans(path, text, (config or {}).get("dialect"))
    digest = E.stmt_hash_at(spans, line)
    # Outside any SQL statement, only an Airflow DAG (the DAG check) or
    # a file that imports pandas has findings a line ignore can cover.
    if digest is None and not (path.endswith(".py") and (
            E.project_checks.applies_to_file(path, text)
            or E.pandas_checks.imports_pandas(text))):
        print(f"truequery: line {line} is not inside a SQL statement",
              file=sys.stderr)
        return EXIT_ERROR
    state_dir = os.path.join(root, ".truequery")
    os.makedirs(state_dir, exist_ok=True)
    ignores_path = os.path.join(state_dir, "ignores.json")
    try:
        with open(ignores_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {"ignores": []}
    if not isinstance(data, dict):
        data = {"ignores": []}
    entries = data.setdefault("ignores", [])
    entry = {"rule": args.rule, "path": path, "line": line,
             "stmt": digest, "offset": E.stmt_offset(spans, line),
             "reason": args.reason.strip()}
    if digest is None:
        # A Python line outside any statement span (an Airflow DAG
        # finding): that one line, matched by rule + file + line.
        del entry["stmt"], entry["offset"]
    if entry not in entries:
        entries.append(entry)
    with open(ignores_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    print(f"ignore added: {args.rule} on {path}:{line} "
          f"(reason recorded)")
    return EXIT_CLEAN


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        # Program name follows the invoked entry point (`truequery`
        # or its `tq` alias), never a hardcoded default.
        prog=os.path.basename(sys.argv[0] or "truequery"),
        description="TrueQuery: catch SQL that silently returns the wrong "
                    "answer. Offline, deterministic, no LLM.")
    # The plugin's hook shim uses this as a handshake: it runs a
    # `truequery` on PATH only when it prints exactly "truequery <plugin
    # version>", so a stale or foreign binary is never trusted silently.
    parser.add_argument("--version", action="version",
                        version=f"truequery {ENGINE_VERSION}")
    sub = parser.add_subparsers(dest="command", required=False)

    # `check` is what agents tend to guess first; accept it.
    p_detect = sub.add_parser(
        "detect", aliases=["check"],
        help="run enabled checks on files/dirs (zero config)",
        epilog=EXIT_HELP)
    p_detect.add_argument("paths", nargs="+",
                          help=".sql and .py files or dirs")
    _add_output(p_detect)
    _add_rule_flags(p_detect)
    p_detect.set_defaults(func=cmd_detect)

    p_review = sub.add_parser(
        "review", help="checks plus the receipt (what was/wasn't checked)",
        epilog=EXIT_HELP)
    p_review.add_argument("paths", nargs="*",
                          help="files/dirs; default: the project")
    _add_output(p_review)
    _add_rule_flags(p_review)
    p_review.add_argument("--data", action="store_true",
                          help="also read the local data your contracts "
                               "point to (read-only) and add it to the "
                               "receipt; the same checks as `truequery "
                               "verify`. Data that breaks a contract exits "
                               "1; data that couldn't be verified exits 3")
    p_review.add_argument("--require", choices=["data"], default=None,
                          help="--require data: implies --data, and fails "
                               "(exit 3) when the data reader isn't "
                               "installed")
    p_review.add_argument("--now", default=None, metavar="ISO_TIME",
                          help="the time freshness is judged at (default: "
                               "now)")
    p_review.set_defaults(func=cmd_review)

    p_verify = sub.add_parser(
        "verify", help="check local data against its contracts (read-only)",
        epilog=VERIFY_EXIT_HELP)
    p_verify.add_argument("datasets", nargs="*",
                          help="dataset names; default: every contract with "
                               "local data")
    p_verify.add_argument("--json", action="store_true",
                          help="machine-readable report")
    p_verify.add_argument("--require", choices=["data"], default=None,
                          help="--require data: fail (exit 3) when the data "
                               "reader isn't installed, for CI")
    p_verify.add_argument("--now", default=None, metavar="ISO_TIME",
                          help="the time freshness is judged at, for "
                               "reproducible runs (default: now)")
    p_verify.add_argument("--allow-unverified", action="store_true",
                          help="exit 0 even when some data couldn't be "
                               "verified")
    p_verify.add_argument("--verbose", action="store_true",
                          help="print the reader's full error text on stderr "
                               "(it can quote a line of your data)")
    p_verify.set_defaults(func=cmd_verify)

    p_setup = sub.add_parser(
        "setup", help="install an optional part (only with --yes)")
    p_setup_sub = p_setup.add_subparsers(dest="setup_command", required=True)
    p_sdata = p_setup_sub.add_parser(
        "data", help="the data reader (DuckDB) that `truequery verify` uses "
                     "for data files and .duckdb databases")
    p_sdata.add_argument("--yes", action="store_true",
                         help="install it (without --yes: print the command "
                              "and change nothing)")
    p_sdata.add_argument("--no", action="store_true",
                         help="record in DATA.md that this project doesn't "
                              "want it; install nothing")
    p_sdata.set_defaults(func=cmd_setup)

    p_init = sub.add_parser(
        "init", help="detect stack/dialect; write DATA.md; seed contracts/ "
                     "(--from: draft contracts from data files)")
    p_init.add_argument("path", nargs="?", default=".",
                        help="project root (default .)")
    p_init.add_argument("--force", action="store_true",
                        help="rewrite DATA.md (contracts are never "
                             "overwritten)")
    p_init.add_argument("--from", dest="from_paths", nargs="+", default=None,
                        metavar="PATH",
                        help="also write draft contracts (version 2, status: "
                             "draft) from the CSV, JSONL and Parquet files "
                             "in these files or folders; they must be inside "
                             "the project or a base declared in DATA.md")
    p_init.add_argument("--json", action="store_true")
    p_init.add_argument("--sarif", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_scaffold = sub.add_parser(
        "scaffold", help="generate a pipeline's boundaries in plain pandas "
                         "from contracts: typed readers, output checks, "
                         "pytest tests")
    p_scaffold.add_argument("contracts", nargs="+", metavar="CONTRACT",
                            help="contract files (or dataset names in "
                                 "contracts/); version 2, with columns")
    p_scaffold.add_argument("--out", default=SCAFFOLD_OUT, metavar="DIR",
                            help=f"where to write (default: {SCAFFOLD_OUT})")
    p_scaffold.add_argument("--force", action="store_true",
                            help="replace files a previous scaffold wrote")
    p_scaffold.add_argument("--json", action="store_true")
    p_scaffold.set_defaults(func=cmd_scaffold)

    p_context = sub.add_parser(
        "context", help="DATA.md summary + referenced contracts + directives")
    p_context.add_argument("--target", default=None,
                           help="only contracts this SQL file (or the SQL "
                                "files in this folder) references")
    p_context.add_argument("--json", action="store_true")
    p_context.add_argument("--sarif", action="store_true")
    p_context.set_defaults(func=cmd_context)

    p_rules = sub.add_parser(
        "rules", help="list rules with effective settings; "
                       "'set' adjusts a rule (human only)",
        epilog="truequery rules set <slug> --enabled on|off --tier T "
               "--severity S [--scope project|user]  # human-only "
               "command; agents must never change rule settings.")
    p_rules.add_argument("--all", action="store_true",
                         help="include unimplemented records")
    p_rules.add_argument("slug", nargs="?",
                         help="show one record (statement, fix, proof); "
                              "'set' writes rule settings")
    p_rules.add_argument("target", nargs="?",
                         help="slug for 'truequery rules set'")
    p_rules.add_argument("--json", action="store_true")
    p_rules.add_argument("--sarif", action="store_true")
    p_rules.add_argument("--enabled", choices=["on", "off"],
                         help="(set) enable or disable the rule")
    p_rules.add_argument("--tier", choices=["immediate", "stop"],
                         help="(set) per-edit or end-of-turn tier")
    p_rules.add_argument("--severity", choices=list(
                         ("error", "warning", "advisory")),
                         help="(set) severity in every mode")
    p_rules.add_argument("--scope", default="project",
                         choices=["project", "user"],
                         help="(set) write to DATA.md or user config")
    _add_rule_flags(p_rules)
    p_rules.set_defaults(func=cmd_rules)

    p_hook = sub.add_parser(
        "hook", help="agent-harness hook entrypoint (event JSON on stdin)")
    p_hook.add_argument("--harness", default="auto",
                        choices=["auto", "claude", "codex", "muse"])
    p_hook.add_argument("--event", default="auto",
                        choices=["auto", "post", "stop"],
                        help="override the payload event (tests)")
    p_hook.add_argument("--log-file", default=None)
    p_hook.set_defaults(func=cmd_hook)

    p_install = sub.add_parser(
        "install", help="write agent-harness hook configs")
    p_install.add_argument("--harness", default="auto",
                           choices=["auto", "claude", "codex", "muse"])
    p_install.add_argument("--scope", default="project",
                           choices=["project", "global"])
    p_install.set_defaults(func=cmd_install)

    p_uninstall = sub.add_parser(
        "uninstall", help="remove hook configs written by truequery install")
    p_uninstall.add_argument("--harness", default="auto",
                             choices=["auto", "claude", "codex", "muse"])
    p_uninstall.add_argument("--scope", default="project",
                             choices=["project", "global"])
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_status = sub.add_parser(
        "status", help="report hook state per harness and the data reader "
                       "(never writes)")
    p_status.add_argument("--harness", default="auto",
                          choices=["auto", "claude", "codex", "muse"])
    p_status.add_argument("--scope", default="project",
                          choices=["project", "global"])
    p_status.set_defaults(func=cmd_status)

    p_ignore = sub.add_parser(
        "ignore", help="narrowest-ignore with a reason (line scope only)")
    p_ignore_sub = p_ignore.add_subparsers(dest="ignore_command",
                                           required=True)
    p_add = p_ignore_sub.add_parser("add")
    p_add.add_argument("rule", help="rule slug from truequery rules")
    p_add.add_argument("target", help="path:line (line required)")
    p_add.add_argument("--reason", default=None,
                       help="who and what evidence (required)")
    p_add.set_defaults(func=cmd_ignore_add)

    p_contract = sub.add_parser(
        "contract", help="validate dataset contracts and metric definitions")
    p_contract_sub = p_contract.add_subparsers(dest="contract_command",
                                               required=True)
    p_cval = p_contract_sub.add_parser(
        "validate",
        help="validate contracts/*.yaml, metrics/*.yaml, and DATA.md bases")
    p_cval.add_argument("paths", nargs="*",
                        help="contract or metric files/dirs (default: "
                             "contracts/ and metrics/)")
    cval_out = p_cval.add_mutually_exclusive_group()
    cval_out.add_argument("--json", action="store_true",
                          help="machine-readable report")
    cval_out.add_argument("--sarif", action="store_true",
                          help="SARIF 2.1.0 report")
    p_cval.set_defaults(func=cmd_contract_validate)
    return parser


@analyzer.quiet_sqlglot()
def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        print("\nTry: truequery detect <file.sql>  (start here)  |  "
              "truequery init  (new project)")
        return EXIT_ERROR
    try:
        return args.func(args)
    except P.ContextError as exc:
        # Unreadable DATA.md / user config: say so, never fall back to
        # defaults silently.
        print(f"truequery: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
