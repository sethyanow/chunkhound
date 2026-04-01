---
id: ch-t3j
title: Decompose test_mcp_tools_lsp.py into per-tool test files
status: open
type: task
priority: 0
parent: ch-zyz
---



## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. `tests/lsp/test_mcp_tools_lsp.py` is 1741 lines with 7 classes / 62 tests covering 4 different MCP tools + infrastructure wiring. Violates the 500-line file limit and prevents clean test ownership as new tools are added.

**Blocked by:** None
**Unlocks:** ch-lic (search extensions need to add tests to the right files from the start)

## Requirements
Pure code motion — split the monolithic test file into per-tool files with zero behavior changes. All 62 tests must pass before and after.

## Design
**Decomposition by tool concern.** Each MCP tool gets its own test file. Shared mock helpers extracted to a helper module (NOT conftest.py — existing conftest already has `_make_mock_pool` with different signature for population tests).

**Name collision:** conftest.py line 35 has `_make_mock_pool(symbols: list[SymbolInfo])` returning `(pool, client)` tuple for population tests. The MCP tool version `_make_mock_pool(client: AsyncMock | None = None)` returns just `pool`. Different interface, different purpose. Extracted to separate helper module.

**Source file:** `tests/lsp/test_mcp_tools_lsp.py` (1741 lines)
**Target files:**

| File | Classes | Tests | Source lines |
|------|---------|-------|-------------|
| `tests/lsp/mcp_tool_helpers.py` | — helpers only | — | 128-143, 583-595 |
| `tests/lsp/test_tool_wiring.py` | TestLspClientPoolWiring | 3 | 23-120 |
| `tests/lsp/test_tool_lsp.py` | TestLspTool | 11 | 145-479 |
| `tests/lsp/test_tool_lsp_status.py` | TestLspStatusTool | 3 | 487-575 |
| `tests/lsp/test_tool_graph.py` | TestGraphTool + TestGraphToolAdversarial | 30 | 598-1189 |
| `tests/lsp/test_tool_symbol_context.py` | TestSymbolContextTool + TestSymbolContextAdversarial | 15 | 1197-1741 |

## Implementation

### Step 1: Create mcp_tool_helpers.py with shared mock factories
- File: `tests/lsp/mcp_tool_helpers.py`
- Move `_make_mock_pool` (lines 128-135), `_make_mock_config` (lines 138-142), `_make_mock_services` (lines 583-595) from test_mcp_tools_lsp.py
- Rename to module-level public functions: `make_mock_pool`, `make_mock_config`, `make_mock_services`
- Include necessary imports: `AsyncMock`, `MagicMock`, `Path`

### Step 2: Create test_tool_wiring.py
- File: `tests/lsp/test_tool_wiring.py`
- Move: `TestLspClientPoolWiring` class (lines 35-120)
- Imports: `TOOL_REGISTRY`, `_generate_json_schema_from_signature`, `execute_tool`, `Tool` from `chunkhound.mcp_server.tools`
- Set `pytestmark = pytest.mark.unit`

### Step 3: Create test_tool_lsp.py
- File: `tests/lsp/test_tool_lsp.py`
- Move: `TestLspTool` class (lines 145-479)
- Import helpers from `tests.lsp.mcp_tool_helpers`: `make_mock_pool`, `make_mock_config`
- Import LSP types: `Location`, `CallHierarchyItem`, `HoverResult`, `Diagnostic`, `LSPCapabilityError`, `LSPTransportError`
- Import `execute_tool` from tools

### Step 4: Create test_tool_lsp_status.py
- File: `tests/lsp/test_tool_lsp_status.py`
- Move: `TestLspStatusTool` class (lines 487-575)
- Import LSP types: `LSPCapability`, `ServerConfig`, `ServerState`
- Import `execute_tool`

### Step 5: Create test_tool_graph.py
- File: `tests/lsp/test_tool_graph.py`
- Move: `TestGraphTool` + `TestGraphToolAdversarial` classes (lines 598-1189)
- Import helpers: `make_mock_services`
- Import `execute_tool`, `_escape_like` (for adversarial tests that test the helper directly)

### Step 6: Create test_tool_symbol_context.py
- File: `tests/lsp/test_tool_symbol_context.py`
- Move: `TestSymbolContextTool` + `TestSymbolContextAdversarial` classes (lines 1197-1741)
- Import helpers: `make_mock_pool`, `make_mock_config`
- Import LSP types: `Location`, `CallHierarchyItem`, `HoverResult`
- Import `execute_tool`

### Step 7: Delete test_mcp_tools_lsp.py and verify
- Delete: `tests/lsp/test_mcp_tools_lsp.py`
- Run: `uv run pytest tests/lsp/test_tool_wiring.py tests/lsp/test_tool_lsp.py tests/lsp/test_tool_lsp_status.py tests/lsp/test_tool_graph.py tests/lsp/test_tool_symbol_context.py -v -m ""` → 62 pass
- Run: `uv run pytest tests/lsp/ -v -m ""` → all lsp tests pass (no import errors from removal)
- Commit and push

## Success Criteria
- [ ] `test_mcp_tools_lsp.py` deleted — no monolithic test file
- [ ] 5 new test files, each under 600 lines, each focused on one tool
- [ ] Shared helpers in `mcp_tool_helpers.py` (no conftest name collision)
- [ ] All 62 tests pass: `uv run pytest tests/lsp/test_tool_*.py -v -m ""`
- [ ] All other lsp tests unaffected: `uv run pytest tests/lsp/ -v -m ""`
- [ ] Zero behavior changes — pure code motion

## Anti-Patterns
- NO merging helpers into conftest.py (name collision with existing `_make_mock_pool`)
- NO modifying test logic during decomposition — pure move
- NO renaming test methods or classes
- NO changing imports in non-test files
