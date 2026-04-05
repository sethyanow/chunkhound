---
id: ch-nxu
title: Abstract graph layer behind DatabaseProvider protocol
status: active
type: task
priority: 0
owner: Seth
---





**Blocked by:** nothing — foundation fix
**Unlocks:** ch-zn5 connection architecture becomes solvable by backend switch. Phase 5 acceptance and Phase 6 resume on clean foundation. LanceDB becomes viable for graph features.

## Context

Phases 1-5 of ch-8e7 built the graph intelligence layer (symbols, symbol_edges, graph walks, fusion tools) directly against DuckDB SQL — raw `execute_query()` calls, sqlglot query builders generating DuckDB-dialect CTEs. None of it goes through the `DatabaseProvider` protocol. Result: LanceDB provider (2400+ lines, concurrent writes, persistent HNSW) has no graph support. The daemon/proxy/IPC architecture exists solely because DuckDB is the only viable backend for graph features.

**Callers to decouple:**

`lsp_population.py` — 9 raw SQL calls (all async via `execute_query_async`):
- `_batch_insert`: INSERT INTO symbols (batch, 12-column tuple)
- `_batch_insert_edges`: INSERT INTO symbol_edges (batch, 9-column tuple)
- `delete_file_edges`: DELETE FROM symbol_edges WHERE from/to_symbol_id IN subquery
- `delete_file_symbols`: DELETE FROM symbols WHERE file_id
- `populate_file` L114: SELECT id, fqn FROM symbols WHERE file_id (fqn→id mapping after insert)
- `populate_files` L242: SELECT id, path FROM files (file list for batch population)
- `_populate_workspace_symbols` L318: SELECT id FROM files WHERE path (file_id lookup)
- `_populate_workspace_symbols` L334: SELECT id FROM symbols WHERE fqn AND file_path (dedup check)
- `_resolve_symbol` L501: SELECT id, fqn, file_path FROM symbols WHERE range overlap (innermost symbol at line)
- Note: `_populate_workspace_symbols` L342 calls `_batch_insert` with single-row list inside a loop — anti-pattern to fix during migration

`mcp_server/tools/queries/` — sqlglot query builders:
- `common.py`: `bidirectional_edges()`, `visited_tracking_columns()`, `scope_filter()`, `escape_like()`
- `graph.py`: `build_walk_query()`, `build_walk_edges_query()`, `build_reachability_all_symbols_query()`, `build_reachability_reachable_query()`, `build_boundary_query()`, `build_overview_query()`, `build_overview_breakdown_query()`
- `search.py`: `build_symbol_search_query()`, `build_symbol_count_query()`, `build_symbol_overlap_query()`, `build_structural_walk_query()`, `build_chunk_resolution_query()`, `build_type_filter_query()`

`mcp_server/tools/` — tool implementations:
- `graph.py`: `_graph_walk()` (2 execute_query), `_graph_reachability()` (2), `_graph_boundary()` (1), `_graph_overview()` (2)
- `fusion.py` internal helpers with raw SQL:
  - `_map_lines_to_symbols()` L114: SELECT symbols WHERE file_path AND range overlap
  - `_query_scope_symbols()` L170: SELECT symbols WHERE scope_filter
  - `_resolve_changed_to_fqns()` L371: SELECT DISTINCT fqn FROM symbols WHERE file_path
  - `_collect_test_fqns()` L412: SELECT symbols WHERE kind='Function' AND name LIKE 'test_%'
  - `_resolve_start_fqn()` L450: SELECT fqn FROM symbols WHERE range overlap ORDER BY narrowest
  - `_annotate_type_signatures()` L485: SELECT fqn, type_signature FROM symbols WHERE fqn IN (batch)
- `search.py`: `_search_symbols()` (2 execute_query via query builders), `_apply_type_filter()` (1)

