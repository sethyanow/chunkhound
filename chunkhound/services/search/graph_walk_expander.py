"""GraphWalkExpander — chunk-to-symbol-to-chunk graph expansion.

Takes seed chunks from semantic search, resolves them to symbols via
range overlap, walks the symbol_edges graph, resolves discovered symbols
back to chunks, and deduplicates against the seed set.

ch-nxu Step 17: All database access goes through the DatabaseProvider
protocol methods (symbol_overlap, graph_walk, chunk_resolution). The
protocol methods are implemented by both DuckDBProvider and LanceDBProvider,
which made the prior SQL-duplication workaround (to dodge the
services→mcp_server circular import) obsolete.
"""

from typing import Any

from chunkhound.interfaces.database_provider import DatabaseProvider


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

        Pipeline: seed chunks → symbols (overlap) → bidirectional graph walk →
        chunks (resolution) → dedup against seed set.

        Args:
            seed_chunks: Chunks with file_path, start_line, end_line keys.
            depth: Walk depth (default 2 hops).
            edge_kind: Optional edge kind filter (e.g. "calls").

        Returns:
            Discovered chunks not in seed set.
        """
        if not seed_chunks:
            return []

        # Stage 1: Resolve seed chunks → symbol FQNs via range overlap.
        seed_fqns = self._db.symbol_overlap(seed_chunks)
        if not seed_fqns:
            return []

        # Stage 2: Bidirectional walk from seed FQNs. directed=False is the
        # contract that was previously encoded as a UNION ALL in the inlined
        # CTE — passing it as a keyword argument keeps both backends aligned.
        walk_limit = len(seed_chunks) * 30
        walked_nodes = self._db.graph_walk(
            seed_fqns=seed_fqns,
            depth=depth,
            directed=False,
            edge_kind=edge_kind,
            limit=walk_limit,
        )[0]  # graph_walk returns (nodes, edges); the expander uses only nodes
        walked_fqns: list[str] = [node["fqn"] for node in walked_nodes]
        if not walked_fqns:
            return []

        # Stage 3: Resolve walked symbols → chunks.
        discovered_chunks = self._db.chunk_resolution(walked_fqns)

        # Stage 4: Deduplicate against seed set.
        seed_keys = {
            (c["file_path"], c["start_line"], c["end_line"]) for c in seed_chunks
        }
        return [
            c
            for c in discovered_chunks
            if (c["file_path"], c["start_line"], c["end_line"]) not in seed_keys
        ]
