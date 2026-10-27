"""Messy-request benchmark: vague prompts, as a busy or junior engineer
types them, against data with real traps. Same prompt text in both arms,
no /truequery prefix (the skill must pick the task up on its own).

  arm plain : the project as shipped (data, docs, dictionary), no plugin
  arm tq    : the same plus the DATA.md/contracts from the owner's
              /truequery init, and the plugin from the repo

Each run: a fresh APFS clone, a headless `claude -p` session, no web tools.
Grading is by hand against the rubric below (rubric.md), with arm labels
hidden in answers.md.

Usage:  python3 messy.py run --arm plain|tq [--tasks S1,B3]
        python3 messy.py export        # answers.md (blinded) + key.json
"""
import argparse
import os
import json
import random
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN = HERE.parents[3] / "plugin"  # this repo's plugin
BENCH = Path.home() / "tq-bench"
BASE = BENCH / "base"
RUNS = BENCH / os.environ.get("MESSY_RUNS", "messy")  # another dir for re-runs
MODEL = "claude-opus-5-5"

TASKS = {
    "S1": ("shop", "how much money did we make last month?"),
    "S2": ("shop", "what's our churn rate?"),
    "S3": ("shop", "how many customers do we have?"),
    "S4": ("shop", "build a daily refunds table"),
    "S5": ("shop", "can you review the marts in dbt_repo before I merge"),
    "B1": ("bank", "average loan size by region?"),
    "B2": ("bank", "what % of clients get weekly statements, split by gender"),
    "B3": ("bank", "how many bad loans do we have"),
    "B4": ("bank", "which region has the most crime"),
    "B5": ("bank", "how much do we make from fees"),
}
BASES = {("shop", "plain"): "shop-plain", ("shop", "tq"): "shop-tq",
         ("bank", "plain"): "plain", ("bank", "tq"): "tq"}

ALLOWED = ["Read", "Write", "Edit", "Glob", "Grep", "Bash"]


def run(arm, task_ids):
    for tid in task_ids:
        kind, prompt = TASKS[tid]
        work = RUNS / arm / tid
        if (work / "result.json").exists():
            print(f"{arm} {tid}: done already, skipping")
            continue
        if work.exists():
            shutil.rmtree(work)
        work.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["cp", "-cR", str(BASE / BASES[(kind, arm)]), str(work)],
                       check=True)
        before = {p for p in work.rglob("*") if p.is_file()}
        cmd = ["claude", "-p", prompt, "--model", MODEL,
               "--output-format", "json", "--setting-sources", "project,local",
               "--disallowedTools", "WebSearch", "WebFetch",
               "--allowedTools", *ALLOWED]
        if arm == "tq":
            cmd += ["--plugin-dir", str(PLUGIN)]
        start = time.time()
        try:
            proc = subprocess.run(cmd, cwd=work, capture_output=True,
                                  text=True, timeout=2400)
            out = proc.stdout
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
        took = round(time.time() - start, 1)
        try:
            result = json.loads(out)
        except ValueError:
            result = {"error": str(out)[-3000:]}
        result["wall_seconds"] = took
        result["prompt"] = prompt
        result["new_files"] = sorted(
            str(p.relative_to(work)) for p in work.rglob("*")
            if p.is_file() and p not in before and ".truequery" not in p.parts)
        (work / "result.json").write_text(json.dumps(result, indent=1))
        print(f"{arm} {tid}: {took}s cost={result.get('total_cost_usd')} "
              f"turns={result.get('num_turns')} files={len(result['new_files'])}")


def export():
    """answers.md with arm labels hidden (X/Y per task, shuffled), key.json
    with the mapping and the numbers."""
    rng = random.Random(7)
    lines, key = ["# Messy-request answers (arms hidden)", ""], {}
    for tid, (kind, prompt) in TASKS.items():
        arms = [a for a in ("plain", "tq") if (RUNS / a / tid / "result.json").exists()]
        rng.shuffle(arms)
        lines += [f"## {tid}: \"{prompt}\"", ""]
        for label, arm in zip("XY", arms):
            res = json.loads((RUNS / arm / tid / "result.json").read_text())
            key[f"{tid}-{label}"] = {
                "arm": arm, "cost": res.get("total_cost_usd"),
                "turns": res.get("num_turns"), "seconds": res.get("wall_seconds"),
                "files": res.get("new_files")}
            lines += [f"### {label}", "", f"Files written: {res.get('new_files')}", "",
                      str(res.get("result", res.get("error", ""))), ""]
    (HERE / "answers.md").write_text("\n".join(lines), encoding="utf-8")
    (HERE / "key.json").write_text(json.dumps(key, indent=1))
    print("wrote answers.md and key.json")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["run", "export"])
    p.add_argument("--arm", choices=["tq", "plain"])
    p.add_argument("--tasks", default="")
    a = p.parse_args()
    if a.action == "run":
        run(a.arm, [t for t in a.tasks.split(",") if t] or list(TASKS))
    else:
        export()


if __name__ == "__main__":
    main()
