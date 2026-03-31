---
id: ch-yda
title: 'Phase 2 Acceptance: Index-Time Population'
status: active
type: task
priority: 1
owner: Seth
depends_on: [ch-91j, ch-ko4, ch-uny]
parent: ch-0um
---










## Context
Phase 2 acceptance task. Blocked by ch-91j (wiring LSPPopulationService into production).
Demo script is written and unit-tested (10 tests pass). Demo fails against live DB because
population service was never wired into production indexing. After ch-91j completes and
a re-index runs, re-run `uv run scripts/demo_lsp.py` — Phase 2 sections must all PASS.

## Requirements
From ch-0um Acceptance Requirements:

### User Walkthrough (Demo Script)
Extend `scripts/demo_lsp.py` with Phase 2 population scenarios covering:
1. Index a Python project → verify symbols table has entries
2. Modify a file → verify that file's symbols are refreshed (not full reindex)
3. Index a multi-language project → verify symbols from multiple languages present
4. Check symbol_edges for calls, references, implements edge kinds

## Success Criteria
- [ ] Scenario 1 PASS: symbols table has >0 rows after indexing, at least one known Python symbol verified by name
- [ ] Scenario 2 PASS: file modification triggers symbol refresh — symbol count for that file changes appropriately
- [ ] Scenario 3 PASS: symbols table contains entries with at least 2 distinct language values
- [ ] Scenario 4 PASS: symbol_edges contains at least calls and references edge kinds
- [ ] `uv run scripts/demo_lsp.py` runs Phase 2 sections and exits 0 on all-pass, 1 on any failure

## Key Considerations
- **Scenario 2 sync gap:** Population is async/background. After file modification, script must poll or await population completion before asserting — immediate query returns stale data. Need a synchronization strategy (poll symbol count with timeout, or call population service directly).
- **Multi-language portability:** Scenario 3 depends on multiple LSP servers being installed. Script should SKIP (not FAIL) if <2 languages available, with explanation. Query distinct languages first.
- **Pre-flight DB check:** All scenarios assume an indexed DB exists. Script must verify DB + files table non-empty before running, with clear early-exit message.
- **Edge population latency:** Scenario 4 may see empty edges if LSP edge operations timed out. Check confidence distribution — all-unavailable is an environment issue, not a code bug.

## Anti-Patterns
- NO vacuous passes — every PASS must assert row counts > 0 or specific values, not just "query ran"
- NO creating a separate demo script — extend the existing `scripts/demo_lsp.py`
- NO skipping scenario 2 (incremental refresh)

## Log

- [2026-03-31T14:42:16Z] [Seth] BLOCKED: Demo reveals LSPPopulationService is dead code in production. Created ch-91j (wiring task). Acceptance cannot pass until ch-91j is done and demo re-run.
- [2026-03-31T17:44:47Z] [Seth] ACCEPTANCE DEMO RESULT: 6/8 scenarios PASS, 2 new scenarios FAIL. live-vs-populated shows all live symbols matched in DB (51/51). Cross-file edge health FAILS — 1319/1323 edges self-referential, workspaceSymbol pass crashed. Two blocking bugs created: ch-ko4 (P0, loop crash on single failure) and ch-uny (P1, client degradation root cause). Demo script hardened with 6 new scenarios + 29 unit tests.
