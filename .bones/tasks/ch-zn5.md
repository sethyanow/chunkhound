---
id: ch-zn5
title: DuckDB connection architecture overhaul
status: open
type: bug
priority: 0
---

## Context

Daemon restart on 2026-04-05 triggered a FATAL DuckDB corruption that wiped the index. Investigation revealed the root cause is architectural: DuckDB's single-connection model doesn't match a daemon serving concurrent async operations.

**Observed failure sequence (daemon.log):**
1. Daemon starts, polling monitor detects 894 files
2. `_store_parsed_results` begins explicit transactions (`BEGIN TRANSACTION`) for tree-sitter indexing
3. Concurrently, `_consume_events` calls `delete_file_completely_async` directly — interleaves with open transactions
4. Transaction state poisons: alternating `"transaction is aborted"` / `"cannot start a transaction within a transaction"`
5. FK constraint violations: `DELETE FROM symbols WHERE file_id = ?` fails because `symbol_edges` still references those symbols
6. DuckDB HNSW index corruption: `"Failed to delete all rows from index. Only deleted 0 out of 13 rows"`
7. FATAL: `"database has been invalidated because of a previous fatal error"` — every subsequent operation fails, DB wiped on restart

**The deeper problem:** Everything — reads AND writes — funnels through one `ThreadPoolExecutor(max_workers=1)`. A `code_research` query blocks behind an indexing write. Searches block behind embed passes. The daemon+proxy+IPC+lock architecture (400+ lines) exists solely to work around DuckDB's single-writer file lock.

**Process model (verified):** Each project runs two PIDs — a `chunkhound mcp` stdio proxy and a `_daemon` subprocess. The proxy is a pure stdio↔IPC bridge (no DB). The daemon owns the sole DB connection. Multiple Claude sessions connect as IPC clients to the same daemon.

**Key files:**
- `chunkhound/providers/database/serial_executor.py` — `SerialDatabaseExecutor`, single-thread pool
- `chunkhound/providers/database/serial_database_provider.py` — base class, all ops through executor
- `chunkhound/providers/database/duckdb_provider.py` — `_executor_delete_file_completely` (missing cascade)
- `chunkhound/services/realtime_indexing_service.py` — `_consume_events` races with `_process_loop`
- `chunkhound/services/lsp_population.py` — 8 sync `execute_query()` calls blocking event loop
- `chunkhound/daemon/server.py` — `ChunkHoundDaemon`
- `chunkhound/daemon/client_proxy.py` — `ClientProxy` (stdio↔IPC bridge)

## Requirements

### R1: Data correctness — `delete_file_completely` cascade

`_executor_delete_file_completely` deletes embeddings → chunks → files but leaves orphaned `symbols` and `symbol_edges`. Subsequent LSP population hits FK violations. Fix: delete in FK order: symbol_edges → symbols → embeddings → chunks → files.

**Status: FIXED** — regression tests written and passing (`tests/integration/test_delete_file_cascade.py`).

### R2: Read/write connection separation

Separate the single DuckDB connection into distinct read and write paths:

- **Write connection** (single, serialized): indexing, embedding, symbol population, deletes — all mutations. Serialized through `_process_loop` file queue.
- **Read connection(s)** (`read_only=True`): search, code_research, graph queries, stats, get_stats — all MCP tool reads. Pool of 2-4 connections for concurrent tool calls from multiple daemon clients.

Reads don't compete with writes. Writes don't block reads. Eliminates the core contention that caused the FATAL.

### R3: Write serialization through file queue

All DB-mutating operations route through `_process_loop`'s file queue — the single serialization point for writes. `_consume_events` must not call the provider directly for mutations (currently `remove_file` and `_cleanup_deleted_directory` bypass the queue).

**Status: PARTIALLY DONE** — queue routing implemented, needs rebase after R2.

### R4: Atomic write transactions

