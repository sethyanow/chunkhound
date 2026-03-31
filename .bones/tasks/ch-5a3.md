---
id: ch-5a3
title: 'Phase 2 completeness: workspaceSymbol, incremental, multi-language, confidence'
status: closed
type: task
priority: 1
owner: Seth
parent: ch-0um
---






## Context
Fourth and final implementation task for Phase 2 (ch-0um). ch-5b3 delivered documentSymbol, ch-nvc delivered hover/type_signature, ch-zlg delivered edge population. This task completes the remaining 4 functional criteria.

**Blocked by:** ch-zlg (closed)
**Unlocks:** Phase 2 acceptance task, which closes ch-0um and unblocks Phase 3 (ch-zyz)

## Requirements
From parent sub-epic ch-0um, scoped to remaining unchecked criteria:
- `workspaceSymbol("")` called during full reindex for cross-file symbol completeness
- Incremental: file change via file watcher → deletes symbols + edges → repopulates (verify existing wiring)
- Multi-language: symbols and edges populated for Python + at least 2 other languages
- Confidence field reflects LSP result quality (compiler_grade, partial, unavailable)

## Design

### workspaceSymbol — new LSPClient method + population integration

**New method on LSPClient:**
- Signature: `async def workspace_symbols(self, query: str = "") -> list[SymbolInfo]`
- LSP method: `workspace/symbol`, capability: `LSPCapability.WORKSPACE_SYMBOL`
- Params: `{"query": query}` — empty string returns all workspace symbols
- Parse response via existing `_parse_symbols`
- Note: some servers (ty) don't support workspaceSymbol. Capability-gate as usual.

**SRE: `location.uri` extraction (decision: option b).** Add `location_uri: str | None = None` to `SymbolInfo`. `_parse_symbols` populates it from `location.uri` when present (workspaceSymbol results have it, documentSymbol results don't). `populate_files` uses `location_uri` to resolve file_path and file_id for DB insertion.

**Integration into `populate_files`:**
- After the per-file `populate_file` loop in `LSPPopulationService.populate_files` (line 147)
- For each language with a configured server: call `workspace_symbols("")`
- For each returned symbol: resolve file_path, check if symbol already exists (by fqn + file_path)
- Insert only NEW symbols not already in the table (skip duplicates)
- This catches cross-file symbols that documentSymbol per-file might miss

### Incremental refresh — verify existing wiring

The wiring already exists:
- `RealtimeIndexingService._process_loop` (line 954-956) queues "lsp" priority after tree-sitter
- `populate_file` does `delete_file_edges` → `delete_file_symbols` → re-insert
- Test needs to verify: second call with different data → old data gone, new data present

### Multi-language — verify language-agnostic code

`populate_file` already accepts `language` param and looks up server from registry. Need a test with 3 mock languages showing symbols from each land in the DB with correct `language` and `lsp_server` fields.

### Confidence field — contextual values

Current state: hardcoded `1.0` everywhere.

Strategy:
- **Symbols** from `documentSymbol`: always `1.0` (compiler_grade) — documentSymbol is authoritative
- **Symbols** from `workspaceSymbol`: `0.9` — workspace symbol results may be less precise for range data
- **Edges**: `1.0` when LSP operation succeeds (compiler_grade data). No `unavailable` edges exist — if the operation fails or isn't supported, no edge is created (it's skipped)
- The `confidence` column is a float, not an enum. Use: `1.0` = compiler_grade, `0.9` = high confidence (workspace), lower values reserved for future use

## Implementation

1. Write test: `LSPClient.workspace_symbols("")` returns list[SymbolInfo]
2. Implement `workspace_symbols` on `LSPClient` — capability-gated, same parse pattern as `document_symbols`
3. Write test: `populate_files` calls `workspace_symbols` after per-file loop, inserts new symbols not already present
4. Implement workspaceSymbol integration in `populate_files` — deduplicate by (fqn, file_path)
5. Write test: confidence is `0.9` for workspaceSymbol-sourced symbols (vs `1.0` for documentSymbol)
6. Update `populate_files` workspaceSymbol insert path to use confidence=0.9
7. Write test: incremental refresh — populate_file twice with DIFFERENT symbols → old replaced by new. Setup: first call inserts symbols [A, B] with edges. Second call inserts symbols [C]. Verify: A and B gone from `symbols` table, their edges gone from `symbol_edges` table, only C remains.
8. Write test: multi-language — 3 languages with mock clients → symbols for all 3 in DB with correct `language` AND `lsp_server` fields per language.
9. Write test: workspaceSymbol for file not in `files` table → symbol silently skipped (no crash, no orphan row).
10. Write test: workspaceSymbol on server without `WORKSPACE_SYMBOL` capability → skipped gracefully.
11. Run full test suite + smoke tests, commit

