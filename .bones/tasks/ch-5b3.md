---
id: ch-5b3
title: 'Population service: documentSymbol → symbols table'
status: active
type: task
priority: 1
owner: Seth
parent: ch-0um
---





## Context
First task for Phase 2 (ch-0um). Phase 1 delivered the LSP client (`chunkhound/lsp/`) and empty DuckDB tables (`symbols`, `symbol_edges`). This task creates the population service that calls `documentSymbol` per file and writes to the `symbols` table.

Scoped to `documentSymbol` → `symbols` only. Hover (type_signature) and edge operations (definition/references/implementation/calls) are separate tasks.

**Blocked by:** ch-0um depends on ch-7j0 (closed). No task-level blockers.
**Unlocks:** Edge population (next task), incremental population, and ultimately the full Phase 2 gate.

## Requirements
From parent epic R3, scoped to symbol extraction only:
- Background population service runs after tree-sitter indexing completes (not blocking it)
- `documentSymbol` called per file → symbols written to `symbols` table
- Batch inserts (no single-row loops per anti-pattern)
- Confidence field: `1.0` when LSP returns results, `0.5` for partial/degraded, `0.0` when no server (FLOAT column, not TEXT)
- Graceful skip for files with no configured language server (`LSPClientPool.get()` raises `LSPError`, not returns None — must catch)
- Wired into both realtime (file watcher) and batch (CLI index) paths

## Design

### New module: `chunkhound/services/lsp_population.py`

**`LSPPopulationService`** class:
- Constructor takes `LSPClientPool`, `DatabaseProvider`, and workspace root
- `populate_file(file_path, file_id, language)` → calls `documentSymbol`, maps to rows, batch inserts
- `populate_files(file_paths_with_ids)` → bulk version for batch indexing
- `delete_file_symbols(file_id)` → clears symbols for a file (prep for incremental, used in next task)

**LSP protocol requirement — `didOpen` before `documentSymbol`:**
LSP servers (pyright confirmed — see `scripts/demo_lsp.py:104-107`) require `textDocument/didOpen` with
file content before `documentSymbol` returns results. Without it, `document_symbols()` returns `[]` silently.

Per-file flow in `populate_file`:
1. Read file content from disk
2. `client.notify_did_open(uri, content, language_id)` — sends content to LSP server
3. `client.document_symbols(uri)` — now returns symbols
4. `client.notify_did_close(uri)` — frees server memory (**NOTE:** `didClose` not yet in LSPClient — must be added as part of this task, see Key Considerations**)

**LSPClientPool lifecycle:**
- Created once at application startup (where `DatabaseServices` is constructed)
- Passed to `LSPPopulationService` constructor
- For `RealtimeIndexingService`: store on `self` (it already has `self.services: DatabaseServices`)
- For `DirectoryIndexingService`: add optional `lsp_population: LSPPopulationService | None` constructor parameter

### Symbol mapping: `SymbolInfo` → `symbols` table row

Each `SymbolInfo` from LSP maps to one row:
- `fqn`: Construct from container hierarchy (`parent.name + "::" + symbol.name`) — "::" avoids ambiguity with dots in symbol names
- `name`: `symbol.name`
- `kind`: `_SYMBOL_KIND.get(symbol.kind, f"unknown_{symbol.kind}")` (int → string, mapping from `scripts/demo_lsp.py:24-52` — move to `chunkhound/lsp/constants.py` for reuse)
- `language`: `Language.from_file_extension(file_path).value` (existing method in `chunkhound/core/types/common.py`)
- `file_id`: Passed in from caller (already resolved by indexing pipeline)
- `file_path`: Relative path
- `range_start` / `range_end`: `symbol.range_start_line` / `symbol.range_end_line`
- `parent_fqn`: Parent symbol's FQN (from recursive traversal of children)
- `confidence`: `1.0` for full LSP results, `0.5` for partial/degraded, `0.0` for unavailable (FLOAT column — matches Phase 1 schema)
- `lsp_server`: Config command name (e.g., `"pyright-langserver"`)

