---
id: ch-yda
title: 'Phase 2 Acceptance: Index-Time Population'
status: active
type: task
priority: 1
owner: Seth
parent: ch-0um
---




## Context
Phase 2 acceptance task. All 4 implementation tasks (ch-5b3, ch-nvc, ch-zlg, ch-5a3) are closed.
All sub-epic success criteria are checked. This task delivers a user demo.

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
