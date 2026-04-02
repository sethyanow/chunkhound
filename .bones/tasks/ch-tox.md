---
id: ch-tox
title: 'Phase 3 Acceptance: Primitive MCP Tools'
status: open
type: task
priority: 1
parent: ch-zyz
---



## Context
Phase 3 (ch-zyz) implementation complete — all 16 success criteria checked.
This acceptance task covers: agent documentation updates + user walkthrough.

## Requirements
From ch-zyz Acceptance Requirements:

**Agent Documentation:**
- CLAUDE.md updated: new MCP tools documented with parameter descriptions
- AGENTS.md updated: tool descriptions for LLM routing

**User Walkthrough Must Cover:**
- Call `lsp` with definition operation on a known symbol → returns correct file + range
- Call `graph(walk)` from a function → returns callers/callees with edge kinds
- Call `search(type: symbols, query="parse")` → returns matching symbols
- Call `search(type: structural, query="error handling", type_filter="Result")` → returns type-filtered results
- Call `symbol_context` on a function → returns compound profile in one call

## Implementation

### Step 1: Update AGENTS.md with Phase 3 tool documentation
- File: `AGENTS.md`
- Add documentation for all Phase 3 MCP tools: lsp, graph, symbol_context, lsp_status
- Add documentation for search extensions: type: symbols, type: structural, type_filter
- Include parameter descriptions and example usage for LLM routing

### Step 2: Update CLAUDE.md if needed
- File: `CLAUDE.md`
- Verify KEY_COMMANDS section is current
- Add any new tool-related commands or notes

### Step 3: User walkthrough demo
- Run each of the 5 walkthrough scenarios against the live indexed codebase
- Present results to the user showing each tool working end-to-end
- The demo is for the user — show the product, not run verification commands

## Success Criteria
- [ ] AGENTS.md documents all Phase 3 MCP tools with parameter descriptions
- [ ] CLAUDE.md is up to date with any new Phase 3 information
- [ ] `lsp(definition)` on a known symbol returns correct file + range
- [ ] `graph(walk)` returns callers/callees with edge kinds
- [ ] `search(type: symbols, query="parse")` returns matching symbols
- [ ] `search(type: structural, query="error handling", type_filter="Result")` returns type-filtered results
- [ ] `symbol_context` returns compound profile in one call
- [ ] Demo presented to user (not just verification commands)

## Anti-Patterns
- NO new implementation code — this is documentation + demo only
- NO summary reports — present the actual tool output to the user
