---
id: ch-ron
title: 'GraphWalkExpander: chunk-to-symbol-to-chunk graph expansion'
status: open
type: task
priority: 1
parent: ch-z2o
---

**Blocked by:** None (first task in Phase 5; Phase 4 ch-dar is closed)
**Unlocks:** UnifiedSearch integration task (wiring expander into the pipeline), code_research prompt templates

## Context

Phase 5 (ch-z2o) adds graph-based expansion to the search pipeline. This first task builds the core `GraphWalkExpander` — a standalone component that takes seed chunks, resolves them to symbols, walks the `symbol_edges` graph, and resolves discovered symbols back to chunks.

The component is independent of the existing `MultiHopStrategy` (which walks embedding-space neighbors via `find_similar_chunks`). Both produce chunks; a later task merges and dedupes them.

Key existing infrastructure:
- `build_walk_query()` at `chunkhound/mcp_server/tools/queries/graph.py:18` — recursive CTE walking `symbol_edges` from a seed FQN
- `_map_lines_to_symbols()` at `chunkhound/mcp_server/tools/fusion.py:82` — resolves file+lines to symbols via range overlap (symbols table)
- `get_chunks_in_range()` on `DatabaseProvider` — returns chunks overlapping a line range in a file
- `execute_query()` on `DatabaseProvider` — raw SQL for custom queries
- Chunks have `file_id`, `start_line`, `end_line`; symbols have `file_path`, `range_start`, `range_end`, `fqn`

## Design

**Algorithm:**
1. Receive seed chunks (list of dicts with file_path, start_line, end_line, chunk_id)
2. Resolve chunks → symbols: for each chunk, query `symbols` table where `file_path` matches and ranges overlap
3. Collect unique FQNs from resolved symbols
4. For each seed FQN, walk `symbol_edges` via recursive CTE (reuse `build_walk_query` pattern) up to `depth` hops, optionally filtered by `edge_kind`
5. Resolve discovered symbols → chunks: for each discovered symbol, use `get_chunks_in_range` (or equivalent SQL) to find overlapping chunks
6. Deduplicate discovered chunks against seed chunk_ids
7. Return discovered chunks

**Location:** `chunkhound/services/search/graph_walk_expander.py` — new file alongside `multi_hop_strategy.py`

**Key decisions:**
- Composition, not inheritance — same pattern as MultiHopStrategy (takes `database_provider` via constructor)
- Pure DuckDB queries — no LSP round-trips, no embeddings, no LLM calls
- Graceful on empty graph: if `symbols` or `symbol_edges` tables are empty, return empty list (no errors)
- The expander doesn't rerank — it returns raw chunks. Reranking happens after merge in UnifiedSearch (later task).

**Interface:**
```python
class GraphWalkExpander:
    def __init__(self, database_provider: DatabaseProvider) -> None: ...

    async def expand(
        self,
        seed_chunks: list[dict[str, Any]],
        depth: int = 2,
        edge_kind: str | None = None,
    ) -> list[dict[str, Any]]: ...
```

## Requirements

From parent epic ch-z2o:
- Expander takes seed chunks from initial semantic search → resolves to symbols via range overlap → walks symbol_edges 1-2 hops → returns additional chunks
- Depth configurable (default 2 hops)
- Edge kind filtering available (e.g., expand only via `calls` edges, not `references`)
- NO hard dependency on graph data being populated — if symbols/edges tables are empty, expander returns empty set gracefully

## Implementation

### Step 1: Write failing test — chunk-to-symbol resolution
Create `tests/test_graph_expander.py`. Test that given seed chunks overlapping known symbols, the expander resolves them to the correct FQNs. Use a mock `DatabaseProvider` that returns known symbols for range overlap queries and empty results for the walk. Assert: correct FQNs extracted from seed chunks.

### Step 2: Write failing test — symbol graph walk
Test that given resolved FQNs, the expander walks `symbol_edges` and discovers additional symbols at configured depth. Mock the walk query to return neighbor symbols. Assert: discovered symbols include 1-hop and 2-hop neighbors.

### Step 3: Write failing test — symbol-to-chunk resolution
Test that discovered symbols are resolved back to chunks via range overlap. Mock `execute_query` to return chunk rows for discovered symbol ranges. Assert: output contains chunks from discovered symbols, not from seeds.

### Step 4: Write failing test — deduplication
Test that chunks already in the seed set are excluded from results. Arrange: one discovered symbol maps to a chunk that's also a seed. Assert: that chunk is not in the output.

### Step 5: Write failing test — graceful empty graph
Test that when `symbols` table returns no rows for seed chunks (empty graph), expander returns `[]` without error. Test that when `symbol_edges` has no rows, walk returns seeds' symbols only, and those chunks are deduped away → empty output.

### Step 6: Write failing test — edge_kind filtering
Test that passing `edge_kind="calls"` restricts the walk to only `calls` edges. Mock returns both `calls` and `references` edges. Assert: only symbols reachable via `calls` appear.

### Step 7: Write failing test — depth configuration
Test that `depth=1` returns only direct neighbors, not 2-hop neighbors. Assert: 2-hop symbols absent from results.

### Step 8: Implement GraphWalkExpander
Create `chunkhound/services/search/graph_walk_expander.py`. Implement the class following the design above. Use `execute_query` for chunk→symbol and symbol→chunk resolution queries. Reuse `build_walk_query` from `chunkhound/mcp_server/tools/queries/graph.py` for the graph walk CTE. Run tests after each method implementation.

### Step 9: Export from search module
Add `GraphWalkExpander` to `chunkhound/services/search/__init__.py` exports.

### Step 10: Run full test suite
`uv run pytest -m "unit or integration or e2e" tests/ -v > /tmp/test_suite_ch_ron.out 2>&1`

## Success Criteria
- [ ] `GraphWalkExpander` class exists at `chunkhound/services/search/graph_walk_expander.py`
- [ ] Chunk → symbol resolution via range overlap works (file_path + line range intersection)
- [ ] Symbol graph walk uses recursive CTE on `symbol_edges`, configurable depth (default 2)
- [ ] Discovered symbols resolved back to chunks via range overlap
- [ ] Results deduped against seed chunk_ids
- [ ] `edge_kind` parameter filters the walk to specific edge types
- [ ] Empty `symbols`/`symbol_edges` tables → returns `[]` gracefully
- [ ] Exported from `chunkhound/services/search/__init__.py`
- [ ] `uv run pytest tests/test_graph_expander.py -v` → all pass
- [ ] Full test suite passes

## Anti-Patterns
- NO embedding calls or LLM calls — this is pure DuckDB graph traversal
- NO reranking in the expander — that's the caller's job (UnifiedSearch integration task)
- NO modifying MultiHopStrategy or SingleHopStrategy
- NO importing from `mcp_server.tools.fusion` — if reusing query patterns from `queries/graph.py`, import from there or inline the SQL
