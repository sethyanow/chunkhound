---
id: ch-9xh
title: 'Phase 5 Acceptance: Search Pipeline + code_research'
status: open
type: task
priority: 1
parent: ch-z2o
---

## Agent Documentation
- [ ] Audit CLAUDE.md for staleness re: search pipeline / code_research — most likely nothing to change; confirm and note.

## What This Phase Built
GraphWalkExpander wired into UnifiedSearch alongside the existing semantic MultiHopStrategy, plus five prompt templates in `chunkhound/services/prompts/graph_patterns/` (type_chain_tracing, call_graph_scope, test_coverage_map, import_dependency, interface_implementors) that code_research's BFS selects from when structural sub-questions emerge.

## Environment Setup
Fresh ChunkHound reindex already complete.

## Demo

**Demo 1 — code_research on a structural question (value + provenance)**
Pick a structural question (e.g., "trace how a file change propagates from the watcher into the chunks table" or "what calls DuckDBProvider.search_semantic").
- Run it once with the expander live; look for graph-sourced chunks in the output and eyeball whether they're relevant (not just present).
- Run the same question with the expander disabled; diff the chunk set.
- Shakedown questions: is chunk provenance visible? Is there a disable toggle? Are the extra chunks useful or noise?

**Demo 2 — Template selection across categories**
Run 2-3 questions designed to match different templates:
- Type chain: "trace the return type of UnifiedSearch.search through its callers"
- Call scope: "what functions call GraphWalkExpander.expand"
- Implementors: "what implements the EmbeddingProvider protocol"

Observe which template fires for each. Shakedown: is template selection logged/observable anywhere? Does the matching heuristic actually pick different templates per question shape, or does it collapse to one default?

**Demo 3 — Direct MCP `search(type: structural)` path**
Run one structural question directly via the MCP tool (not through code_research). Compare the chunk set to Demo 1's code_research result. Shakedown: same expander, same edges, same kind of answer? Any wiring differences?

## Sign-Off
- [ ] Expander contributes chunks code_research wouldn't surface otherwise, and those chunks look relevant
- [ ] Template selection varies across question shapes (at least 2 of 5 templates seen in practice)
- [ ] Direct MCP `search(type: structural)` returns consistent results
- [ ] CLAUDE.md staleness audited
- [ ] Phase 5 complete — ready for Phase 6
