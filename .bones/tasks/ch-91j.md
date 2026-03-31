---
id: ch-91j
title: Wire LSPPopulationService into production indexing
status: open
type: task
priority: 1
parent: ch-0um
---




## Context
Acceptance demo (ch-yda) revealed LSPPopulationService is dead code in production.
The service works (52 tests pass) and the realtime wiring code exists (priority="lsp"
handler at realtime_indexing_service.py:899, enqueue at :955). But `base.py:197` never
passes `lsp_population=` to RealtimeIndexingService, so `self._lsp_population` is always
None. Bulk indexing (`process_directory`) has no LSP population pass at all.

**Blocked by:** none (ready immediately)
**Unlocks:** ch-yda (acceptance demo can pass), ch-0um (Phase 2 closes)

## Requirements
1. RealtimeIndexingService receives a live LSPPopulationService at construction
2. Bulk indexing (process_directory) triggers LSP population after tree-sitter completes
3. LSP population must not block tree-sitter indexing (background pass)

## Design
Two integration points, both verified by reading the code in this session:

### Point 1: MCP server construction (realtime/incremental)
File: `chunkhound/mcp_server/base.py:197`
Currently: `RealtimeIndexingService(self.services, self.config, debug_sink=self.debug_log)`
Need: Construct `LSPClientPool` + `LSPPopulationService`, pass as `lsp_population=`.
The handler code already exists at `realtime_indexing_service.py:899-930` and the
enqueue at `:954-956`. This is purely a construction wiring change.

### Point 2: Bulk indexing (process_directory)
File: `chunkhound/services/indexing_coordinator.py`
After tree-sitter batch storage completes (~line 1385), add a background LSP
population pass over the indexed files. Same pattern as embedding generation:
iterate stored files, call `populate_file()` per file, don't block the return.

The coordinator needs an `LSPPopulationService` attribute — set via constructor
param or a `set_lsp_population()` method. `base.py` wires it at startup.

## Implementation

### Step 1: Write test — base.py passes lsp_population to RealtimeIndexingService
File: `tests/test_lsp_population.py` (existing TestRealtimeWiring or new class)
Test intent: Mock LSPClientPool, construct base server, verify RealtimeIndexingService
receives a non-None lsp_population. Key assertion: `realtime_service._lsp_population is not None`.

### Step 2: Wire base.py construction
File: `chunkhound/mcp_server/base.py`
In `_deferred_connect_and_start`, before constructing RealtimeIndexingService:
- Create `LSPClientPool()`
- Create `LSPPopulationService(pool, self.services.provider, target_path)`
- Pass `lsp_population=service` to RealtimeIndexingService constructor
- Store pool reference for cleanup in stop()

### Step 3: Write test — process_directory triggers LSP population
File: `tests/test_lsp_population.py` (new TestBatchWiring class or extend existing)
Test intent: Mock population service on coordinator, call process_directory on a
small fixture, verify populate_file called for each indexed file.

### Step 4: Wire IndexingCoordinator
File: `chunkhound/services/indexing_coordinator.py`
- Add `lsp_population: LSPPopulationService | None = None` attribute
- Add `set_lsp_population(service)` method
- After batch store in process_directory (~line 1385), iterate stored file results
  and call `await self.lsp_population.populate_file(...)` per file
- Guard with `if self.lsp_population is not None`

### Step 5: Wire coordinator in base.py
File: `chunkhound/mcp_server/base.py`
After creating the LSPPopulationService, call
`self.services.indexing_coordinator.set_lsp_population(service)`.

### Step 6: Verify — run demo script
Command: `uv run scripts/demo_lsp.py`
After re-indexing with the wiring in place, Phase 2 scenarios should pass.

## Success Criteria
- [ ] `RealtimeIndexingService._lsp_population` is not None in production (base.py passes it)
- [ ] File change via watcher → symbols + edges populated for that file (incremental path)
- [ ] `chunkhound index .` → symbols table non-empty after bulk indexing
- [ ] LSP population does not block tree-sitter indexing (background, after TS completes)
- [ ] Existing tests pass (zero regression)

## Anti-Patterns
- NO constructing LSP clients in the tree-sitter pipeline — population is a post-TS background pass
- NO blocking process_directory return on LSP population — fire-and-forget or post-return
- NO importing LSPPopulationService at module level in base.py — lazy import to avoid circular deps
