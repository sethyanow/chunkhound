---
id: ch-u1s
title: 'LSP Client Manager: Transport, Operations, State, Capability Gating'
status: open
type: task
priority: 1
parent: ch-7j0
depends_on: [ch-jsj]
---

## Context
Phase 1 of ch-8e7 (LSP + Graph Intelligence Layer). First implementation task after ch-jsj (path scoping + git-aware indexing). Delivers the standalone LSP client module that all subsequent phases depend on — Phase 2 (index population) calls this client to extract symbols/edges, Phase 3 (MCP tools) exposes it for live queries.

**Blocked by:** ch-jsj (closed)
**Unlocks:** DuckDB schema task (criteria 6-8), then Phase 2 index population (ch-0um)

## Requirements
From parent epic R1: "Standalone LSP client manager module — asyncio JSON-RPC over stdio, config-driven server registry, capability gating, connection pooling. Zero ChunkHound imports. Server configs for all languages with existing tree-sitter grammars (pyright first, then all others)."

Key constraints:
- Zero imports from `chunkhound.*` — the module must be self-contained
- All I/O async (anti-pattern: NO blocking I/O)
- Config-driven only (anti-pattern: NO language-specific logic in client)
- 8 operations: documentSymbol, definition, references, implementation, incomingCalls, outgoingCalls, hover, diagnostics

## Design
Module: `chunkhound/lsp/` (5 files)
- `__init__.py` — public API exports
- `types.py` — ServerConfig, ServerState, LSPCapability, error hierarchy, response dataclasses
- `protocol.py` — JsonRpcTransport (Content-Length framed async stdio)
- `client.py` — LSPClient (lifecycle, state machine, capability gating, 8 operations) + LSPClientPool
- `registry.py` — LANGUAGE_SERVER_REGISTRY dict mapping Language values to ServerConfig

Transport: asyncio.create_subprocess_exec → Content-Length header framing → JSON-RPC 2.0 request/response with ID tracking.

State machine: NOT_STARTED → INITIALIZING (spawn + initialize handshake) → READY (initialized notification sent) → DEGRADED (process exit, timeout, error) → STOPPED (explicit shutdown). Degraded carries structured reason dict.

Capability gating: Parse initialize response capabilities → set[LSPCapability]. Each operation checks before sending. Missing → raise LSPCapabilityError.

Connection pool: Keyed by (language_id, workspace_root). Lazy spawn. stop/stop_all for cleanup.

Registry: Entry for every Language enum member with a tree-sitter grammar (35 languages). Languages without well-known servers get command=None.

Pyright available locally: `/Users/seth/.local/share/mise/installs/npm-pyright/1.1.408/bin/pyright`
Note: pyright LSP mode is `pyright-langserver --stdio` (separate binary from the CLI checker).

## Implementation

### Step 1: Create module structure (scaffold — no logic, TDD escape hatch)
Create `chunkhound/lsp/__init__.py`, `types.py`, `protocol.py`, `client.py`, `registry.py`.
- `types.py`: `ServerConfig` dataclass (language_id, command, args, env, init_options), `ServerState` enum, `LSPCapability` enum, `LSPError`/`LSPCapabilityError`/`LSPTransportError` exceptions, response dataclasses (SymbolInfo, Location, HoverResult, CallHierarchyItem, Diagnostic)
- Zero `chunkhound.*` imports — stdlib only (asyncio, dataclasses, enum, typing, pathlib, json, logging)

### Step 2: RED — Test spawn + initialize handshake
File: `tests/test_lsp_client.py::test_spawn_and_initialize_pyright`
- Spawn pyright via `pyright-langserver --stdio`, send `initialize` with rootUri pointing to a temp workspace containing a .py file
- Assert: response has `capabilities` dict, `serverInfo.name` present, state transitions to READY
- Fixture: temp dir with a simple `main.py` (function def + call)
- Run: `uv run pytest tests/test_lsp_client.py::test_spawn_and_initialize_pyright -v` → FAIL (no implementation)

### Step 3: GREEN — JSON-RPC transport + spawn + initialize
- `protocol.py`: `JsonRpcTransport` — `create(command, args, env, cwd)` class method, `send_request(method, params) → dict`, `send_notification(method, params)`, `close()`. Content-Length header parsing. Request ID auto-increment. Async read loop.
- `client.py`: `LSPClient.start(config, workspace_root)` — spawn via transport, send `initialize` request (processId, rootUri, capabilities), await response, send `initialized` notification, cache server capabilities, set state READY.
- Run test → PASS

### Step 4: RED — Test state tracking
- `test_server_state_transitions` — assert NOT_STARTED before start, INITIALIZING during (may need mock), READY after. Use `client.state` property.
- `test_degraded_state_on_process_exit` — start client, kill subprocess, attempt operation → state is DEGRADED, reason has `code` and `detail` keys
- Run → FAIL

### Step 5: GREEN — State machine
- `client.py`: `_state` property with getter, `_set_state(new_state, reason=None)` with transition validation. `degraded_reason: dict | None`. Process monitor task detects unexpected exits.
- Run tests → PASS

