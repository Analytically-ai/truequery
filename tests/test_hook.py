"""Hook goldens: tiers, session state, triage, envelopes, install.

Triage machinery tests patch one rule's tier to immediate (reverted
after), so they don't depend on which rules the registry marks immediate.
"""

import io
import json
import os
import sys

import pytest

import truequery.hook as hook
from truequery import std_registry

ROOT = os.path.join(os.path.dirname(__file__), "..")


class Args:
    def __init__(self, harness="auto", event="auto", log_file=None,
                 scope="project"):
        self.harness = harness
        self.event = event
        self.log_file = log_file
        self.scope = scope


@pytest.fixture
def immediate_unguarded(monkeypatch):
    slug = "sql/unguarded-update-delete"
    patched = dict(std_registry.RECORDS[slug], tier="immediate")
    monkeypatch.setitem(std_registry.RECORDS, slug, patched)


def _run(payload, args, monkeypatch):
    monkeypatch.setattr(hook, "read_stdin", lambda timeout=5.0: payload)
    out = io.StringIO()
    monkeypatch.setattr(hook.sys, "stdout", out)
    code = hook.main(args)
    return code, out.getvalue()


def _event(path, tool="Write", event="PostToolUse"):
    return json.dumps(
        {
            "hook_event_name": event,
            "tool_name": tool,
            "tool_input": {"file_path": path},
        }
    )


def test_claude_envelope_uses_hook_specific_output(tmp_path, monkeypatch,
                                                   immediate_unguarded):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(target)), Args(harness="claude"),
                     monkeypatch)
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]  # B1: never top-level
    assert "additionalContext" not in json.loads(out)
    assert body["hookEventName"] == "PostToolUse"
    assert body["additionalContext"].startswith(
        "[truequery@1] 1 new finding(s) in q.sql")


def test_first_finding_triage_golden(tmp_path, monkeypatch,
                                     immediate_unguarded):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert text == (
        "[truequery@1] 1 new finding(s) in q.sql\n"
        "- q.sql:1 [sql/unguarded-update-delete] no WHERE: every row is "
        "affected (fix: add a WHERE predicate, or use TRUNCATE if you mean "
        "the whole table)\n"
        "Triage: fix it; or ignore narrowly "
        "(`tq ignore add sql/unguarded-update-delete q.sql:1 "
        '--reason "..."` — line scope only); or ask once. '
        "File-level ignores need a human.")
    session = json.loads(
        (tmp_path / ".truequery" / "session.json").read_text(
            encoding="utf-8"))
    assert session["touched"] == ["q.sql"]
    [entry] = session["reported"]
    assert entry["line"] == 1
    assert entry["key"].startswith("sql/unguarded-update-delete\x00q.sql\x00")


def test_reedit_nudge_then_clean_ack(tmp_path, monkeypatch,
                                     immediate_unguarded):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert text == ("[truequery@1] still open: sql/unguarded-update-delete "
                    "q.sql:1 (seen)")
    target.write_text("SELECT a FROM t ORDER BY a LIMIT 1;\n",
                      encoding="utf-8")
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    # build mode: no explore-only judgment practices in scope
    assert text == ("[truequery@1] checked q.sql (file evidence): "
                    "no new findings.")
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert (code, out) == (0, "")  # acked once


def test_codex_envelope_and_patch_paths(tmp_path, monkeypatch,
                                        immediate_unguarded):
    target = tmp_path / "m.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    payload = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "apply_patch",
            "tool_input": {"command": "*** Update File: m.sql\n@@\n+SELECT 1\n"},
        }
    )
    monkeypatch.chdir(tmp_path)
    code, out = _run(payload, Args(harness="codex"), monkeypatch)
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "PostToolUse"
    assert "1 new finding(s) in m.sql" in body["additionalContext"]


