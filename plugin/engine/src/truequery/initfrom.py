"""`truequery init --from`: draft v2 contracts from data files that exist.

One code path, so the same files give the same drafts everywhere:
- CSV and JSONL are read with the standard library (`csv`, `json`), as
  text. Never DuckDB's sniffer: its drafts would depend on what is
  installed, and it parses naive timestamps as if they were fine.
- Parquet's schema and a sample are read with DuckDB when the data reader
  is installed (locked to the exact files, one thread). Without it, the
  files are listed as not inferred.

Deterministic: files in sorted order (by their `/` path), a fixed sample
(the first `SAMPLE_ROWS` rows of each dataset), `/` in paths and `\\n`
line endings. Nothing here depends on the time, the OS or the locale.

Every contract it writes is `status: draft` with `inferred_from`, passes
`truequery contract validate`, and is never written over an existing one.
The facts a person has to confirm are marked: the grain says TODO, a
status value's meaning is `means: TODO`, and a column with an open
question has `status: draft` and a `# TODO` comment. No values from
columns that look like personal data are written anywhere.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import unicodedata
from datetime import date, datetime

import yaml

from truequery import contract_v2 as C2
from truequery import project as P

SAMPLE_ROWS = 2_000          # rows read per dataset, at most
MAX_LINE_CHARS = 1_048_576   # one physical line, at most
MAX_SAMPLE_CHARS = 64 * 1_048_576  # characters read per dataset, at most
MAX_FILES = 10_000           # data files looked at per run, at most
MAX_VALUES = 20              # distinct values listed as `values`, at most
MAX_VALUE_CHARS = 100        # a longer value is never listed
MAX_PATH_PROBES = 200        # distinct paths looked up on disk per column
MAX_KEY_PAIR_COLUMNS = 15    # columns tried in pairs for a composite key
HEAD_LINES = 50              # lines read to choose a CSV delimiter

FORMATS = {".csv": "csv", ".tsv": "csv", ".jsonl": "jsonl",
           ".ndjson": "jsonl", ".parquet": "parquet"}
NOT_READ = {
    ".json": "JSON documents aren't read by init --from (JSONL is)",
    ".xlsx": "Excel files aren't read",
    ".xls": "Excel files aren't read",
    ".avro": "Avro files aren't read",
    ".orc": "ORC files aren't read",
    ".feather": "Feather files aren't read",
    ".arrow": "Arrow files aren't read",
}
COMPRESSED = (".gz", ".bz2", ".xz", ".zst", ".zip")
SKIP_DIRS = set(P.SKIP_DIRS) | {"__pycache__"}
PARQUET_HINT = "Parquet needs the data reader. Run: truequery setup data"

NULL_TOKENS = frozenset({"NA", "N/A", "n/a", "NULL", "null", "None", "NaN",
                         "nan", "-", "#N/A"})

_INT_RE = re.compile(r"^[+-]?\d+$")
_NUM_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?$")
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2})(?:[.,]\d{1,9})?)?"
    r" ?(Z|z|[+-]\d{2}(?::?\d{2})?)?$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
_PHONE_RE = re.compile(r"^\+?[\d\s().\-]{7,}$")
_DATE_LIKE_RE = re.compile(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}$")
_PERSON_RE = re.compile(r"^[A-Z][a-z]+(?:['-][A-Za-z]+)?"
                        r"(?: [A-Z][a-z]+(?:['-][A-Za-z]+)?){1,2}$")
_PART_RE = re.compile(r"^([^=]+)=(.*)$")
_EXT_RE = re.compile(r"[^/\\]+\.[A-Za-z0-9]{1,8}$")
_PLAIN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_YAML_WORDS = frozenset({"y", "n", "yes", "no", "on", "off", "true", "false",
                         "null"})

# Column-name words. Each list is matched against the name's words
# (split on `_`, `-`, spaces and case changes).
PII_WORDS = frozenset({"email", "mail", "phone", "mobile", "telephone",
                       "surname", "firstname", "lastname", "fullname",
                       "address", "street", "postcode", "zipcode", "zip",
                       "birthdate", "dob", "ssn", "passport", "iban",
                       "username"})
PII_NAME_PREFIXES = frozenset({"first", "last", "full", "given", "family",
                               "middle", "maiden", "customer", "user",
                               "person", "contact", "patient", "employee",
                               "display", "client", "owner", "author",
                               "sender", "recipient"})
PII_PHRASES = (("date", "of", "birth"), ("birth", "date"), ("ip", "address"),
               ("card", "number"), ("account", "number"), ("national", "id"),
               ("postal", "code"))
# Who did something: a person, so personal data (finding: names in
# `assignee`). Not applied to id-named columns (customer_id is a key).
ROLE_WORDS = frozenset({"assignee", "author", "owner", "reviewer",
                        "requester", "approver", "creator", "editor",
                        "recipient", "sender", "customer", "client",
                        "patient", "employee", "user", "contact", "member",
                        "manager", "cell", "tel", "ipv4", "ipv6"})
ROLE_PHRASES = (("created", "by"), ("updated", "by"), ("modified", "by"),
                ("assigned", "to"), ("submitted", "by"))
NOT_PERSONAL_LAST = frozenset({"type", "kind", "count", "file", "files",
                               "status", "flag", "verified", "sent", "opened",
                               "format", "template", "domain", "agent"})
PLACE_WORDS = frozenset({"city", "town", "county", "district", "place",
                         "location", "product", "brand", "company", "team",
                         "department", "store", "vendor", "merchant",
                         "organization", "org", "site", "venue", "stadium",
                         "college", "school", "university", "airport"})
STATUS_WORDS = frozenset({"status", "state", "stage", "outcome", "result",
                          "disposition", "lifecycle"})
CATEGORY_WORDS = STATUS_WORDS | frozenset({"type", "kind", "category",
                                           "class", "level", "tier", "mode",
                                           "reason", "channel", "segment",
                                           "region", "country", "currency"})
PATH_WORDS = frozenset({"path", "file", "filename", "filepath", "dir",
                        "folder"})
KEY_WORDS = frozenset({"id", "key", "uuid", "guid"})
CURRENCY_COLUMNS = ("currency", "currency_code", "ccy")

# Units from a column name's last word(s). Never from the values alone.
UNIT_SUFFIXES = (
    (("bytes",), "bytes"), (("kb",), "KB"), (("kib",), "KiB"),
    (("mb",), "MB"), (("mib",), "MiB"), (("gb",), "GB"), (("gib",), "GiB"),
    (("tb",), "TB"), (("tib",), "TiB"),
    (("ms",), "ms"), (("millis",), "ms"), (("milliseconds",), "ms"),
    (("sec",), "s"), (("secs",), "s"), (("seconds",), "s"),
    (("mins",), "min"), (("minutes",), "min"),
    (("hours",), "h"), (("hrs",), "h"), (("days",), "d"),
    (("pct",), "percent"), (("percent",), "percent"), (("bps",), "bps"),
    (("ratio",), "ratio"),
    (("cents",), "minor"), (("pence",), "minor"), (("minor",), "minor"),
)
WHOLE_UNITS = ("bytes", "minor")  # a fraction means the name is wrong
UNIT_WORDS = {
    "bytes": "bytes", "KB": "KB (1,000 bytes)", "KiB": "KiB (1,024 bytes)",
    "MB": "MB (10^6 bytes)", "MiB": "MiB (2^20 bytes)",
    "GB": "GB (10^9 bytes)", "GiB": "GiB (2^30 bytes)",
    "TB": "TB (10^12 bytes)", "TiB": "TiB (2^40 bytes)",
    "ms": "milliseconds", "s": "seconds", "min": "minutes", "h": "hours",
    "d": "days", "percent": "percent (0-100)", "bps": "basis points",
    "ratio": "a ratio (0-1)", "minor": "minor currency units (cents)",
}
UNIT_TWINS = {"KB": "KiB", "KiB": "KB", "MB": "MiB", "MiB": "MB",
              "GB": "GiB", "GiB": "GB", "TB": "TiB", "TiB": "TB"}


class InitFromError(ValueError):
    """A problem with the request (a path outside the project, ...):
    nothing is written."""


class _LineTooLong(Exception):
    pass


class _Budget(Exception):
    pass


# -- small helpers -----------------------------------------------------------

def _n(count: int) -> str:
    return f"{count:,}"


def _plural(count: int, word: str, many: str | None = None) -> str:
    return f"{_n(count)} {word if count == 1 else (many or word + 's')}"


def _inside(path: str, folder: str) -> bool:
    try:
        return os.path.commonpath([folder, path]) == folder
    except ValueError:
        return False


def _slash(path: str) -> str:
    return path.replace(os.sep, "/")


def _short(value, limit: int = 40) -> str:
    text = str(value)
    text = "".join(ch if ch.isprintable() else "?" for ch in text)
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _words(name: str) -> list[str]:
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [w for w in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if w]


def _has_phrase(words: list[str], phrase: tuple[str, ...]) -> bool:
    size = len(phrase)
    return any(tuple(words[i:i + size]) == phrase
               for i in range(len(words) - size + 1))


def _safe_name(text: str) -> str:
    """An ASCII dataset name: the same on every OS (no NFC/NFD drift)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode(
        "ascii", "ignore").decode("ascii")
    name = re.sub(r"[^a-z0-9_]+", "_", ascii_text.lower())
    name = re.sub(r"_+", "_", name).strip("_")
    if not name:
        return "data"
    if name[0].isdigit():
        name = "d_" + name
    return name


