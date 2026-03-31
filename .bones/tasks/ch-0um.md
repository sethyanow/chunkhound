---
id: ch-0um
title: 'Phase 2: Index-Time Population'
status: open
type: epic
priority: 1
depends_on: [ch-7j0, ch-5b3, ch-nvc, ch-zlg, ch-5a3, ch-yda]
parent: ch-8e7
---















## Context
Parent epic ch-8e7, Phase 2. Depends on Phase 1 (ch-7j0).
Phase 1 delivers the LSP client manager and DuckDB tables. This phase populates those tables — wiring the LSP client into the indexing pipeline as a background pass that runs after tree-sitter chunking completes.

## Requirements
Scoped to parent epic R3:
- R3: Background index-time population using 8 LSP operations

## Success Criteria
- [x] Background population service runs after tree-sitter indexing completes (not blocking it)
- [x] `documentSymbol` called per file → symbols written to `symbols` table with fqn, kind, range, parent_fqn
- [x] `hover` called per symbol → `type_signature` populated on `symbols` rows
- [x] `definition`, `references`, `implementation`, `incomingCalls`, `outgoingCalls` → edges written to `symbol_edges` with correct edge_kind
- [x] `workspaceSymbol("")` called during full reindex for cross-file completeness
- [x] Incremental: file change via file watcher → deletes that file's symbols + edges → repopulates
- [x] Multi-language: symbols and edges populated for Python + at least 2 other languages in a test project
- [x] Confidence field reflects LSP result quality (compiler_grade, partial, unavailable)
- [x] Batch inserts for symbols and edges (no single-row loops)
- [x] All existing tests still pass (zero regression)
- [x] `uv run pytest tests/test_lsp_population.py -v` → all pass (52 passed)
- [x] `uv run pytest tests/test_smoke.py -v -n auto` → all pass (17 passed)

## Anti-Patterns
Inherited from parent epic, plus:
- NO LSP calls in the tree-sitter parsing pipeline — background service only
- NO single-row inserts in loops — batch all symbols and edges per file
- NO blocking the file watcher event loop — LSP population is async
- NO silently dropping failed LSP calls — log with structured reason, set confidence to unavailable

## Key Considerations
- LSP servers may not be ready when the first file finishes tree-sitter parsing. Population service must wait for server readiness per language.
- Some files will have no LSP server (unsupported language). Skip gracefully, don't error.
- `incomingCalls`/`outgoingCalls` require a `prepareCallHierarchy` step first — the population service must handle this two-step protocol.
- Edge deduplication: the same edge may be discoverable from both ends (A calls B found via outgoingCalls on A, also via incomingCalls on B). Deduplicate on (from_fqn, to_fqn, edge_kind).
- For incremental updates, deleting symbols cascades to edges — delete edges WHERE from_symbol_id or to_symbol_id references a deleted symbol.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: population service module, how to trigger manual repopulation
- [ ] AGENTS.md updated: new service, interaction with file watcher

**User Walkthrough Must Cover:**
- Index a Python project → verify symbols table has entries
- Modify a file → verify that file's symbols are refreshed (not full reindex)
- Index a multi-language project → verify symbols from multiple languages present
- Check symbol_edges for calls, references, implements edge kinds