def test_immediate_silent_without_immediate_rules(tmp_path, monkeypatch):
    # No monkeypatched tiers: a cross join is a stop-tier finding, so it
    # stays silent here, but the file is tracked for the Stop pass.
    target = tmp_path / "q.sql"
    target.write_text("SELECT * FROM a, b;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert (code, out) == (0, "")
    session = json.loads(
        (tmp_path / ".truequery" / "session.json").read_text(
            encoding="utf-8"))
    assert session["touched"] == ["q.sql"]


def test_parse_diagnostic_reaches_agent(tmp_path, monkeypatch):
    target = tmp_path / "bad.sql"
    target.write_text("SELECT FROM WHERE (((;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(target)), Args(), monkeypatch)
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "[truequery@1] UNVERIFIED" in body


def _git(path, *args):
    import subprocess
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM="1", HOME=str(path))
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "-c", "init.defaultBranch=main", *args],
        cwd=path, check=True, capture_output=True, env=env)


def _stop_text(target, monkeypatch, event="Stop", active=False,
               harness="auto"):
    payload = json.loads(_event(str(target), event=event))
    if active:
        payload["stop_hook_active"] = True
    code, out = _run(json.dumps(payload), Args(harness=harness),
                     monkeypatch)
    assert code == 0
    if harness == "codex":
        return json.loads(out) if out else None
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "Stop"
    return body["additionalContext"]


def test_stop_lists_every_finding_no_git(tmp_path, monkeypatch):
    old = tmp_path / "old.sql"
    old.write_text("SELECT * FROM a, b;\n", encoding="utf-8")
    new = tmp_path / "new.sql"
    new.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(old)), Args(), monkeypatch)
    _run(_event(str(new)), Args(), monkeypatch)
    text = _stop_text(new, monkeypatch)
    assert text.startswith("[truequery@1] stop pass · 2 file(s) touched")
    # every NEW finding is listed with file:line, rule, why and fix;
    # the receipt belongs to the skill, never to the Stop hook
    assert "- old.sql:1 [sql/accidental-cross-join]" in text
    assert "- new.sql:1 [sql/unguarded-update-delete]" in text
    assert "[truequery@1] receipt" not in text
    session = json.loads(
        (tmp_path / ".truequery" / "session.json").read_text(
            encoding="utf-8"))
    assert session["chain"] == 0


