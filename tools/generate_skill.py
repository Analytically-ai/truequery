"""Generate skill/floor.md + skill/reference/<domain>.md from standard/.

Floor = the non-negotiables: implemented detector records, grouped by
domain. References = every record in the domain, taught or checked.
Never hand-copy records: these files are build output (CI regenerates
and diffs). Run: .venv/bin/python tools/generate_skill.py [--check]
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src"))

from truequery import schema

ROOT = os.path.join(os.path.dirname(__file__), "..")
SKILL = os.path.join(ROOT, "plugin", "skills", "truequery")
REF = os.path.join(SKILL, "reference")


def floor_md(records: dict) -> str:
    enforced = sorted(
        (r for r in records.values() if r.implemented),
        key=lambda r: r.id)
    lines = ["# TrueQuery floor — non-negotiables",
             "",
             "The skill teaches everything below; the checker proves",
             "violations. Never claim more than the registry says: only",
             "the checked section runs in `truequery` today.",
             ""]
    lines += ["## Checked by `truequery` (enabled rules)", ""]
    for rec in enforced:
        if not rec.enabled:
            continue
        sev = rec.severity.get("build", "?")
        lines.append(f"- `{rec.id}` [{sev}] {rec.raw['statement'].strip()}")
    lines += ["", "## Taught, not checked yet (disabled rules)", ""]
    for rec in enforced:
        if rec.enabled:
            continue
        sev = rec.severity.get("build", "?")
        lines.append(f"- `{rec.id}` [{sev}] {rec.raw['statement'].strip()} "
                     f"(off by default: not yet precise enough to run unasked)")
    lines.append("")
    return "\n".join(lines)


def _one_line(text: str, limit: int = 220) -> str:
    first = (text or "").strip().split("\n")[0].strip()
    return first if len(first) <= limit else first[:limit] + "…"


def reference_md(domain: str, records: dict) -> str:
    mine = sorted((r for r in records.values() if r.domain == domain),
                  key=lambda r: r.id)
    lines = [f"# {domain} — TrueQuery practices",
             "",
             "Self-contained: everything below comes from the standard "
             "records, no other file needed.",
             ""]
    for rec in mine:
        modes = ",".join(rec.modes)
        checked = ("yes" if rec.implemented and rec.enabled
                   else "no (taught, not checked)")
        lines.append(f"## {rec.id}")
        lines.append("")
        lines.append(f"*{rec.title}*")
        lines.append("")
        lines.append(rec.raw["statement"].strip())
        lines.append("")
        rationale = _one_line(rec.raw.get("rationale", ""))
        if rationale:
            lines.append(f"Why: {rationale}")
            lines.append("")
        examples = rec.raw.get("examples") or {}
        if examples.get("bad"):
            lines.append("```sql")
            lines.append("-- bad")
            lines.append(examples["bad"].strip())
            lines.append("```")
        if examples.get("good"):
            lines.append("```sql")
            lines.append("-- good")
            lines.append(examples["good"].strip())
            lines.append("```")
        if examples.get("bad") or examples.get("good"):
            lines.append("")
        lines.append(f"- checked by truequery: {checked}")
        lines.append(f"- enforcement: {rec.enforcement}")
        lines.append(f"- modes: {modes}")
        sev = ", ".join(f"{m}={s}" for m, s in sorted(rec.severity.items()))
        lines.append(f"- severity: {sev}")
        sources = rec.raw.get("sources") or []
        titles = [s.get("title") for s in sources
                  if isinstance(s, dict) and s.get("title")]
        if titles:
            lines.append(f"- sources: {'; '.join(titles)}")
        if rec.proof:
            lines.append(f"- proof bad: `{rec.proof.bad}` → "
                         f"{rec.proof.bad_result}")
            lines.append(f"- proof good: `{rec.proof.good}` → "
                         f"{rec.proof.good_result}")
        lines.append("")
    return "\n".join(lines)


def build() -> dict[str, str]:
    records = schema.load_all()
    out = {os.path.join(SKILL, "floor.md"): floor_md(records)}
    for domain in schema.DOMAINS:
        out[os.path.join(REF, f"{domain}.md")] = reference_md(
            domain, records)
    return out


def main() -> int:
    check = "--check" in sys.argv
    files = build()
    failed = False
    for path, content in sorted(files.items()):
        if check:
            try:
                with open(path, encoding="utf-8") as handle:
                    current = handle.read()
            except OSError:
                current = None
            if current != content:
                print(f"skill stale: {path}")
                failed = True
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(content)
            print(f"wrote {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