`services/search/graph_walk_expander.py` — 3 inlined raw SQL calls (duplicates queries/ due to circular import avoidance):
- L130: symbol overlap query (seed chunks → FQNs via range overlap)
- L144: recursive CTE graph walk (bidirectional, visited tracking)
- L152: chunk resolution query (FQNs → chunks via file_id + range)

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
- `query_symbols_by_range(file_path: str, line: int) -> dict | None` (innermost symbol at position)
- `query_symbols_by_range_overlap(file_path: str, min_line: int, max_line: int) -> list[dict]` (all symbols overlapping a line range — used by fusion `_map_lines_to_symbols`)
- `query_symbol_fqns_by_file(file_id: int) -> dict[str, int]` (fqn → id mapping)
- `query_symbols_by_fqn_exists(fqn: str, file_path: str) -> bool` (dedup check for workspace symbols)
- `insert_edges_batch(edges: list[EdgeRow]) -> None`
- Async variants for each (via SerialDatabaseProvider thread delegation pattern)

**Symbol read queries** (used by fusion.py and search.py helpers):
- `query_symbols_by_scope(scope: str) -> list[dict]` (scope-prefix filtered, grouped by name — for `_query_scope_symbols`)
- `query_test_symbols(scope: str | None) -> list[dict]` (kind='Function', name LIKE 'test_%' — for `_collect_test_fqns`)
- `query_symbol_type_signatures(fqns: list[str]) -> dict[str, str | None]` (batch FQN → type_signature — for `_annotate_type_signatures`)
- `query_distinct_fqns_by_file_path(file_path: str) -> list[str]` (for `_resolve_changed_to_fqns`)

**Graph queries:**
- `graph_walk(seed_fqns: list[str], depth: int, directed: bool, edge_kind: str | None, limit: int) -> tuple[list[dict], list[dict]]` (nodes, edges)
- `graph_reachability(scope: str) -> list[dict]`
- `graph_boundary(scope: str, limit: int) -> list[dict]`
- `graph_overview(scope: str | None, limit: int) -> list[dict]`
- `symbol_overlap(chunks: list[dict]) -> list[str]` (seed chunks → FQNs via range overlap)
- `chunk_resolution(fqns: list[str]) -> list[dict]` (FQNs → chunks via file_id + range)
- `symbol_stats() -> dict` (counts for get_stats)

Define `SymbolRow` and `EdgeRow` as TypedDicts or dataclasses in `chunkhound/core/models/` alongside existing `Chunk` and `File`.