Children are flattened recursively — each child becomes its own row with `parent_fqn` pointing up.

### Wiring into indexing pipeline

**Realtime path** (`realtime_indexing_service.py:_process_loop`):
- Add `priority="lsp"` branch mirroring the existing `"embed"` branch (line 887)
- After successful `process_file()` + embed queue (line 916), also queue `priority="lsp"`
- The `"lsp"` branch calls `lsp_population.populate_file()` with file_id looked up from DB

**Batch path** (`directory_indexing_service.py:process_directory`):
- Add optional `lsp_population: LSPPopulationService | None` to `__init__` (currently only takes `indexing_coordinator` and `config`)
- Add `_populate_symbols()` method mirroring `_generate_missing_embeddings()` (line 140)
- Called after embedding generation in `process_directory()` (line 98)
- Queries DB for all files, calls `lsp_population.populate_files()`
- Skip entirely if `lsp_population is None` (backwards compat)

### file_id resolution

After `process_file()` the file is in the DB. Look up by path:
```sql
SELECT id FROM files WHERE path = ?
```

## Implementation

1. Add `notify_did_close(uri)` to `LSPClient` in `chunkhound/lsp/client.py` (mirrors `notify_did_open`)
2. Move `_SYMBOL_KIND` mapping from `scripts/demo_lsp.py` to `chunkhound/lsp/constants.py` (shared)
3. Write test: `populate_file()` calls `didOpen` → `documentSymbol` → `didClose` → inserts correct rows
4. Write test: Graceful skip when `LSPClientPool.get()` raises `LSPError` (no server configured)
5. Write test: Batch insert — verify single `executemany`/batch INSERT, not per-symbol inserts
6. Write test: FQN construction from nested symbol hierarchy (2-3 levels deep)
7. Write test: `delete_file_symbols(file_id)` removes all symbols for a given file_id
8. Write test: Unknown `SymbolKind` (e.g., kind=99) doesn't crash — maps to `"unknown_99"`
9. Implement `LSPPopulationService` in `chunkhound/services/lsp_population.py`
10. Write test: `_process_loop` queues and processes `"lsp"` priority
11. Wire into `realtime_indexing_service.py:_process_loop()` — add `"lsp"` branch + queue
12. Wire into `directory_indexing_service.py` — add optional `lsp_population` param + `_populate_symbols()`
13. Smoke tests pass

## Success Criteria
- [ ] `populate_file()` calls `didOpen` → `documentSymbol` → `didClose` sequence and inserts rows to `symbols` table
- [ ] Symbols have correct fqn, kind, range, parent_fqn, confidence, lsp_server fields
- [ ] Nested symbols (methods inside classes) flatten to rows with correct parent_fqn
- [ ] Files with no configured LSP server are skipped gracefully (`LSPError` caught, no crash, no row)
- [ ] Batch insert used — single batch INSERT per file (verified by mock/spy, not just row count)
- [ ] `delete_file_symbols(file_id)` removes that file's symbols
- [ ] Unknown SymbolKind values (>26) handled gracefully — mapped to `"unknown_N"`, not KeyError
- [ ] Realtime path: file change queues `"lsp"` priority, population runs
- [ ] Batch path: `process_directory()` runs symbol population after embeddings
- [ ] `notify_did_close(uri)` added to `LSPClient` and called after population per file
- [ ] `_SYMBOL_KIND` mapping lives in `chunkhound/lsp/constants.py` (shared, not duplicated)
- [ ] All existing tests pass (zero regression)
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Key Considerations

### Resolved: confidence column

Original skeleton said TEXT (`"compiler_grade"`), but Phase 1 schema is `FLOAT DEFAULT 1.0`. Gaffe — epic used those as semantic labels, not literal values. **Resolution:** use floats: `1.0` = compiler_grade, `0.5` = partial, `0.0` = unavailable.

### Adversarial Failure Catalog