# -- YAML output (by hand, so comments and order are ours) -------------------

def _dq(text: str) -> str:
    """A YAML double-quoted scalar that PyYAML reads back exactly."""
    out = ['"']
    for ch in text:
        code = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif (code < 0x20 or 0x7F <= code <= 0x9F or 0xD800 <= code <= 0xDFFF
              or code in (0x2028, 0x2029, 0xFEFF, 0xFFFE, 0xFFFF)):
            out.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _s(value) -> str:
    """A YAML scalar: plain when that's unambiguous, quoted otherwise."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    text = str(value)
    if _PLAIN_RE.match(text) and text.lower() not in _YAML_WORDS:
        return text
    return _dq(text)


def _comment(text: str) -> str:
    clean = " ".join("".join(ch if ch.isprintable() else " " for ch in text)
                     .split())
    return f"  # {clean}" if clean else ""


def _flow_list(items) -> str:
    return "[" + ", ".join(_s(i) for i in items) + "]"


def render(contract: dict, comments: dict, header: list[str]) -> str:
    """The contract as YAML text. `comments` maps (column, key) or
    (None, key) to a comment for that line."""
    lines = [f"# {h}" if h else "#" for h in header]

    def put(key, value, indent=0, col=None):
        pad = "  " * indent
        note = _comment(comments.get((col, key), ""))
        lines.append(f"{pad}{_s(key)}: {value}{note}")

    for key in ("version", "dataset", "status", "inferred_from", "kind"):
        if key in contract:
            put(key, _s(contract[key]))
    loc = contract.get("location")
    if loc:
        lines.append("location:")
        for key in ("path", "base", "format", "delimiter"):
            if key in loc:
                put(key, _s(loc[key]), 1)
    put("grain", _s(contract["grain"]))
    keys = contract.get("keys") or []
    rendered = ", ".join(_flow_list(k) if isinstance(k, list) else _s(k)
                         for k in keys)
    put("keys", f"[{rendered}]")
    if "partition_key" in contract:
        pk = contract["partition_key"]
        put("partition_key", _flow_list(pk) if isinstance(pk, list) else _s(pk))
    if "quirks" in contract:
        put("quirks", _s(contract["quirks"]))
    columns = contract.get("columns") or {}
    if not columns:
        lines.append("columns: {}")
        return "\n".join(lines) + "\n"
    lines.append("columns:")
    for name, spec in columns.items():
        if not spec:
            lines.append(f"  {_s(name)}: {{}}"
                         + _comment(comments.get((name, None), "")))
            continue
        lines.append(f"  {_s(name)}:" + _comment(comments.get((name, None), "")))
        for key in ("type", "nullable", "unit", "currency_column",
                    "time_zone", "relative_to", "status_column", "pii",
                    "status"):
            if key in spec:
                put(key, _s(spec[key]), 2, name)
        values = spec.get("values")
        if isinstance(values, list):
            put("values", _flow_list(values), 2, name)
        elif isinstance(values, dict):
            lines.append("    values:" + _comment(comments.get((name, "values"), "")))
            for val, meta in values.items():
                inner = ", ".join(f"{_s(k)}: {_s(v)}" for k, v in meta.items())
                lines.append(f"      {_s(val)}: {{{inner}}}")
    return "\n".join(lines) + "\n"


# -- where files may come from -----------------------------------------------

def containers(root: str, config: dict | None) -> list[tuple[str | None, str]]:
    """[(base name or None for the project folder, real folder)], deepest
    first, so a file inside a declared base is described relative to it."""
    from truequery import verify as V  # loads sqlglot; only for init --from
    root_real = os.path.realpath(root)
    bases = (config or {}).get("bases") or {}
    found: list[tuple[str | None, str]] = [(None, root_real)]
    for name in sorted(bases):
        try:
            found.append((name, V.resolve_base_dir(name, bases, root)))
        except (V.VerificationError, ValueError):
            continue
    found.sort(key=lambda c: (-len(c[1]), 0 if c[0] else 1, c[0] or ""))
    return found


def _container_for(real: str, conts) -> tuple[str | None, str] | None:
    for name, folder in conts:
        if _inside(real, folder):
            return name, folder
    return None


def _where(base: str | None) -> str:
    return f"base {base}" if base else "the project folder"


def resolve_sources(root: str, sources: list[str], conts) -> list[tuple]:
    """[(source as typed, real path, base, base folder)]. Raises
    InitFromError naming every source that is missing or outside."""
    out, problems = [], []
    for src in sources:
        # A relative path is looked up in the project folder first, then
        # in the current folder (`truequery init proj --from data`).
        absolute = os.path.abspath(src)
        in_project = os.path.join(os.path.abspath(root), src)
        if not os.path.isabs(src) and os.path.exists(in_project):
            absolute = os.path.abspath(in_project)
        if not os.path.exists(absolute):
            problems.append(f"{src}: no such file or folder")
            continue
        real = os.path.realpath(absolute)
        where = _container_for(real, conts)
        if where is None:
            problems.append(
                f"{src}: outside the project folder and every base declared "
                "in DATA.md, so it isn't read (declare its folder under "
                "`bases:` in DATA.md's tq-config block to use it)")
            continue
        out.append((src, real, where[0], where[1]))
    if problems:
        raise InitFromError("; ".join(problems))
    return out


# -- finding and grouping files ----------------------------------------------

def _walk(real: str, links: list[str]):
    """Files under `real`, sorted. A linked folder isn't followed: its path
    goes into `links`, so the output can say it wasn't read."""
    if os.path.isfile(real):
        yield real
        return
    for dirpath, dirnames, filenames in os.walk(real):
        kept = []
        for d in sorted(dirnames):
            if d.startswith(".") or d in SKIP_DIRS:
                continue
            if os.path.islink(os.path.join(dirpath, d)):
                links.append(os.path.join(dirpath, d))
                continue
            kept.append(d)
        dirnames[:] = kept
        for name in sorted(filenames):
            if not name.startswith("."):
                yield os.path.join(dirpath, name)


def _suffix(name: str) -> tuple[str, str | None]:
    """(format or "", reason it isn't read or None)."""
    stem, ext = os.path.splitext(name)
    ext_l = ext.lower()
    if ext_l in FORMATS:
        return FORMATS[ext_l], None
    if ext_l in NOT_READ:
        return "", NOT_READ[ext_l]
    if ext_l in COMPRESSED:
        inner = os.path.splitext(stem)[1].lower()
        if inner in FORMATS or inner == ".json":
            return "", "compressed files aren't read"
    return "", None


def collect(sources: list[tuple]) -> dict:
    """Group data files into candidate datasets by folder layout."""
    groups: dict[tuple, dict] = {}
    not_read: dict[tuple, dict] = {}
    skipped: list[dict] = []
    seen: set[str] = set()
    capped = False
    count = 0
    for src, real_src, base, folder in sources:
        links: list[str] = []
        for path in _walk(real_src, links):
            real = os.path.realpath(path)
            fmt, reason = _suffix(os.path.basename(path))
            if not fmt and not reason:
                continue
            rel = _slash(os.path.relpath(real, folder)) if _inside(
                real, folder) else None
            shown = _slash(os.path.relpath(path, real_src)) if path != real_src \
                else os.path.basename(path)
            if rel is None:
                skipped.append({"path": f"{src.rstrip('/')}/{shown}"
                                if path != real_src else src,
                                "reason": f"it points outside {_where(base)} "
                                          "(a link), so it isn't read"})
                continue
            if real in seen:
                continue
            seen.add(real)
            if count >= MAX_FILES:
                capped = True
                continue
            count += 1
            label = (f"{base}:" if base else "") + rel
            if any(ch in rel for ch in "*?[]"):
                skipped.append({"path": label, "reason": "its path has glob "
                                "characters (* ? [ ]), which a contract's "
                                "location can't name exactly"})
                continue
            if not C2._is_safe_rel_path(rel):
                skipped.append({"path": label, "reason": "its path can't be "
                                "written as a contract location"})
                continue
            parts = rel.split("/")
            norm, partitions = [], []
            for comp in parts[:-1]:
                match = _PART_RE.match(comp)
                if match and C2.is_valid_ident(match.group(1)):
                    norm.append(f"{match.group(1)}=*")
                    partitions.append((match.group(1), match.group(2)))
                else:
                    norm.append(comp)
            ext = os.path.splitext(parts[-1])[1]
            key = (base or "", "/".join(norm), ext)
            if reason:
                entry = not_read.setdefault(
                    key, {"base": base, "dir": "/".join(norm), "ext": ext,
                          "files": [], "reason": reason})
                entry["files"].append(rel)
                continue
            group = groups.setdefault(
                key, {"base": base, "folder": folder, "dir": "/".join(norm),
                      "ext": ext, "format": fmt, "files": []})
            group["files"].append({"rel": rel, "real": real,
                                   "partitions": partitions})
        for link in links:
            shown = _slash(os.path.relpath(link, real_src))
            skipped.append({"path": f"{src.rstrip('/')}/{shown}",
                            "reason": "it's a link to a folder, which "
                                      "isn't followed"})
    for group in groups.values():
        group["files"].sort(key=lambda f: f["rel"])
    return {"groups": [groups[k] for k in sorted(groups)],
            "not_read": [not_read[k] for k in sorted(not_read)],
            "skipped": skipped, "capped": capped}


