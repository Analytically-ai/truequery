# Messy-request benchmark (2026-10-26)

Plain Claude Code against Claude Code with TrueQuery, on 10 vague requests
typed the way a busy or junior engineer would. Results and caveats are in
[docs/evidence.md](../../evidence.md); this folder holds everything needed to
check them.

| File | What it is |
|---|---|
| `rubric.md` | The answer key and scoring rules, written before any run |
| `answers.md` | Every answer from both arms, with the arm hidden (X/Y per task) |
| `key.json` | Which arm was X and which was Y, plus cost, turns and files written |
| `DATA-shop.md`, `DATA-bank.md` | The definitions the TrueQuery arm started from, written by one `/truequery init` session per project |
| `messy.py` | The runner |

## Setup

- **Model:** Claude Opus 5.5 in headless Claude Code (`claude -p`), one fresh
  session per task, with web search and web fetch blocked.
- **Same prompt text in both arms,** with no `/truequery` prefix: the skill had
  to pick the task up on its own.
- **Plain arm:** the project as shipped: data, docs and data dictionary.
- **TrueQuery arm:** the same, plus the plugin and the project's `DATA.md` and
  `contracts/`.
- **Projects:**
  - the demo shop in `examples/shop` (a DuckDB warehouse and a dbt project
    with written house definitions);
  - the `financial` database from the BIRD text-to-SQL benchmark (a Czech
    bank, CC BY-SA 4.0), with its data dictionary.
- **Grading:** each answer was scored correct, correct with the trap flagged,
  asked a sensible question, or silently wrong, with arm labels hidden.

## Reproduce

1. Build the four base folders under `~/tq-bench/base/`:
   - `shop-plain`: `examples/shop` after `./generate.sh`;
   - `shop-tq`: the same plus `DATA-shop.md` as `DATA.md`, and the contracts
     from a `/truequery init`;
   - `plain`: BIRD `financial` (the `.sqlite` file and `database_description/`);
   - `tq`: the same plus `DATA-bank.md` as `DATA.md`, and the contracts.
2. Run `python3 messy.py run --arm plain`, then `python3 messy.py run --arm tq`.
3. Run `python3 messy.py export`, then grade `answers.md` against `rubric.md`.

Each run is a live model session, so answers will differ in detail from run
to run.