def test_stop_untracked_file_is_unknown(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    text = _stop_text(target, monkeypatch)
    assert "- q.sql:1 [sql/unguarded-update-delete]" in text


def test_stop_committed_bug_is_existing(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    _git(tmp_path, "add", "q.sql")
    _git(tmp_path, "commit", "-qm", "bug")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    code, out = _run(_event(str(target), event="Stop"),
                     Args(), monkeypatch)
    assert (code, out) == (0, "")  # existing findings stay silent


def test_stop_multi_statement_attribution(tmp_path, monkeypatch):
    # Bug committed in statement 1 keeps (existing) after an unrelated
    # statement-2 edit; a worktree bug in statement 2 is (new).
    _git(tmp_path, "init", "-q")
    target = tmp_path / "q.sql"
    target.write_text("delete from sessions;\nselect 1;\n",
                      encoding="utf-8")
    _git(tmp_path, "add", "q.sql")
    _git(tmp_path, "commit", "-qm", "bug in stmt 1")
    target.write_text("delete from sessions;\n"
                      "select * from t where x = null;\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    text = _stop_text(target, monkeypatch)
    # the new statement-2 bug speaks; the existing one stays silent
    assert "- q.sql:2 [sql/null-equality-comparison]" in text
    assert "delete from sessions" not in text.lower()


def test_stop_worktree_bug_is_new(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "q.sql"
    target.write_text("SELECT a FROM t ORDER BY a LIMIT 1;\n",
                      encoding="utf-8")
    _git(tmp_path, "add", "q.sql")
    _git(tmp_path, "commit", "-qm", "clean")
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    text = _stop_text(target, monkeypatch)
    assert "- q.sql:1 [sql/unguarded-update-delete]" in text


def test_codex_stop_blocks_on_new_findings(tmp_path, monkeypatch):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(harness="codex"), monkeypatch)
    code, out = _run(_event(str(target), event="Stop"),
                     Args(harness="codex"), monkeypatch)
    assert code == 0
    body = json.loads(out)
    assert body["decision"] == "block"  # Codex Stop output shape
    assert "q.sql:1 [sql/unguarded-update-delete]" in body["reason"]


def test_codex_stop_silent_when_clean_or_baseline(tmp_path, monkeypatch):
    target = tmp_path / "ok.sql"
    target.write_text("SELECT a FROM t ORDER BY a LIMIT 1;\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(harness="codex"), monkeypatch)
    code, out = _run(_event(str(target), event="Stop"),
                     Args(harness="codex"), monkeypatch)
    assert (code, out) == (0, "")  # clean stop really stops


def test_codex_blocks_once_per_session(tmp_path, monkeypatch):
    # N4: an unchanged finding re-blocks never; a new one blocks with
    # the old mentioned in one line.
    first = tmp_path / "a.sql"
    first.write_text("DELETE FROM a;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(first)), Args(harness="codex"), monkeypatch)
    blocked = _stop_text(first, monkeypatch, harness="codex")
    assert blocked["decision"] == "block"
    again = _stop_text(first, monkeypatch, harness="codex")
    assert again is None  # same finding, second turn: silent
    third = _stop_text(first, monkeypatch, harness="codex")
    assert third is None
    second = tmp_path / "b.sql"
    second.write_text("DELETE FROM b;\n", encoding="utf-8")
    _run(_event(str(second)), Args(harness="codex"), monkeypatch)
    mixed = _stop_text(second, monkeypatch, harness="codex")
    assert mixed["decision"] == "block"
    assert "- b.sql:1 [sql/unguarded-update-delete]" in mixed["reason"]
    assert "a.sql" not in mixed["reason"]  # said before: silent


def test_codex_stop_silent_for_all_existing(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    _git(tmp_path, "add", "q.sql")
    _git(tmp_path, "commit", "-qm", "bug")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(harness="codex"), monkeypatch)
    code, out = _run(_event(str(target), event="Stop"),
                     Args(harness="codex"), monkeypatch)
    assert (code, out) == (0, "")


def test_stop_speaks_once_per_finding(tmp_path, monkeypatch):
    # Fix 1: at most one emission per finding. The first Stop speaks;
    # continuations and new turns with nothing new stay silent; a new
    # bug speaks again.
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    first = _stop_text(target, monkeypatch)
    assert first.startswith("[truequery@1] stop pass ·")
    assert "continuation" not in first.split("\n")[0]
    code, out = _run(json.dumps(
        {"hook_event_name": "Stop", "stop_hook_active": True}),
        Args(), monkeypatch)
    assert (code, out) == (0, "")  # seen already: silent
    code, out = _run(_event(str(target), event="Stop"),
                     Args(), monkeypatch)
    assert (code, out) == (0, "")  # new turn, nothing new: silent
    target.write_text("DELETE FROM t;\nSELECT * FROM a, b;\n",
                      encoding="utf-8")
    _run(_event(str(target)), Args(), monkeypatch)
    code, out = _run(_event(str(target), event="Stop"),
                     Args(), monkeypatch)
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "- q.sql:2 [sql/accidental-cross-join]" in body
    assert "unguarded-update-delete" not in body  # said before: silent


def test_unified_diff_command_paths(tmp_path, monkeypatch,
                                    immediate_unguarded):
    target = tmp_path / "d.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "Edit",
         "tool_input": {"command": "diff --git a/x.sql b/d.sql\n"
                                   "+++ b/d.sql\n@@\n+DELETE FROM t;"}})
    monkeypatch.chdir(tmp_path)
    code, out = _run(payload, Args(), monkeypatch)
    assert code == 0
    text = json.loads(out)["hookSpecificOutput"]["additionalContext"]
    assert "1 new finding(s) in d.sql" in text


def test_install_pins_absolute_tq(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    # Windows only finds executables with a PATHEXT extension.
    exe = bindir / ("truequery.exe" if sys.platform == "win32" else "truequery")
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.setattr(hook.sys, "argv", ["truequery"])
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args(harness="claude")) == 0
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(
        encoding="utf-8"))
    [group] = data["hooks"]["PostToolUse"]
    command = group["hooks"][0]["command"]
    assert command.lower().startswith(str(exe).lower())
    assert command.endswith(" hook --harness claude")


def test_codex_install_has_stop_entry(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args(harness="codex")) == 0
    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(
        encoding="utf-8"))
    assert any("hook --harness" in json.dumps(h)
               for h in data["hooks"]["Stop"])
    group = next(g for g in data["hooks"]["PostToolUse"]
                 if "hook --harness" in json.dumps(g))
    assert group["matcher"] == "Write|Edit"


def test_stop_passes_bounded_and_empty_silent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(tmp_path / "ghost.sql"), event="Stop"),
                     Args(), monkeypatch)
    assert (code, out) == (0, "")  # nothing touched: silent
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(_event(str(target)), Args(), monkeypatch)
    first = _stop_text(target, monkeypatch)
    assert first.startswith("[truequery@1] stop pass ·")
    for _ in range(5):
        # every later stop with nothing new is silent (Fix 1)
        code, out = _run(_event(str(target), event="Stop"),
                         Args(), monkeypatch)
        assert (code, out) == (0, "")


