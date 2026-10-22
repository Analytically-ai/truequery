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

from sqlglot import exp


@dataclass(frozen=True)
class CheckContext:
    """What a check may know beyond the statement itself."""
    dialect: str | None = None
    path: str = "<input>"
    mode: str = "build"
    keys: dict = field(default_factory=dict)  # relation -> candidate keys

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
        sel = _owning_select(join)
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


def _from_keys(sel, join, this_alias):
    """Alias keys of every top-level FROM item except this one."""
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
    sel = _owning_select(join)
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
    sel = _owning_select(join)
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
        if len(keys) == 1 and _picks_one_row(stmt, window):
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
}