def _glob_label(base, folder_glob: str, files: list[str]) -> str:
    where = (f"{base}:" if base else "")
    if len(files) == 1:
        return where + files[0]
    return f"{where}{folder_glob} ({_plural(len(files), 'file')})"


# -- reading CSV -------------------------------------------------------------

def _bounded_lines(handle, budget: list[int]):
    while True:
        line = handle.readline(MAX_LINE_CHARS + 1)
        if not line:
            return
        if len(line) > MAX_LINE_CHARS:
            raise _LineTooLong()
        budget[0] -= len(line)
        if budget[0] < 0:
            raise _Budget()
        yield line


def _choose_delimiter(text: str, ext: str) -> str:
    if ext.lower() == ".tsv":
        return "\t"
    best, best_score = ",", None
    for cand in C2.CSV_DELIMITERS:
        try:
            records = [r for r in csv.reader(io.StringIO(text), delimiter=cand)
                       if r]
        except csv.Error:
            continue
        if not records or len(records[0]) < 2:
            continue
        width = len(records[0])
        same = sum(1 for r in records[1:] if len(r) == width)
        score = (same, width)
        if best_score is None or score > best_score:
            best, best_score = cand, score
    return best


def csv_head(real: str, ext: str) -> dict:
    """{"delimiter", "header"} or {"error"}."""
    budget = [MAX_SAMPLE_CHARS]
    try:
        with open(real, encoding="utf-8-sig", newline="") as handle:
            lines = []
            for line in _bounded_lines(handle, budget):
                lines.append(line)
                if len(lines) >= HEAD_LINES:
                    break
    except UnicodeDecodeError:
        return {"error": "it isn't UTF-8 text"}
    except _LineTooLong:
        return {"error": "its first lines are longer than 1 MB (not CSV?)"}
    except OSError as exc:
        return {"error": f"it can't be read ({exc.strerror or exc})"}
    text = "".join(lines)
    if not text.strip():
        return {"error": "it's empty"}
    delim = _choose_delimiter(text, ext)
    try:
        header = next(r for r in csv.reader(io.StringIO(text), delimiter=delim)
                      if r)
    except (StopIteration, csv.Error):
        return {"error": "it has no header row"}
    return {"delimiter": delim, "header": header}


def csv_sample(files: list[dict], delim: str, header: list[str],
               limit: int = SAMPLE_ROWS) -> dict:
    rows: list[dict] = []
    read_files: list[str] = []
    notes: list[str] = []
    ragged = 0
    more = False
    budget = [MAX_SAMPLE_CHARS]
    for info in files:
        if len(rows) >= limit:
            more = True
            break
        read_files.append(info["rel"])
        part = dict(info["partitions"])
        try:
            with open(info["real"], encoding="utf-8-sig",
                      newline="") as handle:
                reader = csv.reader(_bounded_lines(handle, budget),
                                    delimiter=delim)
                own: list[str] | None = None  # this file's own header
                for record in reader:
                    if own is None:
                        if record:
                            own = record
                        continue
                    if not record:
                        continue
                    width = len(own)
                    if len(rows) >= limit:
                        more = True
                        break
                    if len(record) != width:
                        ragged += 1
                    row = {own[i]: record[i] if i < len(record) else ""
                           for i in range(width)}
                    for key, value in part.items():
                        row.setdefault(key, value)
                    rows.append(row)
        except UnicodeDecodeError:
            notes.append(f"{info['rel']} isn't UTF-8 text after its first "
                         "lines: the sample stops there")
            break
        except _LineTooLong:
            notes.append(f"{info['rel']} has a line longer than 1 MB: the "
                         "sample stops there")
            break
        except _Budget:
            notes.append(f"the sample stopped after "
                         f"{_n(MAX_SAMPLE_CHARS // 1_048_576)} MB of text")
            more = True
            break
        except csv.Error as exc:
            notes.append(f"{info['rel']} can't be parsed as CSV past row "
                         f"{_n(len(rows))} ({exc}): the sample stops there")
            break
        except OSError as exc:
            notes.append(f"{info['rel']} can't be read "
                         f"({exc.strerror or exc})")
            break
    if ragged:
        notes.append(f"{_n(ragged)} of {_plural(len(rows), 'sampled row')} "
                     "have a different number of fields than the header")
    return {"rows": rows, "files": read_files, "more": more, "notes": notes}


# -- reading JSONL -----------------------------------------------------------

def jsonl_sample(files: list[dict], limit: int = SAMPLE_ROWS) -> dict:
    rows: list[dict] = []
    read_files: list[str] = []
    notes: list[str] = []
    bad = not_object = 0
    more = False
    budget = [MAX_SAMPLE_CHARS]
    order: dict[str, None] = {}
    for info in files:
        if len(rows) >= limit:
            more = True
            break
        read_files.append(info["rel"])
        part = dict(info["partitions"])
        try:
            with open(info["real"], encoding="utf-8-sig") as handle:
                for line in _bounded_lines(handle, budget):
                    text = line.strip()
                    if not text:
                        continue
                    if len(rows) >= limit:
                        more = True
                        break
                    try:
                        obj = json.loads(text)
                    except (ValueError, RecursionError):
                        bad += 1
                        continue
                    if not isinstance(obj, dict):
                        not_object += 1
                        continue
                    for key in obj:
                        order.setdefault(key, None)
                    for key, value in part.items():
                        order.setdefault(key, None)
                        obj.setdefault(key, value)
                    rows.append(obj)
        except UnicodeDecodeError:
            notes.append(f"{info['rel']} isn't UTF-8 text: the sample stops "
                         "there")
            break
        except _LineTooLong:
            notes.append(f"{info['rel']} has a line longer than 1 MB: the "
                         "sample stops there")
            break
        except _Budget:
            notes.append(f"the sample stopped after "
                         f"{_n(MAX_SAMPLE_CHARS // 1_048_576)} MB of text")
            more = True
            break
        except OSError as exc:
            notes.append(f"{info['rel']} can't be read "
                         f"({exc.strerror or exc})")
            break
    if bad:
        notes.append(f"{_plural(bad, 'line')} in the sample "
                     f"{'is' if bad == 1 else 'are'} not valid JSON")
    if not_object:
        notes.append(f"{_plural(not_object, 'line')} in the sample "
                     f"{'is' if not_object == 1 else 'are'} not a JSON "
                     "object")
    return {"rows": rows, "files": read_files, "more": more, "notes": notes,
            "header": list(order)}


# -- reading Parquet (DuckDB, only when installed) ---------------------------

def _duckdb_available() -> bool:
    from truequery import verify as V
    return bool(V.duckdb_status().get("installed"))


def parquet_schema(real: str) -> dict:
    from truequery import verify as V
    try:
        con = V.open_duckdb_for_files([real], threads=1)
        try:
            lit = V._sql_string_literal(real)
            desc = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet({lit}, "
                "hive_partitioning=false)").fetchall()
        finally:
            con.close()
    except V.DuckDBMissingError:
        return {"error": PARQUET_HINT}
    except Exception as exc:  # DuckDB raises its own error types
        return {"error": "the data reader can't read it ("
                + _short(type(exc).__name__) + ")"}
    return {"header": [str(r[0]) for r in desc],
            "types": {str(r[0]): str(r[1]).upper() for r in desc}}


