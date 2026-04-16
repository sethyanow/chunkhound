---
id: ch-9aa
title: Fix LanceDB silent error swallowing in graph protocol methods
status: open
type: bug
priority: 1
---


## Context

ch-nxu Steps 10/12 implemented the graph protocol methods on LanceDBProvider. The implementation copied the older LanceDB file/chunk pattern of catching `Exception` and returning empty or logging-and-continuing. For file/chunk CRUD this was defensible (missing table on first query is normal). For the graph layer it's a contract violation: callers trust that empty means "no data" and that deletes succeeded.

**Reproducer confirmed** (`tests/integration/_tmp_lance_repro_test.py::test_silent_delete_creates_duplicates`):
- `delete_symbols_by_file` swallows `RuntimeError`, returns normally
- Follow-up `insert_symbols_batch` creates a duplicate row (2 rows, different IDs, same fqn)
- `symbol_stats` reports inflated count; graph queries hit duplicates

**Cross-backend divergence:** DuckDB's `_executor_delete_symbols_by_file` (duckdb_provider.py:2787) has NO try/except — exceptions propagate to `_run_and_wrap_exceptions` (serial_executor.py:101-109) which wraps as `ProviderError`. LanceDB catches internally and hides them. SC6 intended one error contract; the two providers silently disagree.

**Adjacent bug exposed by swallows:** f-string interpolation at `_executor_graph_walk:2534` (`where(f"fqn = '{fqn}'")`), `_bfs_get_neighbors:2580,2588`, and `_executor_graph_overview:2750` doesn't escape quotes in FQNs. Any FQN containing `'` produces a LanceDB parse error — currently masked by the swallows. Fixing swallows will expose this as a test failure. Parameterize or escape.

## Requirements

1. Write-path `_executor_*` methods (delete_symbols_by_file, delete_edges_by_file) must let exceptions propagate so `_run_and_wrap_exceptions` converts to `ProviderError` — matching DuckDB's behavior
2. Read-path `_executor_*` graph methods must not silently return empty on backend failure — let exceptions propagate (bootstrap "table doesn't exist" cases stay in `_ensure_symbol_tables`, not re-caught at every query)
3. FQN values interpolated into LanceDB `where()` clauses must be escaped or parameterized to handle FQNs containing `'`
4. Existing tests must continue to pass after removing swallows (failures = real bugs to fix, not to re-swallow)

## Affected Sites

**Write paths (log-and-continue or bare pass):**
- `_executor_delete_symbols_by_file` — lancedb_provider.py:2276-2279
- `_executor_delete_edges_by_file` — lancedb_provider.py:2294-2301 (two bare `except Exception: pass`)

**Read paths (return empty on failure):**
- `_executor_graph_walk` — :2546 (node fetch), :2570 (edge scan)
- `_bfs_get_neighbors` — :2593
- `_executor_graph_boundary` — :2673-2674, :2694-2695
- `_executor_graph_overview` — :2734-2735, :2764-2765
- `_executor_graph_overview_breakdown` — :2785-2786
- `_executor_query_symbols_by_range` — :2335
- `_executor_query_symbols_by_range_overlap` — :2359
- `_executor_query_symbol_fqns_by_file` — :2372
- `_executor_query_symbols_by_fqn_exists` — :2387
- `_executor_query_symbols_by_file` — :2312

**FQN interpolation (unescaped f-strings):**
- `_executor_graph_walk` — :2534
- `_bfs_get_neighbors` — :2580, :2588
- `_executor_graph_overview` — :2750
- `_executor_query_symbols_by_range` — :2325-2330 (file_path interpolation)
- `_executor_query_symbols_by_range_overlap` — :2353-2356
- `_executor_query_symbols_by_fqn_exists` — :2383-2384

## Implementation

### Step 1: Write regression test — silent delete produces duplicates
Convert existing reproducer `tests/integration/_tmp_lance_repro_test.py` into a proper test at `tests/integration/test_lancedb_error_contract.py`. Test must assert that `delete_symbols_by_file` propagates `ProviderError` when the backend fails (currently passes because it asserts the BROKEN behavior — flip the assertion to expect the exception).

