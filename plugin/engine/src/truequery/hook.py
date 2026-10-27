"""Hook adapter: immediate + Stop tiers, session state, triage protocol.

Reads one hook-event JSON object on stdin, runs the same engine and
reporters as the CLI, and prints the harness envelope on stdout.
Claude Code, Codex and Muse. Never breaks the user session: unexpected
input is a silent no-op (exit 0). No edit is ever refused; at the end
of a turn, new findings are handed back to the agent once (Claude:
additionalContext; Codex and Muse: decision "block").

Routing: a PostToolUse event runs the immediate tier (rules with
tier == immediate in the registry) over the edited SQL files; a Stop
event runs the full enabled set over every file touched this session,
deduplicated, with baseline attribution. Session state lives in
.truequery/session.json.
"""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import subprocess
import sys
import time

from truequery import engine as E
from truequery import project as P
from truequery import report as RP
from truequery import settings as S
from truequery.analyzers import sql as analyzer
from truequery.engine import run

TIMEOUT_SECONDS = 10
MAX_FINDING_LINES = 20
MAX_CHAIN = 2
SESSION_NAME = "session.json"


def read_stdin(timeout: float = 5.0) -> str:
    """Read stdin with a timeout; empty string when nothing arrives.
    Windows can't select() on pipes; harnesses always pipe the event and
    close stdin, so a plain read is safe there."""
    if sys.platform == "win32":
        try:
            return sys.stdin.read()
        except Exception:
            return ""
    try:
        ready, _, _ = select.select([sys.stdin], [], [], timeout)
    except Exception:
        return ""
    if not ready:
        return ""
    try:
        return sys.stdin.read()
    except Exception:
        return ""


MUSE_TOOLS = frozenset({"edit_file", "write_file", "multi_edit"})


def _exe_name(path: str) -> str:
    """`truequery` for /x/truequery, C:\\x\\truequery.exe or TRUEQUERY.EXE."""
    name = os.path.basename(path).lower()
    return name[:-4] if name.endswith(".exe") else name


def _quoted(path: str) -> str:
    # A path with a space (C:\\Program Files, Application Support) must be
    # quoted, or the hook command splits at the space.
    return f'"{path}"' if any(c.isspace() for c in path) else path


def _hook_command(harness: str) -> str:
    """Absolute path of the checker, so the hook works even when the
    harness runs hooks with a PATH that lacks the install dir."""
    argv0 = sys.argv[0] if sys.argv else ""
    if os.path.isabs(argv0) and _exe_name(argv0) in ("tq", "truequery"):
        return f"{_quoted(argv0)} hook --harness {harness}"  # running now
    # `truequery`, not `tq`: other tools ship a `tq` too.
    found = shutil.which("truequery")
    if found and os.path.isabs(found):
        return f"{_quoted(found)} hook --harness {harness}"
    return f"truequery hook --harness {harness}"


def detect_harness(payload: dict) -> str:
    """apply_patch payloads are Codex, edit_file/write_file/multi_edit
    are Muse; anything else is Claude."""
    tool_name = payload.get("tool_name", payload.get("toolName", ""))
    if isinstance(tool_name, str) and tool_name == "apply_patch":
        return "codex"
    if isinstance(tool_name, str) and tool_name in MUSE_TOOLS:
        return "muse"
    return "claude"


def detect_event(payload: dict) -> str:
    """PostToolUse (immediate) or Stop. Anything else means immediate."""
    event = payload.get("hook_event_name", payload.get("hookEventName", ""))
    if isinstance(event, str) and event.lower() == "stop":
        return "stop"
    return "post"