`_store_parsed_results` does `BEGIN → work → COMMIT` as three separate executor submissions. Each `await` yields control, allowing interleaving. Fix: make each write operation a single executor submission (`_executor_store_file` does BEGIN+inserts+COMMIT atomically in the DB thread). No interleaving possible by construction.

### R5: Sync→async conversion in LSP population

8 sync `execute_query()` calls in `lsp_population.py` and 1 in `realtime_indexing_service.py` block the asyncio event loop. Convert to `execute_query_async()`. With R2, read queries route to the read pool; write queries route to the write connection.

**Status: DONE** — converted, test fixes applied (`test_resolve.py`, `test_edges.py`).

### R6: Connection FATAL detection + circuit breaker

When the write connection enters FATAL state, the daemon spammed ~1500 identical error messages. Fix: detect FATAL on first failure, stop all writes, log once, attempt reconnection or graceful shutdown. No cascading error spam.

### R7: DuckLake evaluation (pending research)

DuckLake with a PostgreSQL metadata catalog may eliminate the need for the daemon+proxy+IPC architecture entirely. If DuckLake handles multi-writer coordination, each `chunkhound mcp` process could connect directly — no daemon, no IPC, no lock files, no startup races.

**Research questions (user to investigate):**
- Does DuckLake support HNSW vector indexes on Parquet-backed tables?
- Does DuckLake-over-PostgreSQL handle concurrent writers from separate processes?
- What's the write performance for chunk-level inserts (Parquet compaction overhead)?
- Extension maturity — stable enough for a production dev tool?
- Can the metadata catalog be local PostgreSQL or does it need a running server?

**Pending user research results before scoping implementation.**

## Implementation

### Phase 1: Data correctness (DONE)
- [x] Cascade fix in `_executor_delete_file_completely`
- [x] Regression tests (`test_delete_file_cascade.py`)
- [x] Sync→async in `lsp_population.py`
- [x] `_resolve_symbol` async + test fixes

### Phase 2: Read/write separation
1. Create `ReadConnectionPool` — manages 2-4 `read_only=True` DuckDB connections
2. Split `SerialDatabaseProvider` methods into read vs write categories
3. Route read operations through `ReadConnectionPool`, write operations through existing executor
4. Route `_consume_events` mutations through file queue (delete priority handler)
5. Update MCP tool dispatch to use read connections for search/graph/stats

### Phase 3: Atomic writes
1. Refactor `_store_parsed_results` into `_executor_store_parsed_results` — single executor submission, BEGIN+work+COMMIT atomic
2. Remove `begin_transaction_async` / `commit_transaction_async` calls from `IndexingCoordinator`
3. Same pattern for any other multi-statement write paths

### Phase 4: Circuit breaker
1. Detect DuckDB FATAL state in executor error handler
2. Stop accepting writes, log once
3. Attempt connection reset (close + reopen)
4. If reset fails, signal daemon shutdown

### Phase 5: DuckLake migration (pending R7 research)
Scope TBD based on research results. If viable, replaces Phase 2-4 with a fundamentally different architecture.

## Success Criteria
- [x] `delete_file_completely` removes symbol_edges and symbols (regression test)
- [x] All `execute_query()` calls in async codepaths converted to `execute_query_async()`
- [ ] Read operations use `read_only=True` connections, don't block behind writes
- [ ] Write operations serialized through file queue — no direct provider calls from `_consume_events`
- [ ] Write transactions are single executor submissions (no multi-await interleaving)
- [ ] FATAL DuckDB state triggers circuit breaker, not 1500 error messages
- [ ] Concurrent MCP tool calls from multiple clients don't block each other
- [ ] Full test suite passes
- [ ] Daemon restart with 894+ files does not produce transaction errors

## Anti-Patterns
- NO adding more locks/mutexes — separate connections for reads vs writes
- NO wrapping bare provider calls in try/except to suppress errors — fix the architecture
- NO transaction retry logic — the problem is interleaving, not transient failure
- NO taking comments/docstrings as architectural truth — verify the process model
- NO treating symptoms when the architecture is the root cause
