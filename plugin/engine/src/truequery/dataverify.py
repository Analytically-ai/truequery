"""`truequery verify` and `review --data`: run the read-only data checks
and say what they found in plain words.

The runner (`verify.py`) does the reading. This module decides what a
run means: which datasets were verified, which failed and why, which
couldn't be read, and which were skipped. It also writes the lines that
`truequery verify` prints and the receipt shows. Every line comes from
this run's results only, never from an older `.truequery/evidence.jsonl`:
the data may have changed since then.

Nothing here runs in a hook.
"""

from __future__ import annotations

from datetime import datetime, timezone
import os
from typing import Any

from truequery import project as P
from truequery import verify as V

LABEL = 13  # width of the receipt's first column
RUN_HINT = "truequery setup data"
LIST_LIMIT = 3  # files named per dataset before "and N more"


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def parse_now(text: str | None) -> datetime:
    """`--now` as a UTC datetime; raises ValueError on a bad value."""
    try:
        return V._parse_now(text)
    except (TypeError, ValueError):
        raise ValueError(
            f"--now must be an ISO time such as 2026-11-10T00:00:00Z, "
            f"not {text!r}") from None


def run(root: str, datasets: list[str] | None = None,
        now: datetime | str | None = None,
        write_evidence: bool = True) -> dict[str, Any]:
    """Verify the project's local data. Returns the run document that
    `--json` prints and the receipt reads."""
    now_dt = V._parse_now(now)
    project = V.verify_project(root, datasets=datasets, now=now_dt,
                               write_evidence=False)
    contracts, _ = P.load_contracts_reporting(root)
    by_name = {str(c.get("dataset") or c.get("table")): c
               for c in contracts.values()}
    config = P.load_config(root) or {}
    declined = (config.get("setup") or {}).get("data") is False

    entries = []
    records: list[dict[str, Any]] = []
    for report in project["datasets"]:
        contract = by_name.get(report["dataset"], {})
        entries.append(_entry(report, contract, root))
        records.extend(report.get("evidence_records") or [])
    for item in project["not_checked"]:
        records.append({
            "dataset": item["dataset"], "assertion": None,
            "status": "not_checked", "message": item["message"],
            "checked_at": now_dt.isoformat(),
            "provenance_note": item.get("provenance_note"),
        })

    evidence = None
    if write_evidence and records:
        path = V.append_evidence(root, records)
        if path:
            evidence = {"path": P._rel(path, root), "records": len(records)}

    return {
        "checked_at": now_dt.isoformat(),
        "reader": project["reader"],
        "reader_declined": declined,
        "datasets": entries,
        "not_checked": [{k: v for k, v in item.items() if k != "detail"}
                        for item in project["not_checked"]],
        "not_checked_detail": {item["dataset"]: item.get("detail")
                               for item in project["not_checked"]
                               if item.get("detail")},
        "skipped": project["skipped"],
        "invalid_contracts": project["invalid_contracts"],
        "unknown": project["unknown"],
        "evidence": evidence,
    }


def public_doc(doc: dict) -> dict:
    """The run document without raw error text (which can quote data)."""
    return {k: v for k, v in doc.items() if k != "not_checked_detail"}


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


def failures(doc: dict) -> list[dict]:
    return [f for e in doc["datasets"] for f in e["failures"]]


def unverified(doc: dict, require_data: bool = False,
               include_invalid: bool = True) -> list[dict]:
    """Datasets that weren't fully verified: unreadable, empty, partly
    checked, or (with --require data) the reader missing. Invalid
    contracts count too, unless the caller reports them itself (review)."""
    out = [n for n in doc["not_checked"]
           if n["reason"] != "reader_missing" or require_data]
    out += [{"dataset": e["dataset"], "reason": "partial"}
            for e in doc["datasets"] if e["not_verified"] or e["empty"]]
    if include_invalid and doc["invalid_contracts"]:
        out.append({"dataset": None, "reason": "invalid_contract"})
    return out


def reader_missing(doc: dict) -> list[str]:
    return sorted(n["dataset"] for n in doc["not_checked"]
                  if n["reason"] == "reader_missing")


