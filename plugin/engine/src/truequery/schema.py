"""Standard-record schema (public interface, versioned).

Loads the YAML seed records in ``standard/<domain>/<slug>.yaml`` with
strict validation: unknown keys fail, ``id`` must match the file path,
and proof blocks must carry setup/bad/good. Engine-derived fields
(severity per mode, evidence, detector, tier, implemented) resolve here
so every consumer sees one shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import yaml

SCHEMA_VERSION = 1
STANDARD_VERSION = "0.1"

DOMAINS = ("ingest", "store", "model", "orch", "dq", "sem", "consume",
           "gov", "ops", "sql", "py")
MODES = ("explore", "build", "change", "assure")
ENFORCEMENT = ("detector", "contract", "runtime", "judgment")
EVIDENCE = ("file", "project", "contract", "metadata", "data")
SEVERITIES = ("error", "warning", "advisory", "info")

REQUIRED_KEYS = frozenset({
    "id", "title", "domain", "modes", "applies_to", "statement",
    "rationale", "failure_example", "enforcement", "detection_sketch",
    "severity_suggestion", "examples", "sources", "confidence",
    "conflicts_with", "status",
})
OPTIONAL_KEYS = frozenset({
    "see_also", "contract_required", "proof", "decisions", "note",
})
PROOF_KEYS = frozenset({"setup", "bad", "bad_result", "good", "good_result"})
PROOF_OPTIONAL = frozenset({"setup_alt", "bad_result_alt", "note"})
SOURCE_KEYS = frozenset({"title", "url", "publisher", "accessed", "supports"})

STANDARD_DIR = os.path.join(os.path.dirname(__file__), "standard")


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Proof:
    setup: str
    bad: str
    bad_result: str
    good: str
    good_result: str
    note: str = ""
    setup_alt: str = ""
    bad_result_alt: str = ""


@dataclass(frozen=True)
class Record:
    id: str
    title: str
    domain: str
    modes: tuple
    applies_to: tuple
    statement: str
    rationale: str
    enforcement: str
    severity: dict
    evidence: str
    detector: str | None
    tier: str
    implemented: bool
    enabled: bool
    proof: Proof | None
    sources: tuple
    confidence: str
    status: str
    see_also: tuple = ()
    contract_required: bool = False
    raw: dict = field(default_factory=dict, compare=False)


def _severity_map(raw: dict, record_id: str) -> dict:
    """Per-mode severity. Records declare severity_suggestion (a single
    level); the engine maps it to every mode the record lists. Explore
    downgrades error->warning: ad-hoc analysis must not fail loudly."""
    if "severity" in raw and isinstance(raw["severity"], dict):
        sev = dict(raw["severity"])
    else:
        base = raw.get("severity_suggestion", "warning")
        if base not in SEVERITIES:
            raise SchemaError(f"{record_id}: bad severity {base!r}")
        sev = {}
        for mode in raw.get("modes", []):
            if mode == "explore" and base == "error":
                sev[mode] = "warning"
            else:
                sev[mode] = base
    for mode, level in sev.items():
        if mode not in MODES or level not in SEVERITIES:
            raise SchemaError(f"{record_id}: bad severity map {sev!r}")
    return sev


def _check_proof(raw: dict, record_id: str) -> Proof | None:
    proof = raw.get("proof")
    if proof is None:
        return None
    if not isinstance(proof, dict):
        raise SchemaError(f"{record_id}: proof must be a mapping")
    unknown = set(proof) - PROOF_KEYS - PROOF_OPTIONAL
    if unknown:
        raise SchemaError(f"{record_id}: unknown proof keys {sorted(unknown)}")
    missing = PROOF_KEYS - set(proof)
    if missing:
        raise SchemaError(f"{record_id}: proof missing {sorted(missing)}")
    return Proof(**{k: proof.get(k, "") for k in (
        "setup", "bad", "bad_result", "good", "good_result", "note",
        "setup_alt", "bad_result_alt")})


def load_record(path: str, impl: dict) -> Record:
    """Load and validate one record file. ``impl`` maps slug ->
    {implemented, enabled, tier} from registry_impl.json."""
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)
    if not isinstance(raw, dict):
        raise SchemaError(f"{path}: top level must be a mapping")
    unknown = set(raw) - REQUIRED_KEYS - OPTIONAL_KEYS - {"severity",
                                                          "evidence",
                                                          "detector",
                                                          "tier",
                                                          "implemented"}
    if unknown:
        raise SchemaError(f"{path}: unknown keys {sorted(unknown)}")
    missing = REQUIRED_KEYS - set(raw)
    if missing:
        raise SchemaError(f"{path}: missing keys {sorted(missing)}")
    slug = os.path.splitext(os.path.basename(path))[0]
    domain = os.path.basename(os.path.dirname(path))
    record_id = f"{domain}/{slug}"
    if raw["id"] != record_id:
        raise SchemaError(
            f"{path}: id {raw['id']!r} does not match path {record_id!r}")
    if raw["domain"] != domain:
        raise SchemaError(f"{path}: domain mismatch")
    if domain not in DOMAINS:
        raise SchemaError(f"{path}: unknown domain {domain!r}")
    if not raw["modes"] or not set(raw["modes"]) <= set(MODES):
        raise SchemaError(f"{path}: bad modes {raw['modes']!r}")
    if raw["enforcement"] not in ENFORCEMENT:
        raise SchemaError(f"{path}: bad enforcement")
    for source in raw.get("sources", []):
        if set(source) - SOURCE_KEYS:
            raise SchemaError(f"{path}: bad source keys {sorted(source)}")
    entry = impl.get(record_id, {})
    return Record(
        id=record_id,
        title=raw["title"],
        domain=domain,
        modes=tuple(raw["modes"]),
        applies_to=tuple(raw.get("applies_to", ["any"])),
        statement=raw["statement"],
        rationale=raw["rationale"],
        enforcement=raw["enforcement"],
        severity=_severity_map(raw, record_id),
        evidence=raw.get("evidence", "file"
                         if raw["enforcement"] == "detector" else "data"),
        detector=raw.get("detector"),
        tier=entry.get("tier", raw.get("tier", "stop")),
        implemented=bool(entry.get("implemented", False)),
        enabled=bool(entry.get("enabled", entry.get("implemented", False))),
        proof=_check_proof(raw, record_id),
        sources=tuple((s.get("title"), s.get("url")) for s in raw.get("sources", [])),
        confidence=raw.get("confidence", "medium"),
        status=raw.get("status", "draft"),
        see_also=tuple(raw.get("see_also", [])),
        contract_required=bool(raw.get("contract_required", False)),
        raw=raw,
    )


def load_all(standard_dir: str = STANDARD_DIR,
             impl: dict | None = None) -> dict[str, Record]:
    """Load every record: {slug: Record}. Raises SchemaError on any
    violation — the schema is a public interface."""
    if impl is None:
        impl_path = os.path.join(os.path.dirname(__file__),
                                 "registry_impl.json")
        with open(impl_path, encoding="utf-8") as handle:
            impl = json.load(handle)
    records = {}
    for domain in sorted(os.listdir(standard_dir)):
        dpath = os.path.join(standard_dir, domain)
        if not os.path.isdir(dpath):
            continue
        for name in sorted(os.listdir(dpath)):
            if not name.endswith(".yaml"):
                continue
            record = load_record(os.path.join(dpath, name), impl)
            if record.id in records:
                raise SchemaError(f"duplicate id {record.id}")
            records[record.id] = record
    return records