def test_silent_noops(monkeypatch):
    for payload in ["", "   ", "not json", "[1, 2]"]:
        code, out = _run(payload, Args(), monkeypatch)
        assert (code, out) == (0, "")


def test_missing_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event(str(tmp_path / "ghost.sql")), Args(),
                     monkeypatch)
    assert (code, out) == (0, "")


def test_nonsql_edit_is_noop(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    code, out = _run(_event("notes.txt"), Args(), monkeypatch)
    assert (code, out) == (0, "")


def test_uppercase_and_dbt_extensions_checked(tmp_path, monkeypatch,
                                              immediate_unguarded):
    upper = tmp_path / "Q.SQL"
    upper.write_text("DELETE FROM t;\n", encoding="utf-8")
    dbt = tmp_path / "model.dbt"
    dbt.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for target in (upper, dbt):
        code, out = _run(_event(str(target)), Args(), monkeypatch)
        assert code == 0 and out != "", f"silent on {target.name}"


def test_warn_only_never_blocks(tmp_path, monkeypatch,
                                immediate_unguarded):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code, _ = _run(_event(str(target)), Args(), monkeypatch)
    assert code == 0  # R1 hooks warn; nothing blocks


def test_install_merge_aware(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args(harness="claude")) == 0
    first = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(
        encoding="utf-8"))
    assert hook.install(Args(harness="claude")) == 0  # idempotent
    second = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(
        encoding="utf-8"))
    assert first == second
    text = json.dumps(second)
    assert "hook --harness" in text and '"Stop"' in text