def extract_paths(payload: dict) -> list[str]:
    """Edited file paths from a PostToolUse-style payload."""
    tool_input = payload.get("tool_input", payload.get("toolArgs", {}))
    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except ValueError:
            tool_input = {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    direct = tool_input.get("file_path", tool_input.get("filePath"))
    if direct:
        return [direct]
    # Muse edit_file/write_file carry the path as `path`.
    direct = tool_input.get("path")
    if direct:
        return [direct]
    files = tool_input.get("file_paths", tool_input.get("filePaths"))
    if isinstance(files, list) and all(isinstance(f, str) for f in files):
        return files
    for key in ("command", "patch", "input"):
        paths = _paths_from_command(tool_input.get(key, ""))
        if paths:
            return paths
    return []


def _paths_from_command(command) -> list[str]:
    """File paths from a Codex edit-tool `command` patch body.

    Verified from the Codex hooks docs: edit tools expose the patch as
    `${tool_input.command}`. The exact patch dialect is not documented,
    so accept every common shape: `*** Update/Add File: <path>`,
    unified-diff `+++ b/<path>` headers, and bare `+++ <path>`.
    `command` may also arrive as a list of strings.
    """
    if isinstance(command, list):
        command = "\n".join(str(part) for part in command)
    if not isinstance(command, str) or not command:
        return []
    paths = []
    for line in command.splitlines():
        match = re.match(r"\*\*\*\s+(?:Update|Add) File:\s*(\S+)", line)
        if match:
            paths.append(match.group(1))
            continue
        match = re.match(r"\+\+\+\s+(?:b/)?(\S+)", line)
        if match and match.group(1) != "/dev/null":
            paths.append(match.group(1))
    return paths


is_sql_path = P.is_sql_path


# -- session state ------------------------------------------------------------
def _session_path(root: str) -> str:
    return os.path.join(root, ".truequery", SESSION_NAME)


# Session fields and their types. Anything else on disk (an older
# version, a hand edit, a torn write) is reset, never trusted: a bad
# field must not silence the hook for good.
SESSION_FIELDS = {"session_id": (str, type(None)), "touched": list,
                  "reported": list, "blocked": list, "clean_acked": list,
                  "chain": int, "config_fp": (str, type(None)),
                  "config_error": (str, type(None)),
                  "version_noted": (str, type(None))}


def _fresh_session(session_id=None) -> dict:
    return {"session_id": session_id, "touched": [], "reported": [],
            "blocked": [], "clean_acked": [], "chain": 0,
            "config_fp": None, "config_error": None, "version_noted": None}


def load_session(root: str, session_id: str | None = None) -> dict:
    """Session state for this harness session. A new session_id starts
    fresh, so findings said in yesterday's session are said again."""
    try:
        with open(_session_path(root), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if session_id is not None and data.get("session_id") != session_id:
        return _fresh_session(session_id)
    session = _fresh_session(session_id)
    for name, kinds in SESSION_FIELDS.items():
        value = data.get(name)
        if isinstance(value, kinds) and not isinstance(value, bool):
            session[name] = value
    session["touched"] = [p for p in session["touched"] if isinstance(p, str)]
    session["clean_acked"] = [p for p in session["clean_acked"]
                              if isinstance(p, str)]
    session["reported"] = [e for e in session["reported"]
                           if isinstance(e, dict) and isinstance(e.get("key"), str)]
    session["blocked"] = [e for e in session["blocked"]
                          if isinstance(e, list) and len(e) == 2]
    return session


def save_session(root: str, session: dict) -> None:
    """Atomic write (temp file + rename), plus a .gitignore so session
    state never lands in the user's repo. ignores.json stays committable."""
    state_dir = os.path.join(root, ".truequery")
    try:
        os.makedirs(state_dir, exist_ok=True)
        ignore = os.path.join(state_dir, ".gitignore")
        if not os.path.exists(ignore):
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write("# TrueQuery session state (ignores.json is meant "
                             "to be committed)\nsession.json\nhook.log\n")
        tmp = _session_path(root) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"version": 2,
                       **{name: session.get(name, _fresh_session()[name])
                          for name in SESSION_FIELDS}},
                      handle, indent=2, sort_keys=True)
        os.replace(tmp, _session_path(root))
    except OSError:
        pass


def _key(finding) -> str:
    """rule + file + statement: stable across line shifts, and a new
    statement with the same rule in the same file is new."""
    return "\0".join((finding.rule, finding.file,
                      getattr(finding, "_stmt", None) or ""))


def _key_file(key: str) -> str:
    parts = key.split("\0")
    return parts[1] if len(parts) > 1 else ""


def _reported_keys(session: dict) -> dict[str, int]:
    return {entry["key"]: entry.get("line", 0)
            for entry in session.get("reported", [])
            if isinstance(entry, dict) and "key" in entry}


# -- messages -----------------------------------------------------------------
def _finding_line(finding) -> str:
    return (f"- {finding.file}:{finding.line} [{finding.rule}] "
            f"{finding.why} (fix: {finding.fix})")