def exit_code(doc: dict, allow_unverified: bool, require_data: bool,
              include_invalid: bool = True) -> int:
    """0 verified · 1 the data breaks a contract · 3 some data not verified
    (`--allow-unverified` turns 3 into 0, except a missing reader under
    `--require data`)."""
    if failures(doc):
        return 1
    if require_data and reader_missing(doc):
        return 3
    if not allow_unverified and unverified(doc, include_invalid=include_invalid):
        return 3
    return 0


# ---------------------------------------------------------------------------
# Words
# ---------------------------------------------------------------------------


def _n(count: int) -> str:
    return f"{count:,}"


def _plural(count: int, word: str, plural: str | None = None) -> str:
    return f"{_n(count)} {word if count == 1 else (plural or word + 's')}"


def _verb(count: int, one: str, many: str) -> str:
    return one if count == 1 else many


IT_IS, THEY_ARE = "it's", "they're"


def _dont(count: int) -> str:
    return "doesn't" if count == 1 else "don't"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _key_label(target) -> str:
    if isinstance(target, (list, tuple)):
        return "(" + ", ".join(str(t) for t in target) + ")"
    return str(target)


def _columns(contract: dict) -> dict:
    cols = contract.get("columns")
    return cols if isinstance(cols, dict) else {}


def _tz_words(tz: str) -> str:
    if tz == "UTC":
        return "UTC"
    if tz.startswith("local:"):
        return f"local time ({tz.split(':', 1)[1]})"
    return "naive (no zone), as declared"


def _base_words(base: str | None) -> str:
    return base if base and base != "project_root" else "the project folder"


def _where(report: dict, contract: dict) -> str:
    """Where the data is: the glob or the database, as the contract says."""
    loc = contract.get("location") if isinstance(contract.get("location"), dict) else {}
    if loc.get("path"):
        return str(loc["path"])
    if loc.get("database"):
        rel = loc.get("relation") or report["dataset"]
        return f"{loc['database']}, table {rel}"
    files = report["source"]["files"]
    return files[0] if files else report["dataset"]


def _source_words(report: dict, contract: dict) -> str:
    src = report["source"]
    rows = _plural(src["rows"], "row")
    if src["kind"] == "files":
        what = _plural(len(src["files"]), "file")
    else:
        loc = contract.get("location") or {}
        what = f"{loc.get('database')}, table {loc.get('relation') or report['dataset']}"
    words = f"{what}, {rows}"
    if src.get("sampled"):
        checked = min(src["rows"], V.DEFAULT_MAX_ROWS)
        words += f"; the first {_n(checked)} checked"
    return words


def _keys(result: dict) -> str:
    if result.get("pii_redacted") and result.get("failed_count"):
        return "; keys hidden: marked pii"
    keys = result.get("sample_keys") or []
    if not keys:
        return ""
    shown = []
    for k in keys:
        if isinstance(k, list):
            shown.append("(" + ", ".join("empty" if x is None else str(x)
                                         for x in k) + ")")
        elif k is not None:  # an empty key is the problem, not an example
            shown.append(str(k))
    if not shown:
        return ""
    more = " …" if result.get("failed_count", 0) > len(shown) else ""
    return f"; e.g. key {', '.join(shown)}{more}"


def _compared(result: dict) -> int | None:
    """How many values the check looked at; None when it judged the whole
    column at once (a native timestamp type) or looked at every row."""
    value = (result.get("details") or {}).get("compared")
    return None if value is None else int(value)


