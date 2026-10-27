# Releasing TrueQuery

A release is a version bump that every installed copy can see. Claude
Code only delivers a plugin update when `plugin.json`'s `version`
changes. The hook shim runs the checker whose version matches the plugin,
so the engine version has to change in step.

1. **Pick the version** ([semver](https://semver.org/)): patch for fixes,
   minor for new checks or commands, major for anything that changes rule
   IDs, output formats or exit codes.
2. **Bump it in all three places** (`tests/test_plugin.py` fails if they
   disagree):
   - `plugin/.claude-plugin/plugin.json` (`version`)
   - `plugin/engine/pyproject.toml` (`version`)
   - `plugin/engine/src/truequery/__init__.py` (`__version__`)

   Don't add a `version` to the marketplace entry in
   `.claude-plugin/marketplace.json`: `plugin.json` wins silently.
3. **Regenerate and test:**
   ```bash
   .venv/bin/python tools/generate_registry.py
   .venv/bin/python tools/generate_skill.py
   .venv/bin/python -m pytest
   ```
4. **Write the change** in `CHANGELOG.md`: what users will notice, with
   any new findings called out (a new check makes clean code red). Give
   the heading the release date, never "(unreleased)", before tagging.
5. **Commit, tag `vX.Y.Z`, push.** CI must pass on all three platforms
   before the tag is announced. Publishing the GitHub Release publishes
   the checker to PyPI: the `publish` workflow builds the sdist and wheel
   from `plugin/engine`, fails if the release tag doesn't match the package
   version, and uploads with trusted publishing (no tokens). There is
   nothing else to do for PyPI.
6. **Check an update end to end:** in a project with the previous version
   installed, run `claude plugin update truequery@analytically`, then
   `/reload-plugins`, edit a SQL file, and confirm no "checker is …
   but the plugin is …" notice appears.