def triage_block(rel: str, new_findings: list) -> str:
    lines = [f"[TrueQuery] {len(new_findings)} new finding(s) in {rel}"]
    for finding in new_findings[:MAX_FINDING_LINES]:
        lines.append(_finding_line(finding))
    first = new_findings[0]
    lines.append(
        "Triage: fix it; or ignore narrowly "
        f"(`truequery ignore add {first.rule} {first.file}:{first.line} "
        '--reason "..."` — line scope only); or ask once. '
        "File-level ignores need a human.")
    return "\n".join(lines)


def nudge_line(finding) -> str:
    return (f"[TrueQuery] still open: {finding.rule} "
            f"{finding.file}:{finding.line} (seen)")


def clean_line(rel: str, modes=None) -> str:
    scoped = RP.judgment_in_scope(modes)
    lines = [f"[TrueQuery] checked {rel} (file evidence): no new findings."]
    if scoped:
        lines.append(
            "That doesn't mean it's good — "
            f"{len(scoped)} judgment practices in scope "
            f"({', '.join(e['rule'] for e in scoped)}); "
            "run `truequery review` before merge.")
    return "\n".join(lines)


def unverified_block(diagnostics) -> str:
    if all(d.message == E.TOO_LARGE for d in diagnostics):
        why = "over the size limit, not checked"
    elif any(d.message == E.TOO_LARGE for d in diagnostics):
        why = "could not parse or over the size limit, not checked"
    else:
        why = "could not parse, not checked"
    lines = [f"[TrueQuery] UNVERIFIED — {why}:"]
    for diagnostic in diagnostics[:5]:
        lines.append(f"- {diagnostic.file}: {diagnostic.message}")
    lines.append("Run `truequery detect --verbose` for the full parse error.")
    return "\n".join(lines)


