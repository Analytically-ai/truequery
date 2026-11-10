"""File-level SQL correctness checks, one function per standard record.

Each check takes a parsed statement, a Locator and a CheckContext
(dialect, path, mode, declared keys) and returns a list of
``(node, why, fix)`` tuples; the analyzer attaches rule slug, severity,
and location. Rules are keyed by standard slug — there is no second ID
scheme. Checks only fire where the query objectively returns a wrong
answer, loses data, or is non-deterministic.
"""
import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from truequery import contract_v2


@dataclass(frozen=True)
class CheckContext:
    """What a check may know beyond the statement itself."""
    dialect: str | None = None
    path: str = "<input>"
    mode: str = "build"
    keys: dict = field(default_factory=dict)       # relation -> candidate keys
    contracts: dict = field(default_factory=dict)  # relation -> contract dict
    metrics: dict = field(default_factory=dict)    # metric name -> metric dict
    bases: dict = field(default_factory=dict)      # base name -> {default: ...}

# Functions whose result is non-NULL whenever a fallback is non-NULL:
# a nullable-side column wrapped in one is NULL-tolerant.
TOLERANT_FNS = frozenset({
    "coalesce", "ifnull", "nvl", "nvl2", "if", "zeroifnull",
})
NONDETERMINISTIC_FNS = frozenset({
    "uuid", "uuid_string", "rand", "random", "uniform", "randstr",
    "now", "current_timestamp", "current_time", "current_date",
    "sysdate", "getdate", "newid",
})
# Set-returning items whose rows come from the left side: joining one
# without ON is idiomatic, not cartesian.
_GENERATOR_NODES = (exp.Unnest, exp.GenerateSeries, exp.Explode, exp.Lateral)
_GENERATOR_FNS = frozenset({"unnest", "generate_series", "flatten",
                            "explode", "range"})


def _is_generator(item):
    """The FROM item is a set-returning function (UNNEST, FLATTEN,
    generate_series ...), judged on the AST: a table that merely has
    `range` in its name is still a table."""
    for node in (item, getattr(item, "this", None)):
        if isinstance(node, _GENERATOR_NODES):
            return True
        if isinstance(node, exp.Func) and node.sql_name().lower() in _GENERATOR_FNS:
            return True
    return False


def _join_side(join):
    """LEFT / RIGHT / FULL for outer joins, "" for inner and cross joins.
    Read from the AST, never from the join's text (a column called
    `left_id` in an ON clause does not make the join outer)."""
    side = (join.args.get("side") or "").upper()
    kind = (join.args.get("kind") or "").upper()
    if kind in ("SEMI", "ANTI"):
        return ""  # LEFT SEMI/ANTI joins add no nullable columns
    return side if side in ("LEFT", "RIGHT", "FULL") else ""


def _ident_key(ident):
    """Canonical key for one identifier, from the AST — never printed SQL.

    Unquoted (and quoted all-lowercase) identifiers compare
    case-insensitively ("U:o"); other quoted ones compare exactly ("Q:O"). Missing
    identifiers key as "" (falsy, like the old sentinel)."""
    if ident is None:
        return ""
    if isinstance(ident, exp.Identifier):
        name = ident.name
        # A quoted all-lowercase name is the same relation as the
        # unquoted one ("orders" = orders); other quoted names are exact.
        if ident.args.get("quoted") and name != name.lower():
            return "Q:" + name
        return "U:" + name.lower()
    text = str(ident)
    return "U:" + text.lower() if text else ""


def _col_key(col):
    """Canonical key for a column reference: qualifier + name."""
    return _ident_key(col.args.get("table")) + "." + _ident_key(col.args.get("this"))


def _alias_key(node):
    """Alias name of a FROM item, without any column-alias list.

    `g(d)` keys as the alias `g`, never the printed `g(d)`; quoted
    aliases keep their exact case. Shared by every check that
    resolves aliases."""
    if node is None:
        return ""
    alias = node.args.get("alias") if hasattr(node, "args") else None
    if alias is not None:
        inner = alias.args.get("this")
        if isinstance(inner, exp.Identifier):
            return _ident_key(inner)
    this = node.args.get("this") if hasattr(node, "args") else None
    if isinstance(this, exp.Identifier):
        return _ident_key(this)
    name = node.name if hasattr(node, "name") else ""
    return "U:" + name.lower() if name else ""


def _alias_display(node):
    """Human-readable alias for messages (no key prefixes)."""
    name = getattr(node, "alias_or_name", "") or ""
    return name or "?"


# -- sql/not-in-nullable-subquery --------------------------------------------
def _all_literal_projections(scope):
    """Every branch of the subquery projects a non-null literal (plain
    SELECT 1, or UNION branches of literals). Literals are never NULL,
    so NOT IN over them cannot be poisoned."""
    selects = list(scope.find_all(exp.Select))
    if not selects:
        return False
    for sel in selects:
        proj = (sel.expressions or [None])[0]
        col = proj.unalias() if hasattr(proj, "unalias") else proj
        if not isinstance(col, exp.Literal):
            return False
    return True


def _eq_pins_non_null(eq, key):
    """An equality conjunct pins the column when it must hold TRUE: the
    other side is a non-NULL literal or another column (NULL = anything
    is unknown, so surviving rows have both sides non-null)."""
    if not isinstance(eq, exp.EQ):
        return False
    left, right = eq.left, eq.right
    if isinstance(left, exp.Column) and _col_key(left) in key:
        other = right
    elif isinstance(right, exp.Column) and _col_key(right) in key:
        other = left
    else:
        return False
    if isinstance(other, exp.Column):
        return True
    # Any literal pins (strings and numbers are values); NULL parses as
    # exp.Null, never exp.Literal.
    return isinstance(other, exp.Literal)


