"""Assertion compiler for Contract v2, Contract v1, and Metric v1.

Compiles dataset contract facts (`keys`, `columns`, `freshness_sla`) and
metric definitions (`metrics/*.yaml`) into deterministic, dialect-aware SQL
(`duckdb`, `sqlite`) and filesystem assertion specifications.

Safety invariants (PLAN-0.2 §3.1):
- Every SQL statement is constructed as a `sqlglot.exp.Select` AST node and
  emitted with `identify=True` (every table and column identifier quoted).
- Metric `where` clauses are parsed and validated with
  `contract_v2.validate_metric_where` (exact-type `ALLOWED_WHERE_NODES`
  allowlist) and compiled strictly from the parsed AST tree, NEVER from the
  raw `where` string.
- `assert_single_select` verifies that every compiled SQL string parses back
  as a single `exp.Select` statement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp

from truequery import contract_v2
from truequery import project as P

SUPPORTED_DIALECTS = ("duckdb", "sqlite")


@dataclass(frozen=True)
class CompiledAssertion:
    """One compiled assertion derived from a dataset contract."""

    assertion: str
    dataset: str
    dialect: str
    kind: str  # "sql" | "filesystem"
    target: str | tuple[str, ...]
    sql: str | None = None
    count_sql: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


def _count_where(cond: exp.Expression, name: str) -> exp.Expression:
    """`COALESCE(SUM(CASE WHEN cond THEN 1 ELSE 0 END), 0) AS name`: a count
    beside the failed count, in plain SQL every runner dialect reads."""
    case = exp.Case().when(cond, exp.Literal.number(1)).else_(exp.Literal.number(0))
    return exp.func("COALESCE", exp.func("SUM", case), exp.Literal.number(0)).as_(
        exp.to_identifier(name, quoted=True))


def _filled(col_expr: exp.Expression) -> exp.Expression:
    """The value is there: not NULL and not blank."""
    col_txt = exp.cast(col_expr.copy(), exp.DataType.build("VARCHAR"))
    return exp.and_(col_expr.copy().is_(exp.null()).not_(),
                    exp.func("TRIM", col_txt).neq(exp.Literal.string("")))


def _clean_ident_part(raw: str) -> str:
    """Strip surrounding backticks or quotes from a single v1 identifier part."""
    s = raw.strip()
    if (len(s) >= 2
            and s[0] == s[-1]
            and s[0] in ("`", '"', "'")
            and s[0] not in s[1:-1]):
        s = s[1:-1].strip()
    return s


def _quote_col(name: str) -> exp.Column:
    """Build a quoted `exp.Column` (`"col"`) with no table qualifier."""
    return exp.column(exp.to_identifier(_clean_ident_part(name), quoted=True))


def _quote_table(relation: str) -> exp.Table:
    """Build a quoted `exp.Table` for a single or dotted relation name."""
    cleaned = _clean_ident_part(relation)
    parts = [_clean_ident_part(p) for p in cleaned.split(".") if p.strip()]
    if len(parts) == 1:
        return exp.table_(exp.to_identifier(parts[0], quoted=True))
    if len(parts) == 2:
        return exp.table_(
            exp.to_identifier(parts[1], quoted=True),
            db=exp.to_identifier(parts[0], quoted=True),
        )
    return exp.table_(
        exp.to_identifier(parts[-1], quoted=True),
        db=exp.to_identifier(parts[-2], quoted=True),
        catalog=exp.to_identifier(".".join(parts[:-2]), quoted=True),
    )


def _is_numeric_literal(node: exp.Expression | None) -> bool:
    if isinstance(node, exp.Literal) and node.is_number:
        return True
    if isinstance(node, exp.Neg) and isinstance(node.this, exp.Literal):
        return bool(node.this.is_number)
    return False


def _quote_where_ast(node: exp.Expression) -> exp.Expression:
    """Return a copy of a validated `where` AST with every `exp.Column` quoted,
    and numeric comparisons cast to DOUBLE/REAL so `all_varchar=true` CSV/JSONL
    views and SQLite TEXT columns compare numerically."""
    cloned = node.copy()
    for col in list(cloned.find_all(exp.Column)):
        col.set("this", exp.to_identifier(col.name, quoted=True))
        parent = col.parent
        needs_num_cast = False
        if isinstance(parent, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE)):
            other = parent.expression if parent.this is col else parent.this
            needs_num_cast = _is_numeric_literal(other)
        elif isinstance(parent, exp.Between) and parent.this is col:
            needs_num_cast = (
                _is_numeric_literal(parent.args.get("low"))
                or _is_numeric_literal(parent.args.get("high"))
            )
        elif isinstance(parent, exp.In) and parent.this is col:
            exprs = parent.expressions or []
            needs_num_cast = bool(exprs) and all(
                _is_numeric_literal(e) for e in exprs
            )
        if needs_num_cast:
            col.replace(exp.cast(col.copy(), exp.DataType.build("DOUBLE")))
    return cloned


def assert_single_select(sql_text: str, dialect: str = "duckdb") -> exp.Select:
    """Parse `sql_text` and verify it is a single `exp.Select` statement."""
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"unsupported assertion dialect {dialect!r} "
            f"(expected one of {SUPPORTED_DIALECTS})")
    stmts = [s for s in sqlglot.parse(sql_text, read=dialect) if s is not None]
    if len(stmts) != 1 or type(stmts[0]) is not exp.Select:
        raise ValueError(
            f"compiled assertion must be a single SELECT statement, got: "
            f"{sql_text!r}")
    return stmts[0]


def _render_select(select_node: exp.Select, dialect: str) -> str:
    sql_text = select_node.sql(dialect=dialect, identify=True)
    assert_single_select(sql_text, dialect=dialect)
    return sql_text


def _pii_columns(contract: dict) -> frozenset[str]:
    """Return lowercased column names marked `pii: true` in v2 or listed in v1 `pii`."""
    pii: set[str] = set()
    cols = contract.get("columns")
    if isinstance(cols, dict):
        for cname, spec in cols.items():
            if isinstance(spec, dict) and spec.get("pii") is True:
                pii.add(str(cname).lower())
    v1_pii = contract.get("pii")
    if isinstance(v1_pii, list):
        for entry in v1_pii:
            if isinstance(entry, str):
                pii.add(entry.lower())
            elif isinstance(entry, dict) and isinstance(entry.get("column"), str):
                pii.add(entry["column"].lower())
    return frozenset(pii)


def _primary_key_columns(contract: dict) -> tuple[str, ...]:
    """First declared candidate key (as a tuple of column names), or `()`."""
    keys = contract.get("keys") or []
    if not keys:
        return ()
    first = keys[0]
    if isinstance(first, list):
        return tuple(str(k) for k in first)
    return (str(first),)


def _provenance_params(contract: dict) -> dict[str, Any]:
    dataset = str(contract.get("dataset") or contract.get("table") or "")
    is_draft = P.is_draft_contract(contract)
    inferred_from = contract.get("inferred_from")
    note = None
    if is_draft:
        if inferred_from:
            note = (f"(from draft contract {dataset}, "
                    f"inferred from {inferred_from})")
        else:
            note = f"(from draft contract {dataset})"
    return {
        "contract_status": "draft" if is_draft else "confirmed",
        "inferred_from": inferred_from,
        "provenance_note": note,
    }


def _tz_failing_condition(col_expr: exp.Column, tz: str) -> exp.Expression:
    """Return a SQL boolean condition that is TRUE when `col_expr` violates `tz`.

    Works identically in DuckDB and SQLite on text/varchar values:
    - `UTC`: passes if trimmed text ends with `Z`/`z` or `±00:00`/`±0000`/`±00`;
      fails on naive strings or non-zero offsets (`+02:00`, `-05:00`, etc.).
    - `local:<zone>`: passes on naive strings or explicit numeric offsets;
      fails if trimmed text ends with `Z`/`z` (UTC marker in local column).
    - `naive`: passes on naive strings (no marker); fails on `Z`/`z` or any
      numeric offset after the `YYYY-MM-DD` date prefix.
    """
    txt = exp.func("TRIM", exp.cast(col_expr, exp.DataType.build("VARCHAR")))
    up = exp.func("UPPER", txt)
    tail = exp.func("SUBSTR", txt, exp.Literal.number(11))
    is_z = up.like(exp.Literal.string("%Z"))
    is_zero_off = exp.or_(
        txt.like(exp.Literal.string("%+00:00")),
        txt.like(exp.Literal.string("%-00:00")),
        txt.like(exp.Literal.string("%+0000")),
        txt.like(exp.Literal.string("%-0000")),
        txt.like(exp.Literal.string("%+00")),
        txt.like(exp.Literal.string("%-00")),
    )
    has_num_off = exp.or_(
        tail.like(exp.Literal.string("%+%")),
        tail.like(exp.Literal.string("%-%")),
    )
    short_len = exp.func("LENGTH", txt) < exp.Literal.number(10)
    if tz == "UTC":
        return exp.paren(
            exp.or_(short_len, exp.not_(exp.paren(exp.or_(is_z, is_zero_off))))
        )
    if tz.startswith("local:"):
        return exp.paren(exp.or_(is_z, short_len))
    if tz == "naive":
        return exp.paren(exp.or_(is_z, has_num_off, short_len))
    raise ValueError(f"unsupported time_zone {tz!r}")


def _order_exprs_for_cols(cols: tuple[str, ...] | list[str]) -> list[exp.Ordered]:
    return [_quote_col(c).asc() for c in cols]


def compile_contract_assertions(
    contract: dict,
    dialect: str = "duckdb",
    relation: str | None = None,
    sample_limit: int | None = None,
    all_columns: tuple[str, ...] | list[str] | None = None,
) -> list[CompiledAssertion]:
    """Compile a validated contract into dialect-specific assertions."""
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"unsupported assertion dialect {dialect!r} "
            f"(expected one of {SUPPORTED_DIALECTS})")

    dataset = str(contract.get("dataset") or contract.get("table"))
    rel_name = relation
    if not rel_name:
        loc = contract.get("location")
        if (contract.get("kind") == "table"
                and isinstance(loc, dict)
                and loc.get("relation")):
            rel_name = str(loc["relation"])
        else:
            rel_name = dataset
    pii_cols = _pii_columns(contract)
    pk_cols = _primary_key_columns(contract)
    prov = _provenance_params(contract)
    columns = contract.get("columns") if isinstance(contract.get("columns"), dict) else {}

    # Determine deterministic ordering columns for sampling and sample keys
    declared_cols = tuple(str(c) for c in (all_columns or tuple(columns.keys())))
    if pk_cols:
        extra_tie_cols = tuple(
            c for c in declared_cols
            if c.lower() not in {k.lower() for k in pk_cols}
        )
        sample_order_cols: tuple[str, ...] = pk_cols + extra_tie_cols
        sample_order_label = (
            pk_cols[0] if len(pk_cols) == 1 else "(" + ", ".join(pk_cols) + ")"
        )
    else:
        sample_order_cols = declared_cols
        sample_order_label = (
            declared_cols[0]
            if len(declared_cols) == 1
            else ("(" + ", ".join(declared_cols) + ")" if declared_cols else "row")
        )

    base_table = _quote_table(rel_name)
    if sample_limit is not None:
        alias_str = _clean_ident_part(rel_name.split(".")[-1]) or "src"
        sub_select = exp.select(exp.Star()).from_(base_table)
        if sample_order_cols:
            sub_select = sub_select.order_by(*_order_exprs_for_cols(sample_order_cols))
        table_expr: exp.Expression = (
            sub_select
            .limit(int(sample_limit))
            .subquery(alias=exp.to_identifier(alias_str, quoted=True))
        )
    else:
        table_expr = base_table

    sample_meta = {
        "sampled": sample_limit is not None,
        "sample_limit": int(sample_limit) if sample_limit is not None else None,
        "sample_order_by": sample_order_label,
    }

    compiled: list[CompiledAssertion] = []

    # 1. `not_null` on every key column and every column with `nullable: false`
    not_null_cols: list[str] = []
    seen_nn: set[str] = set()
    for key_entry in contract.get("keys") or []:
        key_list = key_entry if isinstance(key_entry, list) else [key_entry]
        for kcol in key_list:
            kstr = str(kcol)
            if kstr.lower() not in seen_nn:
                seen_nn.add(kstr.lower())
                not_null_cols.append(kstr)
    for cname, spec in columns.items():
        if isinstance(spec, dict) and spec.get("nullable") is False:
            if cname.lower() not in seen_nn:
                seen_nn.add(cname.lower())
                not_null_cols.append(cname)

    for col_name in not_null_cols:
        col_expr = _quote_col(col_name)
        is_null_cond = exp.or_(
            col_expr.is_(exp.null()),
            exp.func("TRIM", exp.cast(col_expr, exp.DataType.build("VARCHAR"))).eq(
                exp.Literal.string("")),
        )
        cnt_select = (
            exp.select(exp.func("COUNT", exp.Star()).as_(
                exp.to_identifier("failed_count", quoted=True)))
            .from_(table_expr)
            .where(is_null_cond)
        )
        select_cols = [_quote_col(k) for k in pk_cols] if pk_cols else [col_expr]
        sample_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .where(is_null_cond)
            .order_by(*(c.asc() for c in select_cols))
            .limit(5)
        )
        compiled.append(CompiledAssertion(
            assertion="not_null",
            dataset=dataset,
            dialect=dialect,
            kind="sql",
            target=col_name,
            sql=_render_select(sample_select, dialect),
            count_sql=_render_select(cnt_select, dialect),
            params={
                "column": col_name,
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    col_name.lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 2. `unique` on each candidate key in `keys`
    for key_entry in contract.get("keys") or []:
        key_cols = tuple(
            str(k) for k in (key_entry if isinstance(key_entry, list) else [key_entry])
        )
        if not key_cols:
            continue
        quoted_kcols = [_quote_col(k) for k in key_cols]
        not_null_cond = exp.and_(*(c.is_(exp.null()).not_() for c in quoted_kcols))
        dup_select = (
            exp.select(
                *quoted_kcols,
                exp.func("COUNT", exp.Star()).as_(
                    exp.to_identifier("dup_count", quoted=True)),
            )
            .from_(table_expr)
            .where(not_null_cond)
            .group_by(*quoted_kcols)
            .having(exp.func("COUNT", exp.Star()) > exp.Literal.number(1))
            .order_by(*(c.asc() for c in quoted_kcols))
        )
        dup_subq = dup_select.subquery(alias=exp.to_identifier("dups", quoted=True))
        cnt_select = exp.select(
            exp.func("COUNT", exp.Star()).as_(
                exp.to_identifier("failed_count", quoted=True))
        ).from_(dup_subq)
        sample_select = dup_select.limit(5)
        compiled.append(CompiledAssertion(
            assertion="unique",
            dataset=dataset,
            dialect=dialect,
            kind="sql",
            target=key_cols if len(key_cols) > 1 else key_cols[0],
            sql=_render_select(sample_select, dialect),
            count_sql=_render_select(cnt_select, dialect),
            params={
                "key_columns": key_cols,
                "pii_redacted": any(k.lower() in pii_cols for k in key_cols),
                **sample_meta,
                **prov,
            },
        ))

    # 3. `accepted_values` on columns with `values` (v2) or `allowed_values` (v1)
    value_specs: list[tuple[str, list[str], bool]] = []
    for cname, spec in columns.items():
        if not isinstance(spec, dict) or spec.get("values") is None:
            continue
        vals = spec["values"]
        allowed_list = (
            [str(k) for k in vals.keys()]
            if isinstance(vals, dict)
            else [str(v) for v in vals]
        )
        # Default when `values` is set is nullable=False unless nullable=True is declared
        nullable = spec.get("nullable") is True
        value_specs.append((cname, allowed_list, nullable))

    v1_allowed = contract.get("allowed_values")
    if isinstance(v1_allowed, dict):
        for cname, vals in v1_allowed.items():
            if isinstance(vals, list) and cname not in columns:
                value_specs.append(( str(cname), [str(v) for v in vals], False))

    for cname, allowed_list, nullable in value_specs:
        col_expr = _quote_col(cname)
        col_txt = exp.cast(col_expr, exp.DataType.build("VARCHAR"))
        in_expr = col_txt.isin(*[exp.Literal.string(v) for v in allowed_list])
        if nullable:
            bad_cond = exp.and_(col_expr.is_(exp.null()).not_(), exp.not_(in_expr))
        else:
            bad_cond = exp.or_(col_expr.is_(exp.null()), exp.not_(in_expr))

        # failed, then how many values were compared, then how many of the
        # failures are empty (said as "empty", not "undeclared")
        cnt_select = (
            exp.select(
                _count_where(bad_cond.copy(), "failed_count"),
                _count_where(_filled(col_expr), "compared_count"),
                _count_where(exp.and_(bad_cond.copy(),
                                      exp.not_(_filled(col_expr))),
                             "empty_count"))
            .from_(table_expr)
        )
        if pk_cols:
            select_cols = [_quote_col(k) for k in pk_cols]
            order_cols = [c.asc() for c in select_cols]
        else:
            select_cols = [
                col_txt.as_(exp.to_identifier("__failed_row", quoted=True))
            ]
            order_cols = [col_txt.asc()]
        sample_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .where(bad_cond)
            .order_by(*order_cols)
            .limit(5)
        )
        compiled.append(CompiledAssertion(
            assertion="accepted_values",
            dataset=dataset,
            dialect=dialect,
            kind="sql",
            target=cname,
            sql=_render_select(sample_select, dialect),
            count_sql=_render_select(cnt_select, dialect),
            params={
                "column": cname,
                "allowed_values": allowed_list,
                "nullable": nullable,
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    cname.lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 4. `tz_marker` on timestamp columns with `time_zone`
    for cname, spec in columns.items():
        if not isinstance(spec, dict):
            continue
        tz = spec.get("time_zone")
        if not tz or spec.get("type") != "timestamp":
            continue
        col_expr = _quote_col(cname)
        col_txt = exp.cast(col_expr, exp.DataType.build("VARCHAR"))
        fail_cond = exp.and_(
            col_expr.is_(exp.null()).not_(),
            exp.func("TRIM", col_txt).neq(exp.Literal.string("")),
            _tz_failing_condition(col_expr, str(tz)),
        )
        cnt_select = (
            exp.select(
                _count_where(fail_cond.copy(), "failed_count"),
                _count_where(_filled(col_expr), "compared_count"))
            .from_(table_expr)
        )
        if pk_cols:
            select_cols = [_quote_col(k) for k in pk_cols]
            order_cols = [c.asc() for c in select_cols]
        else:
            select_cols = [
                col_txt.as_(exp.to_identifier("__failed_row", quoted=True))
            ]
            order_cols = [col_txt.asc()]
        sample_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .where(fail_cond)
            .order_by(*order_cols)
            .limit(5)
        )
        compiled.append(CompiledAssertion(
            assertion="tz_marker",
            dataset=dataset,
            dialect=dialect,
            kind="sql",
            target=cname,
            sql=_render_select(sample_select, dialect),
            count_sql=_render_select(cnt_select, dialect),
            params={
                "column": cname,
                "time_zone": str(tz),
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    cname.lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 5. `path_resolves` on `type: path` columns (uses `relative_to` or project root)
    for cname, spec in columns.items():
        if not isinstance(spec, dict) or spec.get("type") != "path":
            continue
        rel_to = spec.get("relative_to")
        status_col = spec.get("status_column")
        status_means: dict[str, str] = {}
        if status_col and isinstance(columns.get(status_col), dict):
            svals = columns[status_col].get("values")
            if isinstance(svals, dict):
                for sval, s_meta in svals.items():
                    if isinstance(s_meta, dict) and s_meta.get("means"):
                        status_means[str(sval)] = str(s_meta["means"])
        path_txt = exp.cast(_quote_col(cname), exp.DataType.build("VARCHAR"))
        select_cols = [_quote_col(k) for k in pk_cols] + [
            path_txt.as_(exp.to_identifier("__path_val", quoted=True))
        ]
        order_cols = [_quote_col(k).asc() for k in pk_cols] + [path_txt.asc()]
        if status_col:
            status_txt = exp.cast(_quote_col(status_col), exp.DataType.build("VARCHAR"))
            select_cols.append(
                status_txt.as_(exp.to_identifier("__status_val", quoted=True))
            )
            order_cols.append(status_txt.asc())
        row_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .order_by(*order_cols)
        )
        compiled.append(CompiledAssertion(
            assertion="path_resolves",
            dataset=dataset,
            dialect=dialect,
            kind="filesystem",
            target=cname,
            sql=_render_select(row_select, dialect),
            params={
                "column": cname,
                "relative_to": str(rel_to) if rel_to else None,
                "status_column": status_col,
                "status_means": status_means,
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    cname.lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 6. `exists_per_status` on `type: path` columns with `status_column`
    for cname, spec in columns.items():
        if not isinstance(spec, dict) or spec.get("type") != "path":
            continue
        status_col = spec.get("status_column")
        if not status_col:
            continue
        status_spec = columns.get(status_col) or {}
        svals = status_spec.get("values") if isinstance(status_spec, dict) else None
        status_means = {}
        if isinstance(svals, dict):
            for sval, s_meta in svals.items():
                if isinstance(s_meta, dict) and s_meta.get("means"):
                    status_means[str(sval)] = str(s_meta["means"])
        if not status_means:
            continue
        path_txt = exp.cast(_quote_col(cname), exp.DataType.build("VARCHAR"))
        status_txt = exp.cast(_quote_col(status_col), exp.DataType.build("VARCHAR"))
        select_cols = [_quote_col(k) for k in pk_cols] + [
            path_txt.as_(exp.to_identifier("__path_val", quoted=True)),
            status_txt.as_(exp.to_identifier("__status_val", quoted=True)),
        ]
        order_cols = (
            [_quote_col(k).asc() for k in pk_cols]
            + [path_txt.asc(), status_txt.asc()]
        )
        row_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .order_by(*order_cols)
        )
        compiled.append(CompiledAssertion(
            assertion="exists_per_status",
            dataset=dataset,
            dialect=dialect,
            kind="filesystem",
            target=cname,
            sql=_render_select(row_select, dialect),
            params={
                "column": cname,
                "relative_to": spec.get("relative_to"),
                "status_column": str(status_col),
                "status_nullable": bool(status_spec.get("nullable") is True),
                "status_means": status_means,
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    cname.lower() in pii_cols
                    or str(status_col).lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 7. `size_matches_disk` on columns with `size_of` and a size `unit`
    for cname, spec in columns.items():
        if not isinstance(spec, dict):
            continue
        size_of = spec.get("size_of")
        unit = spec.get("unit")
        if not size_of or unit not in contract_v2.SIZE_UNITS:
            continue
        path_spec = columns.get(size_of) or {}
        status_col = path_spec.get("status_column") if isinstance(path_spec, dict) else None
        status_means = {}
        if status_col and isinstance(columns.get(status_col), dict):
            svals = columns[status_col].get("values")
            if isinstance(svals, dict):
                for sval, s_meta in svals.items():
                    if isinstance(s_meta, dict) and s_meta.get("means"):
                        status_means[str(sval)] = str(s_meta["means"])
        path_txt = exp.cast(_quote_col(size_of), exp.DataType.build("VARCHAR"))
        size_txt = exp.cast(_quote_col(cname), exp.DataType.build("VARCHAR"))
        select_cols = [_quote_col(k) for k in pk_cols] + [
            path_txt.as_(exp.to_identifier("__path_val", quoted=True)),
            size_txt.as_(exp.to_identifier("__size_val", quoted=True)),
        ]
        order_cols = (
            [_quote_col(k).asc() for k in pk_cols]
            + [path_txt.asc(), size_txt.asc()]
        )
        if status_col:
            status_txt = exp.cast(_quote_col(status_col), exp.DataType.build("VARCHAR"))
            select_cols.append(
                status_txt.as_(exp.to_identifier("__status_val", quoted=True))
            )
            order_cols.append(status_txt.asc())
        row_select = (
            exp.select(*select_cols)
            .from_(table_expr)
            .order_by(*order_cols)
        )
        compiled.append(CompiledAssertion(
            assertion="size_matches_disk",
            dataset=dataset,
            dialect=dialect,
            kind="filesystem",
            target=cname,
            sql=_render_select(row_select, dialect),
            params={
                "column": cname,
                "size_of": str(size_of),
                "unit": str(unit),
                "relative_to": (
                    path_spec.get("relative_to")
                    if isinstance(path_spec, dict) else None
                ),
                "status_column": status_col,
                "status_means": status_means,
                "key_columns": pk_cols,
                "pii_redacted": bool(
                    cname.lower() in pii_cols
                    or str(size_of).lower() in pii_cols
                    or any(k.lower() in pii_cols for k in pk_cols)
                ),
                **sample_meta,
                **prov,
            },
        ))

    # 8. `freshness` when `freshness_sla` is a v2 mapping `{column, max_age}`
    sla = contract.get("freshness_sla")
    if isinstance(sla, dict) and sla.get("column") and sla.get("max_age"):
        fcol = str(sla["column"])
        max_age = str(sla["max_age"])
        fcol_spec = columns.get(fcol) if isinstance(columns.get(fcol), dict) else {}
        fcol_tz = fcol_spec.get("time_zone")
        col_expr = _quote_col(fcol)
        col_txt = exp.cast(col_expr, exp.DataType.build("VARCHAR"))
        ts_select = (
            exp.select(
                col_txt.as_(exp.to_identifier("__ts_val", quoted=True)),
            )
            .from_(table_expr)
            .where(
                exp.and_(
                    col_expr.is_(exp.null()).not_(),
                    exp.func("TRIM", col_txt).neq(exp.Literal.string("")),
                )
            )
        )
        compiled.append(CompiledAssertion(
            assertion="freshness",
            dataset=dataset,
            dialect=dialect,
            kind="sql",
            target=fcol,
            sql=_render_select(ts_select, dialect),
            params={
                "column": fcol,
                "max_age": max_age,
                "time_zone": str(fcol_tz) if fcol_tz else None,
                "key_columns": pk_cols,
                "pii_redacted": True,
                # a pii column's newest value is never shown either
                "value_hidden": fcol.lower() in pii_cols,
                **sample_meta,
                **prov,
            },
        ))

    return compiled


def _build_measure_agg_ast(
    block: dict,
    path: str,
    field_name: str,
    allowed_columns: set[str] | None = None,
) -> exp.Expression:
    """Build a quoted sqlglot aggregate expression from a validated measure block.

    Always parses `where` with `contract_v2.validate_metric_where` and builds
    the SQL expression from the parsed AST tree, never from raw `where` text.
    """
    agg = str(block["agg"])
    col = str(block["column"])
    where_raw = block.get("where")
    where_ast = None
    if where_raw is not None:
        contract_v2.validate_metric_where(
            str(where_raw),
            path=path,
            field_name=f"{field_name}.where",
            allowed_columns=allowed_columns,
        )
        parsed = sqlglot.parse_one(str(where_raw), into=exp.Condition)
        where_ast = _quote_where_ast(parsed)

    if col == "*":
        if agg != "count":
            raise ValueError(f"{path}: {field_name}.column '*' is only valid for count")
        if where_ast is not None:
            inner = exp.Case(
                ifs=[exp.If(this=where_ast, true=exp.Literal.number(1))]
            )
            return exp.Count(this=inner)
        return exp.Count(this=exp.Star())

    if not contract_v2.is_valid_ident(col):
        raise ValueError(
            f"{path}: {field_name}.column {col!r} must be a valid identifier")
    if allowed_columns is not None and col not in allowed_columns:
        raise ValueError(
            f"{path}: {field_name}.column {col!r} is not in contract columns")

    col_node: exp.Expression = _quote_col(col)
    if agg in ("sum", "avg"):
        col_node = exp.cast(col_node, exp.DataType.build("DOUBLE"))
    target_expr: exp.Expression = (
        exp.Case(ifs=[exp.If(this=where_ast, true=col_node)])
        if where_ast is not None
        else col_node
    )

    if agg == "count":
        return exp.Count(this=target_expr)
    if agg == "count_distinct":
        return exp.Count(this=exp.Distinct(expressions=[target_expr]))
    if agg == "sum":
        return exp.Sum(this=target_expr)
    if agg == "avg":
        return exp.Avg(this=target_expr)
    if agg == "min":
        return exp.Min(this=target_expr)
    if agg == "max":
        return exp.Max(this=target_expr)
    raise ValueError(f"{path}: unsupported aggregate {agg!r}")


def _grain_columns_from_metric(
    grain: Any,
    allowed_columns: set[str] | None = None,
    path: str = "<metric>",
) -> list[str]:
    """Extract optional grouping column names from a metric's `grain` field."""
    if isinstance(grain, list):
        cols = [str(g) for g in grain]
        for c in cols:
            if not contract_v2.is_valid_ident(c):
                raise ValueError(f"{path}: grain column {c!r} is not a valid identifier")
            if allowed_columns is not None and c not in allowed_columns:
                raise ValueError(f"{path}: grain column {c!r} is not in contract columns")
        return cols
    if isinstance(grain, str):
        stripped = grain.strip()
        if stripped.lower().startswith("per "):
            candidate = stripped[4:].strip()
            if (contract_v2.is_valid_ident(candidate)
                    and (allowed_columns is None or candidate in allowed_columns)):
                return [candidate]
        elif (contract_v2.is_valid_ident(stripped)
              and (allowed_columns is None or stripped in allowed_columns)):
            return [stripped]
    return []


