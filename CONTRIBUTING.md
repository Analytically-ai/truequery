# Contributing to TrueQuery

Please read our [Code of Conduct](CODE_OF_CONDUCT.md). Be kind, assume good
faith, and keep every space you'd want to ask a question in welcoming.

## Setup

```bash
git clone https://github.com/Analytically-ai/truequery.git && cd truequery
uv venv && uv pip install -e "plugin/engine[test]"
.venv/bin/python -m pytest
```

## Repository layout

```
.claude-plugin/      the marketplace manifest (points Claude Code at plugin/)
plugin/              the installable plugin: the only folder users get
  skills/truequery/    the skill: SKILL.md, flow.md, commands/, generated references
  hooks/, scripts/     hook config and the shim that runs the matching checker
  engine/              the checker (Python package)
tests/               the test suite
tools/               generators for the registry and the skill references
samples/, examples/  demo SQL and the demo shop
docs/                guides
.github/             CI: tests on Linux, macOS and Windows on every push
```

## Rules of the codebase

- The checker stays offline and deterministic: no network calls and no LLM
  in the check path, and the same output for the same input.
- Rule IDs are the standard's slugs (`sql/accidental-cross-join`).
- A file that wasn't checked is never reported as clean.
- Every bug fix comes with a test that fails without it.
- After editing anything under `standard/`, run
  `python tools/generate_registry.py` and `python tools/generate_skill.py`.
  CI fails if the generated files are stale.

Releases follow [`RELEASING.md`](RELEASING.md).