def _sub_never_null(sel):
    """The subquery's projected column is provably non-null from the file:
    a COUNT (never NULL), a WHERE equality / IS NOT NULL on it, or an
    INNER-join equality on it (only rows with both sides non-null join)."""
    proj = (sel.expressions or [None])[0]
    col = proj.unalias() if hasattr(proj, "unalias") else proj
    if isinstance(col, exp.Count):
        return True
    if not isinstance(col, exp.Column):
        return False
    # With a single FROM source, `o.customer_id` and `customer_id` are the
    # same column: accept either spelling.
    key = {_col_key(col)}
    if not sel.args.get("joins"):
        key |= {"." + _ident_key(col.args.get("this"))}
        if col.args.get("table") is None:
            frm = sel.args.get("from_")
            if frm is not None and frm.this is not None:
                key.add(_alias_key(frm.this) + "." + _ident_key(col.args.get("this")))
    where = sel.args.get("where")
    if where is not None:
        for operand in _and_operands(where.this):
            node = operand
            while isinstance(node, exp.Paren):
                node = node.this
            if _is_not_null_test(node, key):
                return True
            if isinstance(node, exp.Not):
                continue
            if _eq_pins_non_null(node, key):
                return True
    for join in sel.args.get("joins", []):
        # Plain JOIN and explicit INNER JOIN filter NULLs on equality;
        # anything with a side (LEFT/RIGHT/FULL), a method (NATURAL) or
        # a non-INNER kind (CROSS) does not.
        method = (join.args.get("method") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        side = (join.args.get("side") or "").upper()
        if side or method not in ("", "INNER") or kind not in ("", "INNER"):
            continue
        on = join.args.get("on")
        if on is None:
            continue
        for operand in _and_operands(on):
            node = operand
            while isinstance(node, exp.Paren):
                node = node.this
            if _eq_pins_non_null(node, key):
                return True
    return False


def _is_not_null_test(node, key):
    """`col IS NOT NULL` (sqlglot: NOT (col IS NULL)) on exactly this column."""
    if not isinstance(node, exp.Not):
        return False
    inner = node.this
    while isinstance(inner, exp.Paren):
        inner = inner.this
    return (isinstance(inner, exp.Is)
            and isinstance(inner.this, exp.Column)
            and _col_key(inner.this) in key
            and isinstance(inner.expression, exp.Null))


def _set_branches(node):
    """The SELECT branches of a (possibly nested) set operation."""
    if isinstance(node, exp.Subquery):
        return _set_branches(node.this)
    if isinstance(node, exp.SetOperation):
        return _set_branches(node.this) + _set_branches(node.expression)
    return [node] if isinstance(node, exp.Select) else []


def _sub_nullable(scope):
    """The subquery may yield NULL: some branch projects a column with no
    guard that must hold for every surviving row (a top-level AND
    conjunct, never one OR branch)."""
    if _all_literal_projections(scope):
        return False
    branches = _set_branches(scope)
    return not branches or any(not _sub_never_null(b) for b in branches)


def not_in_nullable_subquery(stmt, loc, ctx):
    out = []
    for not_n in stmt.find_all(exp.Not):
        inner = not_n.this
        while isinstance(inner, exp.Paren):
            inner = inner.this
        if not isinstance(inner, exp.In):
            continue
        sub = inner.args.get("query") or inner.find(exp.Subquery) \
            or inner.find(exp.Select)
        if sub is None or not _sub_nullable(sub):
            continue
        out.append((not_n,
                    "a NULL in the subquery makes NOT IN return zero rows",
                    "rewrite as NOT EXISTS, or add WHERE col IS NOT NULL"))
    return out


# -- sql/unguarded-update-delete ----------------------------------------------
def _or_operands(node):
    if isinstance(node, exp.Or):
        yield from _or_operands(node.left)
        yield from _or_operands(node.right)
    else:
        yield node


def _is_tautology(cond):
    while isinstance(cond, exp.Paren):
        cond = cond.this
    if isinstance(cond, exp.Boolean) and cond.this is True:
        return True
    if isinstance(cond, exp.Or):
        # X OR TRUE is TRUE (three-valued logic: TRUE absorbs anything)
        if any(_is_tautology(branch) for branch in _or_operands(cond)):
            return True
    if isinstance(cond, exp.EQ):
        left, right = cond.left, cond.right
        if isinstance(left, exp.Literal) and isinstance(right, exp.Literal):
            return left.name == right.name
    return False


def _under_merge(node):
    parent = getattr(node, "parent", None)
    while parent is not None:
        if isinstance(parent, exp.Merge):
            return True
        parent = getattr(parent, "parent", None)
    return False


def _dml_sources(node):
    """Extra source tables of cartesian DML: DELETE .. USING, UPDATE .. FROM."""
    sources = []
    if isinstance(node, exp.Delete):
        for table in node.args.get("using") or []:
            sources.append(table)
    if isinstance(node, exp.Update):
        frm = node.args.get("from_")
        if frm is not None:
            sources.append(frm.this)
    return [t for t in sources if t is not None]


def _is_down_migration(path):
    """Down/rollback migrations intentionally reset whole tables, so a
    missing WHERE is the intent, not an accident. Matched on whole path
    names (`000157_x.down.sql`, `x_rollback.sql`, a `down/` directory),
    so `markdown/` and `drill-down.sql` do not count. Change mode alone is not enough (up-migrations in
    Change mode still get checked)."""
    parts = (path or "").replace("\\", "/").lower().split("/")
    if set(parts[:-1]) & {"down", "rollback", "rollbacks"}:
        return True
    return bool(re.search(r"(^|[._])(down|rollback)\.sql$", parts[-1]))


def unguarded_update_delete(stmt, loc, ctx):
    if _is_down_migration(ctx.path):
        return []
    out = []
    for node in list(stmt.find_all(exp.Delete)) + list(stmt.find_all(exp.Update)):
        if isinstance(node, exp.Update) and _under_merge(node):
            continue  # MERGE ... WHEN MATCHED THEN UPDATE is guarded by ON
        where = node.args.get("where")
        if where is None:
            out.append((node, "no WHERE: every row is affected",
                        "add a WHERE predicate, or use TRUNCATE if you mean the whole table"))
        elif _is_tautology(where.this):
            out.append((node, "WHERE is a constant-true tautology",
                        "replace with a real predicate"))
        else:
            # Cartesian DML: the target is never linked to the source,
            # so the write hits every row (or every filtered-source row).
            target = _alias_key(node.this) if node.this is not None else ""
            for source in _dml_sources(node):
                name = _alias_key(source)
                if not target or not name or name == target:
                    continue
                # An unqualified column can only be the target's (or is
                # ambiguous and the engine says so): it links too.
                linked = any(name in _qualifiers(op)
                             and (target in _qualifiers(op)
                                  or _links_unqualified(op))
                             for op in _and_operands(where.this))
                if not linked:
                    src, tgt = _alias_display(source), _alias_display(node.this)
                    out.append((node, f"no predicate links {src} to {tgt}: "
                                      "the write is cartesian",
                                f"link them (WHERE {tgt}.<key> = {src}.<key>) "
                                "or drop the unused source"))
                    break
    return out


# -- sql/accidental-cross-join --------------------------------------------------
def _same_column_eq(cond):
    """`x = x` (same qualifier and name, modulo folding) — a tautology
    by meaning. Shared with the ON-tautology check below."""
    node = cond
    while isinstance(node, exp.Paren):
        node = node.this
    if not isinstance(node, exp.EQ):
        return False
    if not (isinstance(node.left, exp.Column) and isinstance(node.right, exp.Column)):
        return False
    return _col_key(node.left) == _col_key(node.right)


def accidental_cross_join(stmt, loc, ctx):
    out = []
    for join in stmt.find_all(exp.Join):
        if _is_generator(join.this):
            continue
        if "__tq_jinja" in join.sql().lower():
            continue  # a dbt macro expands here: the join is unknowable
            # from the file, and no finding goes out without evidence
        method = (join.args.get("method") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if (method == "CROSS" or kind == "CROSS") and _written_cross(join, loc):
            continue  # an explicit CROSS JOIN is intentional
        if method == "NATURAL" or kind == "NATURAL":
            continue  # owned by sql/natural-join-explicit-keys
        if join.args.get("using"):
            continue  # JOIN ... USING names explicit keys
        right_alias = _alias_key(join.this)
        sel = _join_scope(join)
        others = _from_keys(sel, join, right_alias) if sel is not None else set()
        if _is_correlated(join.this, others, ctx.dialect):
            continue  # LATERAL / function over earlier columns: linked
        constraint = join.args.get("on")
        if constraint is not None:
            if _is_tautology(constraint) or _same_column_eq(constraint):
                out.append((join, "join condition is always true",
                            "add the real join predicate or use CROSS JOIN explicitly"))
                continue
            if _on_links_both_sides(join, _alias_key(join.this)):
                continue
            out.append((join, "join predicate references only one side: "
                              "no linking predicate (cartesian)",
                        "link both sides with ON <keys>, or use CROSS JOIN explicitly"))
            continue
        this_alias = _alias_key(join.this)
        if not _joined_on_ast(join, this_alias):
            out.append((join, f"no predicate links {_alias_display(join.this)}: "
                              "every row pairs with every row on the other side",
                        "add ON <keys>, or CROSS JOIN if cartesian is intended"))
    return out


_JOIN_WORD = re.compile(r",|\bJOIN\b", re.IGNORECASE)


def _written_cross(join, loc):
    """The join was written as CROSS JOIN, not as a comma. Several
    dialects (BigQuery, Spark, SQLite, ClickHouse) parse `FROM a, b`
    into the same tree as `a CROSS JOIN b`, so read the source text
    just before the joined item. Unknown position: trust the tree."""
    starts = [n.meta["start"] for n in join.this.walk()
              if isinstance(n, exp.Identifier)
              and isinstance(n.meta.get("start"), int)]
    if not starts:
        return True
    before = loc.text[:min(starts)]
    hits = list(_JOIN_WORD.finditer(before))
    if not hits:
        return True
    last = hits[-1]
    if last.group(0) == ",":
        return False
    return bool(re.search(r"\bCROSS\s+$", before[:last.start()],
                          re.IGNORECASE))


def _and_operands(node):
    if isinstance(node, exp.And):
        yield from _and_operands(node.left)
        yield from _and_operands(node.right)
    else:
        yield node


def _links_unqualified(node):
    """A predicate comparing two or more columns where one is
    unqualified (`ON customer_id = c.id`): the unqualified one resolves
    against the other FROM items, so it can link them."""
    cols = list(node.find_all(exp.Column))
    return len(cols) >= 2 and any(c.args.get("table") is None for c in cols)


def _qualifiers(node):
    return {_ident_key(c.args.get("table")) for c in node.find_all(exp.Column)}


def _join_scope(join):
    """The query level a join belongs to: its SELECT, or the UPDATE /
    DELETE whose FROM / USING holds it. sqlglot hangs Postgres
    `UPDATE t ... FROM a JOIN b` and `DELETE FROM t USING a JOIN b`
    joins on the FROM / USING table, with no SELECT around them."""
    node = getattr(join, "parent", None)
    while node is not None:
        if isinstance(node, exp.Select):
            return node
        if isinstance(node, (exp.Update, exp.Delete)):
            return node
        node = getattr(node, "parent", None)
    return None


def _dml_from_keys(dml, join, this_alias):
    """Alias keys an UPDATE / DELETE join can link to: the target, the
    FROM / USING items and their other joins."""
    items = [dml.this]
    sources = dml.args.get("from_") if isinstance(dml, exp.Update) else None
    if sources is not None:
        items.append(sources.this)
        items.extend(sources.expressions or [])
    items.extend(dml.args.get("using") or [])
    others = set()
    for item in items:
        if item is None:
            continue
        name = _alias_key(item)
        if name and name != this_alias:
            others.add(name)
        for other in item.args.get("joins") or []:
            name = _alias_key(other.this) if other is not join else ""
            if name and name != this_alias:
                others.add(name)
    return others


def _from_keys(sel, join, this_alias):
    """Alias keys of every top-level FROM item except this one."""
    if isinstance(sel, (exp.Update, exp.Delete)):
        return _dml_from_keys(sel, join, this_alias)
    others = set()
    frm = sel.args.get("from_")
    if frm is not None and frm.this is not None:
        # Top-level FROM item only (a derived table keys as its alias,
        # never the tables nested inside it).
        name = _alias_key(frm.this)
        if name and name != this_alias:
            others.add(name)
    for other in sel.args.get("joins", []):
        name = _alias_key(other.this) if other is not join else ""
        if name and name != this_alias:
            others.add(name)
    return others


def _joined_on_ast(join, this_alias):
    """True when some predicate links this table to another FROM table,
    judged on column qualifiers — never on text substrings."""
    sel = _join_scope(join)
    if sel is None:
        return False
    others = _from_keys(sel, join, this_alias)
    if not this_alias or not others:
        return False
    where = sel.args.get("where")
    if where is None:
        return False
    for operand in _and_operands(where.this):
        quals = _level_qualifiers(operand)
        if (this_alias in quals and quals & others) or (
                "" in quals and _links_unqualified(operand)):
            return True
    return False


def _is_correlated(item, others, dialect=None):
    """The joined item reads columns of earlier FROM items (LATERAL
    subquery, UNNEST(a.col), table function over a column). That
    correlation IS the link, so ON TRUE is idiomatic, not cartesian."""
    if not others:
        return False
    # BigQuery-style implicit UNNEST: `FROM t, t.array_col AS x` names a
    # column path of an earlier FROM item, so it is correlated too. Only
    # for BigQuery or an unknown dialect: elsewhere `sales.customers` next
    # to an alias `sales` is a schema-qualified table.
    if (dialect in (None, "bigquery") and isinstance(item, exp.Table)
            and _ident_key(item.args.get("db")) in others):
        return True
    return any(_ident_key(col.args.get("table")) in others
               for col in item.find_all(exp.Column))


def _level_qualifiers(node):
    """Qualifier keys of columns at the node's own query level. Columns
    inside nested subqueries live in a different scope: their qualifiers
    (or lack of one) say nothing about how the outer tables link, so
    `ON x.id NOT IN (SELECT uid ...)` never links the outer table."""
    quals = set()
    for col in node.find_all(exp.Column):
        probe = col
        nested = False
        while probe is not node and probe is not None:
            probe = getattr(probe, "parent", None)
            if isinstance(probe, exp.Select):
                nested = True
                break
        if not nested:
            quals.add(_ident_key(col.args.get("table")))
    return quals


def _on_links_both_sides(join, right_alias):
    """An ON clause links the joined table if some AND-branch references
    it with a real predicate. The other side may be unqualified
    (`ON key = t.key` can only resolve against the remaining FROM scope),
    so requiring both qualifiers misfires on ordinary joins. `ON a.flag
    = 1` still crosses: it never references the joined table at all."""
    sel = _join_scope(join)
    if sel is None or not right_alias:
        return False
    others = _from_keys(sel, join, right_alias)
    if not others:
        return False
    on = join.args.get("on")
    if on is None:
        return False
    for operand in _and_operands(on):
        quals = _level_qualifiers(operand)
        if right_alias in quals and (quals & others or "" in quals):
            return True
        if quals == {""} and _links_unqualified(operand):
            return True  # ON customer_id = cust_id
    return False


# -- sql/left-join-null-rejecting-where -----------------------------------------
# General rule: any predicate on a nullable-side column that is
# NULL-rejecting counts — comparisons, BETWEEN, LIKE/ILIKE, IN, and
# non-tolerant function calls. NULL-tolerant: IS NULL (anti-join),
# IS NOT NULL (explicit), COALESCE/IFNULL/NVL wrappers, OR ... IS NULL,
# IS NOT DISTINCT FROM.
def _nullable_aliases(join, left_alias):
    side = _join_side(join)
    right = _alias_key(join.this)
    if side == "LEFT":
        return {right} - {""}
    if side == "RIGHT":
        return {left_alias} - {""}
    if side == "FULL":
        return {left_alias, right} - {""}
    return set()


def _side_exposed(side, nullable):
    if isinstance(side, exp.Column):
        return _ident_key(side.args.get("table")) in nullable
    if isinstance(side, exp.Func):
        if side.sql_name().lower() in TOLERANT_FNS:
            return False
        return any(_side_exposed(a, nullable) for a in side.args.values()
                   if isinstance(a, exp.Expression))
    if isinstance(side, (exp.Paren, exp.Cast, exp.Neg)):
        kids = side.args.get("this")
        return _side_exposed(kids, nullable) if isinstance(kids, exp.Expression) else False
    if isinstance(side, exp.Binary):
        return _side_exposed(side.left, nullable) or _side_exposed(side.right, nullable)
    return False


_COMPARISONS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.NEQ)


def _pred_rejects(pred, nullable):
    """True when pred is NULL-rejecting on a nullable-side column."""
    if isinstance(pred, exp.Is):
        return False  # IS NULL (anti-join) and IS NOT NULL (explicit) are intentional
    if isinstance(pred, exp.NullSafeEQ):
        # Null-tolerant between two values — but `col IS NOT DISTINCT
        # FROM <non-null literal>` is false for NULL, so it rejects
        # NULLs exactly like `=` and kills the outer join.
        sides = [pred.args.get(k) for k in
                 ("this", "expression", "left", "right")]
        sides = [s for s in sides
                 if isinstance(s, exp.Expression)]
        exposed = any(isinstance(s, exp.Column)
                      and _ident_key(s.args.get("table")) in nullable
                      for s in sides)
        literal = any(isinstance(s, exp.Literal) for s in sides)
        return exposed and literal
    if isinstance(pred, _COMPARISONS):
        return (_side_exposed(pred.left, nullable)
                or _side_exposed(pred.right, nullable))
    if isinstance(pred, exp.Between):
        return _side_exposed(pred.args.get("this"), nullable)
    if isinstance(pred, (exp.Like, exp.ILike)):
        return _side_exposed(pred.args.get("this"), nullable)
    if isinstance(pred, exp.In):
        if pred.args.get("not_") and (pred.find(exp.Subquery) or pred.find(exp.Select)):
            return False  # owned by sql/not-in-nullable-subquery
        return _side_exposed(pred.args.get("this"), nullable)
    if isinstance(pred, exp.Func):
        if isinstance(pred, exp.Connector):
            return False  # AND/OR structure, not a function call
        if pred.sql_name().lower() in TOLERANT_FNS:
            return False
        return any(_side_exposed(a, nullable) for a in pred.args.values()
                   if isinstance(a, exp.Expression))
    return False


def left_join_null_rejecting_where(stmt, loc, ctx):
    out = []
    for sel in stmt.find_all(exp.Select):
        joins = sel.args.get("joins", [])
        if not joins or sel.args.get("where") is None:
            continue
        where = sel.args.get("where")
        frm = sel.args.get("from_")
        left_alias = _alias_key(frm.this) if frm is not None and frm.this is not None else ""
        nullable = set()
        for join in joins:
            nullable |= _nullable_aliases(join, left_alias)
        if not nullable:
            continue
        for pred in where.find_all(*_COMPARISONS, exp.Between, exp.Like,
                                   exp.ILike, exp.In, exp.Func,
                                   exp.NullSafeEQ):
            if _owning_select(pred) is not sel:
                continue  # a subquery (EXISTS ...) has its own scope
            if _under_tolerant(pred, where):
                continue  # COALESCE(LOWER(o.x), 'none'): NULL handled
            if not _pred_rejects(pred, nullable):
                continue
            if _or_null_tolerant(pred, where):
                continue
            out.append((pred,
                        "WHERE rejects NULLs on the nullable side: "
                        "outer join silently becomes inner",
                        "move the filter into the ON clause to keep "
                        "unmatched rows (or add OR right.key IS NULL: test "
                        "the join key, not the filtered column)"))
            break
    return out


def _under_tolerant(pred, where):
    node = pred.parent
    while node is not None and node is not where:
        if isinstance(node, exp.Func) and node.sql_name().lower() in TOLERANT_FNS:
            return True
        node = getattr(node, "parent", None)
    return False


def _or_null_tolerant(pred, where):
    node = pred.parent
    while node is not None and node is not where:
        if isinstance(node, exp.Or):
            return node.find(exp.Is) is not None
        node = getattr(node, "parent", None)
    return False


# -- sql/null-equality-comparison --------------------------------------------------
def _under_dml_write(pred):
    node = pred.parent
    child = pred
    while node is not None:
        if isinstance(node, (exp.Update, exp.OnConflict)):
            return True  # SET assignments (incl. upserts) are not comparisons
        if isinstance(node, exp.Merge):
            # SET assignments are; the ON clause is a real comparison
            return child is not node.args.get("on")
        if isinstance(node, (exp.Select, exp.Where)):
            return False
        child = node
        node = getattr(node, "parent", None)
    return False


def null_equality_comparison(stmt, loc, ctx):
    out = []
    for pred in list(stmt.find_all(exp.EQ)) + list(stmt.find_all(exp.NEQ)):
        if _under_dml_write(pred):
            continue  # SET assignments (UPDATE/MERGE) are not comparisons
        if any(isinstance(x, exp.Null) for x in (pred.left, pred.right)):
            out.append((pred, "= NULL is never true (use IS NULL)",
                        "rewrite as IS NULL / IS NOT NULL"))
    for case in stmt.find_all(exp.Case):
        # Simple CASE uses equality: WHEN NULL never matches. Searched
        # CASE (no operand) with WHEN x IS NULL is the fix, not a hit.
        if case.args.get("this") is None:
            continue
        for cond in case.args.get("ifs", []):
            if isinstance(cond.this, exp.Null):
                out.append((cond, "WHEN NULL never matches (CASE uses "
                                  "equality, and NULL = anything is unknown)",
                            "use a searched CASE with WHEN x IS NULL"))
                break
    return out


# -- sql/count-star-after-left-join --------------------------------------------------
def _outer_joins(sel):
    return [join for join in sel.args.get("joins", []) if _join_side(join)]


def _cte_source(stmt, name):
    """Base table of the CTE called name (lowercased) when the CTE keeps
    that table's grain (one FROM table, no joins, no GROUP BY), else None.
    Searches the whole statement tree: files often parse as one large
    statement with CTEs nested at several levels."""
    for cte in stmt.find_all(exp.CTE):
        if _alias_key(cte).split(":", 1)[-1] != name:
            continue
        inner = cte.this
        if not isinstance(inner, exp.Select):
            return None
        if inner.args.get("joins") or inner.args.get("group"):
            return None  # may fan out or aggregate: not the base's grain
        from_ = inner.args.get("from_")
        table = from_.this if from_ is not None else None
        if isinstance(table, exp.Table) and table.name:
            return ".".join(p for p in (table.db, table.name) if p).lower()
    return None


def _rhs_base_table(join, stmt):
    """Base table name behind a join's right side, lowercased. A table
    resolves by its own name (never its alias: `customers c` is
    customers), schema-qualified when written so; a name that is a CTE
    resolves to the CTE's source table. A derived table resolves through
    its alias only when that names a CTE."""
    rhs = join.this
    if rhs is None:
        return ""
    if isinstance(rhs, exp.Table) and rhs.name:
        name = ".".join(p for p in (rhs.db, rhs.name) if p).lower()
        return _cte_source(stmt, rhs.name.lower()) or name
    name = _alias_key(rhs).split(":", 1)[-1]
    return (_cte_source(stmt, name) or "") if name else ""


def _on_columns_for(join, rhs_alias):
    """Nullable-side columns constraining the join to at most one row:
    the USING list, plus equalities that are top-level AND conjuncts of
    the ON predicate. An OR anywhere at the top level (or a bare OR
    predicate) matches several rows, so it contributes nothing."""
    cols = set()
    using = join.args.get("using") or []
    for ident in using:
        cols.add(ident.name.lower() if hasattr(ident, "name") else "")
    on = join.args.get("on")
    if on is None:
        return cols - {""}
    if isinstance(on, exp.Or):
        return cols - {""}
    for conj in _and_operands(on):
        if not isinstance(conj, exp.EQ):
            continue
        for col in conj.find_all(exp.Column):
            table = col.args.get("table")
            tkey = _ident_key(table) if table is not None else ""
            aname = tkey.split(":", 1)[-1]
            if aname == rhs_alias:
                cols.add(col.name.lower())
    return cols - {""}


def _is_lookup_join(join, stmt, keys):
    """The ON predicate covers one declared candidate key of the nullable
    side, so the join matches at most one row and COUNT(*) stays correct."""
    rhs = join.this
    if rhs is None:
        return False
    alias = _alias_key(rhs).split(":", 1)[-1]
    base = _rhs_base_table(join, stmt)
    if not base:
        return False
    # Plain and schema-qualified spellings share one entry: contracts
    # declare `fx_rates`, SQL may say `raw.fx_rates`.
    candidates = keys.get(base, []) + keys.get(base.split(".")[-1], [])
    covered = _on_columns_for(join, alias)
    return any(key <= covered for key in candidates)


def _owning_select(node):
    """Nearest enclosing Select, or None. Query-level attribution: a
    COUNT(*) belongs to the SELECT it is written in, not to a sibling
    CTE or an outer query."""
    node = getattr(node, "parent", None)
    while node is not None:
        if isinstance(node, exp.Select):
            return node
        node = getattr(node, "parent", None)
    return None


def count_star_after_left_join(stmt, loc, ctx):
    keys = ctx.keys
    out = []
    seen = set()
    for count in stmt.find_all(exp.Count):
        # NOTE: grouped counts fire too (the record's proof is grouped:
        # childless groups report 1, not 0).
        arg = count.this
        if not (isinstance(arg, exp.Star) or (
                isinstance(arg, exp.Literal) and arg.name in ("1",))):
            continue
        # Same query level only: a COUNT(*) in an unjoined pre-agg CTE
        # must not fire for a LEFT JOIN in the outer query, and vice versa.
        sel = _owning_select(count)
        if sel is None or not _outer_joins(sel):
            continue
        if id(sel) in seen:
            continue
        seen.add(id(sel))
        # Lookup suppression: when every outer join at this level
        # matches at most one row (ON covers a declared key of the
        # nullable side), COUNT(*) counts preserved rows correctly.
        outer = _outer_joins(sel)
        if keys and outer and all(
                _is_lookup_join(join, stmt, keys) for join in outer):
            loc.suppressed.append("sql/count-star-after-left-join")
            continue
        alias = _alias_display(outer[0].this) if outer else "child"
        out.append((count,
                    "COUNT(*) over an outer join counts 1 for unmatched rows",
                    f"count the nullable-side key instead, e.g. "
                    f"COUNT({alias}.<key>)"))
    return out


# -- sql/nondeterministic-merge-keys --------------------------------------------------
def nondeterministic_merge_keys(stmt, loc, ctx):
    out = []
    for merge in stmt.find_all(exp.Merge):
        on = merge.args.get("on")
        if on is None:
            continue
        fired = False
        for func in on.find_all(exp.Func):
            if func.sql_name().lower() in NONDETERMINISTIC_FNS:
                out.append((func,
                            "MERGE key uses a nondeterministic function: re-runs match differently",
                            "key the MERGE on stable source columns"))
                fired = True
                break
        if fired:
            continue
        # nondeterminism can hide one level down: USING (SELECT uuid() AS
        # uid ...) with ON comparing that alias.
        using = merge.args.get("using")
        if using is None:
            continue
        volatile = set()
        for sel in using.find_all(exp.Select):
            for proj in sel.expressions:
                alias = proj.args.get("alias")
                if alias is not None and proj.find(exp.Func) is not None and any(
                        f.sql_name().lower() in NONDETERMINISTIC_FNS
                        for f in proj.find_all(exp.Func)):
                    volatile.add(alias.name.lower())
        if not volatile:
            continue
        for col in on.find_all(exp.Column):
            if col.name.lower() in volatile:
                out.append((col,
                            "MERGE key derives from a nondeterministic function: "
                            "re-runs match differently",
                            "key the MERGE on stable source columns"))
                break
    return out


# -- sql/set-operation-column-position --------------------------------------------------
def _branch_projection_star(branch):
    """True when a set-operation branch projects a bare `*` (SELECT * or
    t.*) in its select list. Stars nested inside expressions — COUNT(*),
    f(*) — say nothing about positional fragility."""
    selects = [branch] if isinstance(branch, exp.Select) else (
        list(branch.find_all(exp.Select)) if isinstance(branch, exp.Expr)
        else [])
    for sel in selects:
        for e in sel.expressions:
            node = e.this if isinstance(e, exp.Alias) else e
            if isinstance(node, exp.Star):
                return True
            if isinstance(node, exp.Column) and isinstance(node.this, exp.Star):
                return True  # t.*
    return False


def set_operation_column_position(stmt, loc, ctx):
    out = []
    for union in stmt.find_all(exp.Union, exp.Except, exp.Intersect):
        branches = [union.this, union.args.get("expression")]
        if any(isinstance(b, exp.Select) and b.args.get("from_") is None
               for b in branches if b is not None):
            continue  # VALUES branches have no positional fragility
        if any(b is not None and _branch_projection_star(b)
               for b in branches):
            out.append((union,
                        "set operation branches use SELECT *: positional mapping scrambles on reorder",
                        "list columns explicitly in the same order, or use BY NAME where supported"))
    return out


# -- sql/average-of-ratios (heuristic, off by default) ----------------------------------
def average_of_ratios(stmt, loc, ctx):
    out = []
    for avg in stmt.find_all(exp.Avg):
        arg = avg.this
        if isinstance(arg, exp.Div):
            out.append((avg,
                        "AVG(a/b) weights every group equally; usually SUM(a)/SUM(b) is meant",
                        "rewrite as SUM(num)/SUM(den), or weight explicitly"))
    return out


# -- sql/between-date-on-timestamp --------------------------------------------------
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
# Column types are not in the file, so the column's name decides: these
# read as timestamps. A DATE column (order_date) is left alone, because
# BETWEEN over dates is correct.
_TIMESTAMP_NAME = re.compile(r"(_at|_ts|_time|time|timestamp|_dt)$", re.IGNORECASE)


def _date_only_bound(bound):
    if isinstance(bound, exp.Cast) and bound.to.is_type("date"):
        bound = bound.this  # DATE '2025-03-31'
    return isinstance(bound, exp.Literal) and bool(_DATE_ONLY.match(bound.name or ""))


def between_date_on_timestamp(stmt, loc, ctx):
    out = []
    for between in stmt.find_all(exp.Between):
        col = between.this
        if not (isinstance(col, exp.Column) and _TIMESTAMP_NAME.search(col.name)):
            continue
        if _date_only_bound(between.args.get("high")):
            out.append((between,
                        "BETWEEN with a date-only upper bound on a timestamp "
                        "drops everything after midnight on the last day",
                        "use a half-open range: >= start AND < day-after-end"))
            break
    return out


# -- sql/distinct-dedup-bandaid (heuristic, off by default) ------------------------------
def distinct_dedup_bandaid(stmt, loc, ctx):
    out = []
    for sel in stmt.find_all(exp.Select):
        if sel.args.get("distinct") is None:
            continue
        agg_inside = sel.find(exp.Sum, exp.Count, exp.Avg) is not None
        agg_outside = False
        if not agg_inside:
            # DISTINCT in a derived table feeding an outer aggregate:
            # SELECT SUM(x) FROM (SELECT DISTINCT ...) 
            node = sel.parent
            while node is not None and not isinstance(node, exp.Select):
                node = getattr(node, "parent", None)
            if isinstance(node, exp.Select) and node is not sel:
                agg_outside = node.find(exp.Sum, exp.Count, exp.Avg) is not None
        if agg_inside or agg_outside:
            out.append((sel,
                        "SELECT DISTINCT over aggregates usually papers over a join fanout",
                        "fix the fanout (pre-aggregate before joining) instead of deduplicating"))
            break
    return out


# -- sql/insert-explicit-column-list --------------------------------------------------
def insert_explicit_column_list(stmt, loc, ctx):
    out = []
    for insert in stmt.find_all(exp.Insert):
        if insert.args.get("default"):
            continue  # INSERT INTO t DEFAULT VALUES: no values, no order
        if isinstance(insert.this, exp.Table):
            out.append((insert,
                        "INSERT without a column list breaks silently when columns are added or reordered",
                        "name the columns: INSERT INTO t (a, b) SELECT ..."))
    return out


# -- sql/integer-division-truncation --------------------------------------------------
def _is_int_literal(node):
    return isinstance(node, exp.Literal) and re.fullmatch(r"\d+", node.name or "")


# Engines where `/` between two integers returns a non-integer result.
# Everywhere else (Postgres, SQL Server, SQLite, Redshift, Trino, and an
# unknown dialect) `1 / 2` truncates to 0.
_TRUE_DIVISION_DIALECTS = frozenset({
    "bigquery", "clickhouse", "databricks", "doris", "duckdb", "hive",
    "mysql", "oracle", "snowflake", "spark", "spark2", "starrocks",
})


def integer_division_truncation(stmt, loc, ctx):
    """Known recall limit: division between *columns* (count(x)/count(*))
    is not flagged — column types are unknowable statically. Only
    integer literals are caught: `/` where the dialect truncates, and the
    explicit integer-division operator `//` / DIV everywhere. The receipt
    lists column-typed division under 'not checked'."""
    out = []
    true_division = (ctx.dialect or "").lower() in _TRUE_DIVISION_DIALECTS
    for div in stmt.find_all(exp.Div, exp.IntDiv):
        if not (_is_int_literal(div.left) and _is_int_literal(div.right)):
            continue
        if isinstance(div, exp.IntDiv):
            out.append((div, "integer division truncates (5 // 2 = 2)",
                        "use / with a decimal operand if a fraction is meant"))
        elif not true_division:
            out.append((div,
                        "integer/integer division truncates (1 / 2 = 0) in "
                        "Postgres, SQL Server, SQLite, Redshift and Trino",
                        "multiply by 1.0 or cast one side to a float/decimal"))
    return out


# -- sql/keyset-pagination (heuristic, off by default) ----------------------------------
def keyset_pagination(stmt, loc, ctx):
    out = []
    for sel in stmt.find_all(exp.Select):
        if sel.args.get("offset") is not None and sel.args.get("limit") is not None:
            out.append((sel,
                        "LIMIT+OFFSET pages shift under concurrent writes",
                        "paginate by keyset: WHERE id > ? ORDER BY id LIMIT ?"))
    return out


# -- sql/limit-requires-order-by (heuristic, off by default) ------------------------------
def limit_requires_order_by(stmt, loc, ctx):
    out = []
    for sel in stmt.find_all(exp.Select):
        if sel.args.get("limit") is not None and sel.args.get("order") is None:
            # subqueries feeding an ordered outer query are fine
            parent = sel.parent
            if isinstance(parent, exp.Subquery) and isinstance(parent.parent, exp.Select):
                continue
            out.append((sel,
                        "LIMIT without ORDER BY returns an arbitrary, unstable row set",
                        "add ORDER BY a unique key"))
    return out


# -- sql/natural-join-explicit-keys --------------------------------------------------
def natural_join_explicit_keys(stmt, loc, ctx):
    out = []
    for join in stmt.find_all(exp.Join):
        if (join.args.get("method") or "").upper() == "NATURAL":
            out.append((join,
                        "NATURAL JOIN matches on every same-named column, including future ones",
                        "name the keys: JOIN ... ON / USING"))
    return out


# -- sql/negative-filter-drops-nulls --------------------------------------------------
def _has_tolerant_wrapper(pred):
    return any(isinstance(f, exp.Func) and f.sql_name().lower() in TOLERANT_FNS
               for f in pred.find_all(exp.Func))


def negative_filter_drops_nulls(stmt, loc, ctx):
    out = []
    for pred in list(stmt.find_all(exp.NEQ)) + list(stmt.find_all(exp.Not)):
        if isinstance(pred, exp.NEQ) and any(
                isinstance(x, exp.Null) for x in (pred.left, pred.right)):
            continue  # `x <> NULL` belongs to null-equality-comparison
        if _has_tolerant_wrapper(pred):
            continue  # COALESCE-class wrappers keep NULLs explicitly
        if isinstance(pred, exp.Not):
            inner = pred.this
            while isinstance(inner, exp.Paren):
                inner = inner.this
            if not isinstance(inner, (exp.Like, exp.ILike, exp.In)):
                continue
            if isinstance(inner, exp.In) and (
                    inner.find(exp.Subquery) or inner.find(exp.Select)):
                continue  # owned by sql/not-in-nullable-subquery
        if _or_null_sibling(pred):
            continue  # `... OR col IS NULL` keeps NULLs explicitly
        out.append((pred,
                    "negative filters (<>, NOT LIKE, NOT IN-list) silently drop NULL rows",
                    "add OR col IS NULL when NULLs should be kept"))
    return out


def _or_null_sibling(pred):
    node = pred.parent
    while node is not None and not isinstance(node, (exp.Where, exp.Select)):
        if isinstance(node, exp.Or):
            return node.find(exp.Is) is not None
        node = getattr(node, "parent", None)
    return False


# -- sql/row-number-dedupe-tiebreaker --------------------------------------------------
def _picks_one_row(stmt, window):
    """True when the ROW_NUMBER result is filtered to one row per group:
    QUALIFY rn = 1, or an outer WHERE rn = 1 over this window's alias."""
    node = window
    while node is not None:
        if isinstance(node, exp.Qualify):
            return True
        if isinstance(node, exp.Select):
            qualify = node.args.get("qualify")
            if qualify is not None and _qualify_filters(qualify):
                return True
        if node is stmt:
            break
        node = getattr(node, "parent", None)
    alias = None
    node = window.parent
    while node is not None and not isinstance(node, exp.Select):
        if isinstance(node, exp.Alias):
            alias = node.args.get("alias")
            break
        node = getattr(node, "parent", None)
    if alias is None:
        return False
    name = alias.name.lower() if hasattr(alias, "name") else str(alias).lower()
    for sel in stmt.find_all(exp.Select):
        where = sel.args.get("where")
        if where is None:
            continue
        for eq in where.find_all(exp.EQ):
            for side in (eq.left, eq.right):
                if isinstance(side, exp.Column) and side.name.lower() == name:
                    other = eq.right if side is eq.left else eq.left
                    if isinstance(other, exp.Literal) and other.name == "1":
                        return True
    return False


def _qualify_filters(qualify):
    return any(isinstance(node, (exp.EQ, exp.LT, exp.LTE))
               for node in qualify.find_all(exp.EQ, exp.LT, exp.LTE))


_ORDINALITY = "U:ordinality"  # the column's name when none is declared


def _ordinality_names(item, stmt, depth=0):
    """{(name, qualifier)} of WITH ORDINALITY numbers a FROM item exposes,
    unique per row of it. Declared name (`AS u(v, n)`, the last column)
    or the default `ordinality`. A CTE or subquery passes the column
    through when it reads that one source alone (no join to repeat it)
    and selects it by name."""
    if depth > 4 or item is None:
        return set()
    qual = _alias_key(item)
    inner = item.this if isinstance(item, exp.Lateral) else item
    if isinstance(inner, exp.Unnest):
        offset = inner.args.get("offset")
        if isinstance(offset, exp.Identifier):
            return {(_ident_key(offset), qual)}
        return {(_ORDINALITY, qual)} if offset is True else set()
    if item.args.get("ordinality"):
        alias = item.args.get("alias")
        cols = alias.args.get("columns") if alias is not None else None
        name = _ident_key(cols[-1]) if cols else _ORDINALITY
        return {(name, qual)}
    sel = None
    if isinstance(item, exp.Subquery):
        sel = item.this
    elif isinstance(item, exp.Table) and not item.args.get("db"):
        sel = _cte_select(stmt, _ident_key(item.this))
    if not isinstance(sel, exp.Select) or sel.args.get("joins"):
        return set()
    frm = sel.args.get("from_")
    source = _ordinality_names(frm.this if frm is not None else None, stmt,
                               depth + 1)
    names = {name for name, _ in source}
    out = set()
    for proj in sel.expressions:
        col = proj.this if isinstance(proj, exp.Alias) else proj
        if isinstance(col, exp.Column) and _ident_key(col.this) in names:
            out.add((_ident_key(proj.args.get("alias"))
                     if isinstance(proj, exp.Alias) else _ident_key(col.this),
                     qual))
    return out


def _cte_select(stmt, name):
    for cte in stmt.find_all(exp.CTE):
        if _ident_key(cte.args.get("alias").this) == name:
            return cte.this
    return None


def _orders_by_ordinality(window, key):
    """The single ORDER BY key is a WITH ORDINALITY number of the one
    source the window's SELECT reads: unique, so no ties to break."""
    col = key.this if isinstance(key, exp.Ordered) else key
    sel = _owning_select(window)
    if not isinstance(col, exp.Column) or sel is None or sel.args.get("joins"):
        return False
    frm = sel.args.get("from_")
    stmt = sel.root()
    names = _ordinality_names(frm.this if frm is not None else None, stmt)
    name = _ident_key(col.this)
    qual = _ident_key(col.args.get("table"))
    return any(name == n and (not qual or qual == q) for n, q in names)


def row_number_dedupe_tiebreaker(stmt, loc, ctx):
    out = []
    for window in stmt.find_all(exp.Window):
        if window.find(exp.RowNumber) is None:
            continue
        order = window.args.get("order")
        if order is None:
            if _picks_one_row(stmt, window):
                out.append((window,
                            "ROW_NUMBER without ORDER BY keeps an arbitrary row "
                            "per group, and a different one on each run",
                            "order by the column that picks the winner, then "
                            "a unique tiebreaker"))
            continue
        keys = order.expressions if hasattr(order, "expressions") else []
        if (len(keys) == 1 and _picks_one_row(stmt, window)
                and not _orders_by_ordinality(window, keys[0])):
            out.append((window,
                        "ROW_NUMBER with a single ORDER BY key picks an arbitrary "
                        "winner on ties (uniqueness can't be verified from the file)",
                        "add a unique tiebreaker column as the last ORDER BY key"))
    return out


# -- sql/union-all-default (heuristic, off by default) ----------------------------------
def union_all_default(stmt, loc, ctx):
    out = []
    for union in stmt.find_all(exp.Union):
        if union.args.get("distinct") is True:
            out.append((union,
                        "UNION deduplicates; most pipelines mean UNION ALL",
                        "use UNION ALL unless dedup is the intent (and affordable)"))
    return out


# -- sql/group-by-bare-column (permissive dialects only) ----------------------------------
_PERMISSIVE_GROUP_BY = ("mysql", "sqlite")


def group_by_bare_column(stmt, loc, ctx):
    if (ctx.dialect or "generic") not in _PERMISSIVE_GROUP_BY:
        return []  # DuckDB/Postgres raise; nothing to flag
    out = []
    for sel in stmt.find_all(exp.Select):
        group = sel.args.get("group")
        if group is None:
            continue
        if any(isinstance(e, exp.Literal) for e in group.expressions):
            continue  # GROUP BY 1, 2: positions, unknowable without resolving
        keys = {e.sql().lower() for e in group.expressions}
        keys |= {e.name.lower() for e in group.expressions
                 if isinstance(e, exp.Column)}
        for proj in sel.expressions:
            col = proj.unalias() if hasattr(proj, "unalias") else proj
            grouped = {col.sql().lower(), col.name.lower(),
                       (proj.alias or "").lower()} & keys
            if isinstance(col, exp.Column) and not grouped:
                if sel.find(exp.Sum, exp.Count, exp.Avg, exp.Min, exp.Max):
                    out.append((proj,
                                "bare column not in GROUP BY returns an arbitrary value (MySQL/SQLite)",
                                "add it to GROUP BY or aggregate it"))
                    break
    return out


# -- sql/case-without-else-in-aggregate --------------------------------------------------
_AGGS = (exp.Sum, exp.Avg)  # record scope: SUM, AVG. COUNT is the
# conditional-count idiom; MAX/MIN(CASE) without ELSE is the conditional
# extremum idiom (NULL when nothing matches is the wanted answer).


def _is_null_guarded(agg):
    """True when the aggregate sits inside a COALESCE/IFNULL/NVL call in
    the same query level (sqlglot normalizes all three to Coalesce).
    The guard supplies the neutral value for the all-NULL case, so the
    missing ELSE is the intended conditional-aggregate idiom, not a
    silent skip."""
    node = getattr(agg, "parent", None)
    while node is not None:
        if isinstance(node, exp.Select):
            return False
        if isinstance(node, exp.Coalesce):
            return True
        node = getattr(node, "parent", None)
    return False


def case_without_else_in_aggregate(stmt, loc, ctx):
    out = []
    for agg in stmt.find_all(*_AGGS):
        if _is_null_guarded(agg):
            continue
        for case in agg.find_all(exp.Case):
            if case.args.get("default") is None:
                # ELSE 0 is right for SUM and wrong for AVG (it drags the
                # mean toward zero); the fix names the neutral value.
                fix = ("write ELSE NULL to make the skip explicit (ELSE 0 "
                       "would change the mean)"
                       if isinstance(agg, exp.Avg) else
                       "add ELSE 0 (or ELSE <neutral>) inside aggregates")
                out.append((case,
                            "CASE without ELSE yields NULL, which aggregates silently skip",
                            fix))
                break
    return out


# -- contract-aware SQL checks (dq/*, sem/*) ---------------------------------
def _draft_note(*contracts) -> str:
    """Provenance suffix when a relied-upon contract is a draft: `status:
    draft`, or a grain nobody has confirmed (as `truequery init` seeds)."""
    seen = []
    for contract in contracts:
        if not isinstance(contract, dict):
            continue
        name = contract.get("dataset") or contract.get("table") or "unknown"
        if contract.get("status") == "draft":
            src = contract.get("inferred_from") or "sample"
            tag = f"from draft contract {name}, inferred from {src}"
        elif contract_v2.is_unconfirmed_grain(contract.get("grain")):
            tag = (f"from draft contract {name}: its grain is unconfirmed, "
                   "as seeded by truequery init")
        else:
            continue
        if tag not in seen:
            seen.append(tag)
    return f" ({'; '.join(seen)})" if seen else ""


def _same_select(node, sel) -> bool:
    """True when node belongs to sel (not a nested subquery Select)."""
    cur = getattr(node, "parent", None)
    while cur is not None:
        if cur is sel:
            return True
        if isinstance(cur, exp.Select):
            return False
        cur = getattr(cur, "parent", None)
    return False


def _contract_col_map(contract: dict) -> dict[str, tuple[dict, str, dict]]:
    """Map lowercased column name -> (contract, declared_col_name, col_spec)."""
    col_map: dict[str, tuple[dict, str, dict]] = {}
    cols = contract.get("columns")
    if isinstance(cols, dict):
        for cname, spec in cols.items():
            col_map[str(cname).lower()] = (
                contract, str(cname), dict(spec) if isinstance(spec, dict) else {}
            )
    allowed = contract.get("allowed_values")
    if isinstance(allowed, dict):
        for cname, vals in allowed.items():
            key = str(cname).lower()
            if key not in col_map:
                col_map[key] = (contract, str(cname), {})
            if "values" not in col_map[key][2] and isinstance(vals, list):
                col_map[key][2]["values"] = list(vals)
    return col_map


def _unparen(node):
    while isinstance(node, exp.Paren):
        node = node.this
    return node


def _conjuncts(node) -> list:
    """The operands of a top-level AND (through parentheses)."""
    node = _unparen(node)
    if node is None:
        return []
    if isinstance(node, exp.And):
        return _conjuncts(node.left) + _conjuncts(node.right)
    return [node]


def _find_cte(stmt, name_low: str):
    """The body of the CTE named name_low, or None. A CTE named like a
    contracted table is the CTE, whatever its body (a UNION too)."""
    for cte in stmt.find_all(exp.CTE):
        if _alias_key(cte).split(":", 1)[-1] == name_low:
            return cte.this
    return None


def _declared_relations(contract: dict) -> list[str]:
    names = [contract.get("dataset"), contract.get("table")]
    loc = contract.get("location")
    if isinstance(loc, dict):
        names.append(loc.get("relation"))
    return [str(n).lower() for n in names if isinstance(n, str) and n]


def _contract_for_table(table, contracts: dict):
    """The contract for a table reference. A contract that names only the
    table matches it in any schema, as key lookup does; a contract that
    names its schema doesn't match the same table in another schema."""
    name = (table.name or "").lower()
    full = ".".join(p for p in (table.db, table.name) if p).lower()
    contract = contracts.get(full)
    if contract is not None or not name:
        return contract
    contract = contracts.get(name)
    if contract is None or not table.db:
        return contract
    schemas = {rel.split(".")[-2] for rel in _declared_relations(contract)
               if "." in rel and rel.split(".")[-1] == name}
    if schemas and table.db.lower() not in schemas:
        return None
    return contract


def _from_items(node) -> list:
    items = []
    frm = node.args.get("from_")
    if isinstance(frm, exp.From):
        if frm.this is not None:
            items.append(frm.this)
        items.extend(frm.expressions or [])
    for join in node.args.get("joins") or []:
        if join.this is not None:
            items.append(join.this)
    using = node.args.get("using")
    if isinstance(using, list):
        items.extend(using)
    return items


def _item_sources(items, stmt, contracts: dict, _depth: int):
    item_aliases: list[str] = []
    sources: dict[str, tuple[bool, dict]] = {}
    for item in items:
        if isinstance(item, exp.Table) and item.name:
            tname = item.name.lower()
            alias = (item.alias or item.name).lower()
            body = _find_cte(stmt, tname) if not item.db else None
            if body is not None:
                # Only a plain SELECT carries a contract's columns through;
                # a UNION mixes rows from elsewhere.
                src = (_subselect_source(body, stmt, contracts, _depth + 1)
                       if isinstance(body, exp.Select) else (False, {}))
            else:
                contract = _contract_for_table(item, contracts)
                src = ((True, _contract_col_map(contract))
                       if contract is not None else (False, {}))
            item_aliases.append(alias)
            sources[alias] = src
            sources.setdefault(tname, src)
        elif isinstance(item, exp.Subquery) and isinstance(item.this, exp.Select):
            alias = (item.alias or "").lower() or f"_sub_{len(item_aliases)}"
            item_aliases.append(alias)
            sources[alias] = _subselect_source(item.this, stmt, contracts,
                                               _depth + 1)
        else:
            item_aliases.append(f"_other_{len(item_aliases)}")
            sources[item_aliases[-1]] = (False, {})
    return item_aliases, sources


def _select_sources(sel, stmt, contracts: dict, _depth: int = 0):
    """Return (item_aliases, sources_by_alias) for one Select block.

    Each entry in sources_by_alias maps alias_lower -> (has_contract, col_map).
    """
    if _depth > 4 or not contracts:
        return [], {}
    return _item_sources(_from_items(sel), stmt, contracts, _depth)


def _select_filters(sel) -> list:
    """Conditions every row of a SELECT passes: the top-level conjuncts of
    its WHERE and of its inner joins' ON clauses."""
    out = []
    where = sel.args.get("where")
    if where is not None:
        out += _conjuncts(where.this)
    for join in sel.args.get("joins") or []:
        on = join.args.get("on")
        if on is not None and not join.side:
            out += _conjuncts(on)
    return out


def _subselect_source(inner_sel, stmt, contracts: dict, _depth: int):
    """Propagate raw (unconverted) column specs through a CTE or subquery.
    A column also carries the declared values the inner SELECT's filters
    already ruled out (under the internal key `__excluded`)."""
    inner_aliases, inner_sources = _select_sources(
        inner_sel, stmt, contracts, _depth)
    if not inner_aliases:
        return (False, {})
    has_any = any(inner_sources.get(a, (False, {}))[0] for a in inner_aliases)
    if not has_any:
        return (False, {})
    inner_scope = (inner_aliases, inner_sources)
    col_map: dict[str, tuple[dict, str, dict]] = {}
    for proj in inner_sel.expressions:
        if isinstance(proj, exp.Star):
            if len(inner_aliases) == 1:
                col_map.update(inner_sources[inner_aliases[0]][1])
            continue
        if isinstance(proj, exp.Column) and isinstance(proj.this, exp.Star):
            q = (proj.table or "").lower()
            if q and q in inner_sources:
                col_map.update(inner_sources[q][1])
            continue
        out_name = (proj.alias or (proj.name if isinstance(proj, exp.Column) else "")).lower()
        raw = _unparen(proj.unalias() if hasattr(proj, "unalias") else proj)
        if out_name and isinstance(raw, exp.Column):
            resolved = _resolve_col(raw, inner_scope)
            if resolved is not None:
                col_map[out_name] = resolved
    filters = _select_filters(inner_sel)
    if filters:
        for out_name, (contract, cname, spec) in list(col_map.items()):
            before = set(spec.get("__excluded") or ())
            excluded = before | _excluded_by(
                filters, _same_col_fn(inner_scope, contract, cname),
                _declared_values(spec))
            if excluded != before:
                spec = dict(spec, __excluded=frozenset(excluded))
                col_map[out_name] = (contract, cname, spec)
    all_contracted = all(inner_sources.get(a, (False, {}))[0] for a in inner_aliases)
    return (all_contracted and bool(col_map), col_map)


def _resolve_col(col: exp.Column, scope_info):
    """Resolve an exp.Column to (contract, col_name, col_spec) or None."""
    item_aliases, sources = scope_info
    if not item_aliases:
        return None
    cname = (col.name or "").lower()
    if not cname or "__tq_jinja" in cname:
        return None
    qual = (col.table or "").lower()
    if qual:
        src = sources.get(qual)
        return src[1].get(cname) if src else None
    if len(item_aliases) == 1:
        src = sources.get(item_aliases[0])
        return src[1].get(cname) if src else None
    # Unqualified column in a multi-table query: resolve only when every
    # FROM/JOIN source is contracted and exactly one source defines cname.
    if not all(sources.get(a, (False, {}))[0] for a in item_aliases):
        return None
    matches = [sources[a][1][cname] for a in item_aliases
               if cname in sources.get(a, (False, {}))[1]]
    return matches[0] if len(matches) == 1 else None


def _same_col_fn(scope, contract, cname):
    """A test: does a column node stand for this contract column? An
    unqualified column that can't be resolved counts when its name matches."""
    key = (id(contract), cname.lower())

    def same(col):
        resolved = _resolve_col(col, scope)
        if resolved is not None:
            return (id(resolved[0]), resolved[1].lower()) == key
        return not col.table and (col.name or "").lower() == key[1]
    return same


def _declared_values(col_spec: dict) -> list[str] | None:
    """Declared allowed values for a column (v2 values dict/list or v1 list)."""
    vals = col_spec.get("values")
    if isinstance(vals, dict) and vals:
        return [str(k) for k in vals.keys()]
    if isinstance(vals, list) and vals:
        return [str(v) for v in vals]
    return None


def _literal_str(node) -> str | None:
    if isinstance(node, exp.Literal):
        val = str(node.this)
        if "__tq_jinja" in val:
            return None
        return val
    return None


def _value_test(node):
    """(column, values, negated) for a test of one column against
    literals: col = 'v', col <> 'v', col IN (...), col NOT IN (...),
    NOT (col = 'v'), or an OR of positive tests on one column."""
    node = _unparen(node)
    negated = False
    while isinstance(node, exp.Not):
        negated, node = not negated, _unparen(node.this)
    if isinstance(node, (exp.EQ, exp.NEQ)):
        col, lit = node.left, node.right
        if isinstance(lit, exp.Column) and isinstance(col, exp.Literal):
            col, lit = lit, col
        val = _literal_str(lit)
        if not isinstance(col, exp.Column) or val is None:
            return None
        return (col, (val,), negated != isinstance(node, exp.NEQ))
    if isinstance(node, exp.In) and isinstance(node.this, exp.Column):
        vals = [_literal_str(e) for e in node.expressions or []]
        if not vals or any(v is None for v in vals):
            return None
        return (node.this, tuple(vals), negated)
    if isinstance(node, exp.Or) and not negated:
        parts = [_value_test(p) for p in _or_operands(node)]
        if parts and all(p is not None and not p[2] for p in parts):
            cols = {((p[0].table or "").lower(), (p[0].name or "").lower())
                    for p in parts}
            if len(cols) == 1:
                return (parts[0][0], tuple(v for p in parts for v in p[1]),
                        False)
    return None


def _excluded_by(conjuncts, same_col, declared) -> set[str]:
    """Values of one column that ANDed conditions rule out: `<>` and
    NOT IN rule out their values; `=` and IN rule out every other
    declared value."""
    out: set[str] = set()
    for op in conjuncts:
        test = _value_test(op)
        if test is None or not same_col(test[0]):
            continue
        _, vals, negated = test
        if negated:
            out |= set(vals)
        elif declared:
            out |= set(declared) - set(vals)
    return out


def _and_list(values) -> str:
    vals = list(values)
    if len(vals) <= 1:
        return "".join(vals)
    return ", ".join(vals[:-1]) + " and " + vals[-1]


# -- dq/undeclared-status-value ---------------------------------------------
def _undeclared_findings(nodes, scope) -> list:
    out = []
    for node in nodes:
        if isinstance(node, (exp.EQ, exp.NEQ)):
            left, right = node.left, node.right
            if isinstance(left, exp.Column) and isinstance(right, exp.Literal):
                col_node, lit_node = left, right
            elif isinstance(right, exp.Column) and isinstance(left, exp.Literal):
                col_node, lit_node = right, left
            else:
                continue
            bad_nodes = [(node, lit_node)]
        elif isinstance(node, exp.In) and isinstance(node.this, exp.Column):
            col_node = node.this
            bad_nodes = [(node, item) for item in node.expressions or []]
        elif isinstance(node, exp.Case) and isinstance(
                node.args.get("this"), exp.Column):
            col_node = node.args["this"]
            bad_nodes = [(cond, cond.this)
                         for cond in node.args.get("ifs") or []]
        else:
            continue
        resolved = _resolve_col(col_node, scope)
        if resolved is None:
            continue
        contract, cname, spec = resolved
        allowed = _declared_values(spec)
        if not allowed:
            continue
        bad = [(anchor, _literal_str(lit)) for anchor, lit in bad_nodes]
        bad = [(anchor, v) for anchor, v in bad
               if v is not None and v not in allowed]
        if not bad:
            continue
        ds = contract.get("dataset") or contract.get("table")
        listed = ", ".join(repr(v) for v in allowed)
        draft = _draft_note(contract)
        if isinstance(node, exp.Case):
            for anchor, val in bad:
                out.append((
                    anchor,
                    f"a CASE on {ds}.{cname} tests for {val!r}, which its "
                    f"contract doesn't list ({listed}), so that branch "
                    f"never runs{draft}",
                    f"use one of the listed values for {ds}.{cname}, or ask "
                    f"the user whether the contract should list {val!r}",
                ))
            continue
        vals = ", ".join(repr(v) for _, v in bad)
        out.append((
            node,
            f"{ds}.{cname} is compared to {vals}, which its contract doesn't "
            f"list ({listed}), so the comparison matches no rows{draft}",
            f"use one of the listed values for {ds}.{cname}, or ask the user "
            f"whether the contract should list {vals}",
        ))
    return out


_VALUE_NODES = (exp.EQ, exp.NEQ, exp.In, exp.Case)


def undeclared_status_value(stmt, loc, ctx):
    if not ctx.contracts:
        return []
    out = []
    for sel in stmt.find_all(exp.Select):
        scope = _select_sources(sel, stmt, ctx.contracts)
        if not scope[0]:
            continue
        nodes = [n for n in sel.find_all(*_VALUE_NODES)
                 if _same_select(n, sel)
                 and not (isinstance(n, (exp.EQ, exp.NEQ))
                          and _under_dml_write(n))]
        out += _undeclared_findings(nodes, scope)
    # The WHERE of an UPDATE or DELETE: a value typo there changes nothing.
    for dml in stmt.find_all(exp.Update, exp.Delete):
        where = dml.args.get("where")
        if where is None or not isinstance(dml.this, exp.Table):
            continue
        scope = _item_sources([dml.this] + _from_items(dml), stmt,
                              ctx.contracts, 0)
        if not any(src[0] for src in scope[1].values()):
            continue
        nodes = [n for n in where.find_all(*_VALUE_NODES)
                 if n.find_ancestor(exp.Select, exp.Update, exp.Delete)
                 is dml]
        out += _undeclared_findings(nodes, scope)
    return out


# -- dq/expected-absence-counted-as-missing ---------------------------------
def _boolean_top(node):
    """The whole condition node sits in: up through AND, OR and parentheses."""
    cur = node
    while isinstance(getattr(cur, "parent", None), (exp.And, exp.Or, exp.Paren)):
        cur = cur.parent
    return cur


def _negated_by_parent(node) -> bool:
    parent = node.parent
    while isinstance(parent, exp.Paren):
        parent = parent.parent
    return isinstance(parent, exp.Not)


def _is_number(node, value) -> bool:
    if not isinstance(node, exp.Literal) or node.is_string:
        return False
    try:
        return float(node.this) == value
    except ValueError:
        return False


def _single_case(agg):
    """(test, then, default) for AGG(CASE WHEN <test> THEN .. ELSE .. END)."""
    arg = _unparen(agg.this)
    if not isinstance(arg, exp.Case) or arg.args.get("this") is not None:
        return None
    ifs = arg.args.get("ifs") or []
    if len(ifs) != 1:
        return None
    test = _value_test(ifs[0].this)
    if test is None or test[2]:
        return None
    return test, ifs[0].args.get("true"), arg.args.get("default")


def _is_null(node) -> bool:
    return node is None or isinstance(node, exp.Null)


def _counts_all_but(agg):
    """(column, values) for an aggregate that counts every row except those
    with the given values: SUM(CASE WHEN col = 'v' THEN 0 ELSE 1 END) or
    COUNT(CASE WHEN col = 'v' THEN NULL ELSE 1 END)."""
    case = _single_case(agg) if isinstance(agg, (exp.Sum, exp.Count)) else None
    if case is None:
        return None
    (col, vals, _), then, default = case
    if isinstance(agg, exp.Sum):
        ok = _is_number(then, 0) and _is_number(default, 1)
    else:
        ok = _is_null(then) and not _is_null(default)
    return (col, list(vals)) if ok else None


def _counts_only(node):
    """(column, values) for an aggregate that counts only the rows with the
    given values: COUNT(CASE WHEN col = 'v' THEN 1 END), SUM(CASE WHEN ..
    THEN 1 ELSE 0 END), COUNT(*) FILTER (WHERE col = 'v')."""
    node = _unparen(node)
    if isinstance(node, exp.Filter) and isinstance(_unparen(node.this), exp.Count):
        where = node.expression
        test = _value_test(where.this if isinstance(where, exp.Where) else where)
        return (test[0], list(test[1])) if test and not test[2] else None
    case = _single_case(node) if isinstance(node, (exp.Sum, exp.Count)) else None
    if case is None:
        return None
    (col, vals, _), then, default = case
    if isinstance(node, exp.Sum):
        ok = _is_number(then, 1) and (_is_null(default) or _is_number(default, 0))
    else:
        ok = not _is_null(then) and _is_null(default)
    return (col, list(vals)) if ok else None


def _counts_rows(node) -> bool:
    node = _unparen(node)
    return (isinstance(node, exp.Count)
            and (node.this is None
                 or isinstance(node.this, (exp.Star, exp.Literal))))


def expected_absence_counted_as_missing(stmt, loc, ctx):
    if not ctx.contracts:
        return []
    out = []
    for sel in stmt.find_all(exp.Select):
        scope = _select_sources(sel, stmt, ctx.contracts)
        if not scope[0]:
            continue
        filters = _select_filters(sel)
        # (anchor, column, the present values it leaves out, its own condition)
        candidates = []
        for node in sel.find_all(exp.NEQ, exp.Not):
            if not _same_select(node, sel) or _under_dml_write(node):
                continue
            if _negated_by_parent(node):
                continue  # NOT (status <> 'x') is a positive test
            test = _value_test(node)
            if test is None or not test[2]:
                continue
            candidates.append((node, test[0], list(test[1]),
                               _conjuncts(_boolean_top(node))))
        for agg in sel.find_all(exp.Sum, exp.Count):
            if _same_select(agg, sel):
                hit = _counts_all_but(agg)
                if hit is not None:
                    candidates.append((agg, hit[0], hit[1], []))
        for sub in sel.find_all(exp.Sub):
            if _same_select(sub, sel) and _counts_rows(sub.left):
                hit = _counts_only(sub.right)
                if hit is not None:
                    candidates.append((sub, hit[0], hit[1], []))
        for anchor, col_node, neg_vals, own in candidates:
            resolved = _resolve_col(col_node, scope)
            if resolved is None:
                continue
            contract, cname, spec = resolved
            if contract.get("version") != 2:
                continue
            values_map = spec.get("values")
            if not isinstance(values_map, dict):
                continue
            means = {str(k): v.get("means") for k, v in values_map.items()
                     if isinstance(v, dict)}
            present = [k for k, m in means.items() if m == "present"]
            absent_exp = [k for k, m in means.items() if m == "absent_expected"]
            absent_unexp = [k for k, m in means.items()
                            if m == "absent_unexpected"]
            if not present or not absent_exp:
                continue
            # Leaving out only `present` values sweeps in everything else.
            if not set(neg_vals) <= set(present):
                continue
            excluded = set(spec.get("__excluded") or ())
            excluded |= _excluded_by(own + filters,
                                     _same_col_fn(scope, contract, cname),
                                     list(means))
            if set(absent_exp) <= excluded:
                continue
            ds = contract.get("dataset") or contract.get("table")
            gone = _and_list(absent_exp)
            shown = " ".join(anchor.sql(dialect=ctx.dialect).split())
            if len(shown) > 80:
                shown = shown[:77] + "..."
            if absent_unexp:
                missing = (f"{cname} = {absent_unexp[0]!r}"
                           if len(absent_unexp) == 1 else
                           f"{cname} IN ({', '.join(repr(v) for v in absent_unexp)})")
                fix = (f"count only the values that mean missing ({missing}), "
                       f"or leave out {gone} explicitly")
            else:
                fix = f"leave out {gone} explicitly"
            out.append((
                anchor,
                f"{gone} in {ds}.{cname} are absent on purpose; `{shown}` "
                f"counts them as missing or failed{_draft_note(contract)}",
                fix,
            ))
    return out


# -- sem/unit-mismatch ---------------------------------------------------------
_VALUE_PASSTHROUGH = (exp.Paren, exp.Cast, exp.TryCast, exp.Coalesce,
                      exp.Sum, exp.Avg, exp.Min, exp.Max)


def _unscaled_col(expr, scope):
    """Return (col_node, contract, cname, spec) when expr is an unscaled
    column (or SUM/AVG/MIN/MAX of one) with no *, / or conversion function."""
    cur = expr
    while isinstance(cur, _VALUE_PASSTHROUGH):
        cur = cur.this
    if isinstance(cur, exp.Column):
        resolved = _resolve_col(cur, scope)
        if resolved is not None:
            return (cur, *resolved)
    return None


def _unit_words(spec: dict) -> str:
    unit, currency = spec.get("unit"), spec.get("currency")
    if currency:
        kind = {"minor": "minor units, such as cents",
                "major": "whole units"}.get(unit, unit or "")
        return f"{currency} ({kind})" if kind else str(currency)
    return str(unit or "")


def _units_differ(spec_a: dict, spec_b: dict) -> bool:
    u_a, u_b = spec_a.get("unit"), spec_b.get("unit")
    c_a, c_b = spec_a.get("currency"), spec_b.get("currency")
    return bool((u_a and u_b and u_a != u_b) or (c_a and c_b and c_a != c_b))


def _arith_leaves(node) -> list:
    node = _unparen(node)
    if isinstance(node, (exp.Add, exp.Sub)):
        return _arith_leaves(node.left) + _arith_leaves(node.right)
    return [node]


def unit_mismatch(stmt, loc, ctx):
    if not ctx.contracts:
        return []
    out = []
    op_types = (exp.Add, exp.Sub, exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
    for sel in stmt.find_all(exp.Select):
        scope = _select_sources(sel, stmt, ctx.contracts)
        if not scope[0]:
            continue
        for node in sel.find_all(*op_types):
            if not _same_select(node, sel) or _under_dml_write(node):
                continue
            if isinstance(node, (exp.Add, exp.Sub)):
                parent = node.parent
                while isinstance(parent, exp.Paren):
                    parent = parent.parent
                if isinstance(parent, (exp.Add, exp.Sub)):
                    continue  # the whole chain is checked once, at its top
                operands, verb = _arith_leaves(node), "added or subtracted"
            else:
                operands, verb = [node.left, node.right], "compared"
            infos = []
            for operand in operands:
                info = _unscaled_col(operand, scope)
                if (info is not None and info[1].get("version") == 2
                        and (info[3].get("unit") or info[3].get("currency"))):
                    infos.append(info)
            pair = next(((a, b) for i, a in enumerate(infos)
                         for b in infos[i + 1:] if _units_differ(a[3], b[3])),
                        None)
            if pair is None:
                continue
            (_, c_a, name_a, spec_a), (_, c_b, name_b, spec_b) = pair
            ds_a = c_a.get("dataset") or c_a.get("table")
            ds_b = c_b.get("dataset") or c_b.get("table")
            ref_a = f"{ds_a}.{name_a}" if ds_a != ds_b else name_a
            ref_b = f"{ds_b}.{name_b}" if ds_a != ds_b else name_b
            out.append((
                node,
                f"{ref_a} is in {_unit_words(spec_a)} and {ref_b} is in "
                f"{_unit_words(spec_b)}; they're {verb} without converting "
                f"one to the other{_draft_note(c_a, c_b)}",
                f"convert {ref_a} or {ref_b} so both are in the same unit "
                f"first",
            ))
    return out


# -- sem/timezone-mismatch -----------------------------------------------------
_TZ_CONV_FNS = frozenset({
    "at_time_zone", "attimezone", "convert_timezone", "timezone",
    "from_utc_timestamp", "to_utc_timestamp", "datetime",
    "with_timezone", "at_timezone", "convert_tz",
})


def _has_tz_conversion(expr) -> bool:
    for sub in [expr] + list(expr.walk()):
        sub = sub[0] if isinstance(sub, tuple) else sub
        if isinstance(sub, exp.AtTimeZone):
            return True
        # BigQuery TIMESTAMP(datetime, 'Europe/Prague')
        if isinstance(sub, exp.Timestamp) and sub.args.get("zone") is not None:
            return True
        if isinstance(sub, exp.Func) and sub.sql_name().lower() in _TZ_CONV_FNS:
            return True
        if isinstance(sub, exp.Anonymous) and (sub.name or "").lower() in _TZ_CONV_FNS:
            return True
    return False


def _unconverted_tz_col(expr, scope):
    if expr is None or _has_tz_conversion(expr):
        return None
    cols = list(expr.find_all(exp.Column))
    if len(cols) != 1:
        return None
    resolved = _resolve_col(cols[0], scope)
    if resolved is None:
        return None
    contract, cname, spec = resolved
    if contract.get("version") != 2:
        return None
    tz = spec.get("time_zone")
    if not tz:
        return None
    return (cols[0], contract, cname, str(tz))


def _tz_words(tz: str) -> str:
    if tz.startswith("local:"):
        return f"local time ({tz.split(':', 1)[1]})"
    if tz == "naive":
        return "a time with no time zone recorded"
    return tz


def timezone_mismatch(stmt, loc, ctx):
    if not ctx.contracts:
        return []
    out = []
    cmp_types = (exp.EQ, exp.NEQ, exp.LT, exp.LTE, exp.GT, exp.GTE)
    for sel in stmt.find_all(exp.Select):
        scope = _select_sources(sel, stmt, ctx.contracts)
        if not scope[0]:
            continue
        pairs = []
        for node in sel.find_all(*cmp_types):
            if not _same_select(node, sel) or _under_dml_write(node):
                continue
            pairs.append((node, node.left, node.right))
        for btw in sel.find_all(exp.Between):
            if not _same_select(btw, sel):
                continue
            pairs.append((btw, btw.this, btw.args.get("low")))
        for anchor, left_e, right_e in pairs:
            info_a = _unconverted_tz_col(left_e, scope)
            info_b = _unconverted_tz_col(right_e, scope)
            if info_a is None or info_b is None:
                continue
            _, c_a, name_a, tz_a = info_a
            _, c_b, name_b, tz_b = info_b
            if tz_a == tz_b:
                continue
            ref_a = f"{c_a.get('dataset') or c_a.get('table')}.{name_a}"
            ref_b = f"{c_b.get('dataset') or c_b.get('table')}.{name_b}"
            out.append((
                anchor,
                f"{ref_a} is in {_tz_words(tz_a)} and {ref_b} is in "
                f"{_tz_words(tz_b)}; comparing them without a conversion "
                f"shifts every boundary by the difference"
                f"{_draft_note(c_a, c_b)}",
                f"convert {ref_a} and {ref_b} to the same time zone "
                f"(for example with AT TIME ZONE) before comparing them",
            ))
    return out


# -- sem/relative-path-base-mismatch -------------------------------------------
_PATH_NORM_FNS = frozenset({
    "concat", "concat_ws", "replace", "regexp_replace", "substr",
    "substring", "ltrim", "trim", "right",
})


def _concat_parts(node) -> list:
    node = _unparen(node)
    if isinstance(node, exp.DPipe):
        return _concat_parts(node.left) + _concat_parts(node.right)
    if isinstance(node, exp.Concat):
        parts = []
        for part in node.expressions or []:
            parts += _concat_parts(part)
        return parts
    return [node]


def _same_dir(a: str, b: str) -> bool:
    def norm(p):
        p = p.strip().rstrip("/")
        while p.startswith("./"):
            p = p[2:]
        return p or "."
    return norm(a) == norm(b)


def _path_side(expr, scope, bases: dict):
    """How one side of a comparison anchors a declared path column:
    (col, contract, name, anchor). anchor is the declared base name, None
    for a full path, or ("prefix", literal, base) when the query puts a literal
    in front that isn't the declared base. None when it can't tell."""
    if expr is None:
        return None
    parts = _concat_parts(expr)
    prefix = None
    if len(parts) > 1:
        if (len(parts) != 2 or _literal_str(parts[0]) is None
                or not isinstance(parts[1], exp.Column)):
            return None
        prefix, target = _literal_str(parts[0]), parts[1]
    else:
        for sub in [expr] + list(expr.walk()):
            sub = sub[0] if isinstance(sub, tuple) else sub
            if isinstance(sub, exp.Func) and sub.sql_name().lower() in _PATH_NORM_FNS:
                return None
            if isinstance(sub, exp.Anonymous) and (sub.name or "").lower() in _PATH_NORM_FNS:
                return None
        cols = list(expr.find_all(exp.Column))
        if len(cols) != 1:
            return None
        target = cols[0]
    resolved = _resolve_col(target, scope)
    if resolved is None:
        return None
    contract, cname, spec = resolved
    if contract.get("version") != 2 or spec.get("type") != "path":
        return None
    base = spec.get("relative_to")
    if prefix is None:
        return (target, contract, cname, base)
    declared = bases.get(base) if base else None
    if not isinstance(declared, dict) or declared.get("env") \
            or not declared.get("default"):
        # A full path with something in front, or a base whose value is
        # only known at run time (an environment variable): can't tell.
        return None
    if _same_dir(prefix, str(declared["default"])):
        return (target, contract, cname, None)
    return (target, contract, cname, ("prefix", prefix, base))


def relative_path_base_mismatch(stmt, loc, ctx):
    if not ctx.contracts:
        return []
    out = []

    def describe(base):
        if not base:
            return "a full path"
        default = (ctx.bases.get(base) or {}).get("default")
        return f"relative to {base} ({default})" if default else f"relative to {base}"

    for sel in stmt.find_all(exp.Select):
        scope = _select_sources(sel, stmt, ctx.contracts)
        if not scope[0]:
            continue
        for node in sel.find_all(exp.EQ, exp.NEQ):
            if not _same_select(node, sel) or _under_dml_write(node):
                continue
            info_a = _path_side(node.left, scope, ctx.bases)
            info_b = _path_side(node.right, scope, ctx.bases)
            if info_a is None or info_b is None or info_a[3] == info_b[3]:
                continue
            _, c_a, name_a, base_a = info_a
            _, c_b, name_b, base_b = info_b
            ref_a = f"{c_a.get('dataset') or c_a.get('table')}.{name_a}"
            ref_b = f"{c_b.get('dataset') or c_b.get('table')}.{name_b}"
            draft = _draft_note(c_a, c_b)
            wrong = next(((ref, anchor) for ref, anchor in
                          ((ref_a, base_a), (ref_b, base_b))
                          if isinstance(anchor, tuple)), None)
            if wrong is not None:
                ref, (_, lit, base) = wrong
                out.append((
                    node,
                    f"the query puts {lit!r} in front of {ref}, but {ref} is "
                    f"{describe(base)}, so the paths it builds don't match"
                    f"{draft}",
                    f"put the declared base in front of {ref}, or ask the user "
                    f"whether the contract's base is right",
                ))
                continue
            default = next((ctx.bases[b]["default"].rstrip("/") + "/"
                            for b in (base_a, base_b)
                            if b and (ctx.bases.get(b) or {}).get("default")),
                           None)
            example = (f" (for example '{default}' || <relative path>)"
                       if default else "")
            out.append((
                node,
                f"{ref_a} is {describe(base_a)} and {ref_b} is "
                f"{describe(base_b)}; compared directly, they don't match"
                f"{draft}",
                f"add or strip the declared base{example} before comparing "
                f"{ref_a} and {ref_b}",
            ))
    return out


# -- sem/metric-drift ---------------------------------------------------------
def _norm_cond_ast(node) -> str:
    """Canonical string for a condition AST so equivalent SQL predicates match."""
    if node is None:
        return ""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        parts = sorted(_norm_cond_ast(op) for op in _and_operands(node))
        return " AND ".join(p for p in parts if p)
    if isinstance(node, exp.Or):
        parts = sorted(_norm_cond_ast(op) for op in _or_operands(node))
        return "(" + " OR ".join(p for p in parts if p) + ")"
    if isinstance(node, exp.Not):
        return f"NOT ({_norm_cond_ast(node.this)})"
    if isinstance(node, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ, exp.NEQ)):
        op_map = {exp.GT: ">", exp.GTE: ">=", exp.LT: "<", exp.LTE: "<=",
                  exp.EQ: "=", exp.NEQ: "<>"}
        left, right = node.left, node.right
        l_str = left.name.lower() if isinstance(left, exp.Column) else left.sql()
        r_str = right.name.lower() if isinstance(right, exp.Column) else right.sql()
        return f"{l_str} {op_map[type(node)]} {r_str}"
    if isinstance(node, exp.Is) and isinstance(node.this, exp.Column):
        return f"{node.this.name.lower()} IS {node.expression.sql().upper()}"
    copy = node.copy()
    for col in copy.find_all(exp.Column):
        col.set("table", None)
    return copy.sql()


def _filter_canon(conjuncts):
    """Canonical form of ANDed conditions, so equivalent filters compare
    equal: the tests of one column against literals become the set of
    values they allow (or rule out), whatever their form (=, IN, OR, <>,
    NOT IN); anything else compares as normalized text."""
    tests: dict[str, tuple] = {}
    other = set()
    for op in conjuncts:
        test = _value_test(op)
        if test is None:
            other.add(_norm_cond_ast(op))
            continue
        col, vals, negated = test
        name = (col.name or "").lower()
        allow, rule_out = tests.get(name, (None, frozenset()))
        if negated:
            rule_out = rule_out | set(vals)
        else:
            allow = set(vals) if allow is None else allow & set(vals)
        tests[name] = (allow, rule_out)
    canon = set()
    for name, (allow, rule_out) in tests.items():
        if allow is not None:
            canon.add((name, "in", frozenset(set(allow) - set(rule_out))))
        else:
            canon.add((name, "not in", frozenset(rule_out)))
    return (frozenset(canon), frozenset(other))


def _declared_conjuncts(where_str):
    """The declared filter as conjuncts; None when it doesn't parse."""
    if not where_str or not str(where_str).strip():
        return []
    try:
        parsed = sqlglot.parse_one(str(where_str), into=exp.Condition)
    except Exception:
        return None
    return _conjuncts(parsed)


def _unwrap_scalar_wrappers(expr):
    """Strip outer ROUND, CAST, COALESCE, NULLIF, Paren, and * 1.0 scaling."""
    cur = expr
    while cur is not None:
        if isinstance(cur, (exp.Paren, exp.Cast, exp.TryCast)):
            cur = cur.this
            continue
        if isinstance(cur, (exp.Round, exp.Coalesce, exp.Nullif)):
            cur = cur.this
            continue
        if isinstance(cur, exp.Mul):
            if isinstance(cur.left, exp.Literal):
                cur = cur.right
                continue
            if isinstance(cur.right, exp.Literal):
                cur = cur.left
                continue
        if isinstance(cur, exp.Div) and isinstance(cur.right, exp.Literal):
            try:
                if float(cur.right.name) == 1.0:
                    cur = cur.left
                    continue
            except ValueError:
                pass
        break
    return cur


def _extract_agg_spec(expr):
    """(agg, column, inline filter conjuncts) for one aggregate, or None.
    Inline filters come from FILTER (WHERE ..), COUNTIF/COUNT_IF and a
    CASE inside the aggregate; the query's WHERE is added by the caller."""
    cur = _unwrap_scalar_wrappers(expr)
    if cur is None:
        return None
    inline = []
    if isinstance(cur, exp.Filter):
        w = cur.expression
        inline += _conjuncts(w.this if isinstance(w, exp.Where) else w)
        cur = _unwrap_scalar_wrappers(cur.this)
    if isinstance(cur, exp.CountIf):
        return ("count", "*", inline + _conjuncts(cur.this))
    if not isinstance(cur, (exp.Count, exp.Sum, exp.Avg, exp.Min, exp.Max)):
        return None
    arg = _unparen(cur.this)
    # Conditional aggregation: COUNT/SUM/AVG(CASE WHEN <cond> THEN <val> ... END)
    if isinstance(arg, exp.Case) and not arg.args.get("this"):
        ifs = arg.args.get("ifs") or []
        if len(ifs) == 1:
            when_cond = ifs[0].this
            then_val = ifs[0].args.get("true")
            else_val = arg.args.get("default")
            if isinstance(cur, exp.Sum):
                if _is_null(else_val) or _is_number(else_val, 0):
                    inline += _conjuncts(when_cond)
                    if _is_number(then_val, 1):
                        return ("count", "*", inline)
                    arg = then_val
            elif _is_null(else_val):
                inline += _conjuncts(when_cond)
                arg = then_val
    if isinstance(cur, exp.Count):
        if isinstance(arg, exp.Distinct) or cur.args.get("distinct"):
            d_arg = (arg.expressions[0]
                     if isinstance(arg, exp.Distinct) and arg.expressions
                     else arg)
            col_name = d_arg.name.lower() if isinstance(d_arg, exp.Column) else "*"
            return ("count_distinct", col_name, inline)
        if arg is None or isinstance(arg, (exp.Star, exp.Literal)):
            return ("count", "*", inline)
        if isinstance(arg, exp.Column):
            return ("count", arg.name.lower(), inline)
        return ("count", arg.sql().lower(), inline)
    agg_map = {exp.Sum: "sum", exp.Avg: "avg", exp.Min: "min", exp.Max: "max"}
    col_name = (arg.name.lower() if isinstance(arg, exp.Column)
                else (arg.sql().lower() if arg is not None else ""))
    return (agg_map[type(cur)], col_name, inline)


def _ratio_parts(expr, sel):
    """(numerator, denominator) aggregates of a ratio expression, as
    _extract_agg_spec results; None when the expression isn't a ratio."""
    inner = _unwrap_scalar_wrappers(expr)
    if isinstance(inner, exp.If):
        # Snowflake DIV0/DIV0NULL: IF(<zero or NULL denominator>, 0, a / b)
        false = _unwrap_scalar_wrappers(inner.args.get("false"))
        if isinstance(false, exp.Div):
            inner = false
    if isinstance(inner, exp.SafeDivide):
        return (_extract_agg_spec(inner.this),
                _extract_agg_spec(inner.expression))
    if isinstance(inner, exp.Avg):
        # AVG(CASE WHEN c THEN 1 ELSE 0 END) = COUNT(c) / COUNT(*)
        case = _unparen(inner.this)
        if isinstance(case, exp.Case) and not case.args.get("this"):
            ifs = case.args.get("ifs") or []
            if (len(ifs) == 1 and _is_number(ifs[0].args.get("true"), 1)
                    and _is_number(case.args.get("default"), 0)):
                return (("count", "*", _conjuncts(ifs[0].this)),
                        ("count", "*", []))
    if not isinstance(inner, exp.Div):
        divs = [d for d in expr.find_all(exp.Div) if _same_select(d, sel)]
        inner = divs[0] if len(divs) == 1 else None
    if inner is None:
        return None
    return (_extract_agg_spec(inner.left), _extract_agg_spec(inner.right))


def _compare_metric_part(label: str, got, spec: dict, slice_kept) -> str | None:
    agg_name, col_name, inline = got
    exp_agg = str(spec["agg"]).lower()
    exp_col = str(spec["column"]).lower()
    if agg_name != exp_agg:
        return f"the {label} uses {agg_name.upper()}, the metric {exp_agg.upper()}"
    if exp_col != "*" and col_name != exp_col:
        return f"the {label} aggregates {col_name}, the metric {spec['column']}"
    mine = inline + list(slice_kept)
    declared = _declared_conjuncts(spec.get("where"))
    if declared is None:
        same = " AND ".join(sorted(_norm_cond_ast(c) for c in mine)) == \
            str(spec.get("where")).strip().lower()
    else:
        same = _filter_canon(mine) == _filter_canon(declared)
    if same:
        return None
    mine_sql = " AND ".join(c.sql() for c in mine)
    if spec.get("where") and not mine:
        return f"the {label} omits declared filter {spec['where']!r}"
    if mine and not spec.get("where"):
        return f"the {label} adds filter {mine_sql!r}, which the metric doesn't have"
    return (f"the {label} filters on {mine_sql!r}, the metric on "
            f"{spec['where']!r}")


def _select_base_datasets(sel, stmt, _depth: int = 0) -> set[str]:
    """Tables a SELECT reads, through CTEs and subqueries."""
    names: set[str] = set()
    if _depth > 6:
        return names
    for item in _from_items(sel):
        if isinstance(item, exp.Table) and item.name:
            tname = item.name.lower()
            body = _find_cte(stmt, tname) if not item.db else None
            if body is not None:
                names |= _body_bases(body, stmt, _depth + 1)
                continue
            names.add(tname)
            names.add(".".join(p for p in (item.db, item.name) if p).lower())
        elif isinstance(item, exp.Subquery) and item.this is not None:
            names |= _body_bases(item.this, stmt, _depth + 1)
    return names


def _body_bases(body, stmt, _depth: int) -> set[str]:
    if isinstance(body, exp.Select):
        return _select_base_datasets(body, stmt, _depth)
    names: set[str] = set()
    for sel in body.find_all(exp.Select):  # the branches of a UNION
        names |= _select_base_datasets(sel, stmt, _depth)
    return names


def _metric_parts(metric: dict) -> list[dict]:
    if metric.get("kind") == "ratio":
        return [metric["numerator"], metric["denominator"]]
    if metric.get("kind") == "simple":
        return [metric["measure"]]
    return []


def _slice_kept(sel, stmt, ds: str, metric: dict) -> list:
    """The query WHERE's conjuncts that bear on the metric's own filters.
    Any other condition (a run, a date window, a dimension, a joined
    table) slices the metric and applies to every part alike."""
    where = sel.args.get("where")
    if where is None:
        return []
    filter_cols = set()
    for part in _metric_parts(metric):
        for cond in _declared_conjuncts(part.get("where")) or []:
            filter_cols |= {c.name.lower() for c in cond.find_all(exp.Column)}
    short = ds.split(".")[-1]
    ds_aliases = set()
    for item in _from_items(sel):
        if isinstance(item, exp.Table) and item.name:
            body = _find_cte(stmt, item.name.lower()) if not item.db else None
            bases = (_body_bases(body, stmt, 1) if body is not None
                     else {item.name.lower()})
            alias = (item.alias or item.name).lower()
        elif isinstance(item, exp.Subquery) and item.this is not None:
            bases, alias = _body_bases(item.this, stmt, 1), (item.alias or "").lower()
        else:
            continue
        if ds in bases or short in bases:
            ds_aliases.add(alias)
    kept = []
    for cond in _conjuncts(where.this):
        if any((c.name or "").lower() in filter_cols
               and (not c.table or c.table.lower() in ds_aliases)
               for c in cond.find_all(exp.Column)):
            kept.append(cond)
    return kept


def metric_drift(stmt, loc, ctx):
    if not ctx.metrics:
        return []
    out = []
    for sel in stmt.find_all(exp.Select):
        datasets = _select_base_datasets(sel, stmt)
        if not datasets:
            continue
        for proj in sel.expressions:
            if not isinstance(proj, exp.Alias) or not proj.alias:
                continue
            metric = ctx.metrics.get(proj.alias.lower())
            if metric is None:
                continue
            ds = str(metric.get("dataset") or "").lower()
            if ds not in datasets and ds.split(".")[-1] not in datasets:
                continue
            kind = metric.get("kind")
            if kind == "ratio":
                parts = _ratio_parts(proj.this, sel)
                if parts is None:
                    continue  # the name is reused for something else
                specs = [("numerator", metric["numerator"]),
                         ("denominator", metric["denominator"])]
            elif kind == "simple":
                if not proj.this.find(exp.Count, exp.Sum, exp.Avg, exp.Min,
                                      exp.Max, exp.CountIf):
                    continue
                parts = (_extract_agg_spec(proj.this),)
                specs = [("measure", metric["measure"])]
            else:
                continue
            if any(part is None for part in parts):
                # A part built elsewhere (a CTE column, a parameter) can't
                # be compared from here: say nothing rather than guess.
                continue
            kept = _slice_kept(sel, stmt, ds, metric)
            diffs = [d for (label, spec), got in zip(specs, parts)
                     if (d := _compare_metric_part(label, got, spec, kept))]
            if not diffs:
                continue
            contract = ctx.contracts.get(ds) or ctx.contracts.get(ds.split(".")[-1])
            name = metric.get("metric") or proj.alias
            declared = "; ".join(
                f"{label}: {str(spec['agg']).upper()}({spec['column']})"
                + (f" WHERE {spec['where']}" if spec.get("where") else "")
                for label, spec in specs)
            out.append((
                proj,
                f"{name!r} isn't computed as metrics/ defines it: "
                f"{'; '.join(diffs)}{_draft_note(contract)}",
                f"compute {name!r} as defined ({declared}), or give the column "
                f"another name if it means something else",
            ))
    return out


CHECKS = {
    "sql/not-in-nullable-subquery": not_in_nullable_subquery,
    "sql/unguarded-update-delete": unguarded_update_delete,
    "sql/accidental-cross-join": accidental_cross_join,
    "sql/left-join-null-rejecting-where": left_join_null_rejecting_where,
    "sql/null-equality-comparison": null_equality_comparison,
    "sql/count-star-after-left-join": count_star_after_left_join,
    "sql/nondeterministic-merge-keys": nondeterministic_merge_keys,
    "sql/set-operation-column-position": set_operation_column_position,
    "sql/average-of-ratios": average_of_ratios,
    "sql/between-date-on-timestamp": between_date_on_timestamp,
    "sql/distinct-dedup-bandaid": distinct_dedup_bandaid,
    "sql/insert-explicit-column-list": insert_explicit_column_list,
    "sql/integer-division-truncation": integer_division_truncation,
    "sql/keyset-pagination": keyset_pagination,
    "sql/limit-requires-order-by": limit_requires_order_by,
    "sql/natural-join-explicit-keys": natural_join_explicit_keys,
    "sql/negative-filter-drops-nulls": negative_filter_drops_nulls,
    "sql/row-number-dedupe-tiebreaker": row_number_dedupe_tiebreaker,
    "sql/union-all-default": union_all_default,
    "sql/group-by-bare-column": group_by_bare_column,
    "sql/case-without-else-in-aggregate": case_without_else_in_aggregate,
    "dq/undeclared-status-value": undeclared_status_value,
    "dq/expected-absence-counted-as-missing": expected_absence_counted_as_missing,
    "sem/unit-mismatch": unit_mismatch,
    "sem/timezone-mismatch": timezone_mismatch,
    "sem/relative-path-base-mismatch": relative_path_base_mismatch,
    "sem/metric-drift": metric_drift,
}