def _pass_phrases(report: dict, contract: dict) -> tuple[list[str], list[str]]:
    """What passed, and the checks that passed only because they had
    nothing to compare (never said as verified)."""
    cols = _columns(contract)
    phrases: list[str] = []
    nothing: list[str] = []
    not_null = [r["target"] for r in report["assertions"]
                if r["assertion"] == "not_null" and r["status"] == "passed"]
    if not_null:
        phrases.append(
            "no empty " + (_join([str(c) for c in not_null])
                           if len(not_null) <= 4
                           else f"values in {len(not_null)} required columns"))
    for r in report["assertions"]:
        if r["status"] != "passed":
            continue
        name, target = r["assertion"], r["target"]
        det = r.get("details") or {}
        if _compared(r) == 0:
            nothing.append(_nothing_words(name, target))
            continue
        if name == "unique":
            phrases.append(f"key {_key_label(target)} unique")
        elif name == "accepted_values":
            phrases.append(f"{target} values all declared")
        elif name == "tz_marker":
            tz = str((cols.get(target) or {}).get("time_zone") or "")
            phrases.append(f"{target} timestamps {_tz_words(tz)}" if tz
                           else f"{target} timestamps as declared")
        elif name == "path_resolves":
            base = _base_words(det.get("base"))
            phrases.append(f"no {target} outside {base}" if det.get("by_status")
                           else f"every {target} under {base}")
        elif name == "exists_per_status":
            phrases.append("files exist where the status says present")
        elif name == "size_matches_disk":
            phrases.append(f"{target} matches the size on disk"
                           f" ({_plural(det.get('files_compared', 0), 'file')})")
        elif name == "freshness":
            phrases.append(f"fresh (newest {target} within "
                           f"{det.get('max_age')})")
    return phrases, nothing


def _nothing_words(name: str, target) -> str:
    """A check that had nothing to look at, in a few words."""
    if name == "accepted_values":
        return f"{target} values (all empty)"
    if name == "tz_marker":
        return f"{target} time zone (all empty)"
    if name == "path_resolves":
        return f"{target} (all empty)"
    if name == "exists_per_status":
        return "files per status (no row whose status says present)"
    if name == "size_matches_disk":
        return f"{target} sizes (none of the files was found)"
    return f"{name} on {_key_label(target)} (nothing to compare)"


NOTHING_CHECKABLE = (
    "the contract declares nothing `truequery verify` checks (it checks "
    "keys, nullable: false, values, time zones, paths, sizes and freshness; "
    "column types aren't checked)")


def _absent_note(report: dict) -> str:
    """Rows with no file that the status accounts for: counted, never
    failed. `means` says what an absence means, not whether verify fails."""
    out = ""
    for r in report["assertions"]:
        det = r.get("details") or {}
        if r["assertion"] != "exists_per_status":
            continue
        if det.get("absent_expected_count"):
            statuses = ", ".join(det.get("absent_expected_statuses") or [])
            out += (f" · {_plural(det['absent_expected_count'], 'row')} "
                    f"absent as expected ({statuses})")
        if det.get("absent_unexpected_count"):
            n = det["absent_unexpected_count"]
            statuses = ", ".join(det.get("absent_unexpected_statuses") or [])
            out += (f" · {_plural(n, 'row')} whose status means a failure "
                    f"({statuses}): no file, as the status says")
    return out


def _doubled_words(count: int, target, det: dict) -> tuple[str, str]:
    """The wrong-base error, as its own failure: the paths repeat the base
    folder's own name. Never resolved quietly: the paths as written are
    wrong."""
    name = det.get("base_name") or "data"
    what = (f"{_plural(count, 'path')} in {target} "
            f"{_verb(count, 'starts', 'start')} with {name}/, the base "
            f"folder's own name, so {_verb(count, 'it points', 'they point')} "
            f"to {name}/{name}/…; the {_verb(count, 'file is', 'files are')} "
            "there without it")
    fix = ("correct the paths where they're written, or check how "
           f"`relative_to` is declared for {target}")
    return what, fix


def _failures(result: dict, report: dict, contract: dict, root: str) -> list[dict]:
    """The failures one failed assertion reports. Paths outside the base
    folder, and paths that repeat the base folder's own name, are each
    their own failure, apart from the files that are really missing: the
    fix differs. (`sample_keys` holds the rest's keys first, then the
    outside ones, then the doubled ones.)"""
    det = result.get("details") or {}
    escaped = int(det.get("escaped_count") or 0)
    doubled = int(det.get("doubled_base_count") or 0)
    if not (escaped or doubled):
        return [_failure(result, report, contract, root)]
    keys = list(result.get("sample_keys") or [])
    escaped_keys = list(det.get("escaped_sample_keys") or [])
    doubled_keys = list(det.get("doubled_sample_keys") or [])
    rest_keys = keys[:len(keys) - len(escaped_keys) - len(doubled_keys)]
    rest = int(result.get("failed_count") or 0) - escaped - doubled
    out = []
    for count, part, part_keys in ((escaped, "escaped_part", escaped_keys),
                                   (doubled, "doubled_part", doubled_keys)):
        if count:
            out.append(_failure({**result, "failed_count": count,
                                 "sample_keys": part_keys,
                                 "details": {**det, part: True}},
                                report, contract, root))
    if rest > 0:
        out.append(_failure({**result, "failed_count": rest,
                             "sample_keys": rest_keys,
                             "details": {**det, "escaped_count": 0,
                                         "doubled_base_count": 0}},
                            report, contract, root))
    return out


