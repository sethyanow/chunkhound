---
id: ch-dlx
title: 'Task 2: graph MCP tool (walk, reachability, boundary, overview)'
status: open
type: task
priority: 1
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

### Step 1: Write failing tests — graph walk + edge filter
- File: `tests/test_mcp_tools_lsp.py` (extend with `TestGraphTool` class)
- Mock `services.provider.execute_query` to return controlled row dicts
- `test_graph_walk`: call `execute_tool("graph", ..., arguments={"operation": "walk", "symbol": "module::MyClass", "depth": 2})`. Assert result has `results` list with dicts containing `fqn`, `name`, `kind`, `file_path`, and `edges` containing `from_symbol`, `to_symbol`, `edge_kind`.
- `test_graph_walk_edge_filter`: same but with `edge_kind="calls"`. Assert only call edges returned.
- `test_graph_walk_empty`: walk from nonexistent FQN returns `{"results": [], "count": 0}`.
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestGraphTool -v -m "" -k "walk"` → fails

### Step 2: Implement graph walk + edge filter
- File: `chunkhound/mcp_server/tools.py`
- Add `GRAPH_DESCRIPTION` constant (describe all 4 operations, parameter meanings)
- Register: `@register_tool(description=GRAPH_DESCRIPTION, name="graph")`
- Signature: `async def graph_impl(services: Any, operation: Literal["walk", "reachability", "boundary", "overview"], symbol: str | None = None, depth: int = 2, edge_kind: str | None = None, scope: str | None = None, limit: int = 20) -> dict[str, Any]`
- Implement walk operation: recursive CTE on `symbol_edges` starting from `symbol` FQN, joining `symbols` for metadata. `edge_kind` filter as optional WHERE clause. Depth-limited traversal.
- Return `{"results": [{"fqn": ..., "name": ..., "kind": ..., "file_path": ..., "depth": N}], "edges": [{"from_symbol": ..., "to_symbol": ..., "edge_kind": ..., "from_file": ..., "to_file": ...}], "count": N}`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestGraphTool -v -m "" -k "walk"` → passes

### Step 3: Write failing tests — reachability + boundary
- `test_graph_reachability`: mock returns symbols reachable from scope roots. Assert result identifies unreachable symbols (complement).
- `test_graph_boundary`: mock returns cross-scope edges. Assert result contains edges where `from_file` is inside scope but `to_file` is outside (or vice versa).
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
- [ ] `graph` tool registered with @register_tool, callable via execute_tool
- [ ] `graph(walk)` returns connected symbols + edges from a starting FQN, respects depth and edge_kind filters
- [ ] `graph(walk)` with nonexistent FQN returns empty results (not error)
- [ ] `graph(reachability)` returns symbols unreachable from scope roots
- [ ] `graph(boundary)` returns edges crossing a scope boundary
- [ ] `graph(overview)` returns most-connected symbols with edge_kind breakdown, respects limit
- [ ] All operations return clean structured dicts (no raw DB column names)
- [ ] Invalid operation returns structured error dict
- [ ] `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""` → all pass (including prior lsp/lsp_status tests)
- [ ] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass

## Anti-Patterns
- NO live LSP calls — graph tool is pure DuckDB
- NO raw DB column names in responses (from_symbol_id → from_symbol)
- NO print() in MCP server code
- NO changes to existing tool signatures
- NO SQL injection — use parameterized queries for user-provided symbol/scope values

## Key Considerations
- `execute_query` returns `list[dict]` — alias SQL columns for predictable keys
- DuckDB recursive CTEs: `WITH RECURSIVE` syntax works but mind performance on large graphs — depth parameter bounds traversal
- `edge_kind` values populated by Phase 2: expect "calls", "references", "definitions", "implementations" — verify during implementation
- `scope` parameter is a file path prefix (e.g., "chunkhound/mcp_server/") — use LIKE with proper escaping
- Parameterize all user inputs (`symbol`, `scope`, `edge_kind`) to prevent SQL injection
