---
id: ch-r4a
title: 'Task 3: symbol_context MCP tool (compound symbol profile)'
status: closed
type: task
priority: 1
owner: Seth
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

**Reused infrastructure (SRE-verified against current code):**
- `_uri_to_path` (tools.py:518) — convert URI to path
- `_location_to_dict` (tools.py:528) — format Location
- `_call_item_to_dict` (tools.py:539) — format CallHierarchyItem
- `_graph_walk` (tools.py:830) — graph traversal, returns `{results, edges, count}`
- `Language.from_file_extension` — resolve language from file path (used at lsp_impl line 616)
- `execute_tool` (tools.py:1178) — signature inspection maps `lsp_client_pool` (line 1227), `services`, `config` automatically
- `_generate_json_schema_from_signature` — skips `lsp_client_pool` in schema generation (line 180)

## Implementation

### Step 1: Write failing tests — symbol_context happy path + error cases
- File: `tests/test_mcp_tools_lsp.py` (extend with `TestSymbolContextTool` class)
- Mock both `lsp_client_pool` (LSP client with hover/definition/incoming_calls/outgoing_calls) and `services.provider.execute_query` (FQN lookup + graph walk data)
- `test_symbol_context`: Mock LSP client returning hover content, one definition location, one caller, one callee. Mock execute_query returning FQN lookup row + graph walk nodes + edges. Assert result dict has keys: `hover` (contents string), `definition` (list of location dicts), `callers` (list of call item dicts), `callees` (list of call item dicts), `graph_neighborhood` (dict with results/edges/count from _graph_walk format). Assert clean dict format — no raw LSP or DB column names.
- `test_symbol_context_pool_not_ready`: lsp_client_pool=None → `{"error": "lsp_not_ready", ...}`
- `test_symbol_context_unsupported_language`: file with `.xyz` extension → `{"error": "unsupported_language", ...}`
- `test_symbol_context_no_hover`: hover returns None → result has `"hover": null`, other fields still populated
- `test_symbol_context_no_symbol_in_index`: execute_query for FQN lookup returns [] → result has LSP fields but `"graph_neighborhood": null`
- `test_symbol_context_partial_lsp_failure`: hover succeeds but incoming_calls raises `Exception("connection reset")` → result has `"hover"` populated, `"callers": []`, other fields populated. Validates graceful degradation at the individual call level.
- `test_symbol_context_file_uri_input`: file passed as `"file:///workspace/foo.py"` → tool converts to path before Language check and FQN lookup. Assert same behavior as passing bare path.
- `test_symbol_context_graph_walk_failure`: FQN lookup succeeds but `_graph_walk` raises (mock `execute_query` to succeed for FQN then raise on graph walk queries) → result has LSP fields but `"graph_neighborhood": null`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py::TestSymbolContextTool -v -m ""` → fails

### Step 2: Implement symbol_context
- File: `chunkhound/mcp_server/tools.py`
- Add `SYMBOL_CONTEXT_DESCRIPTION` constant describing the compound profile tool
- Register: `@register_tool(description=SYMBOL_CONTEXT_DESCRIPTION, name="symbol_context")`
- Signature: `async def symbol_context_impl(lsp_client_pool: Any, services: Any, config: Any, file: str, line: int, character: int) -> dict[str, Any]`
- Guard: pool None → `{"error": "lsp_not_ready", ...}`
- Guard: unknown language → `{"error": "unsupported_language", ...}`
- Handle `file://` URI input: if `file.startswith("file://")`, convert via `_uri_to_path(file)` (same as lsp_impl lines 631-633). Do this BEFORE Language.from_file_extension check.
- Resolve workspace_root from config (same pattern as lsp_impl lines 625-628)
- Get LSP client from pool via `await lsp_client_pool.get(language_id, workspace_root)`
- Convert resolved file to `file_uri` via `Path(resolved_file).resolve().as_uri()` for LSP calls
- Define 4 async wrapper functions, each catching broad `Exception` → returning `None`/`[]`. Then `asyncio.gather(*wrappers)` (no `return_exceptions` needed since wrappers handle errors internally). Pattern:
  ```python
  async def _safe_hover():
      try: return await client.hover(file_uri, line, character)
      except Exception: return None
  ```
- Convert absolute file path to relative for FQN lookup: `os.path.relpath(str(Path(resolved_file).resolve()), workspace_root)`. The symbols table stores relative paths from population.
- FQN lookup: `SELECT fqn FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ? ORDER BY (range_end - range_start) ASC LIMIT 1` with relative file path + line
- If FQN found: wrap `_graph_walk(services, fqn, depth=1, edge_kind=None, limit=20)` in try/except → `None` on failure (DB errors should not kill the tool)
- Compose and return result dict: format hover via `hover_result.contents` / `None`, definition via `_location_to_dict`, callers/callees via `_call_item_to_dict`
- Run: → passes