@pytest.mark.parametrize("harness", ["claude", "codex", "muse"])
def test_uninstall_restores_the_users_config(tmp_path, monkeypatch, harness):
    """install then uninstall leaves the user's own config exactly as it
    was: other tools' hooks and settings kept, no command-less husks."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / hook.HARNESSES[harness][0][0]
    path.parent.mkdir(parents=True)
    original = {"hooks": {"PostToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "my-own-linter"}]}]},
        "model": "opus"}
    path.write_text(json.dumps(original), encoding="utf-8")
    assert hook.install(Args(harness=harness)) == 0
    assert hook.uninstall(Args(harness=harness)) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_uninstall_leaves_untouched_files_alone(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text('{"model": "opus",   "theme": "dark"}', encoding="utf-8")
    assert hook.uninstall(Args(harness="claude")) == 0
    assert path.read_text(encoding="utf-8") == (
        '{"model": "opus",   "theme": "dark"}')


def test_muse_posttooluse_path_envelope(tmp_path, monkeypatch,
                                        immediate_unguarded):
    # Muse edit_file carries tool_input.path (not file_path); the
    # PostToolUse envelope is hookSpecificOutput.additionalContext.
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    payload = json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "edit_file",
         "tool_input": {"path": str(target)}})
    monkeypatch.chdir(tmp_path)
    code, out = _run(payload, Args(), monkeypatch)  # auto-detect
    assert code == 0
    body = json.loads(out)["hookSpecificOutput"]
    assert body["hookEventName"] == "PostToolUse"
    assert "1 new finding(s) in q.sql" in body["additionalContext"]


def test_muse_autodetect_tool_names():
    assert hook.detect_harness({"tool_name": "edit_file"}) == "muse"
    assert hook.detect_harness({"tool_name": "write_file"}) == "muse"
    assert hook.detect_harness({"tool_name": "multi_edit"}) == "muse"
    assert hook.detect_harness({"tool_name": "apply_patch"}) == "codex"
    assert hook.detect_harness({"tool_name": "Edit"}) == "claude"


def test_muse_paths_from_patch_and_input():
    assert hook.extract_paths(
        {"tool_input": {"patch": "*** Update File: a.sql\n@@\n+x"}}) == [
            "a.sql"]
    assert hook.extract_paths(
        {"tool_input": {"input": "*** Add File: b.sql\n@@\n+x"}}) == [
            "b.sql"]
    assert hook.extract_paths(
        {"tool_input": {"path": "c.sql"}}) == ["c.sql"]
    # explicit file_path still wins over Muse path
    assert hook.extract_paths(
        {"tool_input": {"file_path": "d.sql", "path": "c.sql"}}) == [
            "d.sql"]


def test_muse_stop_blocks_once_like_codex(tmp_path, monkeypatch):
    target = tmp_path / "q.sql"
    target.write_text("DELETE FROM t;\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "write_file",
         "tool_input": {"path": str(target)}}),
        Args(harness="muse"), monkeypatch)
    code, out = _run(json.dumps(
        {"hook_event_name": "Stop", "tool_name": "write_file",
         "tool_input": {"path": str(target)}}),
        Args(harness="muse"), monkeypatch)
    assert code == 0
    body = json.loads(out)
    assert body["decision"] == "block"
    assert "q.sql:1 [sql/unguarded-update-delete]" in body["reason"]
    code, out = _run(json.dumps({"hook_event_name": "Stop"}),
                     Args(harness="muse"), monkeypatch)
    assert (code, out) == (0, "")  # same finding, second turn: silent


def test_muse_stop_silent_when_clean(tmp_path, monkeypatch):
    target = tmp_path / "ok.sql"
    target.write_text("SELECT a FROM t ORDER BY a LIMIT 1;\n",
                      encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    _run(json.dumps(
        {"hook_event_name": "PostToolUse", "tool_name": "edit_file",
         "tool_input": {"path": str(target)}}),
        Args(harness="muse"), monkeypatch)
    code, out = _run(json.dumps({"hook_event_name": "Stop"}),
                     Args(harness="muse"), monkeypatch)
    assert (code, out) == (0, "")


def test_muse_install_writes_hooks_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args(harness="muse")) == 0
    data = json.loads((tmp_path / ".muse" / "hooks.json").read_text(
        encoding="utf-8"))
    group = next(g for g in data["hooks"]["PostToolUse"]
                 if "hook --harness" in json.dumps(g))
    assert group["matcher"] == ("edit_file|write_file|"
                                "multi_edit|apply_patch")
    assert any("hook --harness" in json.dumps(h)
               for h in data["hooks"]["Stop"])
    assert hook.install(Args(harness="muse")) == 0  # idempotent
    again = json.loads((tmp_path / ".muse" / "hooks.json").read_text(
        encoding="utf-8"))
    assert again == data


def test_install_preserves_existing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / ".codex", exist_ok=True)
    (tmp_path / ".codex" / "hooks.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [{"command": "other-tool"}]}}),
        encoding="utf-8",
    )
    assert hook.install(Args(harness="codex")) == 0
    data = json.loads((tmp_path / ".codex" / "hooks.json").read_text(
        encoding="utf-8"))
    commands = [h.get("command", "")
                for h in data["hooks"]["PostToolUse"][-1]["hooks"]]
    assert any("hook --harness" in c for c in commands)


def test_both_entry_points_resolve_to_same_main():
    import tomllib
    with open(os.path.join(ROOT, "plugin", "engine", "pyproject.toml"), "rb") as handle:
        scripts = tomllib.load(handle)["project"]["scripts"]
    assert scripts["truequery"] == scripts["tq"] == "truequery.cli:main"
    from truequery.cli import main
    assert callable(main)


def test_status_reports_off_and_tq_install(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert hook.status(Args()) == 0
    out = capsys.readouterr().out
    assert "claude: off" in out and "codex: off" in out
    assert hook.install(Args(harness="claude")) == 0
    assert hook.status(Args(harness="claude")) == 0
    out = capsys.readouterr().out
    assert "claude: tq-install" in out


def test_install_refuses_plugin_double_install(tmp_path, monkeypatch,
                                               capsys):
    monkeypatch.chdir(tmp_path)
    os.makedirs(tmp_path / ".claude", exist_ok=True)
    (tmp_path / ".claude" / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"truequery@analytically": True}}),
        encoding="utf-8")
    assert hook.install(Args(harness="claude")) == 0
    out = capsys.readouterr().out
    assert "already active" in out and "second copy" in out
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_uninstall_removes_tq_entries_keeps_plugin(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args(harness="claude")) == 0
    assert hook.uninstall(Args(harness="claude")) == 0
    assert hook.status(Args(harness="claude")) == 0
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text(
        encoding="utf-8"))
    assert "hook --harness" not in json.dumps(data)


def test_hook_checks_files_when_its_cwd_is_elsewhere(tmp_path, monkeypatch,
                                                     immediate_unguarded):
    """The harness's payload cwd is the project root; the hook process
    may run anywhere. Paths must resolve against the payload's root."""
    project = tmp_path / "project"
    project.mkdir()
    bad = project / "bad.sql"
    bad.write_text("delete from t;\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    payload = json.dumps({"hook_event_name": "PostToolUse",
                          "tool_name": "Write", "cwd": str(project),
                          "tool_input": {"file_path": str(bad)}})
    code, out = _run(payload, Args(harness="claude"), monkeypatch)
    assert code == 0 and "sql/unguarded-update-delete" in out