## Success Criteria
- [x] `LSPClient.workspace_symbols("")` returns parsed SymbolInfo list
- [x] `populate_files` calls workspaceSymbol after per-file loop
- [x] workspaceSymbol-sourced symbols have confidence=0.9 (not 1.0)
- [x] No duplicate symbols from workspaceSymbol + documentSymbol overlap
- [x] Incremental: second populate_file with different data → old symbols AND edges replaced
- [x] Multi-language: Python + 2 other languages in symbols table with correct `lsp_server` per language
- [x] All existing tests pass (zero regression)
- [x] `uv run pytest tests/test_lsp_population.py -v` → all pass (52 passed)

## Anti-Patterns
- NO full table scan to deduplicate workspaceSymbol results — use targeted query by (fqn, file_path)
- NO modifying confidence on existing symbols — workspaceSymbol adds NEW rows only
- NO changing the per-file populate_file flow — workspaceSymbol is a separate batch-level pass
- NO assuming all LSP servers support workspaceSymbol — capability-gate it
- NO reusing `test_second_run_is_idempotent` as the incremental test — idempotent uses same data, incremental requires DIFFERENT data to prove deletion
- NO treating `_parse_symbols` as sufficient for workspaceSymbol — must also extract `location.uri` for file resolution

## Key Considerations

### workspaceSymbol result format varies by server
Some servers return flat lists, others return hierarchical results. `_parse_symbols` already handles both (it was written for documentSymbol which has the same variation). Verify this works for workspace results too.

### workspaceSymbol may return symbols from files not in the DB
If a symbol's file_path doesn't have a matching `files` row, skip it — FK constraint on file_id would reject it. Only insert symbols for files that are already indexed.

### Incremental test is a verification, not new code
The wiring exists in `populate_file` (delete before insert). The test proves the wiring works end-to-end. If the test passes on first run, that's expected — the code was designed for idempotency in ch-zlg.

### SRE: Edge cases to test
- workspaceSymbol returns symbol for file not in `files` table → skip, don't crash
- workspaceSymbol returns symbol overlapping with documentSymbol (same fqn + file_path) → existing row kept, no duplicate
- Server lacks `WORKSPACE_SYMBOL` capability → `populate_files` workspaceSymbol pass silently skipped
- Incremental refresh test must verify BOTH symbols and edges are deleted/replaced, not just symbols

### Adversarial: workspaceSymbol URI handling
- Non-file URIs (`untitled:`, `git:`, `inmemory:`) must be filtered before path resolution — gate on `scheme == "file"` same as `_resolve_symbol` (lsp_population.py:358)
- Percent-encoded paths (`my%20file.py`) must be `unquote()`ed before comparing to `files.path` — same pattern as `_resolve_symbol` (line 360)
- Duplicate symbols in the response batch (re-exports, multiple paths) — dedup in-memory with `set[tuple[str, str]]` of `(fqn, file_path)` before DB check

### Adversarial: workspaceSymbol reliability
- `workspace/symbol` on large workspaces can be slow (10-30s for pyright on 10k+ files). Wrap call in try/except for `LSPError` family — log + skip on timeout, same as per-file pattern
- LSP spec allows partial results — workspace symbols are best-effort for cross-file completeness, not authoritative. Confidence=0.9 already signals this
- If `populate_files` is interrupted mid-loop, workspace pass checks dedup against partial state. Acceptable: next full reindex corrects confidence values. No design change needed

## Log

- [2026-03-31T13:59:39Z] [Seth] Debrief: No workarounds. location_uri as optional SymbolInfo field (user chose option b). DuckDB FLOAT precision needs pytest.approx for confidence. Workspace symbols insert individually (optimization candidate for later). Reflections: Steps 5-6 redundant with 3-4 (confidence inseparable from workspace insertion). URI resolution pattern appears twice (_resolve_symbol + _populate_workspace_symbols) — extract shared helper at 3rd occurrence. User correction: surfaced design decision as 3 options, user chose — good pattern to repeat.