def parquet_sample(files: list[dict], header: list[str],
                   limit: int = SAMPLE_ROWS) -> dict:
    from truequery import verify as V
    reals = [f["real"] for f in files]
    notes: list[str] = []
    rows: list[dict] = []
    read: list[str] = []
    more = False
    try:
        con = V.open_duckdb_for_files(reals, threads=1)
    except Exception as exc:  # DuckDB raises its own error types
        return {"rows": [], "files": [], "more": False,
                "notes": ["the data reader couldn't open the files ("
                          + _short(type(exc).__name__) + ")"]}
    try:
        for info in files:  # one file at a time, in order: a stable sample
            if len(rows) >= limit:
                more = True
                break
            lit = V._sql_string_literal(info["real"])
            try:
                # As text: no Python time-zone packages needed, and the
                # same values on every platform.
                result = con.execute(
                    f"SELECT COLUMNS(*)::VARCHAR FROM read_parquet({lit}, "
                    f"hive_partitioning=false) "
                    f"LIMIT {int(limit - len(rows)) + 1}").fetchall()
            except Exception as exc:  # DuckDB raises its own error types
                notes.append(f"{info['rel']} can't be read by the data "
                             f"reader ({_short(type(exc).__name__)})")
                break
            read.append(info["rel"])
            for record in result:
                if len(rows) >= limit:
                    more = True
                    break
                # Each file by its own columns: files of one dataset may
                # order them differently, or lack one.
                row = dict(zip(info.get("own_header") or header, record))
                for key, value in info["partitions"]:
                    row.setdefault(key, value)
                rows.append(row)
    finally:
        con.close()
    return {"rows": rows, "files": read, "more": more, "notes": notes}


PARQUET_TYPES = (
    (("VARCHAR", "STRING", "TEXT", "UUID"), "string"),
    (("BOOLEAN",), "boolean"),
    (("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT",
      "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"), "integer"),
    (("FLOAT", "DOUBLE", "REAL"), "number"),
    (("DATE",), "date"),
)


def _parquet_type(duck: str) -> tuple[str | None, str | None]:
    """(contract type, time zone) for a DuckDB column type."""
    if duck.startswith("DECIMAL"):
        return "number", None
    if duck in ("TIMESTAMP WITH TIME ZONE", "TIMESTAMPTZ"):
        return "timestamp", "UTC"
    if duck.startswith("TIMESTAMP"):
        return "timestamp", "naive"
    for names, kind in PARQUET_TYPES:
        if duck in names:
            return kind, None
    return None, None


# -- inference ---------------------------------------------------------------

def _valid_date(y, m, d) -> bool:
    try:
        date(int(y), int(m), int(d))
        return True
    except ValueError:
        return False


def _valid_ts(groups) -> bool:
    y, mo, d, h, mi, s = groups[:6]
    try:
        datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0))
        return True
    except ValueError:
        return False


def classify(text: str) -> tuple[str, str | None]:
    """(kind, detail) of one non-empty text value. kinds: bool, int, num,
    date, ts, text. detail: the zone marker of a ts (utc, offset, naive)
    or why a number-like value is text (leading_zero, too_long)."""
    if text.lower() in ("true", "false"):
        return "bool", None
    if _INT_RE.match(text):
        digits = text.lstrip("+-")
        if len(digits) > 1 and digits[0] == "0":
            return "text", "leading_zero"
        if len(digits) > 18:
            return "text", "too_long"
        return "int", None
    if _NUM_RE.match(text):
        whole = text.lstrip("+-").split(".")[0].split("e")[0].split("E")[0]
        if len(whole) > 1 and whole[0] == "0":
            return "text", "leading_zero"
        return "num", None
    match = _DATE_RE.match(text)
    if match and _valid_date(*match.groups()):
        return "date", None
    match = _TS_RE.match(text)
    if match and _valid_ts(match.groups()):
        marker = match.group(7)
        if not marker:
            return "ts", "naive"
        if marker in ("Z", "z") or re.fullmatch(r"[+-]00(?::?00)?", marker):
            return "ts", "utc"
        return "ts", "offset"
    return "text", None


def _json_kind(value) -> tuple[str, str | None]:
    if isinstance(value, bool):
        return "bool", None
    if isinstance(value, int):
        return "int", None
    if isinstance(value, float):
        return "num", None
    if isinstance(value, (dict, list)):
        return "nested", None
    kind, detail = classify(value)
    if kind in ("date", "ts"):
        return kind, detail
    return "text", None  # a JSON string stays a string, whatever it holds


def _is_null(value, fmt: str) -> bool:
    if value is None:
        return True
    return fmt == "csv" and value == ""


def profile_column(name: str, values: list, fmt: str,
                   duck_type: str | None = None) -> dict:
    """Facts about one column's sampled values."""
    kinds: dict[str, int] = {}
    zones = {"utc": 0, "naive": 0, "offset": 0}
    details: dict[str, int] = {}
    tokens: dict[str, int] = {}
    non_null: list = []
    nulls = 0
    for value in values:
        if _is_null(value, fmt):
            nulls += 1
            continue
        non_null.append(value)
        if fmt == "parquet":
            continue
        kind, detail = (_json_kind(value) if fmt == "jsonl"
                        else classify(value))
        if kind == "text" and isinstance(value, str) and value in NULL_TOKENS:
            tokens[value] = tokens.get(value, 0) + 1
            kind = "token"
        kinds[kind] = kinds.get(kind, 0) + 1
        if kind == "ts":
            zones[detail] += 1
        elif detail:
            details[detail] = details.get(detail, 0) + 1
    return {"name": name, "nulls": nulls, "non_null": non_null,
            "kinds": kinds, "zones": zones, "details": details,
            "tokens": tokens, "duck_type": duck_type}


def decide_type(prof: dict, fmt: str) -> tuple[str | None, str | None, list]:
    """(type, time zone, [(kind, text)]) for a profiled column."""
    notes: list = []
    if fmt == "parquet":
        kind, zone = _parquet_type(prof["duck_type"] or "")
        if kind is None:
            notes.append(("note", f"its Parquet type ({_short(prof['duck_type'])})"
                                  " has no contract type"))
            return None, None, notes
        if kind == "string":
            text_prof = profile_column(prof["name"], prof["non_null"], "csv")
            kind2, zone2, notes2 = decide_type(text_prof, "csv")
            if kind2 in ("timestamp", "date"):
                return kind2, zone2, notes2
        if zone == "naive":
            notes.append(("question", "the Parquet type has no time zone: "
                          "are these UTC, or local time somewhere (which "
                          "zone)?"))
        return kind, zone, notes
    kinds = dict(prof["kinds"])
    tokens = kinds.pop("token", 0)
    if not kinds and not tokens:
        return None, None, notes
    if "nested" in kinds:
        notes.append(("note", "it holds nested JSON (objects or lists)"))
        return None, None, notes
    found = set(kinds)
    if tokens and found and found <= {"int", "num"}:
        shown = ", ".join(f"'{t}'" for t in sorted(prof["tokens"]))
        notes.append(("question", f"it holds numbers, except "
                      f"{_plural(tokens, 'value')} {shown}: if {shown} means "
                      "empty, set type: number (an empty field is what "
                      "the contract calls missing)"))
        return None, None, notes
    if tokens:
        found.add("text")
    if found == {"bool"}:
        return "boolean", None, notes
    if found == {"int"}:
        return "integer", None, notes
    if found <= {"int", "num"}:
        return "number", None, notes
    if found == {"date"}:
        return "date", None, notes
    if found <= {"ts", "date"}:
        return "timestamp", _zone(prof, notes), notes
    details = prof["details"]
    texts = [v for v in prof["non_null"] if isinstance(v, str)]
    if texts and any("," in v for v in texts) and all(
            re.fullmatch(r"[+-]?\d+(?:,\d+)?", v) for v in texts):
        notes.append(("comment", "numbers with a decimal comma (1,5): text "
                      "unless you read them with decimal=','"))
    elif details.get("leading_zero"):
        notes.append(("comment", "numbers with leading zeros: text, so the "
                      "zeros stay"))
    elif details.get("too_long"):
        notes.append(("comment", "numbers too long for a 64-bit integer: text"))
    return "string", None, notes


def _zone(prof: dict, notes: list) -> str | None:
    zones = dict(prof["zones"])
    zones["naive"] += prof["kinds"].get("date", 0)  # a bare date has no zone
    total = sum(zones.values())
    if zones["utc"] == total:
        return "UTC"
    if zones["naive"] == total:
        notes.append(("question", "no value has a time zone: are these UTC, "
                      "or local time somewhere (which zone)?"))
        return "naive"
    parts = [f"{_n(zones['utc'])} UTC (Z or +00:00)" if zones["utc"] else "",
             f"{_n(zones['naive'])} with no zone" if zones["naive"] else "",
             f"{_n(zones['offset'])} with another offset"
             if zones["offset"] else ""]
    mix = ", ".join(p for p in parts if p)
    if zones["offset"] == total:
        notes.append(("question", "every value has a non-UTC offset (such as "
                      "+02:00): which zone are they local to?"))
    else:
        notes.append(("question", f"the values mix zones ({mix}): which zone "
                      "is right? Until then no time_zone is set"))
        notes.append(("quirk", f"{prof['name']} mixes time zones: {mix}"))
    return None