def _payload(root, event, path=None, session_id="s1"):
    body = {"hook_event_name": event, "cwd": str(root),
            "session_id": session_id}
    if path is not None:
        body.update(tool_name="Write", tool_input={"file_path": str(path)})
    return json.dumps(body)


def test_stop_reports_unparsable_touched_file(tmp_path, monkeypatch):
    """A touched file that does not parse is never a silent Stop."""
    monkeypatch.chdir(tmp_path)
    broken = tmp_path / "broken.sql"
    broken.write_text("select a from t where x in (1, 2;\n", encoding="utf-8")
    _run(_payload(tmp_path, "PostToolUse", broken), Args(harness="claude"),
         monkeypatch)
    _, out = _run(_payload(tmp_path, "Stop"), Args(harness="claude"),
                  monkeypatch)
    assert "UNVERIFIED" in out and "broken.sql" in out
    _, again = _run(_payload(tmp_path, "Stop"), Args(harness="claude"),
                    monkeypatch)
    assert again == ""  # said once


def test_corrupt_session_file_does_not_silence_the_hook(tmp_path, monkeypatch,
                                                        immediate_unguarded):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".truequery").mkdir()
    (tmp_path / ".truequery" / "session.json").write_text(
        '{"touched": {}, "chain": "x", "reported": 7}', encoding="utf-8")
    bad = tmp_path / "d.sql"
    bad.write_text("delete from t;\n", encoding="utf-8")
    _, out = _run(_payload(tmp_path, "PostToolUse", bad, session_id=None),
                  Args(harness="claude"), monkeypatch)
    assert "sql/unguarded-update-delete" in out


def test_new_session_hears_findings_again(tmp_path, monkeypatch,
                                         immediate_unguarded):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "d.sql"
    bad.write_text("delete from t;\n", encoding="utf-8")
    args = Args(harness="claude")
    _, first = _run(_payload(tmp_path, "PostToolUse", bad, "s1"), args,
                    monkeypatch)
    _, same = _run(_payload(tmp_path, "PostToolUse", bad, "s1"), args,
                   monkeypatch)
    _, fresh = _run(_payload(tmp_path, "PostToolUse", bad, "s2"), args,
                    monkeypatch)
    assert "1 new finding" in first and "still open" in same
    assert "1 new finding" in fresh


def test_new_statement_same_rule_is_a_new_finding(tmp_path, monkeypatch,
                                                  immediate_unguarded):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "d.sql"
    bad.write_text("delete from t;\n", encoding="utf-8")
    args = Args(harness="claude")
    _run(_payload(tmp_path, "PostToolUse", bad), args, monkeypatch)
    bad.write_text("delete from t;\ndelete from u;\n", encoding="utf-8")
    _, out = _run(_payload(tmp_path, "PostToolUse", bad), args, monkeypatch)
    assert "d.sql:2" in out and "no WHERE" in out


