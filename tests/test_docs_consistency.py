"""The docs and messages stay honest: counts match the registry, finding
text and hook messages keep the house rules, relative links resolve.

These read the real files and the real registry, stay offline, and name
the file, the line and the expected value when they fail. The version
pins in snippets are guarded in test_precommit_and_action.py.
"""
import functools
import glob
import io
import json
import os
import re
import tempfile
from urllib.parse import unquote

import pytest

import truequery.hook as hook
from truequery import engine as E
from truequery import schema, std_registry

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# A standalone `tq`, the short alias user-facing text must not use. The
# `tq-config` block in DATA.md is a real name, not the alias.
ALIAS = re.compile(r"(?<![\w-])tq(?![\w-])")


def _rel_paths(*patterns):
    found = set()
    for pattern in patterns:
        found.update(glob.glob(os.path.join(ROOT, pattern), recursive=True))
    return sorted(os.path.relpath(p, ROOT).replace(os.sep, "/") for p in found)


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as handle:
        return handle.read()


def _line_of(text, offset):
    return text.count("\n", 0, offset) + 1


# -- 1. check counts ----------------------------------------------------------

_UNITS = ("one two three four five six seven eight nine ten eleven twelve "
          "thirteen fourteen fifteen sixteen seventeen eighteen nineteen"
          ).split()
_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_WORDS = {word: i + 1 for i, word in enumerate(_UNITS)}
_WORDS.update({word: (i + 2) * 10 for i, word in enumerate(_TENS)})
NUM = (r"(?<![\w.,-])(\d+|(?:" + "|".join(_TENS) + r")(?:-(?:"
       + "|".join(_UNITS[:9]) + r"))?|" + "|".join(_UNITS) + r")(?![\w.-])")


def _value(token):
    token = token.lower()
    if token.isdigit():
        return int(token)
    return sum(_WORDS[part] for part in token.split("-"))


def _expected_counts():
    implemented = {slug: r for slug, r in std_registry.RECORDS.items()
                   if r["implemented"]}
    pandas = [r for slug, r in implemented.items() if slug.startswith("py/")]
    sql = [r for slug, r in implemented.items() if slug.startswith("sql/")]
    enabled = sum(r["enabled"] for r in implemented.values())
    return {
        "checks": len(implemented),
        "checks on by default": enabled,
        "checks off by default": len(implemented) - enabled,
        "pandas checks": len(pandas),
        "SQL checks": len(sql),
        # model/, dq/, ingest/, orch/: everything else.
        "dbt and orchestration checks":
            len(implemented) - len(sql) - len(pandas),
        "pandas checks on by default": sum(r["enabled"] for r in pandas),
    }


# Phrasings the docs use today, most specific first. Each group is a number
# and the fact it states. `only` limits a phrasing to one file, where the
# words alone don't say which checks are meant. Any other "<n> checks" or
# "<n> rules" falls to the last line, so a new phrasing can't dodge the test.
CLAIMS = (
    {"pattern": NUM + r"\s+of\s+(?:the\s+)?" + NUM + r"\s+checks\s+(?:are\s+)?"
                r"on\s+by\s+default(?:\s+and\s+the\s+other\s+" + NUM + r")?",
     "facts": ("checks on by default", "checks", "checks off by default")},
    # Separate lines, so an older form of the split can't dodge the test.
    {"pattern": NUM + r"\s+in\s+SQL\b", "facts": ("SQL checks",)},
    {"pattern": NUM + r"\s+in\s+dbt\s+and\s+orchestration\b",
     "facts": ("dbt and orchestration checks",)},
    {"pattern": NUM + r"\s+in\s+pandas\s+code\b",
     "facts": ("pandas checks",)},
    # The pandas bullet of the evidence page: "The 5 checks on by default".
    {"pattern": r"The\s+" + NUM + r"\s+checks\s+on\s+by\s+default\s+found",
     "facts": ("pandas checks on by default",), "only": "docs/evidence.md"},
    {"pattern": NUM + r"\s+pandas\s+checks\b", "facts": ("pandas checks",)},
    {"pattern": NUM + r"\s+kinds\s+of\s+it\b", "facts": ("checks",)},
    {"pattern": NUM + r"\s+(?:of\s+them\s+)?on\s+by\s+default\b",
     "facts": ("checks on by default",)},
    {"pattern": NUM + r"\*{0,2}(?:\s+[\w`-]+)?\s+(?:checks|rules)(?![\w-])",
     "facts": ("checks",)},
)