### Step 6: RED — Test capability gating
- `test_capability_gating_unadvertised` — create client, manually remove a capability from `_capabilities`, call that operation → `LSPCapabilityError` raised with method name and server info
- `test_capability_gating_advertised` — normal call to an advertised capability succeeds (no error)
- Run → FAIL

### Step 7: GREEN — Capability gating
- Parse initialize response: map `capabilities` keys to `LSPCapability` enum values. Store as `_capabilities: set[LSPCapability]`.
- Each operation: `if LSPCapability.X not in self._capabilities: raise LSPCapabilityError(...)`
- Run tests → PASS

### Step 8: RED — Test 8 LSP operations
File: `tests/test_lsp_client.py`, fixture: `tests/fixtures/lsp_test_sample.py` (small file with class, function, call, variable with type annotation — enough for all operations).
- `test_document_symbols` → returns list with function/class names, kinds, ranges
- `test_go_to_definition` → position on function call resolves to function def location
- `test_find_references` → function name position returns ≥1 reference
- `test_hover` → variable position returns type info in markup
- `test_incoming_calls` → function returns callers as CallHierarchyItems
- `test_outgoing_calls` → function returns callees
- `test_go_to_implementation` → class method returns implementation location(s)
- `test_get_diagnostics` → returns list (may be empty for valid code; include a deliberate error in fixture to test non-empty)
- Run → FAIL (operations not implemented)

### Step 9: GREEN — Implement 8 operations
- `client.py` methods: `document_symbols(uri)`, `go_to_definition(uri, line, char)`, `find_references(uri, line, char)`, `hover(uri, line, char)`, `incoming_calls(uri, line, char)`, `outgoing_calls(uri, line, char)`, `go_to_implementation(uri, line, char)`, `get_diagnostics(uri)`.
- Each: capability gate → build LSP-spec params (TextDocumentIdentifier, Position) → transport.send_request → parse response into typed dataclass.
- For diagnostics: two strategies — (a) pull via `textDocument/diagnostic` if server supports, (b) collect from `textDocument/publishDiagnostics` notifications. Check pyright's capabilities to determine which.
- Run tests → PASS

### Step 10: RED — Test connection pooling
- `test_pool_reuse` — `pool.get("python", workspace_a)` twice → same LSPClient instance
- `test_pool_different_workspace` — `pool.get("python", workspace_a)` vs `pool.get("python", workspace_b)` → different instances
- `test_pool_respawn_after_stop` — get client, stop it, get again → new instance, state READY
- Run → FAIL

### Step 11: GREEN — Connection pool
- `client.py` or `pool.py`: `LSPClientPool` — `_clients: dict[tuple[str, str], LSPClient]`, `async get(language_id, workspace_root) → LSPClient` (lazy spawn from registry config), `async stop(language_id, workspace_root)`, `async stop_all()`
- Run tests → PASS

### Step 12: RED — Test language configs
- `test_registry_covers_all_tree_sitter_languages` — import Language enum, filter to those with tree-sitter grammars (exclude TEXT, PDF, UNKNOWN), assert each has entry in LANGUAGE_SERVER_REGISTRY with matching language_id
- `test_pyright_config_correct` — assert pyright config has command containing "pyright-langserver", args contain "--stdio"
- Run → FAIL

### Step 13: GREEN — Registry
- `registry.py`: `LANGUAGE_SERVER_REGISTRY: dict[str, ServerConfig]`. Key entries: pyright (python), typescript-language-server (ts/js/tsx/jsx), gopls (go), rust-analyzer (rust), clangd (c/cpp/objc), zls (zig), bash-language-server (bash), kotlin-language-server (kotlin), lua-language-server (lua), phpactor (php), svelte-language-server (svelte), vue-language-server (vue), sourcekit-lsp (swift), dart language-server (dart), elixir-ls (elixir), haskell-language-server (haskell), terraform-ls (hcl), jdtls (java), groovy-language-server (groovy), taplo (toml), yaml-language-server (yaml), vscode-json-languageserver (json), marksman (markdown), sqls (sql). Languages without viable server: command=None (makefile, matlab).
- Run tests → PASS

### Step 14: Final verification
- `uv run pytest tests/test_lsp_client.py -v` → all pass
- `uv run pytest tests/test_smoke.py -v -n auto` → all pass (zero regression)
- Commit and push

## Success Criteria
- [ ] LSP client manager spawns and initializes pyright successfully
- [ ] LSP client manager has configs for all languages with tree-sitter grammars in pyproject.toml
- [ ] documentSymbol, definition, references, implementation, incomingCalls, outgoingCalls, hover, diagnostics all return valid results via pyright
- [ ] Server state tracking works (not_started → initializing → ready, and degraded with structured reason)
- [ ] Capability gating: calls against unadvertised capabilities return graceful error, not crash
- [ ] `uv run pytest tests/test_lsp_client.py -v` → all pass
- [ ] Zero `chunkhound.*` imports in `chunkhound/lsp/` module
- [ ] All existing tests still pass (zero regression)

## Anti-Patterns
- NO blocking I/O — all operations async
- NO language-specific logic in client — config-driven only
- NO `chunkhound.*` imports — standalone module
- NO hardcoded server paths — registry-driven with command=None for unavailable servers
- NO print() in any module code (inherited from project rules)