**Dependency Treachery: didOpen protocol (populate_file)**
- Assumption: `document_symbols(uri)` works on any file URI
- Betrayal: Pyright (confirmed `scripts/demo_lsp.py:104`) requires `didOpen` with file content first. Without it, returns `[]` silently.
- Consequence: Zero symbols populated for every file — silent data loss, no error
- Mitigation: Structural — `populate_file` flow is `didOpen → documentSymbol → didClose`. Design section updated.

**Resource Exhaustion: No didClose in LSPClient (populate_file)**
- Assumption: LSP server handles thousands of open documents
- Betrayal: `LSPClient` has `notify_did_open` but no `notify_did_close`. Server accumulates memory per file.
- Consequence: OOM for large codebases (10,000+ files in batch mode)
- Mitigation: Add `notify_did_close(uri)` to `LSPClient` as part of this task. Simple notification, mirrors didOpen.

**Dependency Treachery: LSPClientPool.get() throws (populate_file)**
- Assumption: No-config languages return None
- Betrayal: `LSPClientPool.get()` raises `LSPError("No server config for language: ...")` (line 596) or `LSPError("No server binary configured for: ...")` (line 600) for `command=None` entries
- Consequence: Unhandled exception kills population service
- Mitigation: Catch `LSPError` specifically around pool.get(). Log language, skip file, no crash.

**Input Hostility: Unknown SymbolKind (symbol mapping)**
- Assumption: All kind values are in standard LSP range 1-26
- Betrayal: Custom LSP servers may return extension values >26
- Consequence: KeyError on dict lookup
- Mitigation: `_SYMBOL_KIND.get(kind, f"unknown_{kind}")` — never raises

**Encoding Boundaries: FQN separator (symbol mapping)**
- Assumption: FQN uses "." separator
- Betrayal: C++/Rust use "::", Go uses "/"
- Consequence: Inconsistent FQN format across languages
- Mitigation: Use "." universally in DB as ChunkHound's canonical separator. Not language-native display — symbol identity.

**Temporal Betrayal: file_id lookup race (realtime wiring)**
- Assumption: After `process_file()`, file_id is queryable
- Betrayal: If using separate DB connection, isolation could hide uncommitted writes
- Mitigation: Pass file_id from `process_file()` result through the queue rather than re-querying. Or use same connection.

**State Corruption: Crash between tree-sitter and LSP population (realtime wiring)**
- Assumption: LSP population always follows tree-sitter success
- Betrayal: Process crash, LSP server crash, or timeout
- Consequence: File has chunks but no symbols — appears indexed but incomplete
- Mitigation: Batch `_populate_symbols()` recovers this by checking for files with chunks but no symbols. Accept gap in realtime — next file change re-triggers.

**Resource Exhaustion: Queue buildup during LSP startup (realtime wiring)**
- Assumption: LSP server is ready when first "lsp" event processes
- Betrayal: First file triggers server spawn (5-30s). All subsequent files queue their "lsp" events.
- Consequence: Thousands of queued events
- Mitigation: Processing is sequential — queue is bounded by indexing speed, not event count. First file slow (startup), subsequent fast. Document expected behavior.

**Dependency Treachery: No LSP servers installed (batch wiring)**
- Assumption: Some LSP servers are on PATH
- Betrayal: CI, containers, fresh machines have no LSP servers
- Consequence: Every file triggers LSPError, zero symbols populated
- Mitigation: Log one clear warning: "No LSP servers found — symbol population skipped." Don't fail indexing pipeline.

## Anti-Patterns
- NO LSP calls in the tree-sitter parsing pipeline — population is a post-processing pass
- NO single-row inserts in loops — batch all symbols per file
- NO blocking the file watcher event loop — population is async
- NO silently dropping failed LSP calls — log with structured reason, set confidence to `0.0`
- NO `type_signature` population in this task — that's hover, scoped to next task
- NO edge population in this task — that's definition/references/calls, scoped to next task