def _git(root: str, *argv: str) -> str | None:
    """stdout of a read-only, local git command; None on any failure."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")
    try:
        proc = subprocess.run(["git", "-C", root, *argv], capture_output=True,
                              text=True, timeout=30, env=env)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _in_git(root: str) -> bool:
    return (_git(root, "rev-parse", "--is-inside-work-tree") or "").strip() \
        == "true"


def _head_signatures(root: str, rel: str, dialect, mode) -> set | None:
    """{(rule, stmt-hash)} for the file's git HEAD version, or None when
    there is no baseline (no git, untracked file, unreadable HEAD)."""
    if _git(root, "ls-files", "--error-unmatch", "--", rel) is None:
        return None  # untracked (or no git): no baseline
    head = _git(root, "show", f"HEAD:{rel}")
    if head is None:
        return None
    try:
        res = analyzer.check_text(head, dialect=dialect, path=rel, mode=mode)
    except Exception:
        return None
    spans = E._chunk_hashes(head, dialect)
    return {(finding["rule"], E.stmt_hash_at(spans, finding["line"]))
            for finding in res["findings"]}


def attribute_findings(root: str, results, dialect) -> None:
    """Tag every finding against the git baseline: new (not in HEAD),
    existing (in HEAD), or attribution unknown (no baseline). HEAD
    content is read with `git show` — local and offline."""
    cache: dict[str, set | None] = {}
    in_git = _in_git(root)
    for result in results:
        if not in_git:
            cache[result.rel] = None
        elif result.rel not in cache:
            cache[result.rel] = _head_signatures(
                root, result.rel, dialect, result.mode)
        baseline = cache[result.rel]
        for finding in result.findings:
            if baseline is None:
                finding._attribution = "attribution unknown"
            elif (finding.rule,
                  getattr(finding, "_stmt", None)) in baseline:
                finding._attribution = "existing"
            else:
                finding._attribution = "new"


def stop_block(results, chain: int, fresh: list) -> str:
    """Stop speaks only for new, unacknowledged findings — one emission
    per finding, never a receipt (the receipt belongs to the skill's
    final message, e.g. `tq review`)."""
    files = len(results)
    head = (f"[TrueQuery] stop pass · {files} file(s) touched")
    if chain:
        head += f" · continuation {chain}/{MAX_CHAIN}"
    lines = [head]
    for finding in fresh[:MAX_FINDING_LINES]:
        tag = getattr(finding, "_attribution", "new")
        lines.append(_finding_line(finding) +
                     f" ({RP.ATTRIBUTION_WORDS.get(tag, tag)})")
    extra = len(fresh) - MAX_FINDING_LINES
    if extra > 0:
        lines.append(f"- ... and {extra} more (run `truequery review`)")
    return "\n".join(lines)


def envelope(harness: str, event: str, text: str) -> dict:
    """PostToolUse: hookSpecificOutput.additionalContext on every harness
    (Codex documents it for PostToolUse too). Codex and Muse Stop take
    decision/block + reason; Claude Stop takes additionalContext, which
    its hooks docs define as non-error feedback that continues the turn."""
    name = "Stop" if event == "stop" else "PostToolUse"
    if harness in ("codex", "muse") and event == "stop":
        return {"decision": "block", "reason": text}
    return {"hookSpecificOutput": {"hookEventName": name,
                                   "additionalContext": text}}


# -- passes -------------------------------------------------------------------
def immediate_pass(root: str, paths: list[str], session: dict,
                   dialect, settings=None, config=None) -> str | None:
    # Absolute paths: the hook's own cwd need not be the project root.
    results = run([os.path.join(root, p) for p in paths], root,
                  dialect=dialect, tiers={"immediate"}, settings=settings,
                  config=config)
    known = _reported_keys(session)
    texts = []
    diags = [d for r in results for d in r.diagnostics]
    if diags:
        texts.append(unverified_block(diags))
    for result in results:
        new = [f for f in result.findings
               if _key(f) not in known]
        old = [f for f in result.findings if _key(f) in known]
        if new:
            texts.append(triage_block(result.rel, new))
            for finding in result.findings:
                known[_key(finding)] = finding.line
            if result.rel in session.get("clean_acked", []):
                session["clean_acked"].remove(result.rel)
        elif old:
            for finding in old:
                texts.append(nudge_line(finding))
        elif result.verified and _had_reported(result.rel, session):
            if result.rel not in session.get("clean_acked", []):
                texts.append(clean_line(result.rel, [result.mode]))
                session["clean_acked"].append(result.rel)
    session["reported"] = [{"key": k, "line": line}
                           for k, line in sorted(known.items())]
    if not texts:
        return None
    return "\n".join(texts)


def _had_reported(rel: str, session: dict) -> bool:
    return any(_key_file(entry.get("key", "")) == rel
               for entry in session.get("reported", [])
               if isinstance(entry, dict))


def stop_pass(root: str, session: dict, dialect, continued: bool,
              settings=None, config=None) -> tuple[list | None, bool]:
    """(results, block_worthy). Block-worthy means findings the agent
    has not seen as baseline: new or attribution-unknown. All-existing
    findings stay silent so a clean stop really stops.

    Only the chain caused by our own feedback is bounded: a continued
    turn (stop_hook_active) may continue at most MAX_CHAIN times; a new
    turn always gets its Stop pass. Callers render per harness.
    """
    touched = [p for p in session.get("touched", [])
               if is_sql_path(p) and os.path.isfile(os.path.join(root, p))]
    if not touched:
        return None, False  # nothing to pass over: silent
    if continued:
        session["chain"] = session.get("chain", 0) + 1
        if session["chain"] > MAX_CHAIN:
            return None, False  # chain cap: stay silent past it
    else:
        session["chain"] = 0
    results = run([os.path.join(root, p) for p in touched], root,
                  dialect=dialect, settings=settings, config=config)
    attribute_findings(root, results, dialect)
    known = _reported_keys(session)
    for result in results:
        for finding in result.findings:
            known[_key(finding)] = finding.line
    session["reported"] = [{"key": k, "line": line}
                           for k, line in sorted(known.items())]
    block = any(getattr(f, "_attribution", "") in ("new",
                                                   "attribution unknown")
                for r in results for f in r.findings)
    # A touched file that did not parse is never a silent pass.
    block = block or any(not r.verified for r in results)
    return results, block


def _version_notice(session) -> str | None:
    """Once per session: the plugin's shim says which engine version it
    expects (TRUEQUERY_PLUGIN_VERSION). If this engine is another one, it
    still checks, but says how to get the matching engine."""
    from truequery import __version__
    wanted = os.environ.get("TRUEQUERY_PLUGIN_VERSION", "").strip()
    if not wanted or wanted == __version__:
        return None
    key = f"{__version__}->{wanted}"
    if session.get("version_noted") == key:
        return None
    session["version_noted"] = key
    return (f"[TrueQuery] The checker is {__version__} but the plugin is "
            f"{wanted}. Results may be out of date; tell the user to install "
            f'uv (https://docs.astral.sh/uv/) so the plugin runs its own '
            f'checker, or to run: pip install -U truequery=={wanted}')


def _stop_text(root, session, dialect, continued, settings,
               config, harness="claude") -> str | None:
    """Stop speaks only for findings that are new (or unattributable) and
    not yet said this session; a clean or already-reported pass is
    silent, so the turn really ends."""
    # In Claude Code, findings handed back right after an edit were said
    # once: the end-of-turn pass doesn't repeat them. Codex and Muse rely
    # on the Stop block, so it still carries every new finding there.
    said = set(_reported_keys(session)) if harness == "claude" else set()
    results, block = stop_pass(root, session, dialect, continued,
                               settings=settings, config=config)
    if not block:
        return None
    seen = _blocked_sigs(session)
    fresh = [f for r in results for f in r.findings
             if getattr(f, "_attribution", "") in ("new", "attribution unknown")
             and tuple(_sig(f)) not in seen and _key(f) not in said]
    diags = [d for r in results for d in r.diagnostics
             if ("unverified", f"{d.file}\0{d.message}") not in seen]
    if not fresh and not diags:
        return None
    for finding in fresh:
        session["blocked"].append(_sig(finding))
    for diag in diags:
        session["blocked"].append(["unverified", f"{diag.file}\0{diag.message}"])
    texts = [stop_block(results, session.get("chain", 0), fresh)] if fresh else []
    if diags:
        texts.append(unverified_block(diags))
    return "\n".join(texts)


def _sig(finding) -> list:
    return [finding.rule, getattr(finding, "_stmt", None)]


def _blocked_sigs(session: dict) -> set[tuple]:
    return {tuple(e) for e in session.get("blocked", [])
            if isinstance(e, list) and len(e) == 2}


# -- install --------------------------------------------------------------------
# Per harness: config files (the one `install` writes first), the
# PostToolUse matcher for its edit tools, and the trust step the user
# still has to take. Claude's project hooks go to settings.local.json:
# the hook command is an absolute path on this machine, so it must not
# land in the shared, committed settings.json.
HARNESSES = {
    "claude": ((os.path.join(".claude", "settings.local.json"),
                os.path.join(".claude", "settings.json")),
               "^Edit$|^Write$|^MultiEdit$|^NotebookEdit$",
               "accept the workspace trust dialog for hooks to run"),
    "codex": ((os.path.join(".codex", "hooks.json"),),
              "Write|Edit",  # the Codex hooks docs' file-edit example
              "approve the hook once via /hooks (trust is hash-recorded)"),
    "muse": ((os.path.join(".muse", "hooks.json"),),
             "edit_file|write_file|multi_edit|apply_patch",
             "trust this workspace for hooks to run (first-run dialog, "
             "or muse exec --trust-workspace)"),
}


class ConfigShapeError(ValueError):
    pass


# `tq hook`, `/x/truequery hook`, `"C:\\Program Files\\truequery.exe" hook`.
_OURS = re.compile(r"""(?:^|[\s"'/\\])(?:tq|truequery)(?:\.exe)?["']?\s+hook\b""",
                   re.IGNORECASE)


def _is_ours(command) -> bool:
    """A hook command `tq install` wrote (not the plugin's shim)."""
    return isinstance(command, str) and bool(_OURS.search(command))


def _scope_root(args) -> str:
    return (os.path.expanduser("~") if args.scope == "global"
            else os.path.abspath("."))


def _targets(args, default_all: bool) -> list[str]:
    if args.harness != "auto":
        return [args.harness]
    return list(HARNESSES) if default_all else ["claude"]


def _config_files(name: str, args) -> list[str]:
    files = HARNESSES[name][0]
    if name == "claude" and args.scope == "global":
        files = (os.path.join(".claude", "settings.json"),)
    return [os.path.join(_scope_root(args), rel) for rel in files]


def _read_config(path: str) -> dict:
    """Decoded config ({} when absent). Raises ConfigShapeError when the
    file is not a JSON object with a well-formed hooks block: we never
    rewrite a config we can't parse."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ConfigShapeError(f"{path} is not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise ConfigShapeError(f"{path} is not a JSON object")
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not all(
            isinstance(groups, list) for groups in hooks.values()):
        raise ConfigShapeError(f"{path}: \"hooks\" must map each event to "
                               "a list of hook groups")
    return data


def _write_config(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)


def _our_commands(config: dict) -> list[str]:
    return [h["command"] for groups in config.get("hooks", {}).values()
            for g in groups if isinstance(g, dict)
            for h in g.get("hooks", []) if isinstance(h, dict)
            and _is_ours(h.get("command"))]


def _with_our_hooks(config: dict, matcher: str, command: str) -> dict:
    """config plus one PostToolUse and one Stop entry running command.
    Idempotent: an existing tq entry is left as it is."""
    hooks = config.setdefault("hooks", {})
    entry = {"type": "command", "command": command,
             "timeout": TIMEOUT_SECONDS}
    for event, group in (("PostToolUse", {"matcher": matcher}),
                         ("Stop", {})):
        groups = hooks.setdefault(event, [])
        if not any(_is_ours(h.get("command"))
                   for g in groups if isinstance(g, dict)
                   for h in g.get("hooks", []) if isinstance(h, dict)):
            groups.append({**group, "hooks": [dict(entry)]})
    return config


def _plugin_enabled(args) -> str | None:
    """Path of a Claude settings file that enables the truequery plugin."""
    candidates = [os.path.join(os.path.expanduser("~"), ".claude",
                               "settings.json")]
    if args.scope != "global":
        candidates += _config_files("claude", args)
    for path in candidates:
        try:
            config = _read_config(path)
        except ConfigShapeError:
            continue
        enabled = config.get("enabledPlugins")
        if isinstance(enabled, dict) and any(
                key.split("@")[0] == "truequery" and value is True
                for key, value in enabled.items()):
            return path
    return None


def install(args) -> int:
    """Write hook configs for one harness (Claude Code unless --harness
    says otherwise). Idempotent and merge-aware: other tools' hooks and
    settings are kept. A config we can't parse is left untouched."""
    targets = _targets(args, default_all=False)
    if "claude" in targets and _plugin_enabled(args):
        print("tq install: the truequery plugin is enabled, and its hooks "
              "are already active; not installing a second copy")
        targets.remove("claude")
    for name in targets:
        path = _config_files(name, args)[0]
        try:
            config = _read_config(path)
        except ConfigShapeError as exc:
            print(f"tq install: {exc}. Nothing was changed; fix the file "
                  "and run install again.", file=sys.stderr)
            return 2
        command = _hook_command(name)
        _write_config(path, _with_our_hooks(config, HARNESSES[name][1],
                                            command))
        print(f"tq install: wrote {path}")
        print(f"tq install: hook command is {command}")
        print(f"tq install: {HARNESSES[name][2]}")
    return 0


def scan(args) -> dict:
    """{harness: (state, detail)}. state is `off`, `tq-install` (hooks
    written by `tq install`) or `plugin` (the truequery plugin is
    enabled in Claude Code and its own hooks are active)."""
    out = {}
    for name in _targets(args, default_all=True):
        if name == "claude":
            plugin = _plugin_enabled(args)
            if plugin:
                out[name] = ("plugin", f"plugin enabled ({plugin})")
                continue
        found = None
        for path in _config_files(name, args):
            try:
                if _our_commands(_read_config(path)):
                    found = path
                    break
            except ConfigShapeError:
                continue
        out[name] = (("tq-install", f"tq install hooks ({found})") if found
                     else ("off", "no truequery hooks"))
    return out


def status(args) -> int:
    """Report hook state per harness. Never writes."""
    found_plugin = False
    for name, (state, detail) in scan(args).items():
        print(f"tq status: {name}: {state} — {detail}")
        found_plugin = found_plugin or state == "plugin"
    if found_plugin:
        print("tq status: plugin hooks are active; do not also run "
              "`tq install` (never double-install)")
    return 0


def _without_our_hooks(config: dict) -> int:
    """Remove tq-installed hook entries in place; drop groups and events
    left empty. Returns the number of entries removed. Other tools'
    entries are untouched."""
    removed = 0
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    for event in list(hooks):
        kept_groups = []
        for group in hooks[event]:
            if not isinstance(group, dict) or not isinstance(
                    group.get("hooks"), list):
                kept_groups.append(group)
                continue
            kept = [h for h in group["hooks"]
                    if not (isinstance(h, dict) and _is_ours(h.get("command")))]
            removed += len(group["hooks"]) - len(kept)
            if kept:
                kept_groups.append({**group, "hooks": kept})
        if kept_groups:
            hooks[event] = kept_groups
        else:
            del hooks[event]
    if not hooks:
        del config["hooks"]
    return removed


def uninstall(args) -> int:
    """Remove tq-installed hook entries from every harness config. The
    plugin's own hooks belong to the plugin manager: disable the plugin
    there instead."""
    removed = 0
    for name in _targets(args, default_all=True):
        for path in _config_files(name, args):
            try:
                config = _read_config(path)
            except ConfigShapeError as exc:
                print(f"tq uninstall: skipped {exc}", file=sys.stderr)
                continue
            count = _without_our_hooks(config) if config else 0
            if count:
                _write_config(path, config)
                removed += count
    if _plugin_enabled(args):
        print("tq uninstall: the truequery plugin is enabled; its hooks are "
              "left alone (disable the plugin to turn them off)")
    print(f"tq uninstall: removed {removed} tq-install entr"
          f"{'y' if removed == 1 else 'ies'}")
    return 0


# -- entry ----------------------------------------------------------------------
def main(args) -> int:
    started = time.monotonic()
    harness = args.harness
    event = getattr(args, "event", "auto")
    raw = read_stdin()
    if not raw.strip():
        return 0  # missing launcher/input = silent no-op
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    if harness == "auto":
        harness = detect_harness(payload)
    if harness not in ("claude", "codex", "muse"):
        return 0  # only Claude Code, Codex and Muse are supported
    if event == "auto":
        event = detect_event(payload)
    claimed = payload.get("cwd")
    root = (claimed if isinstance(claimed, str)
            and os.path.isdir(claimed) else os.path.abspath("."))
    # A continued turn extends the chain (bounded); a new turn resets it.
    continued = payload.get("stop_hook_active") is True
    session_id = payload.get("session_id")
    session = load_session(root, session_id if isinstance(session_id, str)
                           else None)
    notice = None
    text = None
    try:
        # A broken DATA.md or user config must not silently disable
        # checking: say so once per distinct problem, then run on what
        # still loads (shipped defaults for the broken layer).
        problem = None
        try:
            cfg = P.load_config(root)
        except P.ConfigError as exc:
            cfg, problem = None, exc
        try:
            eff = S.effective(project=cfg or {})
        except P.ConfigError as exc:
            eff, problem = S.effective(project=cfg or {}, user={}), problem or exc
        if problem is None:
            session.pop("config_error", None)
        elif session.get("config_error") != str(problem):
            session["config_error"] = str(problem)
            notice = (f"[TrueQuery] {problem}. Checks ran on shipped "
                      "defaults until it is fixed.")
        dialect = (cfg or {}).get("dialect")
        fp = eff.fingerprint()
        if session.get("config_fp") is not None and session["config_fp"] != fp:
            notice = S.NOTICE
        session["config_fp"] = fp
        hook_settings = eff.check_map()
        if event == "stop":
            text = _stop_text(root, session, dialect, continued,
                              hook_settings, cfg or {}, harness)
        else:
            paths = [p for p in extract_paths(payload) if is_sql_path(p)]
            paths = [p for p in paths
                     if os.path.isfile(os.path.join(root, p))
                     or os.path.isfile(p)]
            if not paths:
                return 0  # non-SQL edit = silent no-op
            rels = [P._rel(p if os.path.isabs(p) else os.path.join(root, p),
                           root) for p in paths]
            for rel in rels:
                if rel not in session.get("touched", []):
                    session.setdefault("touched", []).append(rel)
            if eff.interrupt_edits:
                text = immediate_pass(root, rels, session, dialect,
                                      settings=hook_settings, config=cfg or {})
        mismatch = _version_notice(session)
        if mismatch:
            notice = mismatch if notice is None else mismatch + "\n" + notice
        if notice is not None:
            text = notice if text is None else notice + "\n" + text
    except Exception:
        return 0  # never break the session
    finally:
        save_session(root, session)
    if args.log_file:
        try:
            with open(args.log_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(
                    {"harness": harness, "event": event,
                     "elapsed_ms": int((time.monotonic() - started) * 1000),
                     "output": bool(text)}) + "\n")
        except OSError:
            pass
    if not text:
        return 0
    print(json.dumps(envelope(harness, event, text)))
    return 0
