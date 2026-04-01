---
id: ch-1ed
title: 'Task 1: lsp + lsp_status MCP tools + infrastructure wiring'
status: closed
type: task
priority: 1
owner: Seth
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
- File → language_id resolution: `Language.from_file_extension(file).value` → string key for `LANGUAGE_SERVER_REGISTRY` and `LSPClientPool.get()`. Import from `chunkhound.core.types.common.Language`.
- workspace_root: derive from `config.target_dir` (`config` is already wired through `execute_tool` via the skip list). `DatabaseServices` has no workspace_root field — do NOT try to get it from services.
- Response format: clean dicts with `file_path`, `line`, `character`, `name`, `kind` etc. — no raw LSP JSON-RPC
- Capability gating: catch `LSPCapabilityError` from client, return structured error dict (not crash/exception)
- Error handling: also catch `LSPTransportError` (server binary not found, server crashed) and `LSPError` (base class)

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
- Test transport-error: mock LSPClientPool.get() that raises LSPTransportError → tool returns error dict
- Test unknown extension: file="foo.xyz" → Language.UNKNOWN, no registry entry → clear error dict
- Test nonexistent file: file="/nonexistent/path.py" → resolved gracefully (pool.get still works, LSP handles file existence)
- Mock: `LSPClientPool.get()` returns mock `LSPClient` with controlled return values
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_tool"` → fails

### Step 4: Implement `lsp` MCP tool
- File: `chunkhound/mcp_server/tools.py` (extend with new tool)
- Register via `@register_tool(description=LSP_DESCRIPTION, name="lsp")`
- Signature: `async def lsp_impl(lsp_client_pool, services, file: str, line: int, character: int, operation: Literal["definition", "references", "implementations", "callers", "callees", "hover", "diagnostics"])`
- Internal logic:
  - Resolve file path → language_id: `Language.from_file_extension(file).value` (returns string like "python")
  - Guard: if language is `Language.UNKNOWN`, return `{"error": "unsupported_language", "message": "..."}`
  - Determine workspace_root: `str(config.target_dir)` (config injected via execute_tool skip list)
  - Get client: `client = await lsp_client_pool.get(language_id, workspace_root)`
  - Construct file URI: `Path(file).resolve().as_uri()`
  - Dispatch to client method based on operation (match/case or dict dispatch)
  - Convert domain types (SymbolInfo, Location, HoverResult, CallHierarchyItem, Diagnostic) to clean dicts
  - Note: `diagnostics` operation only uses file (ignores line/char) — pass URI only to `get_diagnostics(uri)`
  - Catch `LSPCapabilityError` → return `{"error": "capability_not_supported", "message": str(e), "operation": operation}`
  - Catch `LSPTransportError` → return `{"error": "server_not_available", "message": str(e)}`
  - Catch `LSPError` → return `{"error": "lsp_error", "message": str(e)}`
- Run: `uv run pytest tests/test_mcp_tools_lsp.py -v -k "lsp_tool"` → passes

### Step 5: Write failing test — lsp_status tool
- File: `tests/test_mcp_tools_lsp.py` (extend)
- Test: calling `execute_tool("lsp_status", ...)` returns dict with `servers` key containing per-server entries
- Each entry: `{language_id, workspace_root, state, capabilities: [...], server_info, degraded_reason}`
- Mock: pool with 2 clients (one READY, one DEGRADED)
- Test empty pool: pool with no active clients → returns `{"servers": [], "total": 0, "ready": 0, "degraded": 0}`
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
- [x] `lsp` tool registered and callable with all 7 operations (definition, references, implementations, callers, callees, hover, diagnostics)
- [x] `lsp` tool returns clean structured dicts (no raw JSON-RPC framing, no LSP protocol details)
- [x] `lsp` tool capability-gates: calling unsupported operation returns structured error, not crash
- [x] `lsp` tool returns structured error when pool is None (not ready), transport fails, or language unsupported
- [x] `lsp_status` tool returns per-server state, capabilities, readiness
- [x] Both tools follow existing `@register_tool` pattern with schema auto-generation
- [x] `lsp_client_pool` wired through execute_tool/handle_tool_call/schema skip list
- [x] All existing MCP tools unchanged in behavior (zero regression)
- [x] `uv run pytest tests/test_mcp_tools_lsp.py -v` → all pass
- [x] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Key Considerations
- `config.target_dir` could be `None` if config resolution failed — guard with fallback to `Path.cwd()` or return error
- `LSPClientPool.get()` does lazy spawn — first call for a language will be slow (server startup). Tests must mock, not spawn real servers.
- `diagnostics` is the only operation that doesn't use `line`/`char` — accept params per R4 spec but only pass URI to `get_diagnostics()`
- `lsp_status` accesses `pool._clients` (private) — this is the pragmatic approach since pool has no public inventory method
- File URI construction: `Path(file).resolve().as_uri()` — must resolve relative paths against workspace_root, not CWD
- `Language.from_file_extension()` handles Makefiles by filename, not extension — works correctly for this use case

### Adversarial Failure Catalog

**Temporal Betrayal: lsp_client_pool is None at tool call time**
- Assumption: `self._lsp_pool` is populated when tool is called
- Betrayal: `_deferred_connect_and_start` runs in background task; tool call arrives before pool is constructed
- Consequence: `await None.get(...)` → AttributeError → ugly MCP error
- Mitigation: Guard in tool impl: `if lsp_client_pool is None: return {"error": "lsp_not_ready", ...}`. Same pattern as `requires_embeddings` gating.

**Encoding Boundaries: file param is already a URI**
- Assumption: `file` is a filesystem path
- Betrayal: Agent passes `file:///path/to/file.py` instead of `/path/to/file.py`
- Consequence: `Path("file:///path").resolve()` produces garbage URI
- Mitigation: Guard: if `file.startswith("file://")`, extract path from URI before processing.

**Temporal Betrayal: lsp_status concurrent dict mutation**
- Assumption: `pool._clients` is stable during iteration
- Betrayal: Concurrent `pool.get()` modifies `_clients` during iteration → `RuntimeError: dictionary changed size during iteration`
- Mitigation: Snapshot before iterating: `clients = list(pool._clients.items())`

**Dependency Treachery: LSP server hangs during initialize**
- Assumption: `pool.get()` returns promptly
- Betrayal: Server binary exists but hangs — blocks until `request_timeout` (up to 30s)
- Consequence: MCP tool call hangs silently
- Mitigation: Acceptable for first-call latency. `lsp_status` provides visibility. Document in tool description that first call for a language may be slow.

**Resource Exhaustion: massive reference results**
- Assumption: LSP operations return reasonably-sized responses
- Betrayal: `find_references` on `self` or `None` returns thousands of locations
- Mitigation: Existing `format_tool_response` pipeline handles JSON output. Future `limit` parameter out of scope per R4 spec. Document in tool description.

## Anti-Patterns
- NO raw LSP JSON-RPC in tool responses — translate to clean domain dicts
- NO tool params that expose LSP protocol details (textDocument, position objects)
- NO changes to existing tool signatures
- NO print() in MCP server code
