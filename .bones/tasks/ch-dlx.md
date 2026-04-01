---
id: ch-dlx
title: 'Task 2: graph MCP tool (walk, reachability, boundary, overview)'
status: closed
type: task
priority: 1
owner: Seth
parent: ch-zyz
---







## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. Second task — ch-1ed (lsp + lsp_status) is closed.
Phase 2 delivered `symbols` and `symbol_edges` DuckDB tables populated by LSP. Phase 3 Task 1 delivered `lsp_client_pool` wiring through `execute_tool`, the `lsp` and `lsp_status` tools, and helpers `_uri_to_path`, `_location_to_dict`, `_call_item_to_dict`, `_diagnostic_to_dict` in `tools.py`.

**Blocked by:** None (ch-1ed closed)
**Unlocks:** symbol_context tool (needs graph queries), search extensions, get_stats graph data

## Requirements
From parent epic R4: `graph(operation, ...)` unified graph queries — walk, reachability, boundary, overview. All operations query DuckDB only (no live LSP calls). Deterministic.

## Design
**Cohesion seam:** One MCP tool with 4 DuckDB query operations against `symbols` and `symbol_edges` tables. Each operation is a SQL query (some recursive CTEs) returning clean structured dicts.

**Key decisions:**
- Tool signature: `(operation: Literal["walk","reachability","boundary","overview"], symbol: str | None, depth: int, edge_kind: str | None, scope: str | None, limit: int)` — `symbol` for walk, `scope` for reachability/boundary, `limit` for overview
- Query via `services.provider.execute_query(sql)` which returns `list[dict]`
- Response keys use clean names (`from_symbol`, `to_symbol`), not raw DB column names (`from_symbol_id`)
- No `lsp_client_pool` needed — pure DuckDB queries

**Schema (verified):**
- `symbols`: id, fqn, name, kind, language, file_id, file_path, range_start, range_end, type_signature, parent_fqn, confidence, lsp_server
- `symbol_edges`: id, from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server

## Implementation

### Step 1: Write failing tests — graph walk + edge filter + validation
- File: `tests/test_mcp_tools_lsp.py` (extend with `TestGraphTool` class)
- Mock `services.provider.execute_query` to return controlled row dicts
- `test_graph_walk`: call `execute_tool("graph", ..., arguments={"operation": "walk", "symbol": "module::MyClass", "depth": 2})`. Assert result has `results` list with dicts containing `fqn`, `name`, `kind`, `file_path`, and `edges` containing `from_symbol`, `to_symbol`, `edge_kind`.
- `test_graph_walk_edge_filter`: same but with `edge_kind="calls"`. Assert only call edges returned.
- `test_graph_walk_empty`: walk from nonexistent FQN returns `{"results": [], "edges": [], "count": 0}`.
- `test_graph_walk_missing_symbol`: walk with `symbol=None` returns `{"error": "missing_parameter", ...}`.
- `test_graph_walk_cycle`: mock execute_query to simulate A→B→A cycle. Assert walk terminates and returns finite results without hanging.
- `test_graph_invalid_operation`: operation="bogus" returns `{"error": "invalid_operation", ...}`.
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestGraphTool -v -m "" -k "walk or invalid"` → fails

### Step 2: Implement graph walk + edge filter
- File: `chunkhound/mcp_server/tools.py`
- Add `GRAPH_DESCRIPTION` constant (describe all 4 operations, parameter meanings)
- Register: `@register_tool(description=GRAPH_DESCRIPTION, name="graph")`
- Signature: `async def graph_impl(services: Any, operation: Literal["walk", "reachability", "boundary", "overview"], symbol: str | None = None, depth: int = 2, edge_kind: str | None = None, scope: str | None = None, limit: int = 20) -> dict[str, Any]`
- Implement walk operation: recursive CTE on `symbol_edges` starting from `symbol` FQN, joining `symbols` for metadata. `edge_kind` filter as optional WHERE clause. Depth-limited traversal.
- Return `{"results": [{"fqn": ..., "name": ..., "kind": ..., "file_path": ..., "depth": N}], "edges": [{"from_symbol": ..., "to_symbol": ..., "edge_kind": ..., "from_file": ..., "to_file": ...}], "count": N}`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestGraphTool -v -m "" -k "walk"` → passes

