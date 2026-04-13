"""Query builders for search MCP tool operations.

ch-nxu Step 16 removed ``build_symbol_search_query``, ``build_symbol_count_query``
and ``build_type_filter_query`` — their logic moved into ``DuckDBProvider.
search_symbols`` and ``filter_chunks_by_symbol_type_signature``. The remaining
three builders (``build_symbol_overlap_query``, ``build_structural_walk_query``,
``build_chunk_resolution_query``) are still used by
``services/search/graph_walk_expander.py`` and will be removed by Step 17.

Each function returns (sql, params) — pure functions with no side effects.
Compose shared fragments from common.py for bidirectional edges, scope
filtering, and cycle tracking.
"""

from typing import Any

from .common import (
    bidirectional_edges,
    visited_tracking_columns,
)


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
        conditions.append("(s.file_path = ? AND s.range_start <= ? AND s.range_end >= ?)")
        params.extend([chunk["file_path"], chunk["end_line"], chunk["start_line"]])

    where_clause = " OR ".join(conditions)
    sql = f"SELECT DISTINCT s.fqn, s.file_id FROM symbols s WHERE {where_clause}"

    return sql, params


def build_structural_walk_query(
    seed_fqns: list[str],
    depth: int,
    limit: int,
    edge_kind: str | None = None,
) -> tuple[str, list[Any]]:
    """Recursive CTE: find all reachable nodes from seed FQNs, bidirectional.

    Uses bidirectional_edges() for both forward and reverse edge traversal.
    Uses visited_tracking_columns() for cycle detection.

    Args:
        seed_fqns: Starting symbol FQNs for the walk.
        depth: Maximum walk depth (hops).
        limit: Maximum result count.
        edge_kind: Optional edge kind filter (e.g. "calls"). When set,
            only edges with matching edge_kind are traversed.

    Returns (sql, params) where params are [*seed_fqns, depth, edge_kind?, limit].
    """
    if not seed_fqns:
        raise ValueError("seed_fqns must not be empty")

    bidir = bidirectional_edges()
    append_expr, contains_expr = visited_tracking_columns("s2", "fqn")

    fqn_placeholders = ", ".join(["?"] * len(seed_fqns))
    params: list[Any] = list(seed_fqns) + [depth]

    # Optional edge kind filter
    edge_filter_sql = ""
    if edge_kind:
        edge_filter_sql = "AND e.edge_kind = ?"
        params.append(edge_kind)

    params.append(limit)

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
              {edge_filter_sql}
        )
        SELECT DISTINCT fqn FROM reachable
        ORDER BY fqn
        LIMIT ?
    """

    return sql, params


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

    return sql, list(fqns)


# build_type_filter_query removed by ch-nxu Step 16 — logic moved to
# DuckDBProvider.filter_chunks_by_symbol_type_signature and
# LanceDBProvider.filter_chunks_by_symbol_type_signature.
