---
id: ch-9aa
title: Fix LanceDB silent error swallowing in graph protocol methods
status: active
type: bug
priority: 1
owner: Seth
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
- `_executor_graph_reachability` — :2608, :2624, :2644 (three swallows — SRE addition)
- `_executor_graph_boundary` — :2673-2674, :2694-2695
- `_executor_graph_overview` — :2734-2735, :2764-2765
- `_executor_graph_overview_breakdown` — :2785-2786
- `_executor_symbol_stats` — :2436, :2440, :2457 (three swallows — SRE addition)
- `_executor_symbol_overlap` — :2826 (SRE addition)
- `_executor_chunk_resolution` — :2879 (SRE addition)
- `_executor_query_symbols_by_scope` — :2895 (SRE addition)
- `_executor_query_test_symbols` — :2911 (SRE addition)
- `_executor_query_symbol_type_signatures` — :2933 (SRE addition)
- `_executor_query_distinct_fqns_by_file_path` — :2946 (SRE addition)
- `_executor_search_symbols` — :2987 (SRE addition)
- `_executor_filter_chunks_by_symbol_type_signature` — :3021 (SRE addition)
- `_executor_query_symbols_by_range` — :2335
- `_executor_query_symbols_by_range_overlap` — :2359
- `_executor_query_symbol_fqns_by_file` — :2372
- `_executor_query_symbols_by_fqn_exists` — :2387
- `_executor_query_symbols_by_file` — :2312

**FQN/file_path interpolation (unescaped f-strings):**
- `_executor_graph_walk` — :2534
- `_bfs_get_neighbors` — :2580, :2588
- `_executor_graph_reachability` — :2607, :2619, :2640 (SRE addition)
- `_executor_graph_overview` — :2750
- `_executor_symbol_overlap` — :2820 (file_path interpolation — SRE addition)
- `_executor_chunk_resolution` — :2846 (fqn interpolation — SRE addition)
- `_executor_query_symbols_by_scope` — :2893 (uses `_escape_like_pattern` — already safe)
- `_executor_query_symbol_type_signatures` — :2929 (fqn interpolation — SRE addition)
- `_executor_query_distinct_fqns_by_file_path` — :2944 (file_path interpolation — SRE addition)
- `_executor_query_symbols_by_range` — :2325-2330 (file_path interpolation)
- `_executor_query_symbols_by_range_overlap` — :2353-2356
- `_executor_query_symbols_by_fqn_exists` — :2383-2384
- `_executor_search_symbols` — :2973-2980 (already escapes via `.replace("'", "''")` — safe)

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
- [SRE] `_escape_like_pattern` (like_utils.py) already handles `'` → `''` via `escape_quotes=True`, but it also escapes LIKE metacharacters (`%`, `_`, `[`). For equality comparisons (`fqn = '...'`), use a simpler `_escape_lance_string` that only escapes quotes — don't reuse the LIKE escaper for non-LIKE predicates.
- [SRE] `_executor_search_symbols` (:2973) already does `.replace("'", "''")` inline. Refactor to use the new `_escape_lance_string` for consistency.
- [SRE] `_executor_graph_reachability` was missing from the original Affected Sites — it has 3 swallows and 3 unescaped FQN interpolations. Same pattern as the other graph methods.

### Failure Catalog (Adversarial Planning)

**State Corruption: `_executor_delete_edges_by_file` partial delete**
- Assumption: The per-symbol-id loop either fully completes or fully fails
- Betrayal: If the 3rd of 5 `edge_tbl.delete()` calls fails, the first 2 deletes are already committed — LanceDB has no transaction rollback
- Consequence: Partial edge deletion. On retry (next indexing pass), remaining edges will be targeted again. Orphaned edges accumulate if retry also fails.
- Mitigation: Accept as tolerable — delete-by-predicate is idempotent, so retries converge. DuckDB uses a single `DELETE ... WHERE ... IN (...)` which is atomic, but LanceDB lacks `IN` predicate support for this. The key fix is raising ProviderError so the caller KNOWS the delete failed and doesn't proceed to re-insert symbols (which would compound the corruption).

**Input Hostility: `_escape_lance_string` — characters beyond single quotes**
- Assumption: Only `'` needs escaping for LanceDB/DataFusion SQL string literals
- Betrayal: Backslashes, null bytes, or other SQL-special characters could also break predicates
- Consequence: Parse error or, worse, predicate injection
- Mitigation: Verified empirically — DataFusion string literals use SQL standard `''` escaping. Backslashes are literal (not escape chars in DataFusion). Null bytes in FQNs are structurally impossible (tree-sitter produces valid UTF-8 identifiers). `'` → `''` is sufficient. The existing `_escape_like_pattern` in like_utils.py already uses this same approach with `escape_quotes=True`.

**Dependency Treachery: LanceDB `.delete()` on empty/no-match**
- Assumption: `.delete()` with zero matching rows might throw
- Betrayal: Could be a legitimate exception that the swallows were guarding against
- Consequence: Removing swallows would break normal operation
- Mitigation: **Verified empirically** — `.delete("id = 999")` on empty table and with no-match predicate both succeed silently. No legitimate exception to guard against. Safe to remove swallows unconditionally.

**Temporal Betrayal: Callers unprepared for ProviderError after swallow removal**
- Assumption: All callers handle ProviderError or let it propagate
- Betrayal: A caller that previously relied on empty-return-on-failure may now crash
- Consequence: Unhandled exception in MCP tool or service layer
- Mitigation: MCP tools already have top-level error handling (try/except around provider calls). `lsp_population.populate_file` calls delete then insert — if delete raises, insert correctly doesn't happen. The whole point of propagating errors is to make callers aware. Any caller that breaks was silently corrupt before — the crash is the fix.

## Log

- [2026-04-16T06:53:58Z] [Seth] Filed from ch-nxu review. Diagnosis via LSP incomingCalls + code read + reproducer test. Reproducer at tests/integration/_tmp_lance_repro_test.py confirms silent delete → duplicate rows. Stale scratch file _lance_repro.py at repo root needs manual rm.
- [2026-04-16T08:17:42Z] [Seth] SRE + adversarial complete. Added 9 missing read-path sites, 5 missing FQN interpolation sites. Verified LanceDB .delete() does NOT throw on empty/no-match (empirical probe). Partial delete in _executor_delete_edges_by_file accepted as tolerable — idempotent retries converge. _escape_lance_string only needs quote escaping (DataFusion uses SQL-standard ''). All callers prepared for ProviderError via MCP top-level handling.
