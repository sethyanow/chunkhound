---
id: ch-r4a
title: 'Task 3: symbol_context MCP tool (compound symbol profile)'
status: open
type: task
priority: 1
parent: ch-zyz
---



## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. Third task — ch-1ed (lsp + lsp_status) and ch-dlx (graph) are closed.
Phase 3 Task 1 delivered `lsp` and `lsp_status` tools with helpers `_uri_to_path`, `_location_to_dict`, `_call_item_to_dict` in `tools.py`. Task 2 delivered `graph` tool with `_graph_walk`, `_graph_reachability`, `_graph_boundary`, `_graph_overview`, `_escape_like` in `tools.py`.

**Blocked by:** None (ch-dlx closed)
**Unlocks:** search extensions (search type:symbols/structural, type_filter), get_stats graph data

## Requirements
From parent epic R4: `symbol_context(file, line, character)` compound symbol profile returning hover + definition + callers + callees + graph neighborhood in one response.

## Design
**Cohesion seam:** One MCP tool that composes existing LSP client calls and graph walk. No new SQL — reuses `_graph_walk` for neighborhood and `_location_to_dict`/`_call_item_to_dict` for LSP result formatting.

**Key decisions:**
- Tool signature: `(file: str, line: int, character: int)` — no operation dispatch, always returns full profile
- Needs `lsp_client_pool` (live LSP), `services` (FQN lookup + graph walk), `config` (workspace root)
- Use `asyncio.gather` for 4 independent LSP calls (hover, definition, incoming_calls, outgoing_calls)
- FQN lookup via `services.provider.execute_query` on `symbols` table (file_path + range overlap), then `_graph_walk` for 1-hop neighborhood
- Graceful degradation: if any individual LSP call fails or returns empty, include null/empty for that field — don't fail the whole tool
- Graph neighborhood is optional: if FQN lookup returns nothing (symbol not in index), return LSP results with `graph_neighborhood: null`

**Reused infrastructure (verified):**
- `_uri_to_path` (tools.py:515) — convert URI to path
- `_location_to_dict` (tools.py:528) — format Location
- `_call_item_to_dict` (tools.py:539) — format CallHierarchyItem
- `_graph_walk` (tools.py:~840) — graph traversal, returns `{results, edges, count}`
- `Language.from_file_extension` — resolve language from file path
- `execute_tool` signature inspection maps `lsp_client_pool`, `services`, `config` automatically

## Implementation

### Step 1: Write failing tests — symbol_context happy path + error cases
- File: `tests/test_mcp_tools_lsp.py` (extend with `TestSymbolContextTool` class)
- Mock both `lsp_client_pool` (LSP client with hover/definition/incoming_calls/outgoing_calls) and `services.provider.execute_query` (FQN lookup + graph walk data)
- `test_symbol_context`: Mock LSP client returning hover content, one definition location, one caller, one callee. Mock execute_query returning FQN lookup row + graph walk nodes + edges. Assert result dict has keys: `hover` (contents string), `definition` (list of location dicts), `callers` (list of call item dicts), `callees` (list of call item dicts), `graph_neighborhood` (dict with results/edges/count from _graph_walk format). Assert clean dict format — no raw LSP or DB column names.
- `test_symbol_context_pool_not_ready`: lsp_client_pool=None → `{"error": "lsp_not_ready", ...}`
- `test_symbol_context_unsupported_language`: file with `.xyz` extension → `{"error": "unsupported_language", ...}`
- `test_symbol_context_no_hover`: hover returns None → result has `"hover": null`, other fields still populated
- `test_symbol_context_no_symbol_in_index`: execute_query for FQN lookup returns [] → result has LSP fields but `"graph_neighborhood": null`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestSymbolContextTool -v -m ""` → fails

### Step 2: Implement symbol_context
- File: `chunkhound/mcp_server/tools.py`
- Add `SYMBOL_CONTEXT_DESCRIPTION` constant describing the compound profile tool
- Register: `@register_tool(description=SYMBOL_CONTEXT_DESCRIPTION, name="symbol_context")`
- Signature: `async def symbol_context_impl(lsp_client_pool: Any, services: Any, config: Any, file: str, line: int, character: int) -> dict[str, Any]`
- Guard: pool None → `{"error": "lsp_not_ready", ...}`
- Guard: unknown language → `{"error": "unsupported_language", ...}`
- Resolve workspace_root from config (same pattern as lsp_impl)
- Get LSP client from pool
- `asyncio.gather` 4 LSP calls: hover, go_to_definition, incoming_calls, outgoing_calls — each wrapped in try/except returning None/[] on failure
- FQN lookup: `SELECT fqn FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ? ORDER BY (range_end - range_start) ASC LIMIT 1` with relative file path + line
- If FQN found: call `_graph_walk(services, fqn, depth=1, edge_kind=None, limit=20)` for 1-hop neighborhood
- Compose and return result dict
- Run: → passes

### Step 3: Smoke test + full suite + commit
- `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""`
- `uv run pytest tests/test_smoke.py -v -n auto -m e2e`
- Commit and push

## Success Criteria
- [ ] `symbol_context` tool registered with @register_tool, callable via execute_tool
- [ ] Returns compound dict with `hover`, `definition`, `callers`, `callees`, `graph_neighborhood` keys
- [ ] Graceful degradation: null hover when unavailable, null graph_neighborhood when FQN not in index
- [ ] Pool not ready returns structured error (not crash)
- [ ] Unsupported language returns structured error
- [ ] Graph neighborhood uses 1-hop walk from resolved FQN
- [ ] `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass

## Anti-Patterns
- NO new SQL patterns — reuse `_graph_walk` for graph neighborhood
- NO raw LSP JSON-RPC in responses — use existing `_location_to_dict`, `_call_item_to_dict` helpers
- NO sequential LSP calls when they can be parallel — use `asyncio.gather`
- NO print() in MCP server code
- NO failing the whole tool when one LSP operation fails — graceful degradation

## Key Considerations
- File path for FQN lookup needs conversion from absolute to relative (relative to workspace_root). The `lsp_impl` resolves workspace_root from `config.target_dir` — reuse that pattern.
- `asyncio.gather` with `return_exceptions=True` so one failing LSP call doesn't cancel the others.
- The graph walk returned by `_graph_walk` has the same response format as the `graph` tool's walk operation — `{results, edges, count}`. Nest it as-is under `graph_neighborhood`.
- LSP `incoming_calls`/`outgoing_calls` may not be available (capability-gated by the LSP server). If `LSPCapabilityError` is raised, return empty list for that field.
