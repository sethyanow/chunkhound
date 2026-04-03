"""sqlglot query builders for search MCP tool operations.

Each function returns (sql, params) — pure functions with no side effects.
Compose shared fragments from common.py for bidirectional edges, scope
filtering, and cycle tracking.
"""

from typing import Any

import sqlglot

from .common import (
    bidirectional_edges,
    escape_like,
    scope_filter,
    visited_tracking_columns,
)


def _build_symbol_conditions(
    query: str,
    path: str | None,
    type_filter: str | None,
) -> tuple[list[str], list[Any]]:
    """Build WHERE conditions for symbol search/count queries.

    Returns (conditions, params) — shared between search and count builders.
    """
    conditions: list[str] = []
    params: list[Any] = []

    if query:
        escaped = escape_like(query)
        like_pattern = f"%{escaped}%"
        conditions.append("(name LIKE ? ESCAPE '\\' OR fqn LIKE ? ESCAPE '\\')")
        params.extend([like_pattern, like_pattern])

    if path:
        scope_sql, scope_params = scope_filter(path)
        conditions.append(scope_sql)
        params.extend(scope_params)

    if type_filter:
        escaped_type = escape_like(type_filter)
        conditions.append("type_signature LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped_type}%")

    return conditions, params


def build_symbol_search_query(
    query: str,
    path: str | None,
    type_filter: str | None,
    limit: int,
    offset: int,
) -> tuple[str, list[Any]]:
    """LIKE-based search on symbol name/fqn with optional path and type filters.

    Returns (sql, params) where params vary based on which filters are active.
    """
    conditions, params = _build_symbol_conditions(query, path, type_filter)
    where_clause = " AND ".join(conditions) if conditions else "1 = 1"

    sql = f"""
        SELECT fqn, name, kind, language, file_path, range_start, range_end,
               type_signature
        FROM symbols
        WHERE {where_clause}
        ORDER BY name
        LIMIT ?
        OFFSET ?
    """
    params.extend([limit, offset])

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), params


def build_symbol_count_query(
    query: str,
    path: str | None,
    type_filter: str | None,
) -> tuple[str, list[Any]]:
    """Count query for symbol search pagination.

    Uses same WHERE conditions as build_symbol_search_query but returns COUNT(*).
    """
    conditions, params = _build_symbol_conditions(query, path, type_filter)
    where_clause = " AND ".join(conditions) if conditions else "1 = 1"

    sql = f"SELECT COUNT(*) AS total FROM symbols WHERE {where_clause}"

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), params


def build_symbol_overlap_query(
    chunks: list[dict],
) -> tuple[str, list[Any]]:
    """Find symbol FQNs overlapping semantic result chunk ranges.

    Each chunk dict must have 'file_path', 'start_line', 'end_line'.
    Returns (sql, params) with 3 params per chunk.
    """
    if not chunks:
        raise ValueError("chunks must not be empty")

    conditions: list[str] = []
    params: list[Any] = []
    for chunk in chunks:
        conditions.append(
            "(s.file_path = ? AND s.range_start <= ? AND s.range_end >= ?)"
        )
        params.extend([chunk["file_path"], chunk["end_line"], chunk["start_line"]])

    where_clause = " OR ".join(conditions)
    sql = f"SELECT DISTINCT s.fqn, s.file_id FROM symbols s WHERE {where_clause}"

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), params


def build_structural_walk_query(
    seed_fqns: list[str],
    depth: int,
    limit: int,
) -> tuple[str, list[Any]]:
    """Recursive CTE: find all reachable nodes from seed FQNs, bidirectional.

    Uses bidirectional_edges() for both forward and reverse edge traversal.
    Uses visited_tracking_columns() for cycle detection.

    Returns (sql, params) where params are [*seed_fqns, depth, limit].
    """
    if not seed_fqns:
        raise ValueError("seed_fqns must not be empty")

    bidir = bidirectional_edges()
    append_expr, contains_expr = visited_tracking_columns("s2", "fqn")

    fqn_placeholders = ", ".join(["?"] * len(seed_fqns))
    params: list[Any] = list(seed_fqns) + [depth, limit]

    bidir_sql = bidir.sql(dialect="duckdb")
    append_sql = append_expr.sql(dialect="duckdb")
    contains_sql = contains_expr.sql(dialect="duckdb")

    sql = f"""
        WITH RECURSIVE reachable AS (
            SELECT s.fqn, 0 AS depth, [s.fqn] AS visited
            FROM symbols s
            WHERE s.fqn IN ({fqn_placeholders})

            UNION ALL

            SELECT s2.fqn, r.depth + 1,
                   {append_sql}
            FROM reachable r
            JOIN ({bidir_sql}) e ON e.src = r.fqn
            JOIN symbols s2 ON s2.fqn = e.dst
            WHERE r.depth < ?
              AND NOT {contains_sql}
        )
        SELECT DISTINCT fqn FROM reachable
        ORDER BY fqn
        LIMIT ?
    """

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), params


def build_chunk_resolution_query(
    fqns: list[str],
) -> tuple[str, list[Any]]:
    """Resolve symbol FQNs to chunks via file_id + range overlap.

    Returns (sql, params) where params are the FQN list.
    Column aliases: file_path, content, start_line, end_line.
    """
    if not fqns:
        raise ValueError("fqns must not be empty")

    placeholders = ", ".join(["?"] * len(fqns))

    sql = f"""
        SELECT DISTINCT f.path AS file_path, c.code AS content,
               c.start_line, c.end_line
        FROM chunks c
        JOIN files f ON c.file_id = f.id
        JOIN symbols s ON s.file_id = f.id
          AND s.range_start >= c.start_line
          AND s.range_end <= c.end_line
        WHERE s.fqn IN ({placeholders})
    """

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), list(fqns)


def build_type_filter_query(
    results: list[dict],
    type_filter: str,
) -> tuple[str, list[Any]]:
    """Batch symbol lookup for type_signature overlap with chunk results.

    Each result dict must have 'file_path', 'start_line', 'end_line'.
    Returns (sql, params) with 3 params per result + 1 for type_filter.
    Column aliases: file_path, range_start, range_end.
    """
    if not results:
        raise ValueError("results must not be empty")

    escaped = escape_like(type_filter)

    conditions: list[str] = []
    params: list[Any] = []
    for r in results:
        conditions.append(
            "(s.file_path = ? AND s.range_start <= ? AND s.range_end >= ?)"
        )
        params.extend([r["file_path"], r["end_line"], r["start_line"]])

    where_clause = " OR ".join(conditions)
    params.append(f"%{escaped}%")

    sql = f"""
        SELECT DISTINCT s.file_path, s.range_start, s.range_end
        FROM symbols s
        WHERE ({where_clause}) AND s.type_signature LIKE ? ESCAPE '\\'
    """

    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb"), params
