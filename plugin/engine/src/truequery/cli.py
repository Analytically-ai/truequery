"""tq: deterministic SQL checks for the agent edit loop. Offline, no LLM."""

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

FAIL_LEVELS = ("error", "warning", "advisory")


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
    """Why files went unchecked: parse failures, the size limit, or both."""
    diags = [d for r in results if not r.verified for d in r.diagnostics]
    reasons = []
    if any(d.message != E.TOO_LARGE for d in diags):
        reasons.append("could not parse")
    if any(d.message == E.TOO_LARGE for d in diags):
        reasons.append("over the size limit")
    return " / ".join(reasons) or "could not parse"


def _summary_line(results, findings) -> str:
    """Never print 'clean' unless every file was verified."""
    verified = sum(1 for r in results if r.verified)
    unverified = len(results) - verified
    checked = RP._plural(verified, "file")
    hidden = sum(len(r.suppressed) for r in results)
    ignored = f" ({hidden} hidden by ignores)" if hidden else ""
    reason = _unverified_reason(results)
    if not findings:
        if unverified:
            return (f"no findings in {checked} checked{ignored} · "
                    f"{unverified} not checked ({reason})")
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
    return line


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
        note = "tq detect: no SQL files found"
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
    return P.tables_in_files([r.path for r in results if r.verified], dialect)