def _failure(result: dict, report: dict, contract: dict, root: str) -> dict:
    """{"what": ..., "fix": ...} for one failed assertion, in plain words."""
    name, target = result["assertion"], result["target"]
    count = int(result.get("failed_count") or 0)
    det = result.get("details") or {}
    cols = _columns(contract)
    if name == "not_null":
        keys = {str(k).lower() for k in _key_columns(contract)}
        what = f"{_plural(count, 'row')} with no {target}"
        fix = ("a key can't be empty: fix those rows where they're written"
               if str(target).lower() in keys else
               f"fill them where they're written, or set `nullable: true` for "
               f"{target} in the contract if empty is allowed")
    elif name == "unique":
        what = (f"key {_key_label(target)} is not unique: "
                f"{_plural(count, 'value')} {_verb(count, 'appears', 'appear')} "
                "more than once")
        fix = ("remove the duplicates (dedupe on the key before writing), or "
               "correct `keys` in the contract if a row is something finer")
    elif name == "accepted_values":
        empty = min(int(det.get("empty_count") or 0), count)
        bits = []
        if count - empty:
            bits.append(f"{_plural(count - empty, 'row')} with a {target} "
                        "value the contract doesn't declare")
        if empty:
            bits.append(f"{_plural(empty, 'row')} with an empty {target}")
        what = "; ".join(bits)
        fixes = []
        if count - empty:
            fixes.append(f"add the value under {target} `values` (with what "
                         "it means), or correct those rows")
        if empty:
            fixes.append(f"fill in {target}, or set `nullable: true` on it if "
                         "empty is allowed")
        fix = "; ".join(fixes)
    elif name == "tz_marker":
        tz = str((cols.get(target) or {}).get("time_zone")
                 or det.get("time_zone") or "")
        if det.get("physical_type"):
            what = (f"{target} is stored as {det['physical_type']}, but the "
                    f"contract says {_tz_words(tz)}")
        elif tz == "UTC":
            what = (f"{_plural(count, 'timestamp')} in {target} "
                    f"{_verb(count, 'isn', 'aren')}'t UTC "
                    "(no Z or +00:00, or another offset)")
        elif tz.startswith("local:"):
            what = (f"{_plural(count, 'timestamp')} in {target} "
                    f"{_verb(count, 'is', 'are')} marked "
                    f"UTC (Z), but the contract says {_tz_words(tz)}")
        else:
            what = (f"{_plural(count, 'timestamp')} in {target} "
                    f"{_verb(count, 'carries', 'carry')} a zone, "
                    "but the contract says naive")
        fix = ("write the zone marker where the data is written, or declare "
               f"the zone the data really uses (`time_zone` for {target})")
    elif det.get("doubled_part"):  # path_resolves or exists_per_status
        what, fix = _doubled_words(count, target, det)
    elif det.get("escaped_part"):  # path_resolves
        base = _base_words(det.get("base"))
        what = (f"{_plural(count, 'path')} in {target} "
                f"{_verb(count, 'points', 'point')} outside {base}, through "
                "an absolute path, ~, .. or a link")
        fix = (f"write each path relative to {base}, with no absolute path, "
               "~ or ..; a link that leaves the folder is never followed")
    elif name == "path_resolves":
        base = det.get("base")
        real = det.get("real_base")
        if real and not os.path.isdir(real):
            shown = P._rel(real, root) if _inside(real, root) else real
            what = f"the base folder {base} ({shown}) doesn't exist"
            fix = (f"create it, or point `bases: {base}` in DATA.md at the "
                   "folder the paths are relative to")
        else:
            gone = det.get("unresolved_base_count") or 0
            what = (f"{_plural(gone, 'path')} in {target} {_dont(gone)} "
                    f"exist under {_base_words(base)}" if gone else
                    f"{_plural(count, 'path')} in {target} "
                    f"{_dont(count)} resolve")
            fix = ("check which folder the paths are relative to (`bases:` in "
                   "DATA.md, `relative_to` in the contract) and how the paths "
                   "are written")
    elif name == "exists_per_status":
        bits = []
        if det.get("present_missing"):
            bits.append(f"{_plural(det['present_missing'], 'file')} missing "
                        "where the status says present")
        what = "; ".join(bits) or f"{_plural(count, 'row')} {_dont(count)} match the status"
        fix = ("fetch the missing files again, or correct the status of "
               "those rows")
    elif name == "size_matches_disk":
        unit = det.get("unit")
        twins = det.get("twin_unit_matches") or {}
        if twins:
            twin, n_twin = max(twins.items(), key=lambda kv: kv[1])
            what = (f"{_plural(n_twin, 'value')} of {target} "
                    f"{_verb(n_twin, 'is', 'are')} in {twin}, the contract "
                    f"says {unit}")
            if count > n_twin:
                what += f" ({_n(count - n_twin)} more don't match either)"
            fix = (f"if the data really is in {twin}, set `unit: {twin}` in "
                   "the contract; else convert the sizes where they're written")
        else:
            what = (f"{_plural(count, 'value')} of {target} "
                    f"{_dont(count)} match the file "
                    f"on disk ({unit})")
            fix = "check the unit and how the sizes are rounded where they're written"
    elif name == "columns_present":
        missing = [str(c) for c in (target or [])]
        n = len(missing)
        what = (f"{'column' if n == 1 else 'columns'} {_join(missing)} "
                f"{_verb(n, 'is', 'are')} in the contract but not in the data, "
                f"so the checks on {_verb(n, 'it', 'them')} didn't run")
        fix = (f"add {_verb(n, 'it', 'them')} where the data is written, or "
               f"take {_verb(n, 'it', 'them')} out of the contract if "
               f"{_verb(n, IT_IS, THEY_ARE)} gone for good")
    elif name == "freshness":
        if det.get("value_hidden"):
            what = (f"stale: the newest {target} is more than "
                    f"{det.get('max_age')} before {det.get('checked_at')}")
        elif det.get("max_timestamp"):
            what = (f"stale: the newest {target} is {det['max_timestamp']}, "
                    f"more than {det.get('max_age')} before "
                    f"{det.get('checked_at')}")
        else:
            what = f"no {target} timestamps, so freshness can't be shown"
        fix = "run the load again, or check `freshness_sla` in the contract"
    else:  # pragma: no cover - every assertion the compiler emits is above
        what, fix = result.get("message") or name, "check the contract"
    # freshness reports one newest value, a missing column has no rows
    keys = "" if name in ("freshness", "columns_present") else _keys(result)
    if name == "freshness" and det.get("value_hidden"):
        keys = "; its value isn't shown: marked pii"
    note = report.get("provenance_note") or ""
    if note.startswith("(") and note.endswith(")"):
        note = "; " + note[1:-1]  # one bracket, not two in a row
    elif note:
        note = f"; {note}"
    what += f" ({_where(report, contract)}{keys}{note})"
    return {"dataset": report["dataset"], "assertion": name,
            "target": target, "failed_count": count, "what": what,
            "fix": fix}


