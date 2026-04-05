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
- `chunkhound/services/realtime_indexing_service.py` — `_consume_events` (events) + `_process_loop` (writes) — queue routing fixed (R3)
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

All DB-mutating operations route through `_process_loop`'s file queue — the single serialization point for writes. `_consume_events` must not call the provider directly for mutations.

**Status: DONE** — `remove_file` and `_cleanup_deleted_directory` both route through `add_file(priority="delete")`, handled by `_process_loop` delete handler (verified 2026-04-05). No direct provider mutation calls remain in `_consume_events`.

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
1. Create `ReadConnectionPool` in `chunkhound/providers/database/serial_executor.py` — manages 2-4 `read_only=True` DuckDB connections with round-robin checkout
2. Add `_read_executor` to `SerialDatabaseProvider` alongside existing `_executor` (write path)
3. Add `_execute_in_read_thread` method parallel to `_execute_in_db_thread` — routes to read pool
4. Classify and route `SerialDatabaseProvider` methods:
   - **Read (→ read pool):** `search_semantic`, `search_regex`, `search_text`, `search_by_embedding`, `find_similar_chunks`, `get_stats`, `get_file_by_path`, `get_file_by_id`, `get_chunk_by_id`, `get_chunks_by_file_id`, `get_chunks_in_range`, `get_all_chunks_with_metadata`, `get_scope_stats`, `get_scope_file_paths`, `get_provider_stats`, `get_existing_embeddings`, `health_check`, `execute_query` (SELECT-only — read path for MCP tools)
   - **Write (→ existing executor):** `insert_file`, `update_file`, `delete_file_completely`, `insert_chunks_batch`, `delete_chunks_batch`, `insert_embeddings_batch`, `create_schema`, `create_indexes`, `create_vector_index`, `drop_vector_index`, `begin/commit/rollback_transaction`, `optimize_tables`, `execute_query` (mutations — write path for indexing)
5. ~~Route `_consume_events` mutations through file queue~~ (DONE — already implemented)
6. Override `_create_connection` in DuckDB provider to accept `read_only` param — `duckdb.connect(path, read_only=True)` + load VSS extension

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
- [ ] DuckDB `read_only=True` connections can load VSS and query HNSW indexes (spike test — gate for pool design)
- [ ] Read operations use `read_only=True` connections, don't block behind writes
- [x] Write operations serialized through file queue — no direct provider calls from `_consume_events`
- [ ] Write transactions are single executor submissions (no multi-await interleaving)
- [ ] FATAL DuckDB state triggers circuit breaker, not 1500 error messages
- [ ] Concurrent MCP tool calls from multiple clients don't block each other
- [ ] Full test suite passes
- [ ] Daemon restart with 894+ files does not produce transaction errors

## Key Considerations

### Failure Catalog

**[Dependency Treachery]: ReadConnectionPool — VSS extension on read_only**
- Assumption: `LOAD vss` and HNSW index scans work on `read_only=True` connections
- Betrayal: DuckDB may block extension loading or HNSW scans on read_only connections. Extension state is per-connection — write connection loading VSS doesn't help read connections.
- Consequence: All semantic search fails on read connections. Falls back to write executor, defeating the purpose of the entire pool.
- Mitigation: **Spike test FIRST** — before building the pool, write a minimal test that opens `duckdb.connect(path, read_only=True)`, loads VSS, runs an HNSW query. If it fails, alternative: use `access_mode='automatic'` instead of `read_only=True`, or convention-based write prohibition without the flag.

**[State Corruption]: ReadConnectionPool — WAL visibility gap**
- Assumption: Read connections see writes committed by the write connection immediately
- Betrayal: DuckDB WAL is flushed on checkpoint (every 100 ops or 5 min per `_executor_maybe_checkpoint`). Between checkpoints, read_only connections may not see recent inserts — file indexed 2 seconds ago returns 0 search results.
- Consequence: User perceives indexing as broken ("I just saved the file but search doesn't find it")
- Mitigation: Verify DuckDB read_only connections read through WAL (version-specific). If not: force checkpoint after each write batch in `_process_loop`, or accept eventual consistency with documented latency.

**[Temporal Betrayal]: ReadConnectionPool — initialization race**
- Assumption: Schema and indexes exist before read connections are created
- Betrayal: First MCP read arrives before `create_schema` completes. Eager pool init connects to empty DB. Reads fail with "table does not exist."
- Consequence: Silent failures or errors from MCP tools on daemon startup
- Mitigation: Lazy pool initialization — first read triggers pool creation. Pool init verifies tables exist before returning a connection. Or: pool creation gated on a `schema_ready` event set by the write path.

**[Temporal Betrayal]: Method routing — write misrouted to read pool**
- Assumption: All write methods correctly classified and routed to write executor
- Betrayal: A method classified as "read" contains a side effect, or `execute_query` called with INSERT/UPDATE from a read-routed code path
- Consequence: DuckDB read_only connections throw on write attempts (not silent — this is good). But unhandled exception crashes the MCP tool call.
- Mitigation: Write an integration test that attempts writes on a read_only connection and verifies the error is raised. Add `execute_query_read` as a separate method — no routing parameter needed. Write-path callers use `execute_query` (always write executor); read-path callers use `execute_query_read`.