def compile_metric_sql(
    metric: dict,
    dialect: str = "duckdb",
    relation: str | None = None,
    allowed_columns: set[str] | None = None,
    path: str = "<metric>",
) -> str:
    """Compile a Metric v1 definition into a single quoted `SELECT` statement.

    Enforces `contract_v2.validate_metric` (including the exact-type AST
    allowlist on `where`), quotes every identifier, builds SQL strictly from
    the parsed `sqlglot` AST tree, and verifies the output with
    `assert_single_select`.
    """
    if dialect not in SUPPORTED_DIALECTS:
        raise ValueError(
            f"unsupported assertion dialect {dialect!r} "
            f"(expected one of {SUPPORTED_DIALECTS})")

    validated = contract_v2.validate_metric(
        {k: v for k, v in metric.items() if not str(k).startswith("_")},
        path=path,
        allowed_columns=allowed_columns,
    )
    m_name = str(validated["metric"])
    ds_name = str(relation or validated["dataset"])
    table_expr = _quote_table(ds_name)
    grain_cols = _grain_columns_from_metric(
        validated.get("grain"), allowed_columns=allowed_columns, path=path
    )
    quoted_grains = [_quote_col(g) for g in grain_cols]

    if validated["kind"] == "simple":
        agg_expr = _build_measure_agg_ast(
            validated["measure"],
            path=path,
            field_name="measure",
            allowed_columns=allowed_columns,
        )
        value_expr = agg_expr.as_(exp.to_identifier(m_name, quoted=True))
    else:
        num_expr = _build_measure_agg_ast(
            validated["numerator"],
            path=path,
            field_name="numerator",
            allowed_columns=allowed_columns,
        )
        den_expr = _build_measure_agg_ast(
            validated["denominator"],
            path=path,
            field_name="denominator",
            allowed_columns=allowed_columns,
        )
        ratio_expr = exp.Div(
            this=exp.cast(num_expr, exp.DataType.build("DOUBLE")),
            expression=exp.Nullif(
                this=den_expr, expression=exp.Literal.number(0)
            ),
        )
        value_expr = ratio_expr.as_(exp.to_identifier(m_name, quoted=True))

    select_node = exp.select(*quoted_grains, value_expr).from_(table_expr)
    if quoted_grains:
        select_node = select_node.group_by(*quoted_grains).order_by(
            *(g.asc() for g in quoted_grains)
        )
    return _render_select(select_node, dialect=dialect)
