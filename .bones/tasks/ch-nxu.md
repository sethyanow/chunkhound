---
id: ch-nxu
title: Abstract graph layer behind DatabaseProvider protocol
status: open
type: task
priority: 0
---

**Blocked by:** nothing — foundation fix
**Unlocks:** ch-zn5 connection architecture becomes solvable by backend switch. Phase 5 acceptance and Phase 6 resume on clean foundation. LanceDB becomes viable for graph features.

## Context

Phases 1-5 of ch-8e7 built the graph intelligence layer (symbols, symbol_edges, graph walks, fusion tools) directly against DuckDB SQL — raw `execute_query()` calls, sqlglot query builders generating DuckDB-dialect CTEs. None of it goes through the `DatabaseProvider` protocol. Result: LanceDB provider (2400+ lines, concurrent writes, persistent HNSW) has no graph support. The daemon/proxy/IPC architecture exists solely because DuckDB is the only viable backend for graph features.

**Callers to decouple:**

`lsp_population.py` — 10 raw SQL calls (all async):
- `execute_query_async`: SELECT id/fqn FROM symbols, SELECT id FROM files, SELECT id FROM symbols WHERE fqn, SELECT language FROM symbols, SELECT id/fqn/file_path FROM symbols WHERE range overlap
- `_batch_insert`: INSERT INTO symbols (batch)
- `_batch_insert_edges`: INSERT INTO symbol_edges (batch)
- `delete_file_edges`: DELETE FROM symbol_edges
- `delete_file_symbols`: DELETE FROM symbols

`mcp_server/tools/queries/` — sqlglot query builders:
- `common.py`: `bidirectional_edges()`, `visited_tracking_columns()`, `scope_filter()`
- `graph.py`: `build_walk_query()`, `build_walk_edges_query()`, `build_structural_walk_query()`
- `search.py`: `build_symbol_overlap_query()`, `build_chunk_resolution_query()`

`mcp_server/tools/` — tool implementations:
- `graph.py`: `graph_impl()` → `_graph_walk()`, `_graph_reachability()`, `_graph_boundary()`, `_graph_overview()`
- `fusion.py`: `test_targeting_impl()`, `impact_cascade_impl()`, `cross_language_check_impl()`, `semantic_diff_impl()`
- `search.py`: `_search_structural()`, `_search_symbols()`, `_apply_type_filter()`

## Requirements

1. All symbol/edge operations go through `DatabaseProvider` protocol methods
2. All graph walk/query operations go through `DatabaseProvider` protocol methods
3. Both DuckDB and LanceDB providers implement the new protocol methods
4. No raw `execute_query()` calls for symbol/edge operations outside provider implementations
5. No sqlglot query builders outside provider implementations
6. MCP tools and LSP population call provider methods, not SQL

## Design

New protocol methods derived from current raw SQL calls:

**Symbol/edge CRUD:**
- `insert_symbols_batch(symbols: list[SymbolRow]) -> None`
- `delete_symbols_by_file(file_id: int) -> None`
- `delete_edges_by_file(file_id: int) -> None`
- `query_symbols_by_file(file_id: int) -> list[dict]`
- `query_symbols_by_range(file_path: str, line: int) -> dict | None`
- `query_symbol_fqns_by_file(file_id: int) -> dict[str, int]` (fqn → id mapping)
- `insert_edges_batch(edges: list[EdgeRow]) -> None`
- Async variants for each

**Graph queries:**
- `graph_walk(seed_fqns: list[str], depth: int, directed: bool, edge_kind: str | None, limit: int) -> tuple[list[dict], list[dict]]` (nodes, edges)
- `graph_reachability(scope: str) -> list[dict]`
- `graph_boundary(scope: str) -> list[dict]`
- `graph_overview(scope: str | None, limit: int) -> list[dict]`
- `symbol_overlap(chunks: list[dict]) -> list[str]` (seed chunks → FQNs)
- `chunk_resolution(fqns: list[str]) -> list[dict]` (FQNs → chunks)
- `symbol_stats() -> dict` (counts for get_stats)

Define `SymbolRow` and `EdgeRow` as TypedDicts or dataclasses in `chunkhound/core/models/` alongside existing `Chunk` and `File`.

DuckDB: existing SQL logic moves into `_executor_*` methods. LanceDB: Lance tables + Python BFS for graph walks (no recursive CTEs in Lance).

## Implementation

### Step 1: Write failing test — protocol has symbol methods
File: `tests/unit/test_database_provider_protocol.py` (new)
Test that `DatabaseProvider` protocol declares: `insert_symbols_batch`, `delete_symbols_by_file`, `delete_edges_by_file`, `query_symbols_by_file`, `query_symbols_by_range`, `insert_edges_batch`, `query_symbol_fqns_by_file`. Use structural check. Fails because methods don't exist on protocol.

### Step 2: Add symbol/edge CRUD methods to DatabaseProvider protocol
File: `chunkhound/interfaces/database_provider.py`
Add abstract methods with signatures from Design section. Define `SymbolRow` and `EdgeRow` in `chunkhound/core/models/`.

