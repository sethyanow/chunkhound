"""Query builders for graph MCP tool operations.

Each function returns (sql, params) — pure functions with no side effects.
Compose shared fragments from common.py for bidirectional edges, scope
filtering, and cycle tracking.
"""

from typing import Any

from .common import (
    bidirectional_edges,
    escape_like,
    scope_filter,
    visited_tracking_columns,
)


def build_walk_query(
    symbol: str,
    depth: int,
    edge_kind: str | None,
    limit: int,
    directed: bool = False,
) -> tuple[str, list[Any]]:
    """Recursive CTE: find all reachable nodes from seed FQN.

    Args:
        directed: When True, traverse forward edges only (from_fqn → to_fqn).
            When False (default), traverse bidirectionally. Directed mode is
            needed for impact_cascade (callers only, not callees).

    Returns (sql, params) where params are [symbol, depth, edge_kind?, limit].
    """
    # Edge subquery: bidirectional or forward-only
    if directed:
        edge_sql = "SELECT from_fqn AS src, to_fqn AS dst, edge_kind FROM symbol_edges"
    else:
        bidir = bidirectional_edges()
        edge_sql = bidir.sql(dialect="duckdb")

    # Cycle tracking expressions
    append_expr, contains_expr = visited_tracking_columns("s2", "fqn")

    # Edge kind filter clause (optional)
    edge_filter_sql = ""
    params: list[Any] = [symbol, depth]
    if edge_kind:
        edge_filter_sql = "AND e.edge_kind = ?"
        params.append(edge_kind)
    params.append(limit)

    # Render AST fragments to SQL strings, then compose
    append_sql = append_expr.sql(dialect="duckdb")
    contains_sql = contains_expr.sql(dialect="duckdb")

    sql = f"""
        WITH RECURSIVE reachable AS (
            SELECT s.fqn, s.name, s.kind, s.file_path, 0 AS depth,
                   [s.fqn] AS visited
            FROM symbols s
            WHERE s.fqn = ?

            UNION ALL

            SELECT s2.fqn, s2.name, s2.kind, s2.file_path,
                   r.depth + 1,
                   {append_sql}
            FROM reachable r
            JOIN ({edge_sql}) e ON e.src = r.fqn
            JOIN symbols s2 ON s2.fqn = e.dst
            WHERE r.depth < ?
              AND NOT {contains_sql}
              {edge_filter_sql}
        )
        SELECT DISTINCT fqn, name, kind, file_path, depth
        FROM reachable
        ORDER BY depth, fqn
        LIMIT ?
    """

    return sql, params


def build_walk_edges_query(fqns: list[str], edge_kind: str | None) -> tuple[str, list[Any]]:
    """Fetch edges between a set of discovered FQNs.

    Returns (sql, params) where params are [*fqns, *fqns, edge_kind?].
    """
    placeholders = ", ".join(["?"] * len(fqns))
    params: list[Any] = list(fqns) + list(fqns)

    edge_filter = ""
    if edge_kind:
        edge_filter = "AND e.edge_kind = ?"
        params.append(edge_kind)

    sql = f"""
        SELECT e.from_fqn, e.to_fqn, e.edge_kind, e.from_file, e.to_file
        FROM symbol_edges e
        WHERE e.from_fqn IN ({placeholders})
          AND e.to_fqn IN ({placeholders})
          {edge_filter}
    """

    return sql, params


def build_reachability_all_symbols_query(scope: str) -> tuple[str, list[Any]]:
    """Query 1 of reachability: all symbols in scope.

    Returns (sql, params) with scope pattern param.
    """
    scope_sql, scope_params = scope_filter(scope)

    sql = f"""
        SELECT fqn, name, kind, file_path
        FROM symbols
        WHERE {scope_sql}
    """

    return sql, scope_params


