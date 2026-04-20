---
id: ch-wma
title: 'Bug: LanceDBProvider disconnect asymmetry (files/chunks cleared, symbols retained)'
status: open
type: bug
priority: 3
parent: ch-ljh
---


## Context
`LanceDBProvider._executor_disconnect` clears `self._files_table` and `self._chunks_table` to None, but does not clear `self._symbols_table` or `self._symbol_edges_table`. Meanwhile `_ensure_symbol_tables` provides lazy re-init for symbols, but files/chunks have no equivalent helper. After any disconnect/reconnect cycle, files/chunks methods silently return empty (truthy check fails), while symbol methods self-heal via `_ensure_symbol_tables`.

Latent, not currently manifesting — the daemon uses a long-lived connection. But it's a footgun for any code path that triggers disconnect (pool rotation, test fixtures, recovery paths).

**Reproduction:**
1. Create a LanceDBProvider, connect, populate tables
2. Call `_executor_disconnect`
3. Call a files or chunks method (e.g., `get_file_paths_by_ids`, `search_regex`)
4. Observe empty return instead of data, with no exception raised

## Diagnosis
Root cause: Inconsistent lifecycle pattern. Symbols got lazy-init discipline when they were added (ch-8e7 Phase 2); files/chunks retain the older "init-once-and-hope" pattern.

**Evidence:**
- `_executor_disconnect` at `chunkhound/providers/database/lancedb_provider.py:331-343` — clears only 2 of 4 cached tables
- `_ensure_symbol_tables` at `chunkhound/providers/database/lancedb_provider.py:2230-2239` — lazy-init helper for symbols only
- Truthy check (not `is None`) used at ~15 call sites — would silently fail if refs are None

Fix location: `chunkhound/providers/database/lancedb_provider.py` — `_executor_disconnect` (331-343), add `_ensure_files_table` and `_ensure_chunks_table` helpers, update ~15 call sites.

## Implementation

### Step 1: Write failing tests
File: `tests/integration/test_lancedb_lifecycle.py` (new)
- `test_disconnect_clears_all_cached_table_refs` — after disconnect, all four cached attrs are None (or all preserved, depending on chosen strategy)
- `test_access_after_disconnect_reinitializes_or_raises` — calling a files method after disconnect either succeeds via lazy re-init OR raises a clear error (never silent empty)
- `test_reconnect_restores_all_tables` — after disconnect→reconnect, all four methods (files, chunks, symbols, edges) work

### Step 2: Run tests
Expect failures on at least the symmetry tests.

### Step 3: Add lazy-init helpers and symmetric cleanup
File: `chunkhound/providers/database/lancedb_provider.py`

Pattern to apply (Option C — symmetric lazy-init, matches existing symbols pattern):
- Add `_ensure_files_table(self, conn, state) -> Any` mirroring `_ensure_symbol_tables`
- Add `_ensure_chunks_table(self, conn, state) -> Any` mirroring `_ensure_symbol_tables`
- Update `_executor_disconnect` to also clear `_symbols_table` and `_symbol_edges_table` (symmetric)
- Update all sites that use `self._files_table` / `self._chunks_table` to call the ensure helpers OR use `is not None` for the check

### Step 4: Update call sites
Use LSP to find all references:
- `LSP.findReferences` on `_files_table` (~7 sites)
- `LSP.findReferences` on `_chunks_table` (~9 sites)
For each: either use ensure helper (if `conn` is available) or keep truthy with `is not None` fix.

### Step 5: Run tests
Expect pass.

### Step 6: Run full LanceDB provider suite
```
uv run pytest tests/integration/test_lancedb*.py tests/integration/test_duckdb_graph_protocol.py tests/integration/test_lancedb_graph_protocol.py -v
```

### Step 7: Commit
```
git add -u && git commit -m "fix(lancedb): symmetric table lifecycle with lazy-init helpers for files/chunks"
```

## Success Criteria
- [ ] All four cached table attrs follow the same lifecycle pattern
- [ ] `_ensure_files_table` and `_ensure_chunks_table` helpers exist, mirroring `_ensure_symbol_tables`
- [ ] All call sites use consistent `is None` check (not truthy)
- [ ] Disconnect-reconnect cycle preserves all four methods' functionality
- [ ] No silent empty returns after disconnect
- [ ] Regression tests pass; existing tests still pass

## Anti-Patterns
- NO partial fix (just disconnect symmetry, not ensure-helpers) — pattern must be consistent end-to-end
- NO introducing thread-race on ensure-helper — re-read symbol pattern carefully (executor serialization protects it)
- NO changing `_executor_create_schema` to be idempotent in ways that conflict with the existing symbol-tables logic

## Key Considerations
- ch-3zc also includes a truthy-check fix at the get_stats site. That touches the same pattern — coordinate to avoid conflicts. Suggested order: land ch-3zc first (small, focused), then ch-wma (wider refactor including the truthy-check cleanup).
- Alternative Option B (don't clear any cached refs on disconnect) is simpler but less correct: table refs may become stale if the underlying lance dataset changes out-of-band.
- Pattern Option A (clear all + lazy init everywhere) selected.

## Log

- [2026-04-20T18:40:11Z] [Seth] Diagnosis: asymmetric cleanup in _executor_disconnect; symbols have lazy-init helper, files/chunks don't. Latent bug. Fix: symmetric helpers + is-None checks.
