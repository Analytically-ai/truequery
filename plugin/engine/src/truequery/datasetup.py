"""`truequery setup data`: install the data reader (DuckDB) the right way
for how TrueQuery itself was installed, and only with a yes.

Without `--yes` it prints the command and what it will download, and
changes nothing. With `--yes` it runs that command (pip or uv do the
download). The checker itself still makes no network calls.

How TrueQuery was installed decides the command:

| Installed as            | What `setup data --yes` does                      |
|-------------------------|---------------------------------------------------|
| the Claude Code plugin  | writes a user-level marker; the plugin's          |
|                         | `truequery` launcher then adds DuckDB to its      |
|                         | `uv run` (hooks don't use the launcher)           |
| `uv tool install`       | `uv tool install <same source> --with duckdb…`    |
| `pipx install`          | `pipx inject truequery duckdb…`                   |
| `uvx` (no install)      | nothing to install: prints the `uvx --with` form  |
| pip, in a virtualenv    | `<that python> -m pip install duckdb…` (or        |
|                         | `uv pip install --python <that python>` when the  |
|                         | environment has no pip)                           |
| pip, outside a venv     | the same, for that Python                         |

The project's answer is kept in DATA.md (`setup: data: true|false`), so
nobody is asked twice.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from typing import Any

import yaml

from truequery import project as P
from truequery import verify as V

SPEC = V.DUCKDB_SPEC
DOWNLOAD = ("DuckDB from PyPI (pypi.org / files.pythonhosted.org): about "
            "15–25 MB to download, about 50 MB installed")
MARKER_NAME = "data-reader"
LAUNCHER_ENV = "TRUEQUERY_LAUNCHER"
DATA_SUFFIXES = (".csv", ".tsv", ".parquet", ".jsonl", ".ndjson", ".json",
                 ".duckdb")
SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
SKIP_DIRS = {".git", ".hg", ".svn", ".venv", "venv", "node_modules",
             "__pycache__", ".truequery", "target", "dbt_packages",
             ".tox", ".mypy_cache", ".pytest_cache", "site-packages"}
SCAN_LIMIT = 5000  # directory entries looked at, at most, to stay fast


# ---------------------------------------------------------------------------
# Where things are
# ---------------------------------------------------------------------------


def config_dir(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "truequery")


def marker_path(env: dict | None = None) -> str:
    """The plugin launcher adds DuckDB when this file exists."""
    return os.path.join(config_dir(env), MARKER_NAME)


def detect_install(env: dict | None = None, prefix: str | None = None,
                   base_prefix: str | None = None) -> dict[str, Any]:
    """How this TrueQuery was installed: `plugin`, `uv-tool`, `pipx`,
    `uvx`, `venv` or `pip`."""
    env = os.environ if env is None else env
    prefix = sys.prefix if prefix is None else prefix
    base_prefix = sys.base_prefix if base_prefix is None else base_prefix
    if env.get(LAUNCHER_ENV) == "plugin":
        return {"kind": "plugin", "where": "the Claude Code plugin"}
    if os.path.isfile(os.path.join(prefix, "uv-receipt.toml")):
        return {"kind": "uv-tool", "where": "a uv tool",
                "receipt": os.path.join(prefix, "uv-receipt.toml")}
    if os.path.isfile(os.path.join(prefix, "pipx_metadata.json")):
        return {"kind": "pipx", "where": "a pipx app"}
    # uv's throwaway environments (uvx, `uv run --with`) live in its cache,
    # under archive-v0/ or builds-v0/. (CACHEDIR.TAG alone says nothing:
    # `uv venv` writes one into every virtualenv it makes.)
    if _in_uv_cache(prefix):
        return {"kind": "uvx", "where": "uvx (a fresh environment each run)"}
    if prefix != base_prefix:
        return {"kind": "venv", "where": f"the virtualenv {prefix}"}
    return {"kind": "pip", "where": f"the Python at {prefix}"}


def _in_uv_cache(prefix: str) -> bool:
    parts = os.path.normpath(prefix).replace("\\", "/").split("/")
    return any(re.fullmatch(r"(archive|builds)-v\d+", p) for p in parts)


def _uv_tool_source(receipt: str) -> list[str] | None:
    """The `uv tool install` arguments that reinstall TrueQuery from the
    same source it came from, keeping its other `--with` packages. None
    when the receipt has a shape we don't reproduce (then we fall back)."""
    try:
        import tomllib
        with open(receipt, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    reqs = (data.get("tool") or {}).get("requirements") or []
    main, others = None, []
    for req in reqs:
        if not isinstance(req, dict) or not req.get("name"):
            return None
        if str(req["name"]).lower() == "truequery" and main is None:
            main = req
        else:
            others.append(req)
    if main is None:
        return None

    def spec_of(req: dict) -> str | None:
        name = str(req["name"])
        extras = req.get("extras") or []
        if extras:
            name += "[" + ",".join(str(e) for e in extras) + "]"
        if set(req) - {"name", "extras", "specifier", "directory",
                       "editable", "marker"}:
            return None
        if req.get("directory"):
            return str(req["directory"])
        if req.get("editable"):
            return str(req["editable"])
        return name + str(req.get("specifier") or "")

    head = spec_of(main)
    if head is None:
        return None
    args = (["--editable", head] if main.get("editable") else [head])
    for req in others:
        if str(req["name"]).lower() == "duckdb":
            continue  # replaced by our own spec below
        spec = spec_of(req)
        if spec is None:
            return None
        args += ["--with", spec]
    return args + ["--with", SPEC]


def plan(install: dict, env: dict | None = None) -> dict[str, Any]:
    """What `setup data --yes` would run (`command`), or write (`marker`),
    and what to tell the user."""
    kind = install["kind"]
    py = sys.executable or "python3"
    if kind == "plugin":
        return {"kind": kind, "command": None, "marker": marker_path(env),
                "say": (f"setup data will record the choice in {marker_path(env)}. From then on "
                        f"the plugin's `truequery` command adds \"{SPEC}\" to "
                        "the environment uv runs it in, and uv downloads it "
                        "once into its cache. Hooks never load it.")}
    if kind == "uvx":
        return {"kind": kind, "command": None, "marker": None,
                "say": ("Nothing to install: uvx makes a fresh environment "
                        "each run. Run TrueQuery with the reader instead: "
                        f"uvx --with \"{SPEC}\" truequery verify")}
    if kind == "uv-tool":
        args = _uv_tool_source(install.get("receipt", ""))
        if args is not None:
            return {"kind": kind, "marker": None,
                    "command": ["uv", "tool", "install", *args]}
        return {"kind": kind, "marker": None,
                "command": ["uv", "pip", "install", "--python", py, SPEC],
                "note": ("TrueQuery's uv tool came from a source this command "
                         "can't repeat, so DuckDB goes straight into its "
                         "environment; `uv tool install --reinstall` would "
                         "drop it.")}
    if kind == "pipx":
        return {"kind": kind, "marker": None,
                "command": ["pipx", "inject", "truequery", SPEC]}
    if _has_pip():
        cmd = [py, "-m", "pip", "install", SPEC]
    elif shutil.which("uv"):
        cmd = ["uv", "pip", "install", "--python", py, SPEC]
    else:
        cmd = [py, "-m", "pip", "install", SPEC]
    return {"kind": kind, "marker": None, "command": cmd}


def _has_pip() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("pip") is not None
    except (ImportError, ValueError):
        return False


def _shell(cmd: list[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(cmd)
    return " ".join(shlex.quote(c) for c in cmd)


# ---------------------------------------------------------------------------
# DATA.md: the project's answer
# ---------------------------------------------------------------------------


def recorded_answer(root: str) -> bool | None:
    try:
        config = P.load_config(root) or {}
    except P.ConfigError:
        return None
    value = (config.get("setup") or {}).get("data")
    return value if isinstance(value, bool) else None


UNSAFE = ""  # record_answer: the block is there but couldn't be edited safely


def record_answer(root: str, wanted: bool) -> str | None:
    """Write `setup: data: <wanted>` into DATA.md's tq-config. Returns the
    path; None when there's no DATA.md with a tq-config block; UNSAFE when
    the block can't be changed without touching other lines.

    DATA.md is the team's file: only the `data:` line under `setup:` is
    written (added if missing). Every other line, comment and spelling
    stays byte for byte."""
    path = os.path.join(root, "DATA.md")
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        return None
    crlf = "\r\n" in text  # kept as the file has it
    if crlf:
        text = text.replace("\r\n", "\n")
    match = P._TQ_FENCE.search(text)
    if not match:
        return None
    body = match.group(1)
    try:
        config = yaml.safe_load(body)
    except yaml.YAMLError:
        return UNSAFE
    if config is None:
        config = {}
    if not isinstance(config, dict):
        return UNSAFE
    block = config.get("setup")
    if isinstance(block, dict) and block.get("data") is wanted:
        return path
    new_body = _set_setup_data(body, "true" if wanted else "false")
    # Write only if the edit means exactly what was asked, nothing else.
    expected = dict(config)
    expected["setup"] = {**(block if isinstance(block, dict) else {}),
                         "data": wanted}
    try:
        ok = new_body is not None and yaml.safe_load(new_body) == expected
    except yaml.YAMLError:
        ok = False
    if not ok:
        return UNSAFE
    out = text[:match.start(1)] + new_body + text[match.end(1):]
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(out.replace("\n", "\r\n") if crlf else out)
    return path


_TOP_SETUP = re.compile(r"^setup[ \t]*:(?P<rest>[^\n]*)$", re.MULTILINE)
_DATA_LINE = re.compile(
    r"^(?P<indent>[ \t]+)data[ \t]*:[ \t]*(?P<value>[^#\n]*?)"
    r"(?P<tail>[ \t]+#[^\n]*)?$")


def _set_setup_data(body: str, value: str) -> str | None:
    """`body` with `data: <value>` under the top-level `setup:` key, every
    other line kept. None when `setup:` is written in a form this doesn't
    edit (a flow mapping such as `setup: {data: true}`)."""
    found = _TOP_SETUP.search(body)
    if not found:
        sep = "" if not body or body.endswith("\n") else "\n"
        return f"{body}{sep}setup:\n  data: {value}"
    rest = found.group("rest").split("#", 1)[0].strip()
    if rest not in ("", "null", "~"):
        return None
    lines = body.split("\n")
    head = body[:found.start()].count("\n")
    child_indent = None
    for i in range(head + 1, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in " \t":
            break  # the next top-level key
        indent = line[:len(line) - len(line.lstrip())]
        if child_indent is None:
            child_indent = indent
        hit = _DATA_LINE.match(line) if indent == child_indent else None
        if hit:
            lines[i] = (f"{hit.group('indent')}data: {value}"
                        f"{hit.group('tail') or ''}")
            return "\n".join(lines)
    if rest:  # `setup: null` becomes a mapping
        lines[head] = "setup:"
    lines.insert(head + 1, f"{child_indent or '  '}data: {value}")
    return "\n".join(lines)


def _say_recorded(root: str, where: str | None, wanted: bool,
                  extra: str = "") -> None:
    word = "true" if wanted else "false"
    if where:
        print(f"Recorded in {P._rel(where, root)} (setup: data: {word}){extra}")
    elif where == UNSAFE:
        print("DATA.md's tq-config block couldn't be changed without touching "
              "other lines, so nothing was recorded. To keep the answer, add "
              f"`setup:` with `data: {word}` under it yourself.")
    else:
        print("No DATA.md with a tq-config block here, so nothing was "
              "recorded (run `truequery init` first to keep the answer).")


# ---------------------------------------------------------------------------
# What this project needs
# ---------------------------------------------------------------------------


def project_needs(root: str) -> dict[str, Any]:
    """Which contracts need the reader, and what data files are around.
    Reads contracts and folder names only, never the data."""
    try:
        contracts, _ = P.load_contracts_reporting(root)
    except P.ConfigError:
        contracts = {}
    duck, sqlite = [], []
    for c in contracts.values():
        if not V.has_local_data(c):
            continue
        name = str(c.get("dataset") or c.get("table"))
        (duck if V.needs_duckdb(c) else sqlite).append(name)
    files, sqlite_files = _scan_data_files(root)
    return {"contracts": sorted(duck), "sqlite_contracts": sorted(sqlite),
            "data_files": files, "sqlite_files": sqlite_files}


def _scan_data_files(root: str) -> tuple[list[str], list[str]]:
    data, sqlite = [], []
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames
                             if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(filenames):
            seen += 1
            if seen > SCAN_LIMIT:
                return data, sqlite
            low = name.lower()
            rel = P._rel(os.path.join(dirpath, name), root)
            if low.endswith(DATA_SUFFIXES):
                data.append(rel)
            elif low.endswith(SQLITE_SUFFIXES):
                sqlite.append(rel)
    return data, sqlite


def status_lines(root: str, env: dict | None = None) -> list[str]:
    """The "Data reader" part of `truequery status`. Read-only."""
    reader = V.duckdb_status()
    needs = project_needs(root)
    answer = recorded_answer(root)
    install = detect_install(env)
    out = []
    if reader["installed"]:
        out.append(f"installed (DuckDB {reader['version']})")
    elif reader["too_old"]:
        out.append(f"too old (DuckDB {reader['version']}; needs {SPEC})")
    else:
        out.append("not installed")
    if needs["contracts"]:
        out.append(f"needed here: {len(needs['contracts'])} contract(s) read "
                   f"data files ({', '.join(needs['contracts'][:5])}"
                   f"{' …' if len(needs['contracts']) > 5 else ''})")
    elif needs["data_files"]:
        out.append(f"useful here: {len(needs['data_files'])} data file(s) "
                   f"(e.g. {needs['data_files'][0]}), but no contract points "
                   "at them yet")
    else:
        out.append("not needed here: no data files"
                   + (", and SQLite needs nothing extra"
                      if needs["sqlite_contracts"] or needs["sqlite_files"]
                      else ""))
    if answer is True:
        out.append("DATA.md: the project wants it (setup: data: true)")
    elif answer is False:
        out.append("DATA.md: the project said no (setup: data: false)")
    if install["kind"] == "plugin":
        marker = marker_path(env)
        out.append(f"plugin launcher: {'adds' if os.path.exists(marker) else 'does not add'} "
                   f"DuckDB ({marker} {'exists' if os.path.exists(marker) else 'absent'})")
    if not reader["installed"] and (needs["contracts"] or needs["data_files"]) \
            and answer is not False:
        out.append("to install: truequery setup data")
    return out


# ---------------------------------------------------------------------------
# The command
# ---------------------------------------------------------------------------


def run_setup(args, root: str, env: dict | None = None,
              runner=subprocess.run) -> int:
    """`truequery setup data [--yes | --no]`."""
    if args.no:
        where = record_answer(root, False)
        print("Not installing the data reader.")
        _say_recorded(root, where, False,
                      ", so this project isn't asked again. Change it there, "
                      "or run `truequery setup data --yes`.")
        return 0

    install = detect_install(env)
    step = plan(install, env)
    reader = V.duckdb_status()
    print(f"TrueQuery is installed as {install['where']}.")
    ready = reader["installed"] and (
        install["kind"] != "plugin" or os.path.exists(marker_path(env)))
    if ready:
        print(f"The data reader is already installed (DuckDB "
              f"{reader['version']}). Nothing to do.")
        if args.yes:
            _record(root, True)
        return 0

    if step["command"] is None:
        print(step["say"])
    else:
        print(f"setup data will run: {_shell(step['command'])}")
        print(f"It downloads {DOWNLOAD}, once.")
        if step.get("note"):
            print(f"Note: {step['note']}")
    if install["kind"] == "plugin":
        print(f"The first `truequery verify` after this downloads {DOWNLOAD}, "
              "once, through uv.")

    if not args.yes:
        print("Nothing changed. Run `truequery setup data --yes` to do it, or "
              "`truequery setup data --no` to record that this project "
              "doesn't want it.")
        return 0

    if install["kind"] == "uvx":
        _record(root, True)
        return 0
    if install["kind"] == "plugin":
        marker = step["marker"]
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(f"# Written by `truequery setup data --yes`. While "
                         f"this file exists, the TrueQuery plugin's\n# "
                         f"`truequery` command runs with \"{SPEC}\". Delete "
                         "it to stop.\n" + SPEC + "\n")
        print(f"Wrote {marker}.")
        _record(root, True)
        return 0

    exe = step["command"][0]
    if os.path.basename(exe) in ("uv", "pipx") and not shutil.which(exe):
        print(f"truequery: `{exe}` isn't on PATH, so nothing was installed. "
              f"Run the command above where {exe} is available.",
              file=sys.stderr)
        return 2
    sys.stdout.flush()  # our lines first, then the installer's own output
    try:
        done = runner(step["command"], check=False)
    except OSError as exc:
        print(f"truequery: couldn't run the installer: {exc}", file=sys.stderr)
        return 2
    if done.returncode != 0:
        print(f"truequery: the installer failed (exit {done.returncode}); "
              "nothing was recorded.", file=sys.stderr)
        return 2
    print("Installed. `truequery verify` can now read data files and "
          ".duckdb databases.")
    _record(root, True)
    return 0


def _record(root: str, wanted: bool) -> None:
    where = record_answer(root, wanted)
    if where is not None:  # no DATA.md: nothing to say after an install
        _say_recorded(root, where, wanted, ".")
