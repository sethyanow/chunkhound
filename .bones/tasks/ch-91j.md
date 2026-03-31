---
id: ch-91j
title: Wire LSPPopulationService into production indexing
status: closed
type: task
priority: 1
owner: Seth
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
Three integration points, verified by SRE code review (2026-03-31):

### Point 1: MCP server construction (realtime/incremental)
File: `chunkhound/mcp_server/base.py:197`
Currently: `RealtimeIndexingService(self.services, self.config, debug_sink=self.debug_log)`
Need: Construct `LSPClientPool()` + `LSPPopulationService(pool, provider, target_path)`,
pass as `lsp_population=`. Store pool on `self` for cleanup and shared access.
The handler code already exists at `realtime_indexing_service.py:899-930` and the
enqueue at `:954-956`. This is purely a construction wiring change.

### Point 2: MCP server bulk indexing (background scan)
File: `chunkhound/mcp_server/base.py:257` (`_background_initial_scan`)
Currently: `DirectoryIndexingService(indexing_coordinator=..., config=..., progress_callback=...)`
Need: Pass `lsp_population=self._lsp_population_service` to DirectoryIndexingService.
**SRE correction:** The prior skeleton targeted `IndexingCoordinator.process_directory()`.
`DirectoryIndexingService` already wraps IndexingCoordinator and already has:
- `lsp_population` constructor param (line 46)
- `_populate_symbols()` method (line 162)
- Integration in `process_directory()` (lines 104-107)
NO changes to `IndexingCoordinator` needed.

### Point 3: CLI bulk indexing (chunkhound index)
File: `chunkhound/api/cli/commands/run.py:118`
Currently: `DirectoryIndexingService(indexing_coordinator=..., config=..., ...)`
Need: Create own `LSPClientPool()` + `LSPPopulationService(pool, provider, target_path)`,
pass `lsp_population=` to DirectoryIndexingService. Add try/finally for pool cleanup.
**SRE finding:** This integration point was missing from the original skeleton.
Required for success criterion 3 ("chunkhound index . → symbols table non-empty").

### Cleanup
File: `chunkhound/mcp_server/base.py` (`cleanup()` method, line 288)
Need: Shut down `self._lsp_pool` if it exists. Best-effort — catch exceptions.

## Implementation

### Step 1: Write test — base.py passes lsp_population to RealtimeIndexingService
File: `tests/test_lsp_population.py` (new TestMCPServerWiring class)
Test intent: Patch LSPClientPool and LSPPopulationService construction in base.py's
`_deferred_connect_and_start`. Verify RealtimeIndexingService receives a non-None
lsp_population AND DirectoryIndexingService (in `_background_initial_scan`) receives
the same service. Key assertions:
- `realtime_service._lsp_population is not None`
- `directory_service._lsp_population is not None`

### Step 2: Wire base.py — realtime + batch + cleanup
File: `chunkhound/mcp_server/base.py`
In `_deferred_connect_and_start` (line 187), before constructing RealtimeIndexingService:
- Guard: if `self._lsp_pool` already exists, skip re-creation (prevents double-construction leak)
- Create `LSPClientPool()` → store as `self._lsp_pool`
- Create `LSPPopulationService(pool, self.services.provider, target_path)` → store as `self._lsp_population_service`
- Pass `lsp_population=self._lsp_population_service` to RealtimeIndexingService (line 197)
- Use lazy import for both classes to avoid circular deps

In `_background_initial_scan` (line 257):
- Pass `lsp_population=self._lsp_population_service` to DirectoryIndexingService

In `cleanup()` (line 288):
- If `self._lsp_pool` exists, shut it down (best-effort, catch exceptions)

Initialize `self._lsp_pool = None` and `self._lsp_population_service = None` in `__init__`.

### Step 3: Write test — CLI index command passes lsp_population
File: `tests/test_lsp_population.py` (new TestCLIWiring class)
Test intent: Patch DirectoryIndexingService construction in run.py's index command.
Verify it receives a non-None lsp_population. Verify pool cleanup runs even on error
(try/finally pattern).