def build_reachability_reachable_query(scope: str) -> tuple[str, list[Any]]:
    """Query 2 of reachability: reachable FQNs via outbound-only recursive CTE.

    Outbound-only is intentional — reachability answers "what's reachable from
    entry points?" which is directional. Uses UNION (not UNION ALL) for dedup.

    Returns (sql, params) with two scope pattern params.
    """
    scope_sql, scope_params = scope_filter(scope)

    sql = f"""
        WITH RECURSIVE reachable AS (
            SELECT DISTINCT s.fqn
            FROM symbols s
            WHERE {scope_sql}

            UNION

            SELECT DISTINCT s2.fqn
            FROM reachable r
            JOIN symbol_edges e ON e.from_fqn = r.fqn
            JOIN symbols s2 ON s2.fqn = e.to_fqn
            WHERE {scope_sql}
        )
        SELECT fqn FROM reachable
    """

    params: list[Any] = scope_params + scope_params
    return sql, params


def build_boundary_query(scope: str, limit: int) -> tuple[str, list[Any]]:
    """Edges crossing a scope boundary — one side in scope, other outside.

    Returns (sql, params) with 4 scope pattern params + limit.
    """
    escaped = escape_like(scope)
    pattern = escaped + "%"

    sql = """
        SELECT
            e.from_fqn, s1.name AS from_name, s1.kind AS from_kind, e.from_file,
            e.to_fqn, s2.name AS to_name, s2.kind AS to_kind, e.to_file,
            e.edge_kind
        FROM symbol_edges e
        JOIN symbols s1 ON e.from_fqn = s1.fqn AND e.from_file = s1.file_path
        JOIN symbols s2 ON e.to_fqn = s2.fqn AND e.to_file = s2.file_path
        WHERE (
            (e.from_file LIKE ? ESCAPE '!' AND e.to_file NOT LIKE ? ESCAPE '!')
            OR
            (e.from_file NOT LIKE ? ESCAPE '!' AND e.to_file LIKE ? ESCAPE '!')
        )
        LIMIT ?
    """

    params: list[Any] = [pattern, pattern, pattern, pattern, limit]
    return sql, params


def build_overview_query(scope: str | None, limit: int) -> tuple[str, list[Any]]:
    """Most-connected symbols via bidirectional UNION ALL edge counting.

    Returns (sql, params) with optional scope pattern + limit.
    """
    scope_clause = ""
    params: list[Any] = []
    if scope:
        escaped = escape_like(scope)
        pattern = escaped + "%"
        scope_clause = "WHERE s.file_path LIKE ? ESCAPE '!'"
        params.append(pattern)

    sql = f"""
        WITH edge_counts AS (
            SELECT s.fqn, s.name, s.kind, s.file_path,
                   COUNT(*) AS total_edges
            FROM symbols s
            JOIN (
                SELECT from_fqn AS fqn FROM symbol_edges
                UNION ALL
                SELECT to_fqn AS fqn FROM symbol_edges
            ) AS all_refs ON all_refs.fqn = s.fqn
            {scope_clause}
            GROUP BY s.fqn, s.name, s.kind, s.file_path
        )
        SELECT fqn, name, kind, file_path, total_edges
        FROM edge_counts
        ORDER BY total_edges DESC
        LIMIT ?
    """

    params.append(limit)
    return sql, params


def build_overview_breakdown_query(fqns: list[str]) -> tuple[str, list[Any]]:
    """Per-symbol edge_kind breakdown for a set of FQNs.

    Returns (sql, params) where params = fqns.
    """
    placeholders = ", ".join(["?"] * len(fqns))

    sql = f"""
        SELECT s.fqn, e.edge_kind, COUNT(*) AS edge_count
        FROM symbols s
        JOIN symbol_edges e ON e.from_fqn = s.fqn OR e.to_fqn = s.fqn
        WHERE s.fqn IN ({placeholders})
        GROUP BY s.fqn, e.edge_kind
    """

    return sql, list(fqns)
