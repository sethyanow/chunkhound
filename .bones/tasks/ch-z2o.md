---
id: ch-z2o
title: 'Phase 5: Search Pipeline + code_research'
status: open
type: epic
priority: 1
depends_on: [ch-dar, ch-ron, ch-ic6]
parent: ch-8e7
---









## Context
Parent epic ch-8e7, Phase 5. Depends on Phase 4 (ch-dar).
Phase 4 delivers the full MCP tool surface. This phase wires the graph into the search pipeline (graph walk expander alongside MultiHopStrategy) and enhances code_research with graph-aware prompt templates so its BFS pulls structural data for appropriate sub-questions.

## Requirements
Scoped to parent epic R7, R8:
- R7: Graph walk expander in search pipeline
- R8: code_research graph-aware prompt templates

## Success Criteria
- [ ] GraphWalkExpander integrated into UnifiedSearch alongside existing semantic MultiHopStrategy
- [x] Expander takes seed chunks from initial semantic search → resolves to symbols via range overlap → walks symbol_edges 1-2 hops → returns additional chunks
- [x] Semantic expander and graph expander are independent — either can return empty without breaking the other
- [x] Both expanders' results merged and deduped on chunk_id before feeding into existing reranker
- [x] Depth configurable (default 2 hops)
- [x] Edge kind filtering available (e.g., expand only via `calls` edges, not `references`)
- [ ] Prompt templates exist in `chunkhound/services/research/prompts/graph_patterns/`
- [ ] code_research BFS uses templates when encountering structural sub-questions (type chain tracing, call graph scope, test coverage mapping, etc.)
- [ ] Measurable: code_research on a structural topic returns chunks that semantic-only search misses
- [x] `uv run pytest tests/test_graph_expander.py -v` → all pass (15 tests)
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
Inherited from parent epic, plus:
- NO replacing the semantic expander — graph walk is additive alongside it
- NO changing MultiHopStrategy behavior — the graph expander is a parallel path
- NO hard dependency on graph data being populated — if symbols/edges tables are empty, expander returns empty set gracefully
- NO prompt templates that duplicate semantic search — templates are for questions where graph data is faster/more accurate

## Key Considerations
- The graph walk expander should be fast — it queries DuckDB locally, no LSP round-trips. If it's slower than semantic expansion, something is wrong.
- Prompt templates should be recognizable by code_research's QuestionGenerator — it needs to match sub-question patterns to templates. Pattern matching on question text, not hardcoded routing.
- Template list for initial delivery: type_chain_tracing, call_graph_scope, test_coverage_map, import_dependency, interface_implementors. More can be added later.
- The UnifiedSearch integration point is between step 2 (semantic search) and step 5 (symbol regex search) — adding a step 3.5 for structural graph expansion.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: graph expander configuration, prompt template directory
- [ ] AGENTS.md updated: how graph expansion affects search results

**User Walkthrough Must Cover:**
- Run code_research on a structural question → verify graph-expanded chunks appear in results
- Compare search results with and without graph expansion → demonstrate additional relevant chunks found
- Verify prompt template is selected for a type-chain question (visible in research logs)