def _key_columns(contract: dict) -> list[str]:
    out = []
    for entry in contract.get("keys") or []:
        out.extend(entry if isinstance(entry, list) else [entry])
    return [str(k) for k in out]


def _inside(path: str, root: str) -> bool:
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root
    except ValueError:
        return False


def _physical_tz_words(result: dict, det: dict) -> str | None:
    """A time zone that a Parquet or DuckDB column's type can't show, in
    plain words."""
    if result["assertion"] != "tz_marker" or not det.get("physical_type"):
        return None
    col, tz = result["target"], str(det.get("time_zone") or "")
    where = ("the Parquet file" if "Parquet" in (result.get("message") or "")
             else "the database")
    if tz == "UTC":
        return (f"{col} is stored with no time zone in {where}, so whether "
                "its times are UTC can't be told from the data")
    if tz.startswith("local:"):
        return (f"{col} is stored as an instant (a time with a zone) in "
                f"{where}, so the local zone the contract declares "
                f"({tz.split(':', 1)[1]}) can't be told from the data")
    return None


def _not_verified(report: dict, contract: dict) -> list[str]:
    out = []
    for r in report["assertions"]:
        det = r.get("details") or {}
        if r["status"] == "not_verified":
            out.append(_physical_tz_words(r, det) or r["message"])
        if r["assertion"] == "exists_per_status" and det.get("unconfirmed_todo_rows"):
            statuses = ", ".join(det.get("unconfirmed_todo_statuses") or [])
            out.append(
                f"{_plural(det['unconfirmed_todo_rows'], 'row')} with status "
                f"{statuses} not judged: what the status means is still TODO "
                "in the contract")
    return out