def is_pii_name(name: str) -> bool:
    words = _words(name)
    if not words:
        return False
    if any(w in PII_WORDS for w in words):
        return True
    if any(_has_phrase(words, p) for p in PII_PHRASES):
        return True
    if words == ["name"] or words == ["ip"]:
        return True
    if words[-1] == "name" and len(words) >= 2 and words[-2] in PII_NAME_PREFIXES:
        return True
    return False


def _mostly(values: list, test) -> bool:
    texts = [v for v in values if isinstance(v, str)]
    return bool(texts) and sum(1 for v in texts if test(v)) * 2 >= len(texts)


def _is_phone(text: str) -> bool:
    if not _PHONE_RE.match(text) or _DATE_LIKE_RE.match(text.strip()):
        return False
    digits = sum(ch.isdigit() for ch in text)
    return 7 <= digits <= 15 and any(ch in text for ch in "+ ()-")


def personal_data_reason(name: str, ctype: str | None,
                         values: list) -> str | None:
    """Why a column looks like personal data, or None. Text columns only:
    a boolean `email_sent` or a count `mail_count` isn't personal data."""
    if ctype not in (None, "string", "path"):
        return None
    words = _words(name)
    by_name = not (set(words) & KEY_WORDS) and not (
        words and words[-1] in NOT_PERSONAL_LAST)
    if by_name and (is_pii_name(name) or set(words) & ROLE_WORDS
                    or any(_has_phrase(words, p) for p in ROLE_PHRASES)):
        return "by its name"
    if _looks_like_emails(values):
        return "its values look like email addresses"
    if _mostly(values, _is_phone):
        return "its values look like phone numbers"
    distinct = {v for v in values if isinstance(v, str)}
    # `country_name`, `subregion1_name`: the name of a thing, not a person.
    thing_name = (len(words) >= 2 and words[-1] == "name"
                  and words[-2] not in PII_NAME_PREFIXES)
    if (len(distinct) >= 2 and not thing_name
            and not set(words) & (CATEGORY_WORDS | PLACE_WORDS)
            and _mostly(sorted(distinct), lambda v: bool(_PERSON_RE.match(v)))):
        return "its values look like people's names"
    return None


def _looks_like_emails(values: list) -> bool:
    texts = [v for v in values if isinstance(v, str)]
    if not texts:
        return False
    hits = sum(1 for v in texts if _EMAIL_RE.match(v))
    return hits * 2 >= len(texts)


def _path_like(values: list, name: str) -> bool:
    texts = [v for v in values if isinstance(v, str)]
    if not texts or len(texts) != len(values):
        return False
    if any("://" in v or "\n" in v for v in texts):
        return False
    words = set(_words(name))
    if words & PATH_WORDS and not all(_INT_RE.match(v) or _NUM_RE.match(v)
                                      for v in texts):
        return True
    return all(("/" in v or "\\" in v) and _EXT_RE.search(v) for v in texts)


LEADING_UNITS = frozenset({"bytes", "KB", "KiB", "MB", "MiB", "GB", "GiB",
                           "TB", "TiB", "ms", "s", "min", "h", "d"})


COUNT_WORDS = frozenset({"count", "cnt", "num", "number", "n"})
MONEY_WORDS = frozenset({"revenue", "sales", "price", "cost", "amount",
                         "spend", "profit", "income", "fee", "fees"})


def _unit_from_name(name: str) -> str | None:
    """The unit a column's name gives: its last word (`size_gib`), or its
    first word for sizes and durations (`bytes_saved`). Never for an ID or
    a count (`kb_article_id`, `tb_count`), and no size unit next to money
    (`revenue_gb` is Great Britain)."""
    words = _words(name)
    if not words or set(words) & (KEY_WORDS | COUNT_WORDS):
        return None
    unit = _unit_word(words)
    if unit in UNIT_TWINS or unit == "bytes":
        if set(words) & MONEY_WORDS:
            return None
    return unit


def _unit_word(words: list[str]) -> str | None:
    for suffix, unit in UNIT_SUFFIXES:
        if (words[-1],) == suffix:
            return unit
    for suffix, unit in UNIT_SUFFIXES:
        if (words[0],) == suffix and unit in LEADING_UNITS and len(words) > 1:
            return unit
    return None


def _has_fraction(values: list) -> bool:
    """True when a value has a fraction, or is written with a decimal
    point (`1.00`): whole units (bytes, cents) are never written so."""
    for value in values:
        if isinstance(value, float) and not value.is_integer():
            return True
        if isinstance(value, str) and ("." in value or "e" in value.lower()):
            return True
    return False


def _hashable(value):
    try:
        hash(value)
        return value
    except TypeError:
        return json.dumps(value, sort_keys=True, default=str)


def candidate_key(rows: list[dict], columns: list[str], types: dict,
                  excluded: set, fmt: str) -> list | None:
    """The first column (id-named first), or else pair of columns, that is
    unique and never empty in the sample. None when none is."""
    if len(rows) < 2:
        return None
    # A number is a key only when its name says so (order_id, key): a
    # measure such as an amount is often unique in a sample by chance.
    eligible = [c for c in columns
                if c not in excluded
                and (types.get(c) in ("string", "path", "date")
                     or (types.get(c) == "integer"
                         and set(_words(c)) & KEY_WORDS))]

    def filled(col):
        return all(not _is_null(r.get(col), fmt) for r in rows)

    def unique(cols):
        seen = set()
        for row in rows:
            item = tuple(_hashable(row.get(c)) for c in cols)
            if item in seen:
                return False
            seen.add(item)
        return True

    full = [c for c in eligible if filled(c)]
    ranked = sorted(full, key=lambda c: (0 if set(_words(c)) & KEY_WORDS
                                         else 1, columns.index(c)))
    for col in ranked:
        if unique([col]):
            return [col]
    pool = full[:MAX_KEY_PAIR_COLUMNS]
    for i, first in enumerate(pool):
        for second in pool[i + 1:]:
            if unique([first, second]):
                return [first, second]
    return None


def _probe_paths(paths: list[str], folders: list[tuple[str, str]]) -> dict:
    """{label: count of sampled relative paths that exist under folder}."""
    from truequery import verify as V
    counts: dict[str, int] = {}
    for label, folder in folders:
        cache: dict = {}
        hits = 0
        for rel in paths:
            target = V.resolve_path_within_base(rel, folder, cache)
            if target is not None and os.path.exists(target):
                hits += 1
        counts[label] = hits
    return counts


# -- one dataset -------------------------------------------------------------