**[State Corruption]: Method routing — `execute_query` dual routing**
- Assumption: Callers correctly choose read vs write path for `execute_query`
- Betrayal: Graph queries in MCP tools use `execute_query` for SELECTs. LSP population uses `execute_query_async` for INSERTs. If a graph tool accidentally uses the write variant, it blocks behind writes. If population uses the read variant, it crashes.
- Consequence: Performance regression (safe) or crash (loud). Not silent corruption.
- Mitigation: Separate methods: `execute_query` (write executor), `execute_query_read` / `execute_query_read_async` (read pool). No ambiguity at call site.

**[Resource Exhaustion]: ReadConnectionPool — memory pressure**
- Assumption: 2-4 read connections is acceptable resource cost
- Betrayal: Each DuckDB connection allocates buffer pool memory. With HNSW indexes, each connection may memory-map index data. 4 connections × index size could cause OOM on smaller machines.
- Consequence: Memory pressure, swapping, or OOM kill of daemon process
- Mitigation: Start with `pool_size=2`, make configurable. Log memory per connection at pool init. Monitor during acceptance.

**[Resource Exhaustion]: Atomic writes (Phase 3) — single-submission timeout**
- Assumption: Atomic per-file store completes within reasonable time
- Betrayal: `execute_sync` has 30s timeout. A file with 500+ chunks plus embedding table inserts could exceed this. `execute_async` has NO timeout — hangs forever.
- Consequence: `execute_sync`: TimeoutError, rollback, file stays unindexed. `execute_async`: daemon thread hangs permanently.
- Mitigation: The refactor moves one file's BEGIN+work+COMMIT into a single submission (not entire batch). Per-file is bounded. But add a timeout to `execute_async` too (currently missing — separate bug).

**[Temporal Betrayal]: Atomic writes (Phase 3) — progress reporting thread safety**
- Assumption: `self.progress.advance()` can be called from executor thread
- Betrayal: Rich Progress is not thread-safe. Calling from executor thread may corrupt display state.
- Consequence: Progress bar garbles or crashes during indexing
- Mitigation: Return progress data from the executor submission. Update progress AFTER submission returns, in the async context (main thread).

**[Temporal Betrayal]: Circuit breaker (Phase 4) — false FATAL classification**
- Assumption: Specific error message means unrecoverable FATAL
- Betrayal: DuckDB has different error classes. "transaction is aborted" is recoverable (rollback). Matching on message strings misclassifies errors.
- Consequence: Circuit breaker trips on recoverable error, daemon shuts down unnecessarily
- Mitigation: Match on `duckdb.FatalException` type (line 2859 already imports `TransactionException`). Classify by exception type, not message string.

**[State Corruption]: Circuit breaker (Phase 4) — reconnection validates nothing**
- Assumption: Close + reopen restores clean state
- Betrayal: WAL corruption survives reconnect. New connection opens successfully but reads return garbage. Or: close hangs because FATAL left internal locks held.
- Consequence: Circuit breaker reports "recovered" but data is silently corrupted
- Mitigation: After reconnection, run validation query (`SELECT count(*) FROM files`). If it fails or returns unexpected results, proceed to graceful shutdown instead of declaring recovery.

**[Temporal Betrayal]: Circuit breaker (Phase 4) — FATAL scope isolation**
- Assumption: FATAL on write connection means the database is broken
- Betrayal: Read pool connections are independent — they may continue working fine. Shutting down the entire daemon kills working read paths.
- Consequence: Over-aggressive circuit breaking kills healthy reads
- Mitigation: Circuit breaker only stops WRITES. Read pool continues serving cached/committed data. If read connections also fail, then escalate to full shutdown.

## Anti-Patterns
- NO adding more locks/mutexes — separate connections for reads vs writes
- NO wrapping bare provider calls in try/except to suppress errors — fix the architecture
- NO transaction retry logic — the problem is interleaving, not transient failure
- NO taking comments/docstrings as architectural truth — verify the process model
- NO treating symptoms when the architecture is the root cause

## Log

- [2026-04-05T16:10:23Z] [Seth] Acceptance investigation findings (2026-04-05):

1. CASCADE FIX (done): delete_file_completely now cleans up symbol_edges + symbols in FK order. Regression tests passing.

2. ASYNC CONVERSION (done): 8 sync execute_query() calls in lsp_population.py converted to async. _resolve_symbol made async with test fixes.

3. ARCHITECTURAL FINDING: The entire graph intelligence layer (Phases 1-5 of ch-8e7) bypasses the DatabaseProvider protocol. Symbols, symbol_edges, graph walks, fusion tools — all hardcoded to raw DuckDB SQL via execute_query(), sqlglot query builders generating DuckDB-dialect CTEs. LanceDB provider has no symbols/symbol_edges tables.

4. CONSEQUENCE: 'provider: lancedb' config gives chunk search but zero graph features. The daemon+proxy+IPC architecture (400+ lines) exists because DuckDB was the default and graph layer cemented the dependency.

5. PROVIDER STATE: LanceDB provider (2400+ lines) supports files, chunks, embeddings, vector search with native concurrent writes and persistent HNSW. DuckDB provider has the graph layer but single-writer limitation requiring the daemon.

6. OPEN DECISION: Abstract the graph layer behind DatabaseProvider protocol (both backends work, LanceDB becomes viable for graph features, possibly eliminates daemon) vs commit to DuckDB-only and fix its connection model (read/write separation, atomic writes, circuit breaker).

Queue routing changes (remove_file through file queue) implemented but held — correct regardless of direction, but scope depends on the abstraction decision.
