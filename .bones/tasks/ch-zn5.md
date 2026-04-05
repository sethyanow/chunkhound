---
id: ch-zn5
title: Fix DuckDB connection concurrency + cascade bugs
status: open
type: bug
priority: 0
---

## Context

Daemon restart on 2026-04-05 triggered a FATAL DuckDB corruption that wiped the index. Root cause: three interacting bugs in the realtime indexing service's database access patterns.

**Observed failure sequence (daemon.log):**
1. Daemon starts, polling monitor detects 894 files
2. `_store_parsed_results` begins explicit transactions (`BEGIN TRANSACTION`) for tree-sitter indexing
3. Concurrently, `_consume_events` calls `delete_file_completely_async` directly — interleaves with open transactions
4. Transaction state poisons: alternating `"transaction is aborted"` / `"cannot start a transaction within a transaction"`
5. FK constraint violations: `DELETE FROM symbols WHERE file_id = ?` fails because `symbol_edges` still references those symbols
6. DuckDB HNSW index corruption: `"Failed to delete all rows from index. Only deleted 0 out of 13 rows"`
7. FATAL: `"database has been invalidated because of a previous fatal error"` — every subsequent operation fails

**Key files:**
- `chunkhound/services/realtime_indexing_service.py` — `_consume_events` (line 706), `remove_file` (line 777), `_cleanup_deleted_directory` (line 818)
- `chunkhound/services/lsp_population.py` — `populate_file` (line 56), `delete_file_edges` (line 526), `delete_file_symbols` (line 535)
- `chunkhound/providers/database/duckdb_provider.py` — `_executor_delete_file_completely` (line 1324)
- `chunkhound/providers/database/serial_executor.py` — `SerialDatabaseExecutor` (line 81)

## Requirements

### Bug 1: Concurrency violation — `_consume_events` races with `_process_loop` transactions

`_consume_events` calls `provider.delete_file_completely_async()` (line 781, 833) and `provider.search_regex_async()` (line 823) **directly**, bypassing the file queue. These run as a separate async task concurrent with `_process_loop`.

Meanwhile, `_process_loop` → `IndexingCoordinator._store_parsed_results` does multi-statement transactions via separate executor submissions:
```
await self._db.begin_transaction_async()    # submission 1
# ... work ...                               # submissions 2-N
await self._db.commit_transaction_async()   # submission N+1
```

Each `await` yields control. Between submissions, `_consume_events` can submit its own operations to the same executor. The executor serializes individual statements but cannot protect logical transaction boundaries.

**Fix:** All DB-mutating operations in `_consume_events` must go through the file queue (priority="delete"), not call the provider directly. The `_process_loop` is the single serialization point for all DB writes.

### Bug 2: Incomplete cascade in `delete_file_completely`

`DuckDBProvider._executor_delete_file_completely` (line 1324) deletes: embeddings → chunks → files.

It does **NOT** delete `symbol_edges` or `symbols`. When the realtime service removes a file, symbol data becomes orphaned. Later, `LSPPopulationService.populate_file` tries to delete edges/symbols for the same file_id and hits FK violations because the edges reference symbol IDs that no longer have a corresponding file row.

**Bare call inventory in `_executor_delete_file_completely`:**
```python
# Current (line 1346-1361):
DELETE FROM {embedding_table} WHERE chunk_id IN (SELECT id FROM chunks WHERE file_id = ?)
DELETE FROM chunks WHERE file_id = ?
DELETE FROM files WHERE id = ?
# Missing:
DELETE FROM symbol_edges WHERE from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)
DELETE FROM symbols WHERE file_id = ?
```

**Fix:** Add symbol_edges and symbols deletion to `_executor_delete_file_completely`, in correct FK order: symbol_edges → symbols → embeddings → chunks → files.

### Bug 3: Sync DB calls blocking the event loop

`LSPPopulationService` makes 8 sync `execute_query()` calls (lines 114, 221, 242, 318, 334, 388, 501, 520) from async context. Each blocks the asyncio event loop while waiting on the executor thread via `future.result(timeout=30)`. `realtime_indexing_service.py` line 911 also uses sync `execute_query()` from async context.

Not directly causing corruption, but degrades concurrency and can cause cascading timeouts under load.

**Fix:** Convert all `execute_query()` calls in async codepaths to `execute_query_async()`.

## Implementation

### Step 1: Write regression test — orphaned symbols + concurrent delete = FK violation
Reproduce the observed crash: insert a file with chunks, symbols, and symbol_edges. Call `delete_file_completely` (which currently skips symbols/edges). Then call `populate_file` for the same file — the `DELETE FROM symbol_edges WHERE from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)` should hit FK violations or find orphaned rows. This is the end-to-end regression for the FATAL.

### Step 2: Write regression test — cascade completeness
Insert a file with chunks, embeddings, symbols, and symbol_edges. Call `delete_file_completely`. Assert all five tables are clean for that file_id.

### Step 3: Fix `_executor_delete_file_completely` cascade
Add `DELETE FROM symbol_edges` and `DELETE FROM symbols` steps in correct FK order before the existing deletes. Check LanceDB provider too.

### Step 4: Write regression test — no direct provider calls from event consumer
Structural or behavioral test: `remove_file` and `_cleanup_deleted_directory` must not call provider DB methods directly. Verify deletions route through the file queue.

### Step 5: Route `_consume_events` DB writes through the file queue
- `remove_file()`: instead of `provider.delete_file_completely_async()`, queue with `priority="delete"`
- `_cleanup_deleted_directory()`: same — queue each file for deletion
- `_process_loop`: add handler for `priority="delete"` that calls `delete_file_completely_async`

### Step 6: Convert sync DB calls to async in LSP population
Convert 8 `execute_query()` calls in `lsp_population.py` to `execute_query_async()`. Convert 1 in `realtime_indexing_service.py` line 911.

## Success Criteria
- [ ] `delete_file_completely` removes symbol_edges and symbols (regression test)
- [ ] No direct provider DB calls in `_consume_events`, `remove_file`, or `_cleanup_deleted_directory`
- [ ] All `execute_query()` calls in async codepaths converted to `execute_query_async()`
- [ ] Full test suite passes
- [ ] Daemon restart with 894+ files does not produce transaction errors (manual verification)

## Anti-Patterns
- NO adding a second lock/mutex — the file queue IS the serialization mechanism, use it
- NO wrapping bare provider calls in try/except to suppress errors — fix the concurrency, don't hide it
- NO adding transaction retry logic — the problem is interleaving, not transient failure
