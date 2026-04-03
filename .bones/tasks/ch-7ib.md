---
id: ch-7ib
title: 'Phase 4 Acceptance: Fusion MCP Tools'
status: open
type: task
priority: 1
parent: ch-dar
---

## Context
Phase 4 implementation complete — all 4 fusion tools pass 120 tests.
This is the product demo. Agent walks through what was built, user evaluates live.

## Preconditions
- ChunkHound MCP server running against an indexed codebase
- AGENTS.md updated with fusion tool routing hints (do before session)

## Demo Outline
Derived from ch-dar success criteria and acceptance requirements:

1. **test_targeting** — pick changed files/symbols, show which tests are affected
2. **impact_cascade** — pick a widely-used symbol, show the caller tree with type signatures
3. **cross_language_check** — compare two scopes, show mismatch detection
4. **semantic_diff** — use a real diff, show change classification (signature vs body-only)

Agent runs each tool live, user evaluates output against expectations. Issues triaged together.

## Success Criteria
- [ ] User has seen each fusion tool on real data and is satisfied
- [ ] Issues found during demo triaged (fix now / track / accept)