def cmd_review(args: argparse.Namespace) -> int:
    root = os.path.abspath(".")
    paths = args.paths or [root]
    try:
        eff, settings_map = _effective_for(args, root)
        results = run(paths, root, dialect=args.dialect, mode=args.mode,
                      settings=settings_map)
    except (P.ContextError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_ERROR
    modes = [r.mode for r in results]
    contracts, invalid = P.load_contracts_reporting(root)
    tables = _review_tables(results, args.dialect)
    receipt = RP.build_receipt(results, modes, contracts, tables,
                               tier_overrides=active_overrides(
                                   P.load_config(root)),
                               rule_settings=eff.settings_line(),
                               invalid_contracts=invalid)
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
            print(_summary_line(results, RP.all_findings(results)))
            print()
        print(RP.render_receipt(receipt))
    _stderr_diagnostics(results, verbose=args.verbose)
    return _exit_for(results, args)


def cmd_init(args: argparse.Namespace) -> int:
    root = os.path.abspath(args.path or ".")
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
    return EXIT_CLEAN


def cmd_context(args: argparse.Namespace) -> int:
    root = os.path.abspath(".")
    config = P.load_config(root)
    stale = config is None
    if stale:
        config = P.default_config(P.detect_stack(root), None)
    rel, target_text = None, ""
    if args.target:
        rel = P._rel(os.path.abspath(args.target), root)
        try:
            with open(args.target, encoding="utf-8") as handle:
                target_text = handle.read()
        except OSError:
            print(f"tq: no such file: {args.target}", file=sys.stderr)
            return EXIT_ERROR
    mode = P.resolve_mode(rel or "", config, None)
    tables = P.referenced_tables(target_text, (config or {}).get("dialect"))
    contracts = P.load_contracts(root)
    directives = [f"MODE: {mode}", "STANDARD: 0.1",
                  "EVIDENCE_AVAILABLE: file"]
    for table in tables:
        if P.match_contract(table, contracts) is None:
            directives.append(f"CONTRACT_MISSING: {table}")
    if stale:
        directives.append("CONTEXT_STALE")
    doc = {"mode": mode, "standard": "0.1",
           "evidence_available": ["file"],
           "config": config, "target": rel,
           "tables_referenced": tables,
           "contracts": {t: contracts[P.match_contract(t, contracts)]
                         for t in tables
                         if P.match_contract(t, contracts) is not None},
           "directives": directives,
           "stale": stale}
    if args.sarif:
        print(RP.render_sarif([], ENGINE_VERSION))
    elif args.json:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(f"# tq context (project root: {root})")
        print(f"stack={config.get('stack')} "
              f"dialect={config.get('dialect') or 'auto'}")
        if rel:
            print(f"target={rel}")
        for table in tables:
            key = P.match_contract(table, contracts)
            contract = contracts.get(key) if key is not None else None
            if contract is not None:
                print(f"contract {table}: grain={contract['grain']} "
                      f"keys={P._format_keys(contract['keys']) or '—'}")
        if not tables:
            print("(target references no tables)")
        for directive in directives:
            print(directive)
    return EXIT_CLEAN


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
                print("tq: no DATA.md here; run `truequery init` first "
                      "(or use --scope user)", file=sys.stderr)
                return EXIT_ERROR
            with open(data_md, "w", encoding="utf-8") as handle:
                handle.write(S.data_md_with_rule(text, slug, updates))
            where = data_md
    except ValueError as exc:
        print(f"tq: {exc}", file=sys.stderr)
        return EXIT_ERROR
    eff = S.effective(root=root).rules[slug]
    state = ("on" if eff["enabled"] else "off") + f" tier={eff['tier']}"
    if eff["severity"]:
        state += f" severity={eff['severity']}"
    print(f"set {slug}: {state} (source: {eff['source']}, wrote {where})")
    return EXIT_CLEAN


def cmd_rules(args: argparse.Namespace) -> int:
    records = std_registry.RECORDS
    if getattr(args, "slug", None) == "set":
        return _rules_set(args, os.path.abspath("."))
    if args.slug:
        slug = args.slug
        if slug not in records:
            print(f"tq: unknown rule: {slug}", file=sys.stderr)
            return EXIT_ERROR
        try:
            full = schema.load_all()[slug]
        except (schema.SchemaError, KeyError, OSError) as exc:
            print(f"tq: cannot load {slug}: {exc}", file=sys.stderr)
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
        print(f"tq: {exc}", file=sys.stderr)
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
    return hook.status(args)


def cmd_ignore_add(args: argparse.Namespace) -> int:
    """Narrowest-ignore with a reason. Line scope is required: the agent
    may self-serve only one line; broader ignores are the user's call
    (hand-edit .truequery/ignores.json)."""
    if args.rule not in std_registry.RECORDS:
        print(f"tq: unknown rule: {args.rule}", file=sys.stderr)
        return EXIT_ERROR
    if not args.reason or not args.reason.strip():
        print("tq: --reason is required (who and what evidence)",
              file=sys.stderr)
        return EXIT_ERROR
    match = re.match(r"^(.*):(\d+)$", args.target)
    if not match:
        print("tq: target must be path:line — narrowest scope only; "
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
        print(f"tq: no such file: {raw_path}", file=sys.stderr)
        return EXIT_ERROR
    spans = E._chunk_hashes(text, (config or {}).get("dialect"))
    digest = E.stmt_hash_at(spans, line)
    if digest is None:
        print(f"tq: line {line} is not inside a SQL statement",
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
    p_detect.add_argument("paths", nargs="+", help=".sql files or dirs")
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
    p_review.set_defaults(func=cmd_review)

    p_init = sub.add_parser(
        "init", help="detect stack/dialect; write DATA.md; seed contracts/")
    p_init.add_argument("path", nargs="?", default=".",
                        help="project root (default .)")
    p_init.add_argument("--force", action="store_true",
                        help="rewrite DATA.md (contracts are never "
                             "overwritten)")
    p_init.add_argument("--json", action="store_true")
    p_init.add_argument("--sarif", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_context = sub.add_parser(
        "context", help="DATA.md summary + referenced contracts + directives")
    p_context.add_argument("--target", default=None,
                           help="only contracts this SQL file references")
    p_context.add_argument("--json", action="store_true")
    p_context.add_argument("--sarif", action="store_true")
    p_context.set_defaults(func=cmd_context)

    p_rules = sub.add_parser(
        "rules", help="list rules with effective settings; "
                       "'set' adjusts a rule (human only)",
        epilog="tq rules set <slug> --enabled on|off --tier T "
               "--severity S [--scope project|user]  # human-only "
               "command; agents must never change rule settings.")
    p_rules.add_argument("--all", action="store_true",
                         help="include unimplemented records")
    p_rules.add_argument("slug", nargs="?",
                         help="show one record (statement, fix, proof); "
                              "'set' writes rule settings")
    p_rules.add_argument("target", nargs="?",
                         help="slug for 'tq rules set'")
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
        "uninstall", help="remove tq-installed harness hook configs")
    p_uninstall.add_argument("--harness", default="auto",
                             choices=["auto", "claude", "codex", "muse"])
    p_uninstall.add_argument("--scope", default="project",
                             choices=["project", "global"])
    p_uninstall.set_defaults(func=cmd_uninstall)

    p_status = sub.add_parser(
        "status", help="report hook state per harness (never writes)")
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
    p_add.add_argument("rule", help="rule slug from tq rules")
    p_add.add_argument("target", help="path:line (line required)")
    p_add.add_argument("--reason", default=None,
                       help="who and what evidence (required)")
    p_add.set_defaults(func=cmd_ignore_add)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        print("\nTry: tq detect <file.sql>  (start here)  |  tq init  (new project)")
        return EXIT_ERROR
    try:
        return args.func(args)
    except P.ContextError as exc:
        # Unreadable DATA.md / user config: say so, never fall back to
        # defaults silently.
        print(f"tq: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