def test_user_config_change_raises_the_notice(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    sql = tmp_path / "q.sql"
    sql.write_text("select 1;\n", encoding="utf-8")
    args = Args(harness="claude")
    _run(_payload(tmp_path, "PostToolUse", sql), args, monkeypatch)
    cfg = tmp_path / "xdg" / "truequery"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "rules:\n  sql/unguarded-update-delete: {enabled: false}\n",
        encoding="utf-8")
    _, out = _run(_payload(tmp_path, "PostToolUse", sql), args, monkeypatch)
    assert "rule settings changed" in out


def test_session_state_is_gitignored(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    sql = tmp_path / "q.sql"
    sql.write_text("select 1;\n", encoding="utf-8")
    _run(_payload(tmp_path, "PostToolUse", sql), Args(harness="claude"),
         monkeypatch)
    ignore = (tmp_path / ".truequery" / ".gitignore").read_text(encoding="utf-8")
    assert "session.json" in ignore and "ignores.json" not in ignore.split("\n")


@pytest.mark.parametrize("text", [
    '{"permissions": {"allow": ["Bash"]},}',  # invalid JSON
    '{"hooks": []}',                          # hooks not a mapping
    '{"hooks": {"PostToolUse": {"matcher": "x"}}}',  # event not a list
])
def test_install_refuses_configs_it_cannot_parse(tmp_path, monkeypatch, text):
    """Never rewrite (or move aside) a config we can't parse."""
    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir()
    path.write_text(text, encoding="utf-8")
    assert hook.install(Args(harness="codex")) == 2
    assert path.read_text(encoding="utf-8") == text
    assert sorted(p.name for p in path.parent.iterdir()) == ["hooks.json"]


def test_install_defaults_to_claude_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert hook.install(Args()) == 0
    assert (tmp_path / ".claude" / "settings.local.json").exists()
    assert not (tmp_path / ".codex").exists()
    assert not (tmp_path / ".muse").exists()
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_version_mismatch_is_said_once(tmp_path, monkeypatch, immediate_unguarded):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRUEQUERY_PLUGIN_VERSION", "9.9.9")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/plugins/truequery")
    sql = tmp_path / "q.sql"
    sql.write_text("select 1;\n", encoding="utf-8")
    args = Args(harness="claude")
    _, first = _run(_payload(tmp_path, "PostToolUse", sql), args, monkeypatch)
    _, second = _run(_payload(tmp_path, "PostToolUse", sql), args, monkeypatch)
    text = json.loads(first)["hookSpecificOutput"]["additionalContext"]
    assert "the plugin is 9.9.9" in text
    assert 'uv tool install --force "/plugins/truequery/engine"' in text
    assert "9.9.9" not in second


def test_matching_version_says_nothing(tmp_path, monkeypatch):
    from truequery import __version__
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TRUEQUERY_PLUGIN_VERSION", __version__)
    sql = tmp_path / "q.sql"
    sql.write_text("select 1;\n", encoding="utf-8")
    _, out = _run(_payload(tmp_path, "PostToolUse", sql), Args(harness="claude"),
                  monkeypatch)
    assert out == ""



@pytest.mark.parametrize("command,ours", [
    ("tq hook --harness claude", True),
    ("/usr/local/bin/truequery hook --harness codex", True),
    (r"C:\Users\a\Scripts\truequery.exe hook --harness claude", True),
    (r'"C:\Program Files\x\truequery.EXE" hook --harness claude', True),
    ('sh "${CLAUDE_PLUGIN_ROOT}/scripts/tq-hook.sh"', False),  # the plugin's
    ("my-own-linter", False),
    ("notq hook", False),
])
def test_recognizes_our_hook_commands_on_every_os(command, ours):
    assert hook._is_ours(command) is ours


def test_hook_command_quotes_paths_with_spaces(monkeypatch):
    exe = os.path.join(os.path.abspath(os.sep), "a b", "bin", "truequery")
    monkeypatch.setattr(hook.sys, "argv", [exe])
    assert hook._hook_command("claude") == f'"{exe}" hook --harness claude'
