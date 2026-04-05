---
id: ch-8mu
title: Wire GraphWalkExpander into UnifiedSearch
status: open
type: task
priority: 1
parent: ch-z2o
---

**Blocked by:** ch-ic6 (GraphWalkExpander wired into MCP search tool)
**Unlocks:** Prompt template tasks (graph data available in code_research pipeline); sub-epic criterion 1; parent epic criterion "Graph walk expander produces chunks in code_research that semantic search alone misses"

## Context

Phase 5 (ch-z2o) R7 requires graph walk expansion in the search pipeline. ch-ic6 wired `GraphWalkExpander` into the MCP `search(type="structural")` tool. This task wires it into `UnifiedSearch.unified_search()` in `chunkhound/services/research/shared/unified_search.py`, the code_research pipeline — satisfying sub-epic criterion 1.

`UnifiedSearch.unified_search()` has a 7-step pipeline:
1. Multi-hop semantic search → `semantic_results`
2-5. Symbol extraction → regex search → `regex_results`
6. Unify (dedup by `chunk_id` via `get_chunk_id()`)
7. Unified rerank

The graph expansion goes between step 1 and steps 2-5 as step 1.5: semantic results → `GraphWalkExpander.expand()` → merge graph-expanded chunks into semantic pool before symbol extraction and regex search.

Key infrastructure:
- `UnifiedSearch` at `chunkhound/services/research/shared/unified_search.py:35`
- `self._db_services.provider` is a `DatabaseProvider` — same type `GraphWalkExpander.__init__` needs
- `get_chunk_id()` at `chunkhound/core/utils/chunk_utils.py:8` — returns `chunk.get("chunk_id") or chunk.get("id")`
- Step 6 dedup: `chunk_id = get_chunk_id(chunk); if chunk_id: unified_map[chunk_id] = chunk`

**Critical gap:** Graph chunks from `GraphWalkExpander.expand()` have `file_path`, `content`, `start_line`, `end_line` but NO `chunk_id`. The resolution query (`_build_resolution_query` in `graph_walk_expander.py:83`) JOINs on `chunks c` but doesn't SELECT `c.id`. Without `chunk_id`, graph chunks would be silently dropped by step 6's dedup. Fix: add `c.id AS chunk_id` to the resolution query's SELECT.

## Design

Two changes:

1. **Add `chunk_id` to expander's resolution query** — `_build_resolution_query` adds `c.id AS chunk_id` to SELECT. This makes graph chunks compatible with `get_chunk_id()` dedup. Safe for the MCP path (extra key ignored).

2. **Add step 1.5 in `unified_search()`** — after semantic search, before symbol extraction:
```python
from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

# Step 1.5: Graph walk expansion
expander = GraphWalkExpander(self._db_services.provider)
graph_chunks = await expander.expand(semantic_results, depth=2)
if graph_chunks:
    # Merge into semantic pool — deduped by chunk_id in step 6
    semantic_results = merge_chunk_lists(semantic_results, graph_chunks)
```

`merge_chunk_lists` already exists in `chunkhound/services/research/shared/chunk_dedup.py` (exported from `__init__.py`). Or simple concatenation + dedup in step 6 handles it.

## Requirements

From Phase 5 sub-epic ch-z2o:
- GraphWalkExpander integrated into UnifiedSearch alongside existing semantic MultiHopStrategy
- Semantic expander and graph expander are independent — either can return empty without breaking the other
- Both expanders' results merged and deduped on chunk_id before feeding into existing reranker

## Implementation

### Step 1: Write failing test — resolution query includes chunk_id
File: `tests/test_graph_expander.py`, add to `TestSymbolToChunkResolution`.
Test: `test_resolution_includes_chunk_id` — mock provider returns chunk with `chunk_id` key. Call `expand()`, assert result dicts contain `chunk_id`.

### Step 2: Add chunk_id to _build_resolution_query
File: `chunkhound/services/search/graph_walk_expander.py:83`
Change: add `c.id AS chunk_id` to the SELECT clause of the SQL in `_build_resolution_query`.

### Step 3: Write failing test — unified_search includes graph-expanded chunks
File: `tests/unit/research/shared/test_unified_search_graph.py` (new).
Test: `test_unified_search_includes_graph_expanded_chunks` — mock `search_service.search_semantic` to return semantic results with `chunk_id`. Mock `GraphWalkExpander.expand()` to return graph chunks with different `chunk_id`. Call `unified_search()`. Assert output includes both semantic and graph-expanded chunks.

### Step 4: Write failing test — graph expansion empty, semantic results unaffected
Same file. Test: `test_graph_empty_returns_semantic_only` — mock `expand()` to return `[]`. Assert result equals semantic results only.

### Step 5: Write failing test — semantic empty, no graph expansion attempted
Same file. Test: `test_semantic_empty_skips_graph_expansion` — mock `search_semantic` to return `[]`. Assert `expand()` is NOT called (or if called with empty input, returns `[]`).

### Step 6: Implement step 1.5 in unified_search()
File: `chunkhound/services/research/shared/unified_search.py`
- Add import: `from chunkhound.services.search.graph_walk_expander import GraphWalkExpander`
- After both semantic search paths (line ~231 and ~182), before "Steps 3-5" (line 242):
  - Create expander from `self._db_services.provider`
  - Call `await expander.expand(semantic_results, depth=2)`
  - Merge graph chunks into `semantic_results` (concatenate; step 6 deduplicates by chunk_id)
  - Emit event for graph expansion results
- Graph expansion returning `[]` → no-op (semantic_results unchanged)

### Step 7: Run targeted tests
`uv run pytest tests/test_graph_expander.py tests/unit/research/shared/test_unified_search_graph.py -v`

### Step 8: Run full test suite
`uv run pytest -m "unit or integration or e2e" tests/ -v > /tmp/test_suite_ch_8mu.out 2>&1`

## Success Criteria
- [ ] `_build_resolution_query` SELECT includes `c.id AS chunk_id`
- [ ] Graph chunks from `expand()` contain `chunk_id` key
- [ ] `unified_search()` calls `GraphWalkExpander.expand()` after semantic search
- [ ] Graph expansion empty → semantic results pass through unchanged
- [ ] Semantic search empty → no graph expansion attempted
- [ ] `get_chunk_id()` works on graph chunks (returns real chunk_id)
- [ ] Step 6 dedup correctly handles both semantic and graph chunks
- [ ] Full test suite passes

## Anti-Patterns
- NO changing MultiHopStrategy behavior — graph expansion is additive alongside it
- NO hard dependency on graph data — if symbols/edges tables are empty, expander returns `[]` gracefully
- NO LLM calls during graph expansion — it's deterministic DuckDB queries
- NO breaking the existing `_search_structural` MCP tool path — adding `chunk_id` is additive
- NO changing step 6's dedup logic — graph chunks just need `chunk_id` to participate

## Key Considerations
- The `merge_chunk_lists` helper in `chunk_dedup.py` may be suitable for merging. Check its dedup key — if it uses `chunk_id`, it works. If it uses something else, simple concatenation + step 6 dedup is fine.
- The expander is instantiated fresh per `unified_search()` call — no persistent state concern.
- Event emission for graph expansion step should follow existing patterns (`emit_event("graph_expansion", ...)`) for observability in code_research logs.
- Both semantic search paths (query-expanded and single-query) need graph expansion after them. Factor the expansion into one block after the semantic results are assembled.