### Step 2: Remove write-path swallows
In `_executor_delete_symbols_by_file`: remove try/except (lines 2276-2279). In `_executor_delete_edges_by_file`: remove try/except blocks around `edge_tbl.delete` calls (lines 2294-2301). Let exceptions propagate to `_run_and_wrap_exceptions`.

### Step 3: Write regression test — FQN with apostrophe
Insert a symbol with `fqn="mod::it's_helper"`. Call `graph_walk(seed_fqns=["mod::it's_helper"], ...)`. Currently: LanceDB parse error silently returns empty. After fix: should either escape and succeed, or raise `ProviderError`.

### Step 4: Add FQN escaping utility
Add `_escape_lance_string(value: str) -> str` that escapes `'` → `''` (or use a Lance-safe mechanism). Apply to all `where(f"fqn = '...'"`) call sites listed in Affected Sites. Alternatively, if LanceDB supports parameterized `where()`, use that instead.

### Step 5: Remove read-path swallows from graph methods
Remove try/except returning empty from all 12 read-path sites listed above. Run suite — any new failures indicate real bugs exposed by the fix (not regressions to re-swallow).

### Step 6: Audit `_ensure_symbol_tables` for bootstrap guard
Confirm that `_ensure_symbol_tables` (line 2221) handles the "table doesn't exist on first call" case. This is the ONLY place where catching a LanceDB table-not-found error is valid. If it doesn't handle it, add a narrow catch there (not a blanket `Exception`).

### Step 7: Verify parity — run graph tests on both backends
`uv run pytest -m integration tests/integration/test_duckdb_graph_protocol.py tests/integration/test_lancedb_graph_protocol.py tests/integration/test_lancedb_mcp_graph_tool.py -v`

## Success Criteria

- [ ] `delete_symbols_by_file` raises `ProviderError` when backend fails (regression test proves it)
- [ ] `delete_edges_by_file` raises `ProviderError` when backend fails
- [ ] Zero bare `except Exception: pass` or `except Exception: return []` in any `_executor_*` graph/symbol method on LanceDBProvider
- [ ] FQN escaping handles `'` in all `where()` f-string interpolations (regression test with apostrophe FQN)
- [ ] Bootstrap case (tables don't exist yet) handled exclusively in `_ensure_symbol_tables`, not re-caught per-query
- [ ] All existing integration tests pass on both backends after swallow removal
- [ ] `_tmp_lance_repro_test.py` deleted, replaced by permanent `test_lancedb_error_contract.py`

## Anti-Patterns

- NO re-introducing blanket `except Exception` in individual executor methods — the executor wrapping layer (serial_executor.py:85-109) is the canonical error boundary
- NO catching backend errors in graph methods "because the old code does it" — the old file/chunk pattern was wrong to copy for a layer whose callers trust empty returns
- NO string interpolation without escaping in LanceDB `where()` clauses
- NO leaving the reproducer test asserting broken behavior — flip it to assert the fixed contract

## Key Considerations

- The older LanceDB file/chunk `_executor_*` methods (lines 618, 648, 697, 723, etc.) have similar swallow patterns. This task scopes to graph/symbol methods only. The older methods are a separate cleanup — don't boil the ocean.
- Some `_executor_query_symbols_by_*` methods return `[] / None / False` on exception. After removing swallows, verify callers handle `ProviderError` or let it propagate (most callers are MCP tools which already have top-level error handling).
- LanceDB table `.delete()` may legitimately throw if the table is empty or the predicate matches zero rows — verify this isn't the case before removing the catch. If it is, narrow the catch to that specific error type.

## Log

- [2026-04-16T06:53:58Z] [Seth] Filed from ch-nxu review. Diagnosis via LSP incomingCalls + code read + reproducer test. Reproducer at tests/integration/_tmp_lance_repro_test.py confirms silent delete → duplicate rows. Stale scratch file _lance_repro.py at repo root needs manual rm.
