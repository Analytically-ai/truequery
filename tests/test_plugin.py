"""Plugin layout gates: one source tree, no-registry install hint."""
import json
import os

ROOT = os.path.join(os.path.dirname(__file__), "..")
PLUGIN = os.path.join(ROOT, "plugin")


def test_shim_install_line_names_no_registry():
    with open(os.path.join(PLUGIN, "scripts", "tq-hook.sh"),
              encoding="utf-8") as handle:
        shim = handle.read()
    assert "uv tool install truequery" not in shim
    assert "pip install truequery" not in shim
    assert "CLAUDE_PLUGIN_ROOT" in shim


def test_marketplace_points_at_plugin_dir():
    with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json"),
              encoding="utf-8") as handle:
        market = json.load(handle)
    sources = [p.get("source") for p in market["plugins"]]
    assert sources == ["./plugin"], sources


def test_plugin_is_self_contained():
    # The plugin manager copies only plugin/, so everything the plugin
    # runs (manifest, skill, hooks, shim, engine) must live inside it.
    for rel in (".claude-plugin/plugin.json", "skills/truequery/SKILL.md",
                "hooks/hooks.json", "scripts/tq-hook.sh", "bin/truequery",
                "engine/pyproject.toml", "engine/src/truequery/cli.py"):
        assert os.path.isfile(os.path.join(PLUGIN, rel)), rel


def test_versions_agree():
    import tomllib
    from truequery import __version__
    with open(os.path.join(PLUGIN, "engine", "pyproject.toml"), "rb") as handle:
        project = tomllib.load(handle)["project"]
    with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"),
              encoding="utf-8") as handle:
        manifest = json.load(handle)
    assert project["version"] == __version__ == manifest["version"]


def test_shim_ignores_a_foreign_tq_and_never_fails(tmp_path):
    """Another tool's `tq` on PATH must never run as our hook, and the
    shim exits 0 whatever happens (exit 2 would block a Stop)."""
    import shutil
    import subprocess
    import sys
    if sys.platform == "win32" or shutil.which("sh") is None:
        return
    fake = tmp_path / "tq"
    fake.write_text("#!/bin/sh\necho foreign; exit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    proc = subprocess.run(
        ["sh", os.path.join(PLUGIN, "scripts", "tq-hook.sh")],
        input="{}", capture_output=True, text=True, timeout=30,
        env={"PATH": f"{tmp_path}:/usr/bin:/bin",
             "CLAUDE_PLUGIN_ROOT": PLUGIN})
    assert proc.returncode == 0
    assert "foreign" not in proc.stdout
    assert "docs.astral.sh/uv" in proc.stdout



def test_license_ships_with_the_plugin_and_the_engine():
    # The marketplace copies only plugin/, and wheels only plugin/engine/.
    for name in ("LICENSE", "NOTICE"):
        with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
            original = handle.read()
        for where in (PLUGIN, os.path.join(PLUGIN, "engine")):
            with open(os.path.join(where, name), encoding="utf-8") as handle:
                assert handle.read() == original, (where, name)


def _shim(tmp_path, tools):
    """Run the shim with only the given fake tools on PATH. Each fake
    records how it was called and what version the shim expected."""
    import subprocess
    import sys
    if sys.platform == "win32":
        return None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in tools.items():
        exe = bindir / name
        exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        exe.chmod(0o755)
    proc = subprocess.run(
        ["sh", os.path.join(PLUGIN, "scripts", "tq-hook.sh")],
        input='{"hook_event_name": "Stop"}', capture_output=True, text=True,
        timeout=30, env={"PATH": f"{bindir}:/usr/bin:/bin",
                         "CLAUDE_PLUGIN_ROOT": PLUGIN})
    assert proc.returncode == 0
    return proc.stdout


def _plugin_version():
    with open(os.path.join(PLUGIN, ".claude-plugin", "plugin.json"),
              encoding="utf-8") as handle:
        return json.load(handle)["version"]


def _fake_truequery(version):
    return (f'[ "$1" = --version ] && {{ echo "truequery {version}"; exit 0; }}\n'
            'echo "PATH-ENGINE wants=$TRUEQUERY_PLUGIN_VERSION input=$(cat)"\n')


FAKE_UVX = ('for a; do [ "$a" = --version ] && { echo "truequery x"; exit 0; }; done\n'
            'echo "UVX $* input=$(cat)"\n')


def test_shim_uses_a_matching_engine_on_path(tmp_path):
    out = _shim(tmp_path, {"truequery": _fake_truequery(_plugin_version()),
                           "uvx": FAKE_UVX})
    if out is not None:
        assert out.startswith("PATH-ENGINE") and "Stop" in out


def test_shim_runs_the_plugins_own_engine_when_path_is_stale(tmp_path):
    out = _shim(tmp_path, {"truequery": _fake_truequery("0.0.1"),
                           "uvx": FAKE_UVX})
    if out is not None:
        assert out.startswith("UVX")
        assert f"--from {PLUGIN}/engine truequery hook" in out
        assert "Stop" in out  # the event reached it


def test_shim_falls_back_to_a_stale_engine_and_names_the_version(tmp_path):
    out = _shim(tmp_path, {"truequery": _fake_truequery("0.0.1")})
    if out is not None:
        assert out.startswith(f"PATH-ENGINE wants={_plugin_version()}")
        assert "Stop" in out


def _launcher(tmp_path, tools, *args):
    """Run plugin/bin/truequery the way the agent's Bash tool does, with
    only the given fake tools (plus a foreign `truequery`) on PATH."""
    import subprocess
    import sys
    if sys.platform == "win32":
        return None
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for name, body in {"truequery": "echo FOREIGN\n", **tools}.items():
        exe = bindir / name
        exe.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        exe.chmod(0o755)
    return subprocess.run(
        ["sh", os.path.join(PLUGIN, "bin", "truequery"), *args],
        capture_output=True, text=True, timeout=30,
        env={"PATH": f"{bindir}:/usr/bin:/bin"})


def test_launcher_runs_the_plugins_own_engine(tmp_path):
    # The agent's `truequery` must be this plugin's engine, not whatever
    # else is on PATH (an older install, another tool's tq).
    proc = _launcher(tmp_path, {"uvx": 'echo "UVX $*"\n'},
                     "review", "models/")
    if proc is not None:
        assert proc.returncode == 0
        assert "FOREIGN" not in proc.stdout
        engine = os.path.realpath(os.path.join(PLUGIN, "engine"))
        assert proc.stdout.strip() == \
            f"UVX --quiet --from {engine} truequery review models/"


def test_launcher_without_uv_says_how_to_fix_it(tmp_path):
    proc = _launcher(tmp_path, {})
    if proc is not None:
        assert proc.returncode == 127
        assert "FOREIGN" not in proc.stdout
        assert "docs.astral.sh/uv" in proc.stderr


def test_launcher_is_executable_in_git():
    import subprocess
    out = subprocess.run(["git", "ls-files", "-s", "plugin/bin/truequery"],
                         cwd=ROOT, capture_output=True, text=True).stdout
    assert out.startswith("100755"), out
