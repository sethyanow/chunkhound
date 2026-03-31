---
id: ch-7j0
title: 'Phase 1: Foundation — LSP Client + Schema + Fixes'
status: open
type: epic
priority: 1
depends_on: [ch-jsj, ch-u1s, ch-a58, ch-1j2]
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
- [x] LSP client manager spawns and initializes pyright successfully
- [x] LSP client manager has configs for all languages with tree-sitter grammars in pyproject.toml
- [x] documentSymbol, definition, references, implementation, incomingCalls, outgoingCalls, hover, diagnostics all return valid results via pyright
- [x] Server state tracking works (not_started → initializing → ready, and degraded with structured reason)
- [x] Capability gating: calls against unadvertised capabilities return graceful error, not crash
- [x] `symbols` table created with correct schema and indexes
- [x] `symbol_edges` table created with correct schema and indexes
- [x] Schema migration integrates with existing `_executor_migrate_schema` pattern
- [x] Semantic search with path filter returns ONLY results within that path prefix (no leakage)
- [x] RealtimeIndexingService reflects committed + staged state
- [x] All existing tests still pass (zero regression)
- [x] `uv run pytest tests/test_lsp_client.py -v` → all pass
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
- [x] CLAUDE.md updated: references AGENTS.md; no new content needed (derivable from code/bones)
- [x] AGENTS.md updated: fixed stale pre-push test command to include all 4 tiers

**User Walkthrough Must Cover:**
- LSP client connects to pyright and returns documentSymbol for a Python file
- LSP client connects to at least one non-Python language server
- DuckDB tables exist after fresh index
- Semantic search path scoping returns no cross-directory leakage
