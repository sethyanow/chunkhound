---
id: ch-ic6
title: Wire GraphWalkExpander into _search_structural
status: closed
type: task
priority: 1
owner: Seth
parent: ch-z2o
---




**Blocked by:** ch-ron (GraphWalkExpander must exist)
**Unlocks:** code_research prompt template tasks; Phase 5 acceptance task once all R7 criteria met

## Context

Phase 5 (ch-z2o) R7 requires graph walk expansion in the search pipeline. ch-ron delivered `GraphWalkExpander` as a standalone service. This task wires it into `_search_structural` in `chunkhound/mcp_server/tools/search.py:258`, replacing the inline SQL stages (2-5) with a single `expander.expand()` call.

The existing `_search_structural` already implements the same pipeline inline using `build_symbol_overlap_query`, `build_structural_walk_query`, `build_chunk_resolution_query` from `queries/search.py`. This task replaces that inline code with the extracted service component.

Also covers a testing gap from ch-ron: `build_structural_walk_query` edge_kind extension has no unit test in `test_queries_search.py`.

Key existing infrastructure:
- `_search_structural` at `chunkhound/mcp_server/tools/search.py:258` — 8-stage pipeline, stages 2-5 are graph expansion
- `GraphWalkExpander` at `chunkhound/services/search/graph_walk_expander.py` — takes `DatabaseProvider`, has `expand(seed_chunks, depth, edge_kind)`
- Import direction: MCP → services is correct (no circular import)
- `services.provider` in tool functions is a `DatabaseProvider` instance
- `call_search_tool` test helper at `tests/lsp/mcp_tool_helpers.py:89` — for integration tests

## Design

Replace inline stages 2-5 with:
```python
from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

expander = GraphWalkExpander(services.provider)
graph_chunks = await expander.expand(results, depth=2)
combined = results + graph_chunks
```

The expander handles: overlap → walk → resolution → dedup internally. Existing stages 1 (semantic search), 6 (combine), 7 (type_filter), 8 (paginate) remain unchanged. Early returns for empty overlap/walk become unnecessary — expander handles these gracefully.

## Requirements

From Phase 5 sub-epic ch-z2o:
- GraphWalkExpander integrated into search pipeline alongside existing semantic MultiHopStrategy
- Semantic expander and graph expander independent — either can return empty without breaking the other
- Both results merged and deduped before feeding into existing pipeline

## Implementation

### Step 1: Write failing test — edge_kind in build_structural_walk_query
File: `tests/mcp_server/test_queries_search.py`, add to `TestBuildStructuralWalkQuery`.
Test: `test_edge_kind_adds_filter_clause` — call with `edge_kind="calls"`, assert `"edge_kind"` in SQL, `"calls"` in params, placeholder count increases by 1 vs no edge_kind.

### Step 2: Write failing test — structural search uses expander
File: `tests/mcp_server/test_structural_search.py` (new).
Test: `test_structural_search_returns_graph_expanded_chunks` — mock `services.search_service.search_semantic` to return seed results; mock `services.provider.execute_query` with sequential overlap → walk → resolution results. Call `search_impl(type="structural")`. Assert output includes both semantic and graph-expanded chunks.

### Step 3: Write failing test — graph expansion empty, semantic results pass through
Same file. Test: `test_graph_empty_returns_semantic_only` — mock overlap to return `[]`. Assert result equals semantic results only. No error.

### Step 4: Write failing test — semantic search empty, result is empty
Same file. Test: `test_semantic_empty_returns_empty` — mock `search_semantic` to return `([], pagination)`. Assert empty result. No error, no graph queries executed.

### Step 5: Implement integration
File: `chunkhound/mcp_server/tools/search.py`
- Add import: `from chunkhound.services.search.graph_walk_expander import GraphWalkExpander`
- Replace stages 2-5 (lines ~310-374) with expander call
- Remove early returns for empty overlap/walk — expander returns `[]` gracefully
- Remove unused query builder imports (`build_symbol_overlap_query`, `build_structural_walk_query`, `build_chunk_resolution_query`) from the import block
- Keep stages 6-8 unchanged

### Step 6: Run targeted tests
`uv run pytest tests/test_graph_expander.py tests/mcp_server/test_queries_search.py tests/mcp_server/test_structural_search.py -v`

### Step 7: Run full test suite
`uv run pytest -m "unit or integration or e2e" tests/ -v > /tmp/test_suite_ch_ic6.out 2>&1`

## Success Criteria
- [x] `_search_structural` uses `GraphWalkExpander` instead of inline SQL stages
- [x] `build_structural_walk_query` edge_kind extension has unit test in `test_queries_search.py`
- [x] Graph expansion empty → semantic results pass through unchanged
- [x] Semantic search empty → result is empty, no error
- [x] `search(type="structural")` returns graph-expanded chunks alongside semantic
- [x] Full test suite passes (2892 passed, 0 failed)

## Anti-Patterns
- NO removing query builders from `queries/search.py` — keep them and their tests as documented SQL patterns
- NO modifying GraphWalkExpander — it was delivered in ch-ron and is tested
- NO adding reranking to the combined results — that's out of scope for this task
- NO breaking the existing `search(type="regex")` or `search(type="semantic")` paths

## Log

- [2026-04-05T02:58:06Z] [Seth] Debrief: clean wiring refactoring. Replaced ~60 lines of inline SQL with 3-line GraphWalkExpander call. All 11 existing structural search tests passed without modification — mock path identical. Added 1 edge_kind coverage test + 3 adversarial tests (dense pagination, unicode dedup, offset boundary). 2892/2892 full suite. Reflections: skeleton Steps 2-4 were redundant (tests/lsp/test_tool_search_structural.py already covered). UnifiedSearch integration (criterion 1) needs separate task — scoped as ch-8mu.
