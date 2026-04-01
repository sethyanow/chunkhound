---
id: ch-1ed
title: 'Task 1: lsp + lsp_status MCP tools + infrastructure wiring'
status: open
type: task
priority: 1
parent: ch-zyz
---



## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. First task — no prior tasks in this phase.
Phase 2 delivered: `LSPClient` with 8 operations + `LSPClientPool` (lazy, respawn-on-degraded) + `symbols`/`symbol_edges` DuckDB tables. `MCPServerBase._lsp_pool` already holds the pool instance.

**Blocked by:** None (first task)
**Unlocks:** graph tool, symbol_context tool, search extensions (all need lsp_client_pool available via execute_tool)

## Requirements
From parent epic R4: Primitive MCP tools — `lsp(file, line, character, operation)` unified LSP tool and `lsp_status()` server health.

## Design
**Cohesion seam:** Expose the existing LSP client pool as MCP tools. Three layers of change:
1. **Infrastructure wiring** — thread `lsp_client_pool` through `execute_tool`/`handle_tool_call`/schema generation skip list
2. **`lsp` tool** — unified tool dispatching to 7 LSP operations, translating domain types to clean dicts
3. **`lsp_status` tool** — health/readiness query across all active LSP servers

**Key decisions:**
- `lsp` tool signature: `(file: str, line: int, character: int, operation: Literal[...])` — matches epic R4 spec
- File → language_id resolution: use file extension mapping (tree-sitter layer has this; reuse or create lightweight mapping)
- workspace_root: derive from `services` (DatabaseServices knows the project dir) or config
- Response format: clean dicts with `file_path`, `line`, `character`, `name`, `kind` etc. — no raw LSP JSON-RPC
- Capability gating: catch `LSPCapabilityError` from client, return structured error dict (not crash/exception)

## Implementation

### Step 1: Write failing test — lsp_client_pool injection wiring
- File: `tests/test_mcp_tools_lsp.py` (new)
- Test `_generate_json_schema_from_signature` skips `lsp_client_pool` param
- Test `execute_tool` passes `lsp_client_pool` kwarg to tool implementation when signature declares it
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "wiring"` → fails

### Step 2: Wire lsp_client_pool into tool infrastructure
- `chunkhound/mcp_server/tools.py`:
  - Add `"lsp_client_pool"` to skip list in `_generate_json_schema_from_signature` (alongside `services`, `embedding_manager`, etc.)
  - Add `lsp_client_pool` param to `execute_tool()` signature (default None)
  - Add `elif param_name == "lsp_client_pool":` mapping in execute_tool's param loop
- `chunkhound/mcp_server/common.py`:
  - Add `lsp_client_pool` param to `handle_tool_call()` signature (default None)
  - Pass through to `execute_tool(lsp_client_pool=lsp_client_pool)`
- `chunkhound/mcp_server/stdio.py`:
  - In `_register_tools` closure, add `lsp_client_pool=self._lsp_pool` to `handle_tool_call()` call
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "wiring"` → passes

### Step 3: Write failing test — lsp tool operations
- File: `tests/test_mcp_tools_lsp.py` (extend)
- Tests for each of the 7 operations via `execute_tool("lsp", ...)`:
  - definition: returns list of `{file_path, line, character, end_line, end_character}` dicts
  - references: returns list of location dicts
  - implementations: returns list of location dicts
  - callers: returns list of `{name, kind, file_path, line, character}` dicts
  - callees: returns list of similar dicts
  - hover: returns `{contents: str, range: {...}}` dict
  - diagnostics: returns list of `{message, severity, line, character, source}` dicts
- Test capability-gate: mock LSPClient that raises LSPCapabilityError → tool returns error dict, not crash
- Mock: `LSPClientPool.get()` returns mock `LSPClient` with controlled return values
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_tool"` → fails

### Step 4: Implement `lsp` MCP tool
- File: `chunkhound/mcp_server/tools.py` (extend with new tool)
- Register via `@register_tool(description=LSP_DESCRIPTION, name="lsp")`
- Signature: `async def lsp_impl(lsp_client_pool, services, file: str, line: int, character: int, operation: Literal["definition", "references", "implementations", "callers", "callees", "hover", "diagnostics"])`
- Internal logic:
  - Resolve file path → language_id via extension mapping
  - Determine workspace_root from services context
  - Get client: `client = await lsp_client_pool.get(language_id, workspace_root)`
  - Construct file URI: `Path(file).resolve().as_uri()`
  - Dispatch to client method based on operation (match/case or dict dispatch)
  - Convert domain types (SymbolInfo, Location, HoverResult, CallHierarchyItem, Diagnostic) to clean dicts
  - Catch `LSPCapabilityError` → return `{"error": "capability_not_supported", "message": str(e), "operation": operation}`
  - Catch `LSPError` → return `{"error": "lsp_error", "message": str(e)}`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_tool"` → passes

### Step 5: Write failing test — lsp_status tool
- File: `tests/test_mcp_tools_lsp.py` (extend)
- Test: calling `execute_tool("lsp_status", ...)` returns dict with `servers` key containing per-server entries
- Each entry: `{language_id, workspace_root, state, capabilities: [...], server_info, degraded_reason}`
- Mock: pool with 2 clients (one READY, one DEGRADED)
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_status"` → fails

### Step 6: Implement `lsp_status` MCP tool
- Register via `@register_tool(description=LSP_STATUS_DESCRIPTION, name="lsp_status")`
- Signature: `async def lsp_status_impl(lsp_client_pool)`
- Iterate pool's `_clients` dict → build status dict per (language_id, workspace_root) entry
- Return `{"servers": [...], "total": N, "ready": N, "degraded": N}`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_status"` → passes

### Step 7: Smoke test + full suite
- `uv run pytest tests/test_mcp_tools_lsp.py -v` → all pass
- `uv run pytest tests/test_smoke.py -v -n auto` → all pass
- Commit and push

## Success Criteria
- [ ] `lsp` tool registered and callable with all 7 operations (definition, references, implementations, callers, callees, hover, diagnostics)
- [ ] `lsp` tool returns clean structured dicts (no raw JSON-RPC framing, no LSP protocol details)
- [ ] `lsp` tool capability-gates: calling unsupported operation returns structured error, not crash
- [ ] `lsp_status` tool returns per-server state, capabilities, readiness
- [ ] Both tools follow existing `@register_tool` pattern with schema auto-generation
- [ ] `lsp_client_pool` wired through execute_tool/handle_tool_call/schema skip list
- [ ] All existing MCP tools unchanged in behavior (zero regression)
- [ ] `uv run pytest tests/test_mcp_tools_lsp.py -v` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
- NO raw LSP JSON-RPC in tool responses — translate to clean domain dicts
- NO tool params that expose LSP protocol details (textDocument, position objects)
- NO changes to existing tool signatures
- NO print() in MCP server code
