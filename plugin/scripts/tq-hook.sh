#!/bin/sh
# TrueQuery plugin hook shim. It runs the checker whose version matches
# this plugin, so a plugin update can never leave an old checker behind:
#   1. `truequery` on PATH, if `truequery --version` is this version;
#   2. else this plugin's own engine copy via uvx (cached per version);
#   3. else whatever `truequery` is on PATH, told which version the plugin
#      expects (it then asks the user to upgrade, once per session);
#   4. else one install line.
# It never breaks a session: it always exits 0. It calls `truequery`, never
# the short `tq` alias, which other tools also use. The plugin root comes
# from CLAUDE_PLUGIN_ROOT, else this script's own location.
ROOT="${CLAUDE_PLUGIN_ROOT:-$(CDPATH= cd -- "$(dirname -- "$0")/.." 2>/dev/null && pwd)}"
WANT=$(sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' \
  "$ROOT/.claude-plugin/plugin.json" 2>/dev/null | head -n 1)
TRUEQUERY_PLUGIN_VERSION="$WANT"
export TRUEQUERY_PLUGIN_VERSION

# Read the hook event once, so a failed attempt can't swallow it.
PAYLOAD=$(cat)
run() { printf '%s' "$PAYLOAD" | "$@" hook --harness auto; }

if command -v truequery >/dev/null 2>&1 \
    && [ -n "$WANT" ] && [ "$(truequery --version 2>/dev/null)" = "truequery $WANT" ]; then
  run truequery
  exit 0
fi
# First run fetches sqlglot/pyyaml once; later runs are cached and offline.
if command -v uvx >/dev/null 2>&1 && [ -f "$ROOT/engine/pyproject.toml" ] \
    && uvx --quiet --from "$ROOT/engine" truequery --version >/dev/null 2>&1; then
  run uvx --quiet --from "$ROOT/engine" truequery
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
