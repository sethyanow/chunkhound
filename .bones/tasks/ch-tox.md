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

**Known issues from shakedown (2026-04-01):**
- **BUG (diagnosed):** `daemon/server.py:_handle_tools_call` doesn't pass `lsp_client_pool=self._lsp_pool`
  to `handle_tool_call()`. The stdio path does (stdio.py:172). Pool constructs fine via inherited
  `_deferred_connect_and_start`, but the daemon tool dispatch never wires it through. One-line fix.
- `graph(walk)` returns 0 edges for meaningful symbols — edge data is mostly self-referential `references`
- `type_filter="Result"` returns empty on Python codebase — use `type_filter="str"` instead
- `search(type: symbols)` and `search(type: structural)` work well from DuckDB

## Requirements

**Demo Script Phase 3 Sections** (extend `scripts/demo_lsp.py`):
Each section imports and calls the MCP tool implementation directly (same pattern as
Phase 1-2 with LSPClient/DuckDB). Script vs MCP — surface what's wired, what's broken.

**Bug fix:** `daemon/server.py:339` — add `lsp_client_pool=self._lsp_pool` to `handle_tool_call()`.
Stdio path has it (stdio.py:172), daemon path doesn't. Phase 3 updated stdio but missed daemon.

**Demo Scenarios (script sections 11+):**
- `lsp(definition)` on a known symbol → correct file + range
- `lsp(references)` on a central function → call sites
- `graph(walk)` from a function → callers/callees with edge kinds
- `graph(walk)` with `edge_kind` filter → filtered edges
- `graph(boundary)` on a module scope → cross-scope dependencies
- `search(type: symbols, query="parse")` → matching symbols
- `search(type: structural, query="error handling", type_filter="str")` → type-filtered results
- `symbol_context` on a function → compound profile

**Agent Documentation** (deferred until demo passes — docs describe what works, not what's planned):
- AGENTS.md: routing hints for Phase 3 tools (brief — not parameter docs)
- CLAUDE.md: verify current (may need nothing)

## Implementation

### Step 1: Fix daemon LSP pool wiring
- File: `chunkhound/daemon/server.py:339`
- Add `lsp_client_pool=self._lsp_pool` to `handle_tool_call()` in `_handle_tools_call`
- Root cause: stdio path passes pool (stdio.py:172), daemon path doesn't
- Regression test: verify daemon tool dispatch includes lsp_client_pool parameter

### Step 2: Add Phase 3 demo sections to `scripts/demo_lsp.py`
- File: `scripts/demo_lsp.py` (extend, sections 11+)
- File: `tests/test_demo_lsp_script.py` (extend with unit tests for new helpers)
- Each section imports MCP tool internals and calls them directly
- Pattern: set up required context (DB conn, LSP pool if available), call tool impl, display results
- PASS/FAIL logic catches wiring gaps — this is a shakedown, not a polish

### Step 3: Run demo for product owner
- `uv run scripts/demo_lsp.py` — all phases, PASS/FAIL summary
- Present results and findings — what works, what's broken, what's a data quality issue vs tool bug
- Product owner decides what needs fixing vs what's acceptable for Phase 3 closure

### Step 4: Agent documentation (after demo passes)
- AGENTS.md: brief routing hints for Phase 3 tools (not parameter docs — those belong in tool descriptions)
- CLAUDE.md: verify current, update only if needed

## Success Criteria
- [ ] Daemon LSP pool wiring fixed (`daemon/server.py` passes `lsp_client_pool` to `handle_tool_call`)
- [ ] Regression test covers daemon tool dispatch includes lsp_client_pool
- [ ] Phase 3 demo sections added to `scripts/demo_lsp.py` (sections 11+)
- [ ] Unit tests added to `tests/test_demo_lsp_script.py` for Phase 3 helpers
- [ ] `uv run scripts/demo_lsp.py` runs all phases with PASS/FAIL summary
- [ ] Demo presented to product owner with honest findings
- [ ] AGENTS.md has brief routing hints for Phase 3 tools
- [ ] CLAUDE.md verified current

## Anti-Patterns
- NO bypassing MCP tool layer — demo sections call tool implementations, not raw DuckDB/LSP
- NO hiding broken things — if a tool returns garbage, the demo shows it honestly
- NO polishing before shakedown — find the wiring gaps first, pretty it up never
- NO parameter docs in AGENTS.md — routing hints only, tool docs go in tool descriptions
- NO declaring CLAUDE.md "already fine" without reading it
- NO rushing to check boxes — product owner decides when Phase 3 is done
