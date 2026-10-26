"""Rule settings: humans adjust every rule; agents never do.

Layers, lowest to highest precedence:
  shipped defaults (registry) < user (~/.config/truequery/config.yaml)
  < project (DATA.md tq-config `rules:` + `interrupt_edits`)
  < CLI flags (--rule-tier / --enable-rule / --disable-rule).

Each rule's effective {enabled, tier, severity} remembers which layer
set it. Resolution is total and forgiving of unknown keys; the `rules
set` write path validates strictly instead (see guard()).
"""
from __future__ import annotations

import hashlib
import json
import os

import yaml

from truequery import project as P
from truequery import std_registry

TIERS = ("immediate", "stop")
SEVERITIES = ("error", "warning", "advisory")
SETTING_KEYS = ("enabled", "tier", "severity")
SOURCE_LABELS = {"user": "user config", "project": "project config",
                 "cli": "CLI flags", "default": "shipped default"}
NOTICE = "rule settings changed this session — confirm with the user"


def user_config_path(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(base, "truequery", "config.yaml")


def _load_yaml(path: str) -> dict:
    """A missing user config is empty; a broken one is a ConfigError."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
    except OSError:
        return {}
    except yaml.YAMLError as exc:
        raise P.ConfigError(f"{path}: not valid YAML: "
                            f"{P._yaml_problem(exc)}") from None
    return data if isinstance(data, dict) else {}


def _clean_entry(raw) -> dict:
    """One rule's settings from a config layer; unknown keys/values
    dropped (typo safety), because a settings file must never crash
    the checker."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    if raw.get("enabled") in (True, False):
        out["enabled"] = raw["enabled"]
    elif isinstance(raw.get("enabled"), str):
        low = raw["enabled"].lower()
        if low in ("on", "true"):
            out["enabled"] = True
        elif low in ("off", "false"):
            out["enabled"] = False
    if isinstance(raw.get("tier"), str) and raw["tier"] in TIERS:
        out["tier"] = raw["tier"]
    if isinstance(raw.get("severity"), str) and raw["severity"] in SEVERITIES:
        out["severity"] = raw["severity"]
    return out


class Effective:
    """Resolved settings: per-rule effective fields + their sources."""

    def __init__(self, rules, interrupt_edits, interrupt_source):
        self.rules = rules  # slug -> {enabled, tier, severity|None, source}
        self.interrupt_edits = interrupt_edits
        self.interrupt_source = interrupt_source

    def check_map(self) -> dict:
        """{slug: {enabled, tier, severity}} for the analyzer."""
        return {slug: {"enabled": r["enabled"], "tier": r["tier"],
                       "severity": r["severity"]}
                for slug, r in self.rules.items()}

    def settings_line(self) -> str | None:
        """Receipt line, or None when nothing differs from defaults."""
        adjusted = [(slug, r["source"])
                    for slug, r in sorted(self.rules.items())
                    if self._differs(slug, r, std_registry.RECORDS)]
        clauses = []
        if adjusted:
            by_source: dict[str, int] = {}
            for _, source in adjusted:
                by_source[source] = by_source.get(source, 0) + 1
            if len(by_source) == 1:
                (source,) = by_source
                clauses.append(
                    f"rule settings: {len(adjusted)} adjusted by "
                    f"{SOURCE_LABELS[source]}")
            else:
                parts = ", ".join(
                    f"{SOURCE_LABELS[source]} × {count}"
                    for source, count in sorted(by_source.items()))
                clauses.append(
                    f"rule settings: {len(adjusted)} adjusted ({parts})")
        if not self.interrupt_edits:
            clauses.append(
                f"interrupt_edits: off ({SOURCE_LABELS[self.interrupt_source]})")
        return "; ".join(clauses) if clauses else None

    def fingerprint(self) -> str:
        """Stable hash of every effective rule setting, from any layer, so
        an in-session change in DATA.md or the user config is noticed."""
        blob = json.dumps({"rules": self.check_map(),
                           "interrupt_edits": self.interrupt_edits},
                          sort_keys=True)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _differs(slug, eff, records) -> bool:
        rec = records.get(slug)
        if rec is None:
            return False
        return (eff["enabled"] != rec["enabled"]
                or eff["tier"] != rec["tier"]
                or eff["severity"] is not None)


def _legacy_overrides(config: dict | None) -> dict:
    """tier_overrides (the old testing knob) read as project-layer tier."""
    raw = (config or {}).get("tier_overrides") or {}
    if not isinstance(raw, dict):
        return {}
    return {slug: {"tier": tier} for slug, tier in raw.items()
            if slug in std_registry.RECORDS and tier in TIERS}


def effective(root: str | None = None, project: dict | None = None,
              user: dict | None = None, cli: dict | None = None) -> Effective:
    """Resolve layered settings. `project`/`user`/`cli` are raw config
    dicts (rules + interrupt_edits); pass None to read from disk."""
    if project is None:
        if root is not None:
            project = P.load_config(root) or {}
        else:
            project = {}
    if user is None:
        user = _load_yaml(user_config_path())
    if cli is None:
        cli = {}
    records = std_registry.RECORDS
    layers = [("user", (user or {}).get("rules")),
              ("project", _legacy_overrides(project)),
              ("project", (project or {}).get("rules")),
              ("cli", (cli or {}).get("rules"))]
    rules = {}
    for slug, rec in records.items():
        eff = {"enabled": rec["enabled"], "tier": rec["tier"],
               "severity": None, "source": "default"}
        for source, raw_layer in layers:
            if not isinstance(raw_layer, dict):
                continue
            entry = _clean_entry(raw_layer.get(slug))
            for key, value in entry.items():
                eff[key] = value
                eff["source"] = source
        if not rec["implemented"]:
            # Unimplemented rules can never run, whatever a layer says.
            if eff["tier"] == "immediate":
                eff["tier"] = "stop"
            eff["enabled"] = False
        rules[slug] = eff
    interrupt = True
    interrupt_source = "default"
    for source, raw_layer in [
            ("user", user or {}), ("project", project or {}),
            ("cli", cli or {})]:
        if isinstance(raw_layer.get("interrupt_edits"), bool):
            interrupt = raw_layer["interrupt_edits"]
            interrupt_source = source
    return Effective(rules, interrupt, interrupt_source)


def guard(slug: str, updates: dict, records) -> str | None:
    """Write-path validation for `tq rules set`. Returns an error string
    or None. Enables/immediate on unimplemented rules are refused here,
    and again at resolution (effective()), so hand-edited configs stay
    safe too."""
    rec = records.get(slug)
    if rec is None:
        return f"tq: unknown rule: {slug}"
    if not updates:
        return ("tq: nothing to set (pass --enabled, --tier or --severity)")
    if not rec["implemented"]:
        if updates.get("enabled") is True:
            return (f"tq: {slug} exists as a record but no detector checks "
                    "it yet; it cannot be enabled")
        if updates.get("tier") == "immediate":
            return (f"tq: {slug} has no detector to run per-edit; "
                    "tier immediate is unavailable")
    return None


def data_md_with_rule(text: str, slug: str, updates: dict) -> str:
    """Return DATA.md `text` with rules[slug] merged into its tq-config
    fence. Raises ValueError when there is no usable fence."""
    match = P._TQ_FENCE.search(text)
    if not match:
        raise ValueError("DATA.md has no ```tq-config fence to edit; "
                         "run `truequery init` first")
    try:
        config = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise ValueError("DATA.md tq-config is not valid YAML; fix it "
                         "before changing rule settings") from exc
    if not isinstance(config, dict):
        config = {}
    rules = config.setdefault("rules", {})
    if not isinstance(rules, dict):
        rules = config["rules"] = {}
    rules[slug] = {**_clean_entry(rules.get(slug)), **updates}
    body = yaml.safe_dump(config, sort_keys=True).rstrip()
    return text[:match.start(1)] + body + text[match.end(1):]


def write_user_rule(slug: str, updates: dict, path: str | None = None) -> str:
    path = path or user_config_path()
    config = _load_yaml(path)
    rules = config.setdefault("rules", {})
    if not isinstance(rules, dict):
        rules = config["rules"] = {}
    rules[slug] = {**_clean_entry(rules.get(slug)), **updates}
    config.setdefault("version", 1)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=True)
    return path