def infer_dataset(ds: dict, root: str, config: dict | None) -> dict:
    """Infer the contract for one grouped dataset. Returns the plan entry:
    {"name", "contract", "comments", "questions", "suggestions", "notes"}."""
    fmt = ds["format"]
    sample = ds["sample"]
    rows = sample["rows"]
    header = ds["header"]
    questions: list[str] = []
    suggestions: list[str] = []
    comments: dict = {}
    quirks: list[str] = list(sample["notes"])
    if ds.get("drift"):
        quirks.append("the files' columns differ: " + ds["drift"])
    columns: dict[str, dict] = {}
    types: dict[str, str] = {}
    profiles: dict[str, dict] = {}
    left_out: list[str] = []
    partition_keys = [k for k, _ in ds["files"][0]["partitions"]] \
        if ds.get("partitioned") else []

    names: list[str] = []
    seen_lower: set[str] = set()
    for col in list(header) + [k for k in partition_keys if k not in header]:
        if not C2.is_valid_ident(col):
            left_out.append(col)
            continue
        if col.lower() in seen_lower:
            left_out.append(col)
            continue
        seen_lower.add(col.lower())
        names.append(col)
    if left_out:
        shown = ", ".join(repr(_short(c, 30)) for c in left_out[:5])
        quirks.append(f"left out of columns: {shown}"
                      + (f" and {len(left_out) - 5} more" if len(left_out) > 5
                         else "")
                      + " (empty, repeated, or with characters a contract "
                      "name can't hold, such as brackets or a leading space)")

    def ask(col, text):
        questions.append(f"{col}: {text}" if col else text)

    for col in names:
        spec: dict = {}
        values = [r.get(col) for r in rows]
        optional_reason = None
        if fmt == "jsonl":
            present = sum(1 for r in rows if col in r)
            if rows and present < len(rows) and col not in partition_keys:
                quirks.append(f"{col} is in {_n(present)} of "
                              f"{_plural(len(rows), 'sampled row')}")
                optional_reason = (f"it's in {_n(present)} of "
                                   f"{_plural(len(rows), 'sampled row')}")
        if col in (ds.get("optional") or {}):
            optional_reason = ds["optional"][col]
        col_fmt = fmt
        if fmt == "parquet" and col not in (ds.get("types") or {}):
            col_fmt = "csv"  # a partition column: text from folder names
        prof = profile_column(col, values, col_fmt,
                              (ds.get("types") or {}).get(col))
        profiles[col] = prof
        ctype, zone, notes = decide_type(prof, col_fmt)
        draft_reasons: list[str] = []
        for kind, text in notes:
            if kind == "question":
                draft_reasons.append(text)
            elif kind == "comment":
                comments[(col, "type")] = text
            elif kind == "quirk":
                quirks.append(text)
            else:
                quirks.append(f"{col}: {text}")
        if ctype is not None:
            spec["type"] = ctype
            types[col] = ctype
        if zone is not None:
            spec["time_zone"] = zone
        reason = personal_data_reason(col, ctype, prof["non_null"])
        pii = reason is not None
        if pii:
            spec["pii"] = True
            draft_reasons.append(
                f"looks like personal data ({reason}): keep pii: true? Its "
                "values are never listed")
        if ctype == "string" and not pii and _path_like(prof["non_null"], col):
            spec["type"] = "path"
            types[col] = "path"
        if optional_reason:
            # Not in every file (or line): empty where it's missing, so
            # `verify` and the scaffolded reader accept the files without it.
            spec["nullable"] = True
            draft_reasons.append(
                f"{optional_reason}: is it optional (empty where it's "
                "missing)? Written as nullable: true")
        if draft_reasons:
            spec["status"] = "draft"
        columns[col] = spec
        for reason in draft_reasons:
            ask(col, reason)

    # Keys and grain.
    excluded = {c for c, s in columns.items() if s.get("pii")}
    key = candidate_key(rows, names, types, excluded, fmt)
    sample_words = (f"the first {_plural(len(rows), 'row')}" if sample["more"]
                    else f"all {_plural(len(rows), 'row')}")
    if key:
        shown = key[0] if len(key) == 1 else f"({', '.join(key)})"
        grain = (f"TODO: one row per {shown}? It is unique and never empty "
                 f"in {sample_words} read")
        keys: list = [key[0]] if len(key) == 1 else [key]
        for col in key:
            columns[col]["nullable"] = False
    else:
        why = ("too few rows to tell" if len(rows) < 2
               else "no column or pair of columns is unique and filled in "
                    f"{sample_words} read")
        grain = f"TODO: one row per what? ({why})"
        keys = []
    questions.insert(0, "grain: " + grain.removeprefix("TODO: ")
                     + ". Confirm the grain and the key")

    # Paths: which folder they're relative to.
    path_cols = [c for c in names if types.get(c) == "path"]
    bases = (config or {}).get("bases") or {}
    for col in path_cols:
        _infer_base(col, profiles[col], columns[col], ds, root, bases,
                    comments, ask, suggestions, quirks)

    # Units from names, confirmed against nothing but the name.
    currency_col = next((c for c in names if c.lower() in CURRENCY_COLUMNS
                         and _currency_values(profiles[c]["non_null"])), None)
    for col in names:
        unit = _unit_from_name(col)
        if unit is None or types.get(col) not in ("integer", "number"):
            continue
        spec = columns[col]
        if unit in WHOLE_UNITS and _has_fraction(profiles[col]["non_null"]):
            spec["status"] = "draft"
            ask(col, f"the name says {UNIT_WORDS[unit]}, but values are "
                "written with a decimal point: which unit is it?")
            continue
        if unit == "minor":
            if currency_col is None:
                spec["status"] = "draft"
                ask(col, "the name says minor currency units (cents): of "
                    "which currency? (adds unit: minor and currency)")
                continue
            spec["unit"] = unit
            spec["currency_column"] = currency_col
        else:
            spec["unit"] = unit
        spec["status"] = "draft"
        twin = UNIT_TWINS.get(unit)
        extra = f", not {twin}" if twin else ""
        text = f"unit from the name: {UNIT_WORDS[unit]}{extra}? Confirm it"
        comments[(col, "unit")] = "TODO: " + text
        ask(col, text)

    # Low-cardinality text as `values`.
    has_path = bool(path_cols)
    for col in names:
        spec = columns[col]
        if types.get(col) != "string" or spec.get("pii") or col in (
                key or []) or col in partition_keys:
            continue
        non_null = profiles[col]["non_null"]
        distinct = sorted({_as_text(v) for v in non_null
                           if isinstance(v, (str, int, float))})
        if not distinct or len(distinct) > MAX_VALUES or any(
                len(v) > MAX_VALUE_CHARS for v in distinct):
            continue
        words = set(_words(col))
        if words & KEY_WORDS and not words & CATEGORY_WORDS:
            continue  # run_id, user_key: an identifier, not a category
        if not (words & CATEGORY_WORDS or len(non_null) >= 2 * len(distinct)):
            continue
        if profiles[col]["nulls"]:
            spec["nullable"] = True
        shown = ", ".join(_short(v, 30) for v in distinct[:8]) + (
            f" and {len(distinct) - 8} more" if len(distinct) > 8 else "")
        if has_path and words & STATUS_WORDS:
            spec["values"] = {v: {"means": "TODO"} for v in distinct}
            spec["status"] = "draft"
            target = path_cols[0] if len(path_cols) == 1 else "the path columns"
            ask(col, f"for each value ({shown}), is the file in {target} "
                "there (present), absent on purpose (absent_expected), or "
                "missing by mistake (absent_unexpected)? Values from "
                f"{sample_words}: there may be others")
            comments[(col, "values")] = ("TODO: what each value means for "
                                         "the file; there may be other values")
        else:
            spec["values"] = distinct
            spec["status"] = "draft"
            comments[(col, "values")] = ("TODO: seen in the sample; are these "
                                         "all? Any other value fails "
                                         "`truequery verify`")
            ask(col, f"values seen in the sample: {shown}. Are these all of "
                "them? Any other value will fail `truequery verify`")

    # One status column with meanings to fill in: it says whether each
    # path's file should exist. Until the meanings are filled in, `verify`
    # reports those rows as not judged instead of as missing files.
    status_cols = [c for c in names
                   if isinstance(columns[c].get("values"), dict)]
    if len(status_cols) == 1:
        for col in path_cols:
            columns[col]["status_column"] = status_cols[0]

    for col, spec in columns.items():
        if spec.get("status") != "draft":
            continue
        if any(k[0] == col and v.startswith("TODO") for k, v in
               comments.items()):
            continue
        first = next((q.split(": ", 1)[1] for q in questions
                      if q.startswith(f"{col}: ")), "")
        if first:
            comments[(col, "status")] = "TODO: " + first

    contract: dict = {"version": 2, "dataset": ds["name"], "status": "draft",
                      "inferred_from": ds["inferred_from"], "kind": "files",
                      "location": ds["location"], "grain": grain,
                      "keys": keys}
    if partition_keys:
        valid = [k for k in partition_keys if k in columns]
        if valid:
            contract["partition_key"] = valid[0] if len(valid) == 1 else valid
    if quirks:
        contract["quirks"] = "From truequery init --from: " + "; ".join(
            dict.fromkeys(quirks)) + "."
    contract["columns"] = columns
    return {"name": ds["name"], "contract": contract, "comments": comments,
            "questions": questions, "suggestions": suggestions,
            "notes": list(dict.fromkeys(quirks))}


