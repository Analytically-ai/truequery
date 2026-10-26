#!/bin/sh
# TrueQuery plugin hook shim. It runs the checker that ships with this
# plugin, so a plugin update can never leave an old checker behind:
#   1. this plugin's own engine via uv, installed editable: it always runs
#      the code in this plugin folder, never a stale cached build;
#   2. else whatever `truequery` is on PATH, told which version the plugin
#      expects (it then asks the user to upgrade, once per session);
#   3. else one line saying why nothing was checked.
# It never breaks a session: it always exits 0. It calls `truequery`, never
# the short `tq` alias, which other tools also use. The plugin root comes
# from CLAUDE_PLUGIN_ROOT, else this script's own location.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." 2>/dev/null && pwd)}"
WANT=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
TRUEQUERY_PLUGIN_VERSION="$WANT"
export TRUEQUERY_PLUGIN_VERSION
# Bytecode goes to a cache, never into the plugin folder.
PYTHONPYCACHEPREFIX="${XDG_CACHE_HOME:-$HOME/.cache}/truequery/pycache"
export PYTHONPYCACHEPREFIX

# Read the hook event once, so a failed attempt can't swallow it.
PAYLOAD=$(cat)
run() { printf '%s' "$PAYLOAD" | "$@" hook --harness auto; }
own() { uv run --quiet --no-project --python ">=3.11" \
          --with-editable "$ROOT/engine" -- truequery "$@"; }

# First run fetches sqlglot/pyyaml once; later runs are cached and offline.
if command -v uv >/dev/null 2>&1 && [ -f "$ROOT/engine/pyproject.toml" ] \
    && own --version >/dev/null 2>&1; then
  run own
  exit 0
fi
if command -v truequery >/dev/null 2>&1; then
  run truequery
  exit 0
fi
if [ -f "$ROOT/engine/pyproject.toml" ]; then
  echo "[TrueQuery] the checker couldn't start, so nothing was checked. It runs with uv (https://docs.astral.sh/uv/), and its first run needs internet once to fetch two dependencies. The /truequery skill still works without it."
else
  echo "[TrueQuery] the checker is not installed, so nothing was checked. Install it from your TrueQuery clone with: uv tool install <clone>/plugin/engine (the /truequery skill still works without it)."
fi
exit 0