### Step 3: Write failing tests — reachability + boundary + scope validation
- `test_graph_reachability`: mock returns symbols reachable from scope roots. Assert result identifies unreachable symbols (complement).
- `test_graph_reachability_missing_scope`: reachability with `scope=None` returns `{"error": "missing_parameter", ...}`.
- `test_graph_boundary`: mock returns cross-scope edges. Assert result contains edges where `from_file` is inside scope but `to_file` is outside (or vice versa).
- `test_graph_boundary_missing_scope`: boundary with `scope=None` returns `{"error": "missing_parameter", ...}`.
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestGraphTool -v -m "" -k "reachability or boundary"` → fails

### Step 4: Implement reachability + boundary
- **reachability**: CTE finds all symbol FQNs reachable from symbols in `scope` (path prefix match on `file_path`). Then `SELECT FROM symbols WHERE fqn NOT IN (reachable_set) AND file_path LIKE scope||'%'`. Returns unreachable symbols.
- **boundary**: `SELECT FROM symbol_edges e JOIN symbols s1 ON e.from_symbol_id = s1.id JOIN symbols s2 ON e.to_symbol_id = s2.id WHERE s1.file_path LIKE scope||'%' AND s2.file_path NOT LIKE scope||'%'`. Returns boundary-crossing edges with both sides' symbol info.
- Run: → passes

### Step 5: Write failing tests — overview
- `test_graph_overview`: mock returns symbol connection counts. Assert result contains top symbols by edge count with per-edge_kind breakdown.
- Run: → fails

### Step 6: Implement overview
- **overview**: `SELECT s.fqn, s.name, s.kind, s.file_path, COUNT(*) as total_edges FROM symbols s JOIN symbol_edges e ON s.id = e.from_symbol_id OR s.id = e.to_symbol_id GROUP BY s.id, s.fqn, s.name, s.kind, s.file_path ORDER BY total_edges DESC LIMIT :limit`. Sub-query for edge_kind breakdown per symbol.
- Run: → passes

### Step 7: Smoke test + full suite + commit
- `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""`
- `uv run pytest tests/test_smoke.py -v -n auto -m e2e`
- Commit and push

## Success Criteria
- [x] `graph` tool registered with @register_tool, callable via execute_tool
- [x] `graph(walk)` returns connected symbols + edges from a starting FQN, respects depth and edge_kind filters
- [x] `graph(walk)` with nonexistent FQN returns empty results (not error)
- [x] `graph(walk)` on cyclic graph (A→B→A) terminates without hanging, returns finite results
- [x] `graph(reachability)` returns symbols unreachable from scope roots
- [x] `graph(boundary)` returns edges crossing a scope boundary
- [x] `graph(overview)` returns most-connected symbols with edge_kind breakdown, respects limit
- [x] All operations return clean structured dicts (no raw DB column names)
- [x] Invalid operation returns structured error dict
- [x] Missing required params (walk without symbol, reachability/boundary without scope) return structured error dict
- [x] `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""` → all pass (including prior lsp/lsp_status tests)
- [x] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass

## Anti-Patterns
- NO live LSP calls — graph tool is pure DuckDB
- NO raw DB column names in responses (from_symbol_id → from_symbol)
- NO print() in MCP server code
- NO changes to existing tool signatures
- NO SQL injection — use parameterized queries for user-provided symbol/scope values

## Key Considerations
- `execute_query` returns `list[dict]` — alias SQL columns for predictable keys
- DuckDB recursive CTEs: `WITH RECURSIVE` syntax works but mind performance on large graphs — depth parameter bounds traversal. **Must track visited FQNs to prevent infinite loops on cyclic graphs** (e.g., A calls B, B calls A). The depth parameter alone is not sufficient — the CTE needs an explicit visited-set or anti-join on already-seen rows.
- `edge_kind` values populated by Phase 2 (verified from `lsp_population.py:_edges_recursive`): `"defines"`, `"references"`, `"implements"`, `"called_by"`, `"calls"` — 5 values. Note: NOT "definitions"/"implementations" as originally estimated.
- `scope` parameter is a file path prefix (e.g., "chunkhound/mcp_server/"). Uses LIKE with `?` placeholder. **Escape `%` and `_` in user-provided scope** before appending `%` for prefix match — otherwise `scope="chunk_ound"` matches `"chunkhound"`.
- Parameterize all user inputs (`symbol`, `scope`, `edge_kind`) using `?` positional placeholders with `params` list — e.g., `execute_query("SELECT ... WHERE fqn = ?", [symbol])`. Never f-string user values into SQL.
- **Parameter validation per operation:** `walk` requires `symbol` (return error if None or empty string). `reachability` and `boundary` require `scope` (return error if None or empty string). `overview` requires neither. Return structured `{"error": "missing_parameter", "message": "..."}` for missing required params. Treat `""` same as None.
- **Limit/depth bounds:** Clamp `depth` to 1-20 range, `limit` to 1-100 range. Values outside range get clamped, not rejected.
- **Walk cycle prevention (structural):** DuckDB `WITH RECURSIVE` has no built-in cycle detection. Accumulate visited FQNs in an array column within the CTE, add anti-join (`AND to_fqn NOT = ANY(visited)`) in the recursive term. Combined with depth limit, prevents infinite loops on mutual recursion (A→B→A) and redundant traversal.
- **Walk result size:** `limit` parameter also applies to walk (max nodes returned). The recursive CTE should terminate early once limit is reached to prevent hub-symbol explosion (500 connections at depth=2 = 250K rows).
- **LIKE escaping for scope:** Escape `%` → `\%` and `_` → `\_` in scope before appending `%`. Use `ESCAPE '\'` clause: `file_path LIKE ? ESCAPE '\'`.
- **Overview OR-join performance:** `ON s.id = e.from_symbol_id OR s.id = e.to_symbol_id` performs poorly in DuckDB. Split into two subqueries (outgoing + incoming counts) combined with UNION ALL, then aggregate. Profile with EXPLAIN on non-trivial data.
- **Empty graph signal:** If `symbols` table is empty (population never ran), all operations return empty results with no error. The `GRAPH_DESCRIPTION` should note this so agents know to check `lsp_status` or `get_stats` if results are unexpectedly empty.

## Log

- [2026-04-01T20:58:25Z] [Seth] Debrief: graph MCP tool delivered — 4 operations (walk/reachability/boundary/overview), 12 unit tests + 18 adversarial tests. All 2448 tests pass. Key decisions: 2-query pattern for walk (CTE nodes + edge query), list_concat/list_contains for CTE cycle detection, UNION ALL for overview to avoid OR-join. SRE caught wrong edge_kind values (defines/implements/called_by, not definitions/implementations). Reflections: skeleton accuracy good after SRE additions; edge_kind mismatch was the biggest surprise. User correction: load Python skills before TDD. Next task: ch-r4a (symbol_context).