def _as_text(value) -> str:
    """A scalar as the data reader shows it in text (JSON true is `true`)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value if isinstance(value, str) else str(value)


def _currency_values(values: list) -> bool:
    texts = [v for v in values if isinstance(v, str)]
    return bool(texts) and all(re.fullmatch(r"[A-Z]{3}", v) for v in texts)


def _infer_base(col, prof, spec, ds, root, bases, comments, ask,
                suggestions, quirks) -> None:
    texts = [v for v in prof["non_null"] if isinstance(v, str)]
    if not texts:
        return
    absolute = [v for v in texts if v.startswith(("/", "\\", "~"))
                or re.match(r"^[A-Za-z]:", v)]
    if absolute:
        quirks.append(f"{col} holds {_plural(len(absolute), 'absolute path')}:"
                      " a base folder doesn't apply to those")
        return
    probes = sorted(set(texts))[:MAX_PATH_PROBES]
    from truequery import verify as V
    folders: list[tuple[str, str]] = []
    for name in sorted(bases):
        try:
            folders.append((name, V.resolve_base_dir(name, bases, root)))
        except (V.VerificationError, ValueError):
            continue
    root_real = os.path.realpath(root)
    folders.append(("", root_real))
    data_dir = os.path.dirname(ds["files"][0]["real"])
    if data_dir != root_real:
        folders.append(("@", data_dir))
    counts = _probe_paths(probes, folders)
    total = len(probes)
    declared = [(n, counts[n]) for n in sorted(bases) if n in counts]
    best_base = max(declared, key=lambda item: item[1], default=(None, 0))
    others = max((counts[label] for label, _ in folders
                  if label in ("", "@")), default=0)
    folder_of = dict(folders)

    def rel_folder(label):
        """How a folder is named in a question or comment: relative to the
        project, or by the base's name alone when it's outside (an absolute
        path would make the contract differ from machine to machine)."""
        if label == "":
            return "the project folder"
        real = folder_of[label]
        if not _inside(real, root_real):
            return "the data's own folder" if label == "@" else label
        shown = _slash(os.path.relpath(real, root_real))
        return f"{shown}/" if label == "@" else f"{label} ({shown}/)"

    if best_base[0] and best_base[1] and best_base[1] >= others:
        spec["relative_to"] = best_base[0]
        spec["status"] = "draft"
        text = (f"{_n(best_base[1])} of {_plural(total, 'sampled path')} "
                f"exist under {rel_folder(best_base[0])}: is that their base?")
        comments[(col, "relative_to")] = "TODO: " + text
        ask(col, text)
        return
    found = [f"{_n(counts[label])} under {rel_folder(label)}"
             for label, _ in folders if counts.get(label)]
    if not found:
        spec["status"] = "draft"
        ask(col, f"none of {_plural(total, 'sampled path')} exist under the "
            "project folder or a declared base: which folder are they "
            "relative to?")
        return
    first_parts = {v.replace("\\", "/").split("/")[0] for v in probes}
    base_names = {os.path.basename(folder_of[n]): n for n, _ in declared}
    wrong = (len(first_parts) == 1 and next(iter(first_parts)) in base_names)
    lead = f"{col}: of {_plural(total, 'sampled path')}, " + ", ".join(found)
    if wrong:
        name = base_names[next(iter(first_parts))]
        suggestions.append(
            f"{lead}; none under {rel_folder(name)}. Every path starts with "
            f"{next(iter(first_parts))}/, the base folder's own name: if they "
            f"are meant to be relative to {name}, whatever writes them adds "
            "the folder twice")
        spec["status"] = "draft"
        ask(col, "every path starts with the base folder's own name: which "
            "folder are they relative to?")
        return
    best_label = max((label for label, _ in folders if label in ("", "@")),
                     key=lambda label: counts.get(label, 0))
    where = ("./" if best_label == "" else
             "./" + _slash(os.path.relpath(folder_of[best_label], root_real)))
    suggestions.append(
        f"{lead}. To check these paths, declare that folder as a base in "
        f"DATA.md's tq-config block (bases: {{data_root: {where}}}), then set "
        f"relative_to: data_root on {col}")


# -- the run -----------------------------------------------------------------

def _existing(root: str) -> tuple[dict, set, list]:
    contracts, _problems = P.load_contracts_reporting(root)
    locations = {}
    for key, contract in contracts.items():
        loc = contract.get("location")
        if isinstance(loc, dict) and loc.get("path"):
            locations[(loc.get("base") or "", str(loc["path"]))] = key
    cdir = os.path.join(root, "contracts")
    files = set()
    if os.path.isdir(cdir):
        files = {os.path.splitext(n)[0].lower() for n in os.listdir(cdir)
                 if n.endswith((".yaml", ".yml"))}
    return contracts, files, locations


def _datasets(collected: dict, not_inferred: list, reader: bool) -> list[dict]:
    """Split each folder group into datasets that share a header."""
    out: list[dict] = []
    for group in collected["groups"]:
        fmt = group["format"]
        files = group["files"]
        heads: dict[tuple, list] = {}
        info_by_sig: dict[tuple, dict] = {}
        if fmt == "parquet" and not reader:
            not_inferred.append({
                "path": _glob_label(group["base"], _join(group["dir"],
                                    "*" + group["ext"]),
                                    [f["rel"] for f in files]),
                "reason": PARQUET_HINT})
            continue
        for info in files:
            if fmt == "csv":
                head = csv_head(info["real"], group["ext"])
                sig_extra = head.get("delimiter")
            elif fmt == "parquet":
                head = parquet_schema(info["real"])
                sig_extra = None
            else:
                head = {"header": []}
                sig_extra = None
            if "error" in head:
                label = (f"{group['base']}:" if group["base"] else "") \
                    + info["rel"]
                not_inferred.append({"path": label, "reason": head["error"]})
                continue
            sig = (sig_extra, tuple(head["header"]))
            info["own_header"] = list(head["header"])  # this file's columns
            heads.setdefault(sig, []).append(info)
            info_by_sig[sig] = head
        sigs = sorted(heads, key=lambda s: heads[s][0]["rel"])
        all_names = [os.path.basename(f["rel"]) for s in sigs for f in heads[s]]
        clusters = _clusters(sigs)
        for cluster in clusters:
            members = sorted((f for sig in cluster for f in heads[sig]),
                             key=lambda f: f["rel"])
            names = [os.path.basename(f["rel"]) for f in members]
            head = _merge_heads(cluster, heads, info_by_sig)
            pattern = _name_pattern(names, all_names, group["ext"],
                                    len(clusters) == 1)
            if pattern is None:
                for info in members:
                    out.append(_dataset(group, [info], head, info["rel"],
                                        partitioned=False))
                continue
            path = _join(group["dir"], pattern)
            partitioned = "=*" in group["dir"]
            out.append(_dataset(group, members, head, path, partitioned))
    return out


SAME_DATASET = 0.5  # share of columns two headers have in common, at least


def _clusters(sigs: list[tuple]) -> list[list[tuple]]:
    """Group headers that describe one dataset: the same delimiter, and at
    least half their columns in common (files of one dataset drift: a
    column added one month). Deterministic: in the order given."""
    parent = list(range(len(sigs)))

    def find(i):
        while parent[i] != i:
            i = parent[i]
        return i

    for i in range(len(sigs)):
        for j in range(i + 1, len(sigs)):
            if sigs[i][0] != sigs[j][0]:
                continue
            a, b = set(sigs[i][1]), set(sigs[j][1])
            if a and b and len(a & b) / len(a | b) >= SAME_DATASET:
                parent[find(j)] = find(i)
    out: dict[int, list] = {}
    for i, sig in enumerate(sigs):
        out.setdefault(find(i), []).append(sig)
    return [out[k] for k in sorted(out)]


def _merge_heads(cluster: list[tuple], heads: dict, info_by_sig: dict) -> dict:
    """One head for a cluster: the columns of its most common header first,
    then the others in the order they appear; a note on the drift."""
    main = max(cluster, key=lambda s: (len(heads[s]), -cluster.index(s)))
    header = list(main[1])
    for sig in cluster:
        for col in sig[1]:
            if col not in header:
                header.append(col)
    merged = dict(info_by_sig[main])
    merged["header"] = header
    types: dict = {}
    for sig in [main] + [s for s in cluster if s != main]:
        for col, kind in (info_by_sig[sig].get("types") or {}).items():
            types.setdefault(col, kind)
    if types:
        merged["types"] = types
    total = sum(len(heads[s]) for s in cluster)
    drift = []
    optional: dict[str, str] = {}
    for col in header:
        count = sum(len(heads[s]) for s in cluster if col in s[1])
        if count < total:
            drift.append(f"{col} is in {_n(count)} of {_n(total)} files")
            optional[col] = f"it's in {_n(count)} of {_n(total)} files"
    merged["optional"] = optional
    if drift:
        merged["drift"] = "; ".join(drift[:5]) + (
            f"; and {len(drift) - 5} more columns differ"
            if len(drift) > 5 else "")
    return merged


def _join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def _name_pattern(member_names: list[str], all_names: list[str], ext: str,
                  alone: bool) -> str | None:
    """The file-name part of a dataset's glob: the name itself, `*.csv`
    when the folder holds only this dataset, or `prefix*suffix` when that
    matches exactly this dataset's files. None: name each file instead."""
    import fnmatch
    names = sorted(set(member_names))
    if alone:
        # One file named like a date or a number (2026-10-01.csv) is the
        # first of a series: name the folder's files, not that one.
        if len(names) == 1 and re.search(r"[A-Za-z]",
                                         os.path.splitext(names[0])[0]):
            return names[0]
        return "*" + ext
    if len(names) == 1:
        pattern = names[0]
    else:
        prefix = os.path.commonprefix(names)
        suffix = os.path.commonprefix([n[::-1] for n in names])[::-1]
        shortest = min(len(n) for n in names)
        if len(prefix) + len(suffix) > shortest:
            suffix = suffix[len(prefix) + len(suffix) - shortest:]
        pattern = f"{prefix}*{suffix}"
    matched = sum(1 for n in all_names if fnmatch.fnmatchcase(n, pattern))
    if matched == len(member_names) and all(
            fnmatch.fnmatchcase(n, pattern) for n in member_names):
        return pattern
    return None


