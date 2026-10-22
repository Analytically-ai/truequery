#!/usr/bin/env python3
"""Generate src/truequery/std_registry.py from standard/ + registry_impl.json.

Deterministic (sorted keys); CI fails if the committed file differs.
Usage: python tools/generate_registry.py [--check]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src"))

from truequery import schema

OUT = os.path.join(os.path.dirname(__file__), "..", "plugin", "engine", "src", "truequery",
                   "std_registry.py")


def render(records):
    lines = [
        '"""Generated from standard/ — do not hand-edit. Run tools/generate_registry.py."""',
        "",
        "from truequery.schema import STANDARD_VERSION",
        "",
        "RECORDS = {",
    ]
    for slug in sorted(records):
        r = records[slug]
        lines.append(f'    "{slug}": {{')
        lines.append(f'        "title": {r.title!r},')
        lines.append(f'        "domain": {r.domain!r},')
        lines.append(f'        "modes": {list(r.modes)!r},')
        lines.append(f'        "enforcement": {r.enforcement!r},')
        lines.append(f'        "evidence": {r.evidence!r},')
        lines.append(f'        "severity": {r.severity!r},')
        lines.append(f'        "detector": {(slug if r.implemented else None)!r},')
        lines.append(f'        "tier": {r.tier!r},')
        lines.append(f'        "implemented": {r.implemented!r},')
        lines.append(f'        "enabled": {r.enabled!r},')
        lines.append(f'        "has_proof": {(r.proof is not None)!r},')
        lines.append(f'        "confidence": {r.confidence!r},')
        lines.append(f'        "status": {r.status!r},')
        lines.append('    },')
    lines.append("}")
    lines.append("")
    lines.append("IMPLEMENTED = sorted(s for s, r in RECORDS.items() if r['implemented'])")
    lines.append("ENABLED = sorted(s for s, r in RECORDS.items() if r['enabled'])")
    lines.append("")
    return "\n".join(lines) + "\n"


def main():
    records = schema.load_all()
    text = render(records)
    if "--check" in sys.argv:
        with open(OUT, encoding="utf-8") as handle:
            current = handle.read()
        if current != text:
            print("std_registry.py is stale — run tools/generate_registry.py")
            return 1
        print(f"registry fresh ({len(records)} records)")
        return 0
    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write(text)
    print(f"wrote {OUT} ({len(records)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
