---
id: ch-tox
title: 'Phase 3 Acceptance: Primitive MCP Tools'
status: active
type: task
priority: 1
owner: Seth
parent: ch-zyz
---




## Context
Phase 3 (ch-zyz) implementation complete — all 16 success criteria checked.
This acceptance task covers: demo script Phase 3 sections + user walkthrough.

**Pattern:** Phase 1-2 demos drive through internals (import LSPClient, query DuckDB directly)
to catch wiring issues. Phase 3 does the same — import and call the MCP tool implementations
directly to shakedown what's wired up vs what's broken. This is an internal product demo
for the product owner, not a polished client presentation.

**Shakedown results (2026-04-02):**

BUG FIXED:
- Daemon LSP pool wiring — `server.py:339` was missing `lsp_client_pool=self._lsp_pool`.
  Fixed, regression test added, verified working via MCP.

DEMO RUN FINDINGS (`uv run scripts/demo_lsp.py`):
- Phase 1-2: all PASS (except incremental_refresh — DB locked by MCP server, expected)
- search_symbols: PASS — 10 results for "parse"
- graph_boundary: PASS — 20 cross-scope edges found
- graph_walk: FAIL — LSPClient has 0 outbound edges. Needs investigation — not triaged.
- lsp_definition: FAIL — demo targeted line 50 char 10 (empty position). Demo bug, not tool bug.
- lsp_references: FAIL — same empty position issue.
- symbol_context: FAIL — crashed `'NoneType' object has no attribute 'provider'` because
  demo passed `services=None`. Tool needs real services from `create_services()`.

DEMO SCRIPT DESIGN FLAW:
- Current Phase 3 sections (search_symbols, graph_walk, graph_boundary) duplicate SQL from
  tools.py and run against raw DuckDB. This bypasses the MCP tool internals — defeats the
  purpose. All Phase 3 demos must go through `execute_tool()` with real `create_services()`
  to catch wiring issues in the actual service stack.

## Requirements

**Demo Script Phase 3 Sections** (extend `scripts/demo_lsp.py`):
Each section imports and calls the MCP tool implementation directly (same pattern as
Phase 1-2 with LSPClient/DuckDB). Script vs MCP — surface what's wired, what's broken.

**Bug fix (DONE):** `daemon/server.py:339` — added `lsp_client_pool=self._lsp_pool`. Committed, pushed, verified.

**Demo Scenarios (script sections 11+):**
- `lsp(definition)` on a known symbol → correct file + range
- `lsp(references)` on a central function → call sites
- `graph(walk)` from a function → callers/callees with edge kinds
- `graph(walk)` with `edge_kind` filter → filtered edges
- `graph(boundary)` on a module scope → cross-scope dependencies
- `search(type: symbols, query="parse")` → matching symbols
- `search(type: structural, query="error handling", type_filter="str")` → type-filtered results
- `symbol_context` on a function → compound profile

## Implementation

### Step 1: Fix daemon LSP pool wiring — DONE
- Committed 99b5b014. Regression test in `tests/unit/test_daemon_lsp_pool_wiring.py`.

### Step 2: Redo Phase 3 demo sections to use real internals
- Current demo sections bypass MCP tool layer — use raw SQL instead of `execute_tool()`
- Rewrite to: `create_services()` → real services → `execute_tool()` for every demo
- Fix demo positions: target known symbol (LSPClient at line 31), not empty lines
- If `create_services()` conflicts with MCP server's DB lock, that's a finding to surface

### Step 3: Run demo for product owner
- `uv run scripts/demo_lsp.py` — all phases, PASS/FAIL summary
- Product owner decides what needs fixing vs acceptable for Phase 3 closure

## Success Criteria
- [x] Daemon LSP pool wiring fixed (`daemon/server.py` passes `lsp_client_pool` to `handle_tool_call`)
- [x] Regression test covers daemon tool dispatch includes lsp_client_pool
- [ ] Phase 3 demo sections use real internals (`create_services()` → `execute_tool()`)
- [ ] Unit tests added to `tests/test_demo_lsp_script.py` for Phase 3 helpers
- [ ] `uv run scripts/demo_lsp.py` runs all phases with PASS/FAIL summary
- [ ] Demo presented to product owner with honest findings

## Anti-Patterns
- NO bypassing MCP tool layer — demo sections call tool implementations, not raw DuckDB/LSP
- NO hiding broken things — if a tool returns garbage, the demo shows it honestly
- NO polishing before shakedown — find the wiring gaps first, pretty it up never
- NO parameter docs in AGENTS.md — routing hints only, tool docs go in tool descriptions
- NO declaring CLAUDE.md "already fine" without reading it
- NO rushing to check boxes — product owner decides when Phase 3 is done
