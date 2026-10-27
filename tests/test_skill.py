"""M5 skill: generated files stay in sync with standard/; the entry
stays short; nothing is hand-copied."""

import os
import subprocess
import sys

from truequery import schema

SKILL = os.path.join(os.path.dirname(__file__), "..", "plugin", "skills", "truequery")


def test_generated_skill_is_fresh():
    proc = subprocess.run(
        [sys.executable, "tools/generate_skill.py", "--check"],
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout


def test_skill_md_short_and_standalone():
    with open(os.path.join(SKILL, "SKILL.md"), encoding="utf-8") as handle:
        lines = handle.readlines()
    assert len(lines) <= 120, len(lines)
    text = "".join(lines)
    assert "truequery context" in text  # standalone path, no hooks
    assert "tq install" not in text and "truequery install" not in text


def test_reference_files_have_teaching_content_and_size():
    import glob
    for path in glob.glob(os.path.join(SKILL, "reference", "*.md")):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        assert len(text.encode("utf-8")) < 65536, path  # loadable alone
        assert "Why:" in text and "checked by truequery:" in text, path
        assert "- sources:" in text, path
    with open(os.path.join(SKILL, "floor.md"), encoding="utf-8") as handle:
        floor = handle.read()
    assert "## Checked by `truequery` (enabled rules)" in floor
    assert "## Taught, not checked yet (disabled rules)" in floor
    assert "on every edit" not in floor  # N6: no overclaim


def test_every_record_taught_somewhere():
    records = schema.load_all()
    for domain in schema.DOMAINS:
        with open(os.path.join(SKILL, "reference", f"{domain}.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        for slug, rec in records.items():
            if rec.domain == domain:
                assert f"## {slug}" in text, slug
    with open(os.path.join(SKILL, "floor.md"), encoding="utf-8") as handle:
        floor = handle.read()
    for slug, rec in records.items():
        if rec.enforcement == "detector" and rec.implemented:
            assert f"`{slug}`" in floor, slug


def test_commands_full_and_drafts():
    full = ("init", "analyze", "review", "harden", "build", "ask",
            "metric", "hooks")
    aliases = ("model", "ingest", "orchestrate")
    drafts = ("dq", "change", "document", "optimize", "adapt")
    for name in full + aliases + drafts:
        with open(os.path.join(SKILL, "commands", f"{name}.md"),
                  encoding="utf-8") as handle:
            text = handle.read()
        if name in drafts:
            assert "DRAFT" in text, name
        elif name in aliases:
            assert "alias" in text, name
        else:
            assert "DRAFT" not in text, name