### Step 4: Wire run.py — CLI bulk indexing
File: `chunkhound/api/cli/commands/run.py`
Around the DirectoryIndexingService construction (line 118):
- Create `LSPClientPool()` + `LSPPopulationService(pool, provider, target_path)`
- Pass `lsp_population=service` to DirectoryIndexingService
- Wrap in try/finally: pool shutdown in finally block (best-effort)
- Use lazy import for both classes

### Step 5: Verify — run existing tests + demo script
- `uv run pytest tests/test_lsp_population.py -v` → all pass (existing 52 + new wiring tests)
- `uv run scripts/demo_lsp.py` → Phase 2 scenarios pass
- Note: `TestBatchWiring` (line 524) already tests DirectoryIndexingService._populate_symbols
  integration — no need to duplicate that test.

## Existing Test Coverage (SRE verified)
- `TestBatchWiring.test_process_directory_calls_populate_symbols` (line 524-575) — tests
  DirectoryIndexingService.process_directory() calls populate_files. Already passes. ✅
- `TestRealtimeWiring.test_process_loop_handles_lsp_priority` (line 578-653) — tests
  RealtimeIndexingService._process_loop handles "lsp" priority events. Already passes. ✅
- New tests needed: verify base.py and run.py actually CONSTRUCT and PASS the services.

## Success Criteria
- [x] `RealtimeIndexingService._lsp_population` is not None in production (base.py passes it)
- [x] `DirectoryIndexingService._lsp_population` is not None in MCP server background scan (base.py passes it)
- [x] File change via watcher → symbols + edges populated for that file (incremental path)
- [x] `chunkhound index .` → symbols table non-empty after bulk indexing (CLI path via run.py)
- [x] LSP population does not block tree-sitter indexing (background, after TS completes)
- [x] Pool cleanup runs on MCP server shutdown (base.py cleanup())
- [x] Pool cleanup runs on CLI exit, including error paths (run.py try/finally)
- [x] Existing tests pass (zero regression) — 1487 passed, 0 failed

## Key Considerations

### Adversarial Failure Catalog (SRE 2026-03-31)

**Temporal Betrayal: Pool double-construction in base.py**
- Assumption: `_deferred_connect_and_start` is called exactly once
- Betrayal: Called again on reconnect/error recovery — pool reference overwritten, first pool's LSP processes leak
- Consequence: Orphaned LSP server processes accumulate
- Mitigation: Guard in Step 2 — if `self._lsp_pool` already exists, skip. Structural prevention, not catch-after-fact.

**Dependency Treachery: CLI pool cleanup (run.py)**
- Assumption: CLI exits cleanly after indexing
- Betrayal: Crash or Ctrl+C during population — pool not shut down, LSP servers orphaned
- Consequence: Zombie language server processes (pyright, etc.) consuming memory
- Mitigation: try/finally wrapping pool lifecycle in Step 4. Pool shutdown is best-effort.

**Temporal Betrayal: Concurrent MCP server + CLI on same DB**
- Assumption: Only one process populates symbols at a time
- Betrayal: User runs `chunkhound index .` while MCP server is running
- Consequence: Both processes populate same file's symbols simultaneously
- Mitigation: Acceptable — `populate_file` is idempotent (delete + re-insert per file_id). Wasted work, no corruption.

**State Corruption: Pool cleanup during active population**
- Assumption: No population calls in flight when cleanup() runs
- Betrayal: cleanup() called while `_populate_symbols()` still awaiting
- Consequence: Pool shuts down LSP servers mid-request → LSPError
- Mitigation: `populate_file` already catches exceptions per-file. Graceful degradation — partial population acceptable.

## Anti-Patterns
- NO constructing LSP clients in the tree-sitter pipeline — population is a post-TS background pass
- NO modifying IndexingCoordinator — DirectoryIndexingService already handles batch population
- NO importing LSPPopulationService at module level in base.py or run.py — lazy import to avoid circular deps
- NO creating pool without cleanup path — every pool constructor needs a corresponding shutdown in finally/cleanup