# Numbers that read like a count of our checks and aren't.
NOT_OUR_COUNT = {
    ("README.md", "five SQL checks"): "an agent's data checks, quoted",
    ("README.md", "three rules"): "the rules behind 6 false alarms",
    ("docs/evidence.md", "three rules"): "the rules behind 6 false alarms",
    ("plugin/skills/truequery/floor.md", "two separate checks"):
        "advice on writing data checks",
    ("plugin/skills/truequery/reference/sql.md", "two separate checks"):
        "advice on writing data checks",
    ("plugin/skills/truequery/reference/consume.md", "Ten Simple Rules"):
        "the title of a paper",
}

# The PyPI page is a user-facing doc too; it went stale first.
COUNTED_DOCS = ("README.md", "plugin/engine/README.md", "docs/*.md",
                "plugin/skills/**/*.md")


def _count_problems(rel, text, expected):
    """(problems, indexes of the CLAIMS that matched) for one document."""
    problems, used, taken = [], set(), []
    for index, claim in enumerate(CLAIMS):
        if claim.get("only", rel) != rel:
            continue
        for match in re.finditer(claim["pattern"], text, re.IGNORECASE):
            if any(match.start() < end and start < match.end()
                   for start, end in taken):
                continue
            taken.append(match.span())
            said = " ".join(match.group(0).split())
            if (rel, said) in NOT_OUR_COUNT:
                continue
            used.add(index)
            for group, fact in enumerate(claim["facts"], start=1):
                if match.group(group) is None:  # an optional part of it
                    continue
                if _value(match.group(group)) != expected[fact]:
                    problems.append(
                        f"{rel}:{_line_of(text, match.start(group))}: "
                        f"\"{said}\" says {match.group(group)}, but "
                        f"{fact} = {expected[fact]}")
    return problems, used


def test_check_counts_in_docs_match_the_registry():
    expected = _expected_counts()
    problems, used = [], set()
    for rel in _rel_paths(*COUNTED_DOCS):
        found, matched = _count_problems(rel, _read(rel), expected)
        problems += found
        used |= matched
    assert not problems, ("\n".join(problems) + "\nIf a number isn't a count of "
                          "our checks, add it to NOT_OUR_COUNT.")
    # A phrasing nothing uses any more is a guard guarding nothing.
    unused = [CLAIMS[i]["pattern"] for i in set(range(len(CLAIMS))) - used]
    assert not unused, f"no doc uses these phrasings; drop them: {unused}"


@pytest.mark.parametrize("stale", [
    "looks for 99 kinds of it",
    "All 99 checks",
    "Ninety-nine checks ship today",
    "(99 in SQL, 4 in dbt and orchestration, 7 in pandas code)",
    "(21 in SQL, 99 in dbt and orchestration, 7 in pandas code)",
    "(21 in SQL, 4 in dbt and orchestration, 99 in pandas\n  code)",
    "(25 in SQL, 7 in pandas code)",
    "99 of them on by default",
    "99 on by default",
    "the 99 pandas checks",
    "99 of the 32 checks are on by default and the other 12",
    "20 of the 99 checks are on by default and the other 12",
    "20 of the 32 checks are on by default and the\n  other 99",
    "99\n  SQL checks",
    "**99** checks",
    "99 `sql` checks",
    "99 of 32 checks on by default",
    "20 of the 99 checks are on by default",
])
def test_count_guard_catches_a_stale_number(stale):
    expected = _expected_counts()
    assert _count_problems("README.md", stale, expected)[0], stale


def test_count_guard_is_not_fooled_by_nearby_numbers():
    expected = _expected_counts()
    for fine in ("version 0.1.32 rules", "1,099 checks", "99 checkpoints",
                 "ran one check", "7 rules-of-thumb"):
        assert _count_problems("README.md", fine, expected)[0] == [], fine