def _entry(report: dict, contract: dict, root: str) -> dict:
    empty = report["source"]["rows"] == 0
    fails = [f for r in report["assertions"] if r["status"] == "failed"
             for f in _failures(r, report, contract, root)]
    note = report.get("provenance_note")
    phrases, nothing = ([], []) if empty else _pass_phrases(report, contract)
    not_verified = [] if empty else _not_verified(report, contract)
    line = None
    if phrases:
        line = (f"{report['dataset']} ({_source_words(report, contract)}): "
                + ", ".join(phrases) + _absent_note(report))
        if nothing:
            line += " · nothing to compare: " + ", ".join(nothing)
        if note:
            line += f" {note}"
    # Read, but nothing in it was compared with the contract: never a pass.
    nothing_checked = not empty and not phrases and not fails and not not_verified
    if nothing_checked:
        why = (("nothing to compare: " + ", ".join(nothing)) if nothing
               else NOTHING_CHECKABLE)
        not_verified = [f"read {_source_words(report, contract)}, but {why}"
                        + (f" {note}" if note else "")]
    return {
        "dataset": report["dataset"],
        "status": "failed" if fails else ("partial" if empty or not_verified
                                          else "passed"),
        "line": line,
        "empty": empty,
        "nothing_checked": nothing_checked,
        "failures": fails,
        "not_verified": not_verified,
        "source": report["source"],
        "contract_status": report["contract_status"],
        "provenance_note": note,
        "assertions": report["assertions"],
        "where": _where(report, contract),
    }


# ---------------------------------------------------------------------------
# Lines (shared by `truequery verify` and the receipt)
# ---------------------------------------------------------------------------


def skipped_lines(doc: dict) -> list[str]:
    return [f"{item['dataset']}: the contract doesn't say where the data is "
            "(add `location.database` for a local database file; remote "
            "databases aren't read until 0.3)" for item in doc["skipped"]]


def not_checked_lines(doc: dict, include_invalid: bool = True,
                      include_skipped: bool = True) -> list[str]:
    """`Not checked` texts for this run (no label). The receipt names
    invalid contracts itself, so it passes include_invalid=False."""
    lines = []
    missing = reader_missing(doc)
    if missing:
        too_old = doc["reader"].get("too_old")
        why = (f"the data reader is too old (DuckDB {doc['reader']['version']})"
               if too_old else "the data reader isn't installed")
        tail = (" (DATA.md says this project doesn't want it: `setup: data: "
                "false`)" if doc.get("reader_declined")
                else f". Run: {RUN_HINT}")
        lines.append(f"data files: {why}{tail} · {_join(missing)}")
    for item in doc["not_checked"]:
        if item["reason"] == "reader_missing":
            continue
        text = f"{item['dataset']}: {item['message']}"
        if item.get("provenance_note"):
            text += f" {item['provenance_note']}"
        lines.append(text)
    for entry in doc["datasets"]:
        if entry["empty"]:
            lines.append(f"{entry['dataset']} ({entry['where']}): no rows, "
                         "so nothing was checked against the contract")
    if include_skipped:
        lines.extend(skipped_lines(doc))
    if include_invalid:
        for problem in doc["invalid_contracts"]:
            lines.append(f"invalid contract: {problem}")
    return lines


