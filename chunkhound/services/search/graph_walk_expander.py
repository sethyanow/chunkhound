"""GraphWalkExpander — chunk-to-symbol-to-chunk graph expansion.

Takes seed chunks from semantic search, resolves them to symbols via
range overlap, walks the symbol_edges graph, resolves discovered symbols
back to chunks, and deduplicates against the seed set.

SQL queries are inlined rather than imported from mcp_server.tools.queries
to avoid circular imports (services → mcp_server → database_factory → services).
The queries mirror build_symbol_overlap_query, build_structural_walk_query,
and build_chunk_resolution_query from queries/search.py.
"""

from typing import Any

from chunkhound.interfaces.database_provider import DatabaseProvider


def _build_overlap_query(
    chunks: list[dict[str, Any]],
) -> tuple[str, list[Any]]:
    """Find symbol FQNs overlapping chunk ranges.

    Each chunk dict must have file_path, start_line, end_line.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for chunk in chunks:
        conditions.append(
            "(s.file_path = ? AND s.range_start <= ? AND s.range_end >= ?)"
        )
        params.extend([chunk["file_path"], chunk["end_line"], chunk["start_line"]])

    where_clause = " OR ".join(conditions)
    sql = f"SELECT DISTINCT s.fqn, s.file_id FROM symbols s WHERE {where_clause}"
    return sql, params


def _build_walk_query(
    seed_fqns: list[str],
    depth: int,
    limit: int,
    edge_kind: str | None = None,
) -> tuple[str, list[Any]]:
    """Recursive CTE: find reachable symbols from seed FQNs, bidirectional."""
    fqn_placeholders = ", ".join(["?"] * len(seed_fqns))
    params: list[Any] = list(seed_fqns) + [depth]

    edge_filter_sql = ""
    if edge_kind:
        edge_filter_sql = "AND e.edge_kind = ?"
        params.append(edge_kind)

    params.append(limit)

    sql = f"""
        WITH RECURSIVE reachable AS (
            SELECT s.fqn, 0 AS depth, [s.fqn] AS visited
            FROM symbols s
            WHERE s.fqn IN ({fqn_placeholders})

            UNION ALL

            SELECT s2.fqn, r.depth + 1,
                   list_concat(r.visited, [s2.fqn])
            FROM reachable r
            JOIN (
                SELECT from_fqn AS src, to_fqn AS dst, edge_kind FROM symbol_edges
                UNION ALL
                SELECT to_fqn AS src, from_fqn AS dst, edge_kind FROM symbol_edges
            ) e ON e.src = r.fqn
            JOIN symbols s2 ON s2.fqn = e.dst
            WHERE r.depth < ?
              AND NOT list_contains(r.visited, s2.fqn)
              {edge_filter_sql}
        )
        SELECT DISTINCT fqn FROM reachable
        ORDER BY fqn
        LIMIT ?
    """
    return sql, params


def _build_resolution_query(
    fqns: list[str],
) -> tuple[str, list[Any]]:
    """Resolve symbol FQNs to chunks via file_id + range overlap."""
    placeholders = ", ".join(["?"] * len(fqns))
    sql = f"""
        SELECT DISTINCT c.id AS chunk_id, f.path AS file_path,
               c.code AS content, c.start_line, c.end_line
        FROM chunks c
        JOIN files f ON c.file_id = f.id
        JOIN symbols s ON s.file_id = f.id
          AND s.range_start >= c.start_line
          AND s.range_end <= c.end_line
        WHERE s.fqn IN ({placeholders})
    """
    return sql, list(fqns)


class GraphWalkExpander:
    """Expand seed chunks via symbol graph walk."""

    def __init__(self, database_provider: DatabaseProvider) -> None:
        self._db = database_provider

    async def expand(
        self,
        seed_chunks: list[dict[str, Any]],
        depth: int = 2,
        edge_kind: str | None = None,
    ) -> list[dict[str, Any]]:
        """Expand seed chunks through symbol graph walk.

        Pipeline: seed chunks → symbols (overlap) → graph walk → chunks (resolution) → dedup.

        Args:
            seed_chunks: Chunks with file_path, start_line, end_line keys.
            depth: Walk depth (default 2 hops).
            edge_kind: Optional edge kind filter (e.g. "calls").

        Returns:
            Discovered chunks not in seed set.
        """
        if not seed_chunks:
            return []

        # Stage 1: Resolve seed chunks → symbol FQNs via range overlap
        overlap_sql, overlap_params = _build_overlap_query(seed_chunks)
        seed_symbols = self._db.execute_query(overlap_sql, overlap_params)
        seed_fqns = [s["fqn"] for s in seed_symbols]

        if not seed_fqns:
            return []

        # Stage 2: Walk symbol_edges graph from seed FQNs
        walk_limit = len(seed_chunks) * 30
        walk_sql, walk_params = _build_walk_query(
            seed_fqns=seed_fqns,
            depth=depth,
            limit=walk_limit,
            edge_kind=edge_kind,
        )
        walked = self._db.execute_query(walk_sql, walk_params)
        walked_fqns = [w["fqn"] for w in walked]

        if not walked_fqns:
            return []

        # Stage 3: Resolve walked symbols → chunks
        chunk_sql, chunk_params = _build_resolution_query(fqns=walked_fqns)
        discovered_chunks = self._db.execute_query(chunk_sql, chunk_params)

        # Stage 4: Deduplicate against seed set
        seed_keys = {
            (c["file_path"], c["start_line"], c["end_line"]) for c in seed_chunks
        }
        return [
            c
            for c in discovered_chunks
            if (c["file_path"], c["start_line"], c["end_line"]) not in seed_keys
        ]
