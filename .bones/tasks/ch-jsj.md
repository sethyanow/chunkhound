---
id: ch-jsj
title: Fix semantic search path scoping + git-aware indexing
status: open
type: task
priority: 0
parent: ch-7j0
---

## Context
First task in Phase 1. Fixes broken behavior before building new features. Parent epic R10.

Two infrastructure issues identified from empirical usage:
1. `search_semantic` with `path` filter leaks results across directories — strict prefix matching needed
2. `RealtimeIndexingService` indexes the working tree blind to git state — should reflect committed + staged, not just filesystem

## Requirements
- Semantic search path filter returns ONLY results within the specified directory prefix (no leakage)
- Git-aware indexing: `RealtimeIndexingService` checks git state so index reflects committed + staged files, not just filesystem presence

## Implementation

### Path Scoping Fix
1. Write test: semantic search with `path="src/auth"` must return zero results from `src/payments/`
2. Locate the path filtering logic in `DuckDBProvider.search_semantic()` (duckdb_provider.py:1965)
3. Tighten the WHERE clause to strict prefix match on `files.path`
4. Verify regex search path filtering is already strict (it likely is) — if so, align semantic to match

### Git-Aware Indexing
1. Write test: file that is `git rm`'d but still on filesystem should not be indexed
2. Write test: untracked file should not be indexed (unless explicitly configured)
3. Locate file discovery in `RealtimeIndexingService` — likely `_should_index()` (realtime_indexing_service.py:101)
4. Add git status check using pygit2 (already a dependency) or git CLI
5. Wire into the existing `_should_index` filter

## Success Criteria
- [ ] Test proves path scoping no longer leaks across directories
- [ ] Test proves git-rm'd files are excluded from indexing
- [ ] All existing tests still pass (`uv run pytest tests/test_smoke.py -v -n auto`)

## Anti-Patterns
- NO changes to existing DuckDB schema
- NO changes to search result format — only which results are returned
- NO shelling to git if pygit2 can do it (pygit2 is already a dependency)

## Blocked by
None — first task.

## Unlocks
Clean foundation for all subsequent Phase 1 work (LSP client, schema additions).