### Step 3: Write failing test — protocol has graph query methods
Same test file. Test that protocol declares: `graph_walk`, `graph_reachability`, `graph_boundary`, `graph_overview`, `symbol_overlap`, `chunk_resolution`. Fails because methods don't exist.

### Step 4: Add graph query methods to DatabaseProvider protocol
File: `chunkhound/interfaces/database_provider.py`
Signatures from Design section.

### Step 5: Write failing test — DuckDB provider implements symbol CRUD
File: `tests/integration/test_duckdb_symbol_protocol.py` (new)
Fresh DuckDB provider → insert file → `insert_symbols_batch` → `query_symbols_by_file` → `delete_symbols_by_file` → verify clean. Fails because methods not on DuckDB provider.

### Step 6: Implement symbol/edge CRUD on DuckDBProvider
File: `chunkhound/providers/database/duckdb_provider.py`
Move existing SQL from `lsp_population.py` into `_executor_*` methods. Update `_executor_delete_file_completely` to call internal symbol/edge delete methods (cascade fix becomes part of protocol).

### Step 7: Write failing test — DuckDB provider implements graph queries
File: `tests/integration/test_duckdb_graph_protocol.py` (new)
Insert symbols + edges → `graph_walk` with seed FQN → verify returns nodes and edges. Fails because method not implemented.

### Step 8: Implement graph query methods on DuckDBProvider
File: `chunkhound/providers/database/duckdb_provider.py`
Move sqlglot query builder logic into `_executor_*` methods:
- `_executor_graph_walk` — absorbs `build_walk_query` + `build_walk_edges_query`
- `_executor_graph_reachability` — absorbs reachability SQL
- `_executor_graph_boundary` — absorbs boundary SQL
- `_executor_graph_overview` — absorbs overview SQL
- `_executor_symbol_overlap` — absorbs `build_symbol_overlap_query`
- `_executor_chunk_resolution` — absorbs `build_chunk_resolution_query`
- `_executor_symbol_stats` — symbol/edge counts

### Step 9: Write failing test — LanceDB provider implements symbol CRUD
File: `tests/integration/test_lancedb_symbol_protocol.py` (new)
Same shape as Step 5 against LanceDB provider. Fails because methods not implemented.

### Step 10: Implement symbol/edge CRUD on LanceDBProvider
File: `chunkhound/providers/database/lancedb_provider.py`
Create `symbols` and `symbol_edges` Lance tables in `_executor_create_schema`. Implement CRUD methods using Lance native query API. Same schema, Lance format.

### Step 11: Write failing test — LanceDB provider implements graph queries
Same shape as Step 7 against LanceDB. Fails because methods not implemented.

### Step 12: Implement graph query methods on LanceDBProvider
File: `chunkhound/providers/database/lancedb_provider.py`
Graph walk: Python BFS/DFS over Lance query results (no recursive CTEs). Query edges by FQN, walk in Python, return same shape as DuckDB. Reachability, boundary, overview: Lance queries with Python post-processing.

### Step 13: Switch lsp_population.py to protocol methods
File: `chunkhound/services/lsp_population.py`
Replace all 10 raw `execute_query_async` calls with protocol method calls. Delete raw SQL methods from `LSPPopulationService`.

### Step 14: Switch MCP graph tools to protocol methods
Files: `chunkhound/mcp_server/tools/graph.py`, `chunkhound/mcp_server/tools/fusion.py`
Replace `execute_query` calls with provider graph query methods. Delete or deprecate `mcp_server/tools/queries/graph.py` and `mcp_server/tools/queries/common.py`.

### Step 15: Switch MCP search tools to protocol methods
File: `chunkhound/mcp_server/tools/search.py`
Replace `_search_structural` and `_search_symbols` raw SQL with provider methods. Delete or deprecate symbol/graph parts of `mcp_server/tools/queries/search.py`.

### Step 16: Remove execute_query from non-provider code
Verify no symbol/edge/graph SQL remains outside provider implementations.

## Success Criteria
- [ ] `DatabaseProvider` protocol declares all symbol/edge/graph methods
- [ ] `DuckDBProvider` implements all new protocol methods (existing SQL in `_executor_*`)
- [ ] `LanceDBProvider` implements all new protocol methods (Lance tables + Python graph walk)
- [ ] `lsp_population.py` has zero `execute_query` calls — protocol methods only
- [ ] `mcp_server/tools/` has zero `execute_query` calls for symbol/edge/graph operations
- [ ] `mcp_server/tools/queries/graph.py` and `common.py` deleted or emptied (logic in providers)
- [ ] All existing tests pass on DuckDB backend
- [ ] New protocol tests pass on both DuckDB and LanceDB backends
- [ ] `chunkhound mcp` with `"provider": "lancedb"` returns graph tool results

## Anti-Patterns
- NO leaving `execute_query` as a backdoor for "just this one query" — if it touches symbols/edges/graph, it goes through the protocol
- NO DuckDB SQL dialect in provider-agnostic code — sqlglot builders belong inside DuckDBProvider only
- NO stub implementations that return empty — both providers fully implement or raise NotImplementedError
- NO changing MCP tool schemas or behavior — abstraction is internal, tool contracts stay the same