# -- 2. finding text, for every rule ------------------------------------------

# What makes the rules with no proof fire: the inputs test_project_checks
# uses for the same rules.
PROJECT_INPUTS = {
    "models/incremental.sql":
        "{{ config(materialized='incremental', "
        "incremental_strategy='merge') }}\nSELECT a FROM t\n",
    "schema.yml":
        "models:\n  - name: orders\n    columns:\n"
        "      - name: order_id\n        tests: [unique]\n"
        "sources:\n  - name: raw\n    loaded_at_field: _loaded_at\n"
        "    tables:\n      - name: payments\n",
    "dag.py": "from airflow import DAG\ndag = DAG('x', start_date=None)\n",
}
# Rules that only speak in a permissive dialect.
DIALECT = {"sql/group-by-bare-column": "mysql"}
EXISTING_INPUTS = ("samples", "tests/fixtures", "examples/shop")


def _all_rules_on():
    return {slug: {"enabled": True, "tier": r["tier"],
                   "severity": r["severity"]}
            for slug, r in std_registry.RECORDS.items() if r["implemented"]}


def _write(folder, rel, text):
    path = os.path.join(folder, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@functools.lru_cache(maxsize=None)
def _findings_from_every_input():
    """Every finding from the proofs (as .sql and inside Python), the
    project inputs, the fixtures and the samples, with every rule on."""
    settings = _all_rules_on()
    findings = []
    with tempfile.TemporaryDirectory() as folder:
        plain = os.path.join(folder, "plain")
        by_dialect = {}
        for name, text in PROJECT_INPUTS.items():
            _write(plain, name, text)
        for slug, record in schema.load_all().items():
            proof = record.proof
            if not record.implemented or proof is None:
                continue
            name = slug.replace("/", "-")
            if proof.engine == "pandas":
                _write(plain, f"{name}.py",
                       f"import pandas as pd\n{proof.setup}\n{proof.bad}\n")
                continue
            target = plain
            if slug in DIALECT:
                target = by_dialect.setdefault(
                    DIALECT[slug], os.path.join(folder, DIALECT[slug]))
            _write(target, f"{name}.sql", proof.bad + "\n")
            _write(target, f"{name}_embedded.py",
                   f'import sqlite3\ncur = sqlite3.connect("x").cursor()\n'
                   f'cur.execute("""{proof.bad}""")\n')
        runs = [(plain, None)] + [(path, dialect)
                                  for dialect, path in by_dialect.items()]
        for path, dialect in runs:
            for result in E.run([path], path, dialect=dialect,
                                settings=settings, config={}):
                findings += result.findings
        existing = [os.path.join(ROOT, rel) for rel in EXISTING_INPUTS]
        for result in E.run(existing, ROOT, settings=settings, config={}):
            findings += result.findings
    return tuple(findings)


def test_every_implemented_rule_fires_from_the_test_inputs():
    fired = {finding.rule for finding in _findings_from_every_input()}
    missing = sorted(set(_all_rules_on()) - fired)
    assert not missing, (
        f"no test input makes these rules fire, so their text is unchecked: "
        f"{missing}. Add a proof, or an entry in PROJECT_INPUTS in {__file__}")


def test_finding_text_keeps_the_house_rules():
    problems = []
    for finding in _findings_from_every_input():
        where = f"{finding.rule} ({finding.file}:{finding.line})"
        for name in ("why", "fix"):
            text = getattr(finding, name)
            if finding.rule in text:
                problems.append(f"{where}: `{name}` repeats the rule id, "
                                f"which every report already prints: {text}")
            if ALIAS.search(text):
                problems.append(f"{where}: `{name}` says `tq`; expected "
                                f"`truequery`: {text}")
    assert not problems, "\n".join(sorted(set(problems)))


# -- 3. hook messages ---------------------------------------------------------

class _Args:
    event = "auto"
    log_file = None
    scope = "project"

    def __init__(self, harness):
        self.harness = harness


class _Session:
    """One agent session in its own folder, driven through hook.main."""

    def __init__(self, root, harness, monkeypatch):
        root.mkdir(parents=True)
        self.root, self.harness, self.monkeypatch = root, harness, monkeypatch

    def _fire(self, body):
        body.update(cwd=str(self.root), session_id="s1")
        self.monkeypatch.setattr(hook, "read_stdin",
                                 lambda timeout=5.0: json.dumps(body))
        out = io.StringIO()
        self.monkeypatch.setattr(hook.sys, "stdout", out)
        assert hook.main(_Args(self.harness)) == 0
        reply = out.getvalue()
        if not reply:
            return ""
        reply = json.loads(reply)
        return (reply["reason"] if "reason" in reply
                else reply["hookSpecificOutput"]["additionalContext"])

    def edit(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        tool = ({"tool_name": "write_file", "tool_input": {"path": str(path)}}
                if self.harness == "muse" else
                {"tool_name": "Write", "tool_input": {"file_path": str(path)}})
        return self._fire({"hook_event_name": "PostToolUse", **tool})

    def stop(self):
        return self._fire({"hook_event_name": "Stop"})


# An on-every-edit rule, and one held for the end of the turn.
EVERY_EDIT = "SELECT a FROM t WHERE b = NULL ORDER BY a;\n"
END_OF_TURN = "SELECT id FROM c WHERE id NOT IN (SELECT x FROM o);\n"
FINE = "SELECT a FROM t ORDER BY a LIMIT 1;\n"
BROKEN = "select a from t where x in (1, 2;\n"
PANDAS = "import pandas as pd\ndf = pd.read_csv('x')\ndf[df.a > 1]['b'] = 0\n"


def _every_hook_message(tmp_path, harness, monkeypatch):
    """{what was said: the text} for each message the hook can say."""
    said = {}

    def start(name):
        return _Session(tmp_path / name, harness, monkeypatch)

    edits = start("edits")
    said["edit: new finding"] = edits.edit("q.sql", EVERY_EDIT)
    said["edit: still open"] = edits.edit("q.sql", EVERY_EDIT)
    said["edit: fixed"] = edits.edit("q.sql", FINE)
    said["edit: new pandas finding"] = start("pandas").edit("p.py", PANDAS)
    said["edit: can't parse"] = start("broken").edit("b.sql", BROKEN)

    turn = start("turn")
    turn.edit("q.sql", END_OF_TURN)
    said["end of turn: new finding"] = turn.stop()

    broken = start("broken-turn")
    broken.edit("b.sql", FINE)
    (broken.root / "b.sql").write_text(BROKEN, encoding="utf-8")
    said["end of turn: can't parse"] = broken.stop()

    both = start("both")
    both.edit("q.sql", END_OF_TURN)
    both.edit("b.sql", BROKEN)
    said["end of turn: finding and can't parse"] = both.stop()

    monkeypatch.setenv("TRUEQUERY_PLUGIN_VERSION", "0.0.0")
    said["version mismatch"] = start("version").edit("q.sql", EVERY_EDIT)
    monkeypatch.delenv("TRUEQUERY_PLUGIN_VERSION")

    config = tmp_path / harness / "xdg" / "truequery"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config.parent))
    settings = start("settings")
    settings.edit("q.sql", FINE)
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        "rules:\n  sql/unguarded-update-delete: {enabled: false}\n",
        encoding="utf-8")
    said["settings changed"] = settings.edit("q.sql", FINE)
    (config / "config.yaml").unlink()

    bad_config = start("bad-config")
    (bad_config.root / "DATA.md").write_text(
        "# DATA.md\n\n```tq-config\nstack: [plain\n```\n", encoding="utf-8")
    said["broken config"] = bad_config.edit("q.sql", EVERY_EDIT)
    return said