def _dataset(group: dict, members: list[dict], head: dict, path: str,
             partitioned: bool) -> dict:
    location = {"path": path, "format": group["format"]}
    if group["base"]:
        location = {"path": path, "base": group["base"],
                    "format": group["format"]}
    if group["format"] == "csv" and head.get("delimiter") not in (None, ","):
        location["delimiter"] = head["delimiter"]
    return {"group": group, "files": members, "header": head.get("header"),
            "types": head.get("types"), "delimiter": head.get("delimiter"),
            "drift": head.get("drift"), "optional": head.get("optional"),
            "location": location, "format": group["format"],
            "partitioned": partitioned, "base": group["base"]}


def _dataset_name(path: str) -> str:
    parts = path.split("/")
    last = parts[-1]
    if "*" not in path:
        return _safe_name(os.path.splitext(last)[0])
    folders = [p for p in parts[:-1] if "=" not in p]
    if folders:
        return _safe_name(folders[-1])
    stem = os.path.splitext(last)[0].replace("*", "")
    return _safe_name(stem) if len(stem.strip("_-. ")) >= 2 else "data"


def _inferred_from(ds: dict, sample: dict) -> str:
    files = sample["files"]
    total = len(ds["files"])
    rows = len(sample["rows"])
    what = "the header" if ds["format"] == "csv" else "the schema" \
        if ds["format"] == "parquet" else "the keys"
    if not files:
        where = ds["files"][0]["rel"]
        return (f"truequery init --from: {what} of {where}; no rows read "
                f"(sample limit {_n(SAMPLE_ROWS)} rows)")
    shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more"
                                    if len(files) > 3 else "")
    of_total = f" ({_n(len(files))} of {_n(total)} files)" if total > 1 else ""
    first = "the first " if sample["more"] else ""
    return (f"truequery init --from: {what} and {first}{_plural(rows, 'row')} "
            f"of {shown}{of_total}; sample limit {_n(SAMPLE_ROWS)} rows")


def plan(root: str, sources: list[str]) -> dict:
    """Everything `init --from` would write and say, without writing."""
    root = os.path.abspath(root)
    config = P.load_config(root)
    conts = containers(root, config)
    resolved = resolve_sources(root, sources, conts)
    collected = collect(resolved)
    not_inferred: list[dict] = []
    for entry in collected["not_read"]:
        not_inferred.append({
            "path": _glob_label(entry["base"], _join(entry["dir"],
                                "*" + entry["ext"]), entry["files"]),
            "reason": entry["reason"]})
    reader = any(g["format"] == "parquet" for g in collected["groups"]) \
        and _duckdb_available()
    datasets = _datasets(collected, not_inferred, reader)
    contracts, files, locations = _existing(root)
    used: set[str] = set()
    drafts, kept = [], []
    for ds in datasets:
        loc = ds["location"]
        loc_key = (loc.get("base") or "", loc["path"])
        label = (f"{loc['base']}:" if loc.get("base") else "") + loc["path"]
        if loc_key in locations:
            kept.append({"path": label, "contract": locations[loc_key],
                         "reason": "already has a contract"})
            continue
        name = _dataset_name(loc["path"])
        if name in used:
            folders = [p for p in loc["path"].split("/")[:-1] if "=" not in p]
            if len(folders) >= 2:
                name = _safe_name(folders[-2] + "_" + name)
            stem, n = name, 2
            while name in used:
                name, n = f"{stem}_{n}", n + 1
        if name in contracts or name in files:
            kept.append({"path": label, "contract": name,
                         "reason": f"contracts/{name}.yaml or a contract for "
                                   f"a dataset named {name} already exists"})
            used.add(name)
            continue
        used.add(name)
        ds["name"] = name
        if ds["format"] == "csv":
            sample = csv_sample(ds["files"], ds["delimiter"], ds["header"])
        elif ds["format"] == "jsonl":
            sample = jsonl_sample(ds["files"])
            ds["header"] = sample["header"]
            if not sample["rows"]:
                used.discard(name)
                reason = "; ".join(sample["notes"]) or "it has no lines"
                not_inferred.append({"path": label, "reason":
                                     f"no JSON object read ({reason})"})
                continue
        else:
            sample = parquet_sample(ds["files"], ds["header"])
        ds["sample"] = sample
        ds["inferred_from"] = _inferred_from(ds, sample)
        entry = infer_dataset(ds, root, config)
        entry["file"] = f"contracts/{name}.yaml"
        entry["source"] = label
        entry["files"] = len(ds["files"])
        entry["rows"] = len(sample["rows"])
        entry["text"] = _finish(entry, root, config)
        drafts.append(entry)
    for item in collected["skipped"]:
        not_inferred.append(item)
    if collected["capped"]:
        not_inferred.append({"path": "(more files)",
                             "reason": f"only the first {_n(MAX_FILES)} data "
                                       "files are looked at"})
    return {"drafts": drafts, "kept": kept, "not_inferred": not_inferred,
            "sources": [s[0] for s in resolved]}


HEADER = [
    "A draft written by `truequery init --from`. Every fact below was",
    "inferred from a sample of the data; nobody has confirmed it yet. The",
    "TODO lines are the questions: answer them in the /truequery init",
    "interview (or edit this file), then remove `status: draft` from what",
    "you confirmed. The contract stays a draft until the grain is confirmed",
    "and no column is a draft; then delete this comment too.",
]


def _finish(entry: dict, root: str, config: dict | None) -> str:
    """Render, then prove the text reads back as the same contract and
    passes `truequery contract validate`."""
    text = render(entry["contract"], entry["comments"], HEADER)
    parsed = yaml.safe_load(text)
    if parsed != entry["contract"]:
        raise InitFromError(
            f"internal error: the draft for {entry['name']} doesn't read back "
            "as written; nothing was written")
    C2.validate_contract_v2(parsed, entry["file"])
    bases = (config or {}).get("bases") or {}
    problems = C2.check_contract_bases(parsed, bases, entry["file"])
    if problems:
        raise InitFromError("internal error: " + "; ".join(problems))
    return text


def write(root: str, result: dict) -> list[str]:
    """Write the drafts; never over an existing file."""
    written = []
    cdir = os.path.join(root, "contracts")
    os.makedirs(cdir, exist_ok=True)
    for entry in result["drafts"]:
        dest = os.path.join(root, entry["file"])
        try:
            with open(dest, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(entry["text"])
        except FileExistsError:
            entry["skipped"] = True
            continue
        written.append(entry["file"])
    return written


def render_text(result: dict, written: list[str]) -> list[str]:
    lines: list[str] = []
    for entry in result["drafts"]:
        if entry["file"] not in written:
            lines.append(f"kept existing {entry['file']} (a draft for "
                         f"{entry['source']} wasn't written over it)")
            continue
        cols = len(entry["contract"]["columns"])
        lines.append(f"wrote {entry['file']} (draft): {entry['source']}, "
                     f"{_plural(cols, 'column')}, "
                     f"{_plural(entry['rows'], 'row')} read")
    for item in result["kept"]:
        lines.append(f"kept: {item['path']}: {item['reason']}"
                     + (f" (contracts/{item['contract']}.yaml)"
                        if item["reason"] == "already has a contract" else ""))
    questions = [(e["name"], q) for e in result["drafts"]
                 if e["file"] in written for q in e["questions"]]
    if questions:
        lines.append("Questions for the interview (/truequery init), or "
                     "answer them in the contracts:")
        for name, q in questions:
            lines.append(f"  {name}.{q}" if not q.startswith("grain:")
                         else f"  {name}: {q}")
    suggestions = [(e["name"], s) for e in result["drafts"]
                   if e["file"] in written for s in e["suggestions"]]
    if suggestions:
        lines.append("Suggested:")
        for name, s in suggestions:
            lines.append(f"  {name}.{s}")
    if result["not_inferred"]:
        lines.append("Not inferred:")
        for item in result["not_inferred"]:
            lines.append(f"  {item['path']}: {item['reason']}")
    lines.append(
        f"init --from: {_plural(len(written), 'draft contract')} written, "
        f"{_n(len(result['kept']) + len(result['drafts']) - len(written))} "
        f"kept, {_n(len(result['not_inferred']))} not inferred. Not read: "
        "READMEs and data dictionaries (init --from doesn't link docs yet)")
    return lines


def public(result: dict, written: list[str]) -> dict:
    return {
        "drafts": [{"file": e["file"], "dataset": e["name"],
                    "source": e["source"], "files": e["files"],
                    "rows": e["rows"], "questions": e["questions"],
                    "suggestions": e["suggestions"], "notes": e["notes"],
                    "written": e["file"] in written}
                   for e in result["drafts"]],
        "kept": result["kept"],
        "not_inferred": result["not_inferred"],
    }