DuckDB: existing SQL logic moves into `_executor_*` methods. LanceDB: Lance tables + Python BFS for graph walks (no recursive CTEs in Lance). LanceDB BFS must track visited FQNs to prevent infinite loops on cyclic edges (mirrors DuckDB's `list_contains(r.visited, ...)` CTE guard).

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
File: `chunkhound/mcp_server/tools/graph.py`
Replace all 7 `execute_query` calls in `_graph_walk`, `_graph_reachability`, `_graph_boundary`, `_graph_overview` with provider graph query methods. Delete `mcp_server/tools/queries/graph.py`.

### Step 15: Switch fusion.py helpers to protocol methods
File: `chunkhound/mcp_server/tools/fusion.py`
Replace raw SQL in 6 internal helpers:
- `_map_lines_to_symbols` → `provider.query_symbols_by_range_overlap()`
- `_query_scope_symbols` → `provider.query_symbols_by_scope()`
- `_resolve_changed_to_fqns` → `provider.query_distinct_fqns_by_file_path()`
- `_collect_test_fqns` → `provider.query_test_symbols()`
- `_resolve_start_fqn` → `provider.query_symbols_by_range()`
- `_annotate_type_signatures` → `provider.query_symbol_type_signatures()`
Delete `mcp_server/tools/queries/common.py` (scope_filter, escape_like absorbed into providers; bidirectional_edges/visited_tracking absorbed into DuckDB graph queries).

### Step 16: Switch MCP search tools to protocol methods
File: `chunkhound/mcp_server/tools/search.py`
Replace `_search_symbols` (2 execute_query) and `_apply_type_filter` (1) with provider methods. Delete symbol/graph parts of `mcp_server/tools/queries/search.py`.

### Step 17: Switch graph_walk_expander.py to protocol methods
File: `chunkhound/services/search/graph_walk_expander.py`
Replace 3 inlined SQL calls with: `provider.symbol_overlap()`, `provider.graph_walk()`, `provider.chunk_resolution()`. Delete the local `_build_overlap_query`, `_build_walk_query`, `_build_resolution_query` functions. This also eliminates the circular import that forced the SQL duplication.

### Step 18: Verify no symbol/edge/graph SQL outside providers
Grep for `execute_query` calls touching symbols/symbol_edges tables. Verify zero remain outside provider implementations. Also verify `mcp_server/tools/queries/` directory has no remaining symbol/graph/edge SQL (pure search queries like semantic/regex may remain if they don't touch symbols).

## Success Criteria
- [x] `DatabaseProvider` protocol declares all symbol/edge/graph methods (CRUD + read queries + graph queries)
- [ ] `DuckDBProvider` implements all new protocol methods (existing SQL in `_executor_*`)
- [ ] `LanceDBProvider` implements all new protocol methods (Lance tables + Python graph walk with visited tracking)
- [ ] `lsp_population.py` has zero `execute_query` calls — protocol methods only
- [ ] `mcp_server/tools/` has zero `execute_query` calls for symbol/edge/graph operations
- [ ] `services/search/graph_walk_expander.py` has zero raw SQL — uses protocol methods only
- [ ] `mcp_server/tools/queries/graph.py` and `common.py` deleted (logic absorbed into providers)
- [ ] All existing tests pass on DuckDB backend
- [ ] New protocol tests pass on both DuckDB and LanceDB backends
- [ ] `chunkhound mcp` with `"provider": "lancedb"` returns graph tool results
- [ ] Graph walk with cyclic edges (A→B→A) terminates correctly on both providers
- [ ] Batch insert handles large symbol sets (>1000 rows) via internal chunking

## Key Considerations

### SRE Findings
- **LanceDB cycle detection**: Python BFS must maintain a `visited: set[str]` of FQNs to prevent infinite loops. DuckDB handles this in-CTE via `list_contains(r.visited, s2.fqn)`. Test with a cyclic edge (A→B→A) on both providers.
- **Empty symbol tables**: All graph query methods must return empty results gracefully when symbols/symbol_edges tables have zero rows. Don't assume tables are populated.
- **`_populate_workspace_symbols` batching**: Currently calls `_batch_insert` with single-row list inside a for-loop. During Step 13 migration, collect rows and call `insert_symbols_batch` once per language (not per symbol).
- **`escape_like` reuse**: `common.py`'s `escape_like()` is used by both graph queries and fusion helpers. When absorbing common.py into providers, expose escape_like as a utility (or inline it) — don't let it block deletion.
- **Async boundary**: `lsp_population.py` uses `execute_query_async` (via `SerialDatabaseProvider` thread delegation). MCP tool functions are sync and use `execute_query`. New protocol methods need both sync and async variants to match existing patterns.

### Adversarial Failure Catalog

**Input Hostility: DuckDB batch insert parameter limits**
- Assumption: All symbols from a file can be inserted in one parameterized statement
- Betrayal: 12 params/symbol × 2700+ symbols (large files) = 32,400 params. DuckDB's per-statement parameter limit could reject the batch.
- Consequence: Population silently fails for large files. Tests pass on small fixtures.
- Mitigation: `insert_symbols_batch` must chunk internally (e.g., 500 rows per statement). Same for `insert_edges_batch` (9 params/edge). Match existing `insert_embeddings_batch` batching pattern.

**Resource Exhaustion: LanceDB Python BFS frontier explosion**
- Assumption: BFS at depth N explores a manageable number of nodes
- Betrayal: Dense graph at depth=5 with 100 edges/node = 100^5 candidates before visited-pruning. Python BFS holds frontier + visited in memory. DuckDB CTE has same theoretical explosion but manages memory in-engine.
- Consequence: OOM or extreme latency on dense subgraphs — fails at production scale, passes unit tests with small fixtures.
- Mitigation: Cap BFS frontier size (not just output limit). If frontier exceeds threshold (e.g., 10K), stop exploration and return partial results. Document the cap in the protocol method docstring.

**Temporal Betrayal: Delete-insert atomicity gap**
- Assumption: Callers always call delete_symbols_by_file then insert_symbols_batch in sequence
- Betrayal: If insert_symbols_batch fails (DB error, OOM), the file's symbols are deleted but not repopulated. Subsequent queries return empty for that file until next indexing run.
- Consequence: Silent data loss — graph queries miss the file's symbols. No error visible to the user.
- Mitigation: Existing code has this same gap. Protocol methods should document that callers must handle insert failure by re-triggering population. Consider wrapping delete+insert in a transaction in the DuckDB provider (not in the protocol — LanceDB may not support transactions).

**Dependency Treachery: Return shape contracts between providers**
- Assumption: DuckDB and LanceDB protocol methods return identical dict structures
- Betrayal: DuckDB returns dicts with column names from SQL aliases; LanceDB returns dicts from Arrow->dict conversion with potentially different key names or types (e.g., numpy types vs Python types, None vs missing key).
- Consequence: Callers work on DuckDB, fail on LanceDB with KeyError or wrong-type comparisons.
- Mitigation: Protocol method docstrings must specify exact return dict keys. Integration tests must assert key presence and value types on both providers. Use the same dict-building code in both providers where possible.

**State Corruption: _populate_workspace_symbols TOCTOU race**
- Assumption: The check-then-insert sequence (query_symbols_by_fqn_exists → insert_symbols_batch) is atomic
- Betrayal: Between check and insert, concurrent population inserts the same (fqn, file_path). DuckDB has no UNIQUE constraint on (fqn, file_path) in the symbols table.
- Consequence: Duplicate symbols in the table. Graph walks return duplicate nodes. Edge counts inflate.
- Mitigation: Accept-and-dedup at query time (cheaper than adding constraints). Current code has this same race — not a regression. If duplicates become a problem later, add UNIQUE constraint with ON CONFLICT IGNORE.

**Encoding Boundaries: LanceDB LIKE/filter semantics**
- Assumption: LanceDB's SQL query layer handles LIKE patterns identically to DuckDB (including custom ESCAPE characters)
- Betrayal: LanceDB's SQL dialect may not support `ESCAPE '!'` syntax. scope_filter patterns that work on DuckDB may silently match nothing (or everything) on LanceDB.
- Consequence: Scope-filtered queries return wrong results on LanceDB. graph_boundary and graph_overview affected.
- Mitigation: Test scope_filter patterns explicitly on LanceDB in integration tests. If ESCAPE not supported, implement prefix matching via string comparison (startswith) instead of LIKE in the LanceDB provider.

## Anti-Patterns
- NO leaving `execute_query` as a backdoor for "just this one query" — if it touches symbols/edges/graph, it goes through the protocol
- NO DuckDB SQL dialect in provider-agnostic code — sqlglot builders belong inside DuckDBProvider only
- NO stub implementations that return empty — both providers fully implement or raise NotImplementedError
- NO changing MCP tool schemas or behavior — abstraction is internal, tool contracts stay the same

## Log

- [2026-04-05T23:14:37Z] [Seth] SRE review complete. Findings: (1) Added graph_walk_expander.py as missing caller (3 raw SQL calls). (2) Added 4 missing protocol methods for fusion.py helpers: query_symbols_by_scope, query_test_symbols, query_symbol_type_signatures, query_distinct_fqns_by_file_path, query_symbols_by_range_overlap, query_symbols_by_fqn_exists. (3) Added LanceDB BFS cycle detection requirement. (4) Added Key Considerations section with 5 edge cases. (5) Updated callers list with precise line numbers and query descriptions. (6) Expanded Steps 14-16 into Steps 14-18 for clearer caller migration scope. No design changes — all additions are gap-fills for callers the skeleton missed.
- [2026-04-05T23:17:10Z] [Seth] Adversarial planning complete. 6 failure catalog entries: (1) DuckDB batch param limits — chunk at 500 rows. (2) LanceDB BFS frontier explosion — cap frontier at 10K. (3) Delete-insert atomicity gap — document caller responsibility. (4) Return shape contracts — test dict keys on both providers. (5) workspace symbols TOCTOU race — accept-and-dedup. (6) LanceDB LIKE/ESCAPE semantics — test explicitly, fall back to startswith. Added 2 new success criteria: cyclic edge termination, large batch chunking.
- [2026-04-05T23:22:01Z] [Seth] Steps 1-4 complete. Protocol fully declared: 9 symbol/edge CRUD methods (+ 6 async variants), 7 graph query methods, 4 symbol read query methods, 2 data types (SymbolRow, EdgeRow). 23 unit tests passing. Committed and pushed. SC1 checked.
