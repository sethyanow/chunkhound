---
id: ch-7j0
title: 'Phase 1: Foundation — LSP Client + Schema + Fixes'
status: open
type: epic
priority: 1
depends_on: [ch-jsj, ch-u1s]
parent: ch-8e7
---








## Context
Parent epic ch-8e7, Phase 1. No prior phases — this is the foundation everything else builds on.
Delivers the LSP client manager, DuckDB schema additions, and infrastructure fixes that all subsequent phases depend on.

## Requirements
Scoped to parent epic R1, R2, R10:
- R1: LSP client manager module
- R2: DuckDB symbols + symbol_edges tables
- R10: Path scoping fix + git-aware indexing

## Success Criteria
- [ ] LSP client manager spawns and initializes pyright successfully
- [ ] LSP client manager has configs for all languages with tree-sitter grammars in pyproject.toml
- [ ] documentSymbol, definition, references, implementation, incomingCalls, outgoingCalls, hover, diagnostics all return valid results via pyright
- [ ] Server state tracking works (not_started → initializing → ready, and degraded with structured reason)
- [ ] Capability gating: calls against unadvertised capabilities return graceful error, not crash
- [ ] `symbols` table created with correct schema and indexes
- [ ] `symbol_edges` table created with correct schema and indexes
- [ ] Schema migration integrates with existing `_executor_migrate_schema` pattern
- [x] Semantic search with path filter returns ONLY results within that path prefix (no leakage)
- [x] RealtimeIndexingService reflects committed + staged state
- [x] All existing tests still pass (zero regression)
- [ ] `uv run pytest tests/test_lsp_client.py -v` → all pass
- [x] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
Inherited from parent epic, plus:
- NO blocking I/O in the LSP client — all operations async
- NO language-specific logic in the client manager — config-driven only
- NO schema changes to existing tables (files, chunks, embeddings)

## Key Considerations
- LSP servers need time to initialize after spawn. Client manager must handle initialization handshake asynchronously and report readiness.
- Pyright requires node.js. Other servers have their own runtime deps. Config should specify the binary command, not assume any particular runtime.
- Path scoping fix must be tested against the existing test suite to verify no regression in semantic search behavior.
- Git-aware indexing requires interfacing with pygit2 (already a dependency) or shelling to git.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: LSP client module location, config format, new DuckDB tables documented
- [ ] AGENTS.md updated: new tables, new module, key commands for LSP

**User Walkthrough Must Cover:**
- LSP client connects to pyright and returns documentSymbol for a Python file
- LSP client connects to at least one non-Python language server
- DuckDB tables exist after fresh index
- Semantic search path scoping returns no cross-directory leakage
