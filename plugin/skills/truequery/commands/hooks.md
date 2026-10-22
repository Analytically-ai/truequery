# /truequery:hooks — hooks on, off, or status

When: the user asks about hooks, or `/truequery init` finds no hooks
and the user wants per-edit checks. Wraps the terminal verbs
(`truequery install` / `uninstall` / `status`); prefer running those
over hand-editing harness configs.

- `/truequery hooks status` — report per harness (Claude Code, Codex,
  Muse) whether hooks are off, installed by `truequery install`, or
  active from the truequery plugin. Never writes.
- `/truequery hooks on` — run `truequery install --harness <name>` for
  the harness you are running in (`claude`, `codex` or `muse`; project
  scope by default, `--scope global` on request).
  If the plugin's hooks are already active, say so and stop: never
  double-install. Tell the user the one trust step the harness needs
  (workspace trust dialog / `/hooks` approval).
- `/truequery hooks off` — run `truequery uninstall`. Plugin-managed
  hooks are not removed here; tell the user to disable the plugin in
  the harness instead.

Hooks never refuse an edit. After an edit they report findings; at
the end of a turn the Stop hook hands new findings back once, so they
can be fixed before the turn ends. If hooks
misbehave, reproduce with `truequery hook --event stop` on stdin JSON
and report the envelope; never silence a hook by deleting config
without saying so in the receipt.