@pytest.mark.parametrize("harness", ["claude", "muse"])
def test_hook_messages_keep_the_house_rules(tmp_path, harness, monkeypatch):
    said = _every_hook_message(tmp_path, harness, monkeypatch)
    problems = []
    for what, text in said.items():
        if not text:
            problems.append(f"{harness}, {what}: the hook said nothing, so "
                            "this message went unchecked")
        elif not text.startswith("[TrueQuery]"):
            problems.append(f"{harness}, {what}: expected it to start with "
                            f"[TrueQuery], got: {text}")
        elif ALIAS.search(text):
            problems.append(f"{harness}, {what}: says `tq`; expected "
                            f"`truequery`: {text}")
    assert not problems, "\n".join(problems)


def test_plugin_shim_messages_keep_the_house_rules():
    rel = "plugin/scripts/tq-hook.sh"
    text = _read(rel)
    messages = list(re.finditer(r'^\s*echo\s+"([^"]*)"', text, re.MULTILINE))
    assert messages, f"{rel}: found no echo lines to check"
    for match in messages:
        where = f"{rel}:{_line_of(text, match.start())}"
        assert match.group(1).startswith("[TrueQuery]"), (
            f"{where}: expected it to start with [TrueQuery]: {match.group(1)}")
        assert not ALIAS.search(match.group(1)), (
            f"{where}: says `tq`; expected `truequery`: {match.group(1)}")


