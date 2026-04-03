---
id: ch-7ib
title: 'Phase 4 Acceptance: Fusion MCP Tools'
status: open
type: task
priority: 1
parent: ch-dar
---



## Context
Phase 4 (ch-dar) implementation is complete. All 4 fusion tools pass tests.
This acceptance task has two deliverables: agent documentation updates and a user demo.

**The user closes this task.** The demo is the vehicle; the user decides when it's done.

## Requirements
From ch-dar Acceptance Requirements:
- CLAUDE.md updated: fusion tool descriptions, example usage
- AGENTS.md updated: tool descriptions for LLM routing
- User walkthrough covering all 4 fusion tools against the live codebase

## Deliverable 1: Agent Documentation

Update AGENTS.md with fusion tool descriptions for LLM routing. Keep it concise — routing hints only, not full docs (per feedback memory: AGENTS.md is project instructions only).

Update CLAUDE.md if any new commands, gotchas, or workflow changes are needed.

## Deliverable 2: User Demo

Extend `scripts/demo_lsp.py` with Phase 4 sections demonstrating all 4 fusion tools against ChunkHound's own codebase. Each demo section:

1. **test_targeting** — Change a function (e.g., `_graph_walk`) → show which tests are affected
2. **impact_cascade** — Pick a widely-used symbol (e.g., `execute_query`) → show multi-level caller tree
3. **cross_language_check** — Set up a test fixture with deliberate mismatch → show detection
4. **semantic_diff** — Create a branch with known changes (or use HEAD~1..HEAD) → show classification

Demo must:
- Use ChunkHound's own MCP tools (dogfooding)
- Print PASS/FAIL per section
- All prior phase sections must still PASS (regression gate)
- Demo scenario functions need unit tests (per feedback: acceptance demos are code under TDD)

## Success Criteria
- [ ] AGENTS.md updated with fusion tool routing hints
- [ ] CLAUDE.md updated if needed (new commands/gotchas)
- [ ] demo_lsp.py Phase 4 sections all PASS
- [ ] All prior demo_lsp.py sections still PASS
- [ ] User has seen the demo and is satisfied

## Anti-Patterns
- NO generating summaries or tutorials — update stale docs only
- NO creating new markdown files for documentation
- NO skipping the demo — it's the user's acceptance gate