### Step 3: Smoke test + full suite + commit
- `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""`
- `uv run pytest tests/test_smoke.py -v -n auto -m e2e`
- Commit and push

## Success Criteria
- [x] `symbol_context` tool registered with @register_tool, callable via execute_tool
- [x] Returns compound dict with `hover`, `definition`, `callers`, `callees`, `graph_neighborhood` keys
- [x] Graceful degradation: null hover when unavailable, null graph_neighborhood when FQN not in index
- [x] Pool not ready returns structured error (not crash)
- [x] Unsupported language returns structured error
- [x] Graph neighborhood uses 1-hop walk from resolved FQN
- [x] Partial LSP failure: one call raises, others still return data (not crash)
- [x] _graph_walk failure returns null graph_neighborhood (not crash)
- [x] file:// URI input handled correctly (converted before Language check)
- [x] `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""` → all pass (62/62)
- [x] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass (17/17)

## Anti-Patterns
- NO new SQL patterns — reuse `_graph_walk` for graph neighborhood
- NO raw LSP JSON-RPC in responses — use existing `_location_to_dict`, `_call_item_to_dict` helpers
- NO sequential LSP calls when they can be parallel — use `asyncio.gather`
- NO print() in MCP server code
- NO failing the whole tool when one LSP operation fails — graceful degradation

## Key Considerations
- File path for FQN lookup needs conversion from absolute to relative (relative to workspace_root). Use `os.path.relpath(resolved_absolute, workspace_root)`. Population service stores relative paths.
- `asyncio.gather` pattern: define 4 async wrapper functions that each catch broad `Exception` → return None/[]. Gather the wrappers (no `return_exceptions` needed). This is cleaner than `return_exceptions=True` + isinstance checks.
- The graph walk returned by `_graph_walk` has the same response format as the `graph` tool's walk operation — `{results, edges, count}`. Nest it as-is under `graph_neighborhood`.
- LSP `incoming_calls`/`outgoing_calls` may not be available (capability-gated by the LSP server). The broad `except Exception` in wrappers catches this along with `LSPCapabilityError`, `LSPTransportError`, `ConnectionResetError`, etc.
- Handle `file://` URIs before Language check — same pattern as lsp_impl (lines 631-633). Agents may pass URIs from editor integration.
- Wrap `_graph_walk` call in try/except: DuckDB errors (corrupt DB, missing table during migration) should return `graph_neighborhood: null`, not crash the tool.

### Adversarial Failure Catalog (SRE)

**Encoding Boundaries: FQN lookup path mismatch**
- Assumption: Input file path format matches `symbols.file_path` format
- Betrayal: `symbols.file_path` stores relative paths; input comes as absolute. Direct comparison returns zero rows.
- Consequence: graph_neighborhood silently always null. Tests with mocks won't catch this.
- Mitigation: `os.path.relpath(resolved_file, workspace_root)` before query. Step 2 now includes this explicitly.

**Dependency Treachery: unexpected LSP exception types**
- Assumption: LSP client raises only typed LSP exceptions
- Betrayal: `asyncio.TimeoutError`, `ConnectionResetError`, bare `Exception` from JSON-RPC framing
- Consequence: If catching only LSP types, unexpected types propagate and crash the tool
- Mitigation: Broad `except Exception` in async wrappers. Acceptable for a compound profile tool where partial data beats no data.

**Dependency Treachery: _graph_walk DB failure**
- Assumption: `_graph_walk` always returns a dict
- Betrayal: `execute_query` raises DuckDB error (schema migration in progress, corrupt index)
- Consequence: Unhandled exception kills the tool despite LSP data being fine
- Mitigation: Wrap `_graph_walk` in try/except → `graph_neighborhood: null`.

**Input Hostility: file:// URI hides extension**
- Assumption: `Language.from_file_extension` receives a filesystem path
- Betrayal: `file://` URI string → `from_file_extension` sees the URI scheme, not the extension → returns UNKNOWN
- Consequence: Tool returns "unsupported_language" error for a valid Python file
- Mitigation: URI-to-path conversion before Language check (same as lsp_impl).

## Log

- [2026-04-01T21:23:51Z] [Seth] SRE refinement complete. Verified all architecture claims against current code (line numbers updated: _uri_to_path→518, _graph_walk→830, execute_tool→1178). Added 3 missing tests (partial LSP failure, file:// URI, graph_walk DB failure). Clarified asyncio.gather pattern (broad-catch wrappers, not return_exceptions). Added file:// URI handling + relative path conversion to Step 2. Added adversarial catalog with 4 findings. All existing test patterns reviewed — follows execute_tool-based integration tests with _make_mock_pool/_make_mock_services helpers.
