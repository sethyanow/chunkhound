---
id: ch-5b3
title: 'Population service: documentSymbol → symbols table'
status: open
type: task
priority: 1
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
- Confidence field set: `compiler_grade` when LSP returns results, `unavailable` when no server
- Graceful skip for files with no configured language server
- Wired into both realtime (file watcher) and batch (CLI index) paths

## Design

### New module: `chunkhound/services/lsp_population.py`

**`LSPPopulationService`** class:
- Constructor takes `LSPClientPool`, DuckDB connection/provider, and workspace root
- `populate_file(file_path, file_id, language)` → calls `documentSymbol`, maps to rows, batch inserts
- `populate_files(file_paths_with_ids)` → bulk version for batch indexing
- `delete_file_symbols(file_id)` → clears symbols for a file (prep for incremental, used in next task)

### Symbol mapping: `SymbolInfo` → `symbols` table row

Each `SymbolInfo` from LSP maps to one row:
- `fqn`: Construct from container hierarchy (`parent.name + "." + symbol.name`)
- `name`: `symbol.name`
- `kind`: `_SYMBOL_KIND[symbol.kind]` (int → string, same mapping as demo script)
- `language`: From file extension / registry key
- `file_id`: Passed in from caller (already resolved by indexing pipeline)
- `file_path`: Relative path
- `range_start` / `range_end`: `symbol.range_start_line` / `symbol.range_end_line`
- `parent_fqn`: Parent symbol's FQN (from recursive traversal of children)
- `confidence`: `"compiler_grade"` for LSP results, `"unavailable"` if no server
- `lsp_server`: Config command name (e.g., `"pyright-langserver"`)

Children are flattened recursively — each child becomes its own row with `parent_fqn` pointing up.

### Wiring into indexing pipeline

**Realtime path** (`realtime_indexing_service.py:_process_loop`):
- Add `priority="lsp"` branch mirroring the existing `"embed"` branch (line 887)
- After successful `process_file()` + embed queue (line 916), also queue `priority="lsp"`
- The `"lsp"` branch calls `lsp_population.populate_file()` with file_id looked up from DB

**Batch path** (`directory_indexing_service.py:process_directory`):
- Add `_populate_symbols()` method mirroring `_generate_missing_embeddings()` (line 140)
- Called after embedding generation in `process_directory()` (line 98)
- Queries DB for all files, calls `lsp_population.populate_files()`

### file_id resolution

After `process_file()` the file is in the DB. Look up by path:
```sql
SELECT id FROM files WHERE path = ?
```

## Implementation

1. Write test: `LSPPopulationService.populate_file()` calls `documentSymbol`, inserts correct rows
2. Write test: Graceful skip when no LSP server configured for language
3. Write test: Batch insert — multiple symbols from one file inserted in single statement
4. Write test: FQN construction from nested symbol hierarchy
5. Write test: `delete_file_symbols()` removes all symbols for a given file_id
6. Implement `LSPPopulationService` in `chunkhound/services/lsp_population.py`
7. Write test: `_process_loop` queues and processes `"lsp"` priority
8. Wire into `realtime_indexing_service.py:_process_loop()` — add `"lsp"` branch + queue
9. Wire into `directory_indexing_service.py:process_directory()` — add `_populate_symbols()` post-pass
10. Smoke tests pass

## Success Criteria
- [ ] `LSPPopulationService.populate_file()` calls `documentSymbol` and inserts rows to `symbols` table
- [ ] Symbols have correct fqn, kind, range, parent_fqn, confidence, lsp_server fields
- [ ] Nested symbols (methods inside classes) flatten to rows with correct parent_fqn
- [ ] Files with no configured LSP server are skipped gracefully (no error, no row)
- [ ] Batch insert used — single INSERT statement per file, not per symbol
- [ ] `delete_file_symbols(file_id)` removes that file's symbols
- [ ] Realtime path: file change queues `"lsp"` priority, population runs
- [ ] Batch path: `process_directory()` runs symbol population after embeddings
- [ ] All existing tests pass (zero regression)
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
- NO LSP calls in the tree-sitter parsing pipeline — population is a post-processing pass
- NO single-row inserts in loops — batch all symbols per file
- NO blocking the file watcher event loop — population is async
- NO silently dropping failed LSP calls — log with structured reason, set confidence to `"unavailable"`
- NO `type_signature` population in this task — that's hover, scoped to next task
- NO edge population in this task — that's definition/references/calls, scoped to next task