def not_verified_lines(doc: dict) -> list[str]:
    return [f"{e['dataset']}: {text}" for e in doc["datasets"]
            for text in e["not_verified"]]


def render_text(doc: dict, verbose: bool = False) -> str:
    """What `truequery verify` prints."""
    lines = ["TrueQuery data check"]
    for entry in doc["datasets"]:
        if entry["line"]:
            lines.append(f"{'Verified':<{LABEL}}{entry['line']}")
    fails = failures(doc)
    for f in fails:
        lines.append(f"{'Found':<{LABEL}}{f['dataset']}: {f['what']}")
        lines.append(f"{'':<{LABEL}}fix: {f['fix']}")
    for text in not_verified_lines(doc):
        lines.append(f"{'Not verified':<{LABEL}}{text}")
    for text in not_checked_lines(doc, include_skipped=False):
        lines.append(f"{'Not checked':<{LABEL}}{text}")
    for text in skipped_lines(doc):
        lines.append(f"{'Skipped':<{LABEL}}{text}")
    if verbose:
        for name, detail in sorted(doc.get("not_checked_detail", {}).items()):
            lines.append(f"{'':<{LABEL}}{name} detail: {detail}")
    if not (doc["datasets"] or doc["not_checked"] or doc["skipped"]
            or doc["invalid_contracts"]):
        lines.append(f"{'Not checked':<{LABEL}}no contract points at local "
                     "data (`kind: files`, or a table with "
                     "`location.database`), so no data was read")
    ev = doc.get("evidence")
    if ev:
        lines.append(f"{'Evidence':<{LABEL}}{ev['path']} "
                     f"(+{_plural(ev['records'], 'record')})")
    lines.append(summary(doc))
    return "\n".join(lines)


def summary(doc: dict) -> str:
    """One line: how many datasets were read and how they did."""
    read = doc["datasets"]
    failed = [e for e in read if e["status"] == "failed"]
    empty = [e for e in read if e["empty"] and e["status"] != "failed"]
    nothing = [e for e in read if e.get("nothing_checked")]
    partial = [e for e in read if e["status"] == "partial" and not e["empty"]
               and not e.get("nothing_checked")]
    if not read:
        head = "no data read"
    elif failed:
        n = len(failures(doc))
        head = (f"{_plural(n, 'data failure')} in {_n(len(failed))} of "
                f"{_plural(len(read), 'dataset')} read")
    elif partial or empty or nothing:
        head = f"{_plural(len(read), 'dataset')} read, no failures"
    else:
        head = (f"{_plural(len(read), 'dataset')} read, "
                + ("it matches its contract" if len(read) == 1
                   else "all match their contracts"))
    if partial:
        head += f"; {_n(len(partial))} checked only in part"
    if empty:
        head += f"; {_n(len(empty))} with no rows"
    if nothing:
        head += f"; {_n(len(nothing))} with nothing to compare"
    parts = [head]
    if doc["not_checked"]:
        parts.append(f"{_n(len(doc['not_checked']))} not checked")
    if doc["skipped"]:
        parts.append(f"{_n(len(doc['skipped']))} skipped (no location)")
    if doc["invalid_contracts"]:
        parts.append(_plural(len(doc["invalid_contracts"]), "invalid contract"))
    return " · ".join(parts)


def receipt_lines(doc: dict) -> dict[str, list[str]]:
    """Texts the receipt places under its own labels."""
    verified = [e["line"] for e in doc["datasets"] if e["line"]]
    return {
        "verified": verified,
        "found": [f"{f['dataset']}: {f['what']}. Fix: {f['fix']}"
                  for f in failures(doc)],
        "not_verified": not_verified_lines(doc),
        "not_checked": not_checked_lines(doc, include_invalid=False),
    }


def utc_now() -> datetime:
    return datetime.now(timezone.utc)