# -- 4. links in the docs -----------------------------------------------------

LINKED_DOCS = ("README.md", "docs/**/*.md", "plugin/skills/**/*.md")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_INLINE_CODE = re.compile(r"(`+)(?:(?!\1).)+?\1")
_LINK_TARGETS = (
    re.compile(r"\]\(\s*<?([^)\s>]+)"),                  # [text](target)
    re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?([^\s>]+)", re.MULTILINE),  # [ref]:
    re.compile(r"""\b(?:href|src)=["']([^"']+)["']"""),  # <img src="...">
)
_EXTERNAL = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//|#)")


def _without_code(text):
    """text with code blanked out, so line numbers still hold."""
    lines, fence = [], None
    for line in text.split("\n"):
        opened = _FENCE.match(line)
        if fence is not None:
            if opened and opened.group(1).startswith(fence):
                fence = None
            lines.append("")
        elif opened:
            fence = opened.group(1)
            lines.append("")
        else:
            lines.append(_INLINE_CODE.sub("", line))
    return "\n".join(lines)


def _broken_links(rel, text):
    prose, broken = _without_code(text), []
    for pattern in _LINK_TARGETS:
        for match in pattern.finditer(prose):
            target = match.group(1)
            if _EXTERNAL.match(target):
                continue
            path = unquote(re.split(r"[#?]", target, maxsplit=1)[0])
            if path.startswith("/"):  # GitHub reads these from the repo root
                resolved = os.path.join(ROOT, path.lstrip("/"))
            else:
                resolved = os.path.join(ROOT, os.path.dirname(rel), path)
            if not os.path.exists(resolved):
                broken.append(
                    f"{rel}:{_line_of(prose, match.start(1))}: {target} "
                    f"doesn't exist; expected a file at "
                    f"{os.path.relpath(resolved, ROOT)}")
    return broken


def test_relative_links_in_the_docs_point_at_real_files():
    docs = _rel_paths(*LINKED_DOCS)
    assert "README.md" in docs and "docs/checker.md" in docs
    broken = [line for rel in docs for line in _broken_links(rel, _read(rel))]
    assert not broken, "\n".join(broken)


def test_link_guard_catches_every_link_form():
    text = ("[a](no-such-1.md)\n"
            "![a](no-such-2.png)\n"
            "[![a](README.md)](no-such-3.md)\n"
            "[a](no-such-4.md#anchor)\n"
            "[a]: no-such-5.md\n"
            '<img src="no-such-6.png">\n'
            '<a href="no-such-7.md">a</a>\n')
    found = "\n".join(_broken_links("README.md", text))
    for number in range(1, 8):
        assert f"no-such-{number}" in found, found


def test_link_guard_ignores_what_it_should():
    text = ("[a](README.md) [a](README.md#anchor) [a](#anchor)\n"
            "[a](docs/) [a](https://example.com/x.md) [a](mailto:a@b.c)\n"
            "`[a](no-such.md)` and ``[a](no-such.md)``\n"
            "```\n[a](no-such.md)\n```\n")
    assert _broken_links("README.md", text) == []
