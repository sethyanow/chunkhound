---
id: ch-8mu
title: Wire GraphWalkExpander into UnifiedSearch
status: closed
type: task
priority: 1
owner: Seth
parent: ch-z2o
---





**Blocked by:** ch-ic6 (GraphWalkExpander wired into MCP search tool)
**Unlocks:** Prompt template tasks (graph data available in code_research pipeline); sub-epic criterion 1; parent epic criterion "Graph walk expander produces chunks in code_research that semantic search alone misses"

## Context

Phase 5 (ch-z2o) R7 requires graph walk expansion in the search pipeline. ch-ic6 wired `GraphWalkExpander` into the MCP `search(type="structural")` tool. This task wires it into `UnifiedSearch.unified_search()` in `chunkhound/services/research/shared/unified_search.py`, the code_research pipeline — satisfying sub-epic criterion 1.

`UnifiedSearch.unified_search()` has a 7-step pipeline (file uses step 2-7 numbering):
2. Multi-hop semantic search → `semantic_results` (two paths: query-expanded or single-query)
3. Extract symbols from semantic results
4. Select top N symbols
5. Regex search for top symbols → `regex_results`
6. Unify (dedup by `chunk_id` via `get_chunk_id()`)
7. Unified rerank

Graph expansion goes between step 2 and step 3 as step 2.5: semantic results → `GraphWalkExpander.expand()` → merge graph-expanded chunks into `semantic_results` before symbol extraction and regex search.

**Insertion point:** ONE location — after the if/else block (lines 124-240) that produces `semantic_results` from either path, before line 242 ("Steps 3-5"). Both semantic search paths converge to `semantic_results` before this point.

**Implicit behavior:** Merging graph chunks into `semantic_results` before step 3 means graph chunks also contribute to symbol extraction (step 3) → regex search (step 5). This widens regex coverage with graph-discovered symbols. Desired — the graph expansion enriches the entire downstream pipeline.

Key infrastructure:
- `UnifiedSearch` at `chunkhound/services/research/shared/unified_search.py:35`
- `self._db_services.provider` is a `DatabaseProvider` — same type `GraphWalkExpander.__init__` needs
- `get_chunk_id()` at `chunkhound/core/utils/chunk_utils.py:8` — returns `chunk.get("chunk_id") or chunk.get("id")`
- Step 6 dedup: `chunk_id = get_chunk_id(chunk); if chunk_id: unified_map[chunk_id] = chunk`

**Critical gap:** Graph chunks from `GraphWalkExpander.expand()` have `file_path`, `content`, `start_line`, `end_line` but NO `chunk_id`. The resolution query (`_build_resolution_query` in `graph_walk_expander.py:83`) JOINs on `chunks c` but doesn't SELECT `c.id`. Without `chunk_id`, graph chunks would be silently dropped by step 6's dedup. Fix: add `c.id AS chunk_id` to the resolution query's SELECT.

## Design

Two changes:

1. **Add `chunk_id` to expander's resolution query** — `_build_resolution_query` adds `c.id AS chunk_id` to SELECT. This makes graph chunks compatible with `get_chunk_id()` dedup. Safe for the MCP path (extra key ignored).

2. **Add step 2.5 in `unified_search()`** — after semantic search (line 240), before symbol extraction (line 242):
```python
from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

# Step 2.5: Graph walk expansion (graceful degradation — never crashes pipeline)
try:
    expander = GraphWalkExpander(self._db_services.provider)
    graph_chunks = await expander.expand(semantic_results, depth=2)
    if graph_chunks:
        semantic_results = semantic_results + graph_chunks
except Exception:
    logger.warning("Graph expansion failed, continuing with semantic-only results")
    graph_chunks = []
```

Simple concatenation, NOT `merge_chunk_lists` — step 6's existing dedup by `chunk_id` handles deduplication. `merge_chunk_lists` would also work but adds unnecessary score comparison for this use case.

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

### Step 5b: Write failing test — graph expansion error degrades gracefully
Same file. Test: `test_graph_expansion_error_degrades_gracefully` — mock `GraphWalkExpander.expand()` to raise `Exception`. Call `unified_search()`. Assert: returns semantic results (no crash), warning logged.

### Step 6: Implement step 2.5 in unified_search()
File: `chunkhound/services/research/shared/unified_search.py`
- Add import: `from chunkhound.services.search.graph_walk_expander import GraphWalkExpander`
- After the if/else block (line 124-240) that produces `semantic_results`, before line 242 ("Steps 3-5"):
  - Wrap in try/except for graceful degradation (anti-pattern: NO hard dependency on graph data)
  - Create expander from `self._db_services.provider`
  - Call `await expander.expand(semantic_results, depth=2)`
  - Concatenate graph chunks into `semantic_results` (step 6 deduplicates by chunk_id)
  - Emit event for graph expansion results
  - On exception: log warning, continue with semantic-only results
- Graph expansion returning `[]` → no-op (semantic_results unchanged)

### Step 7: Run targeted tests
`uv run pytest tests/test_graph_expander.py tests/unit/research/shared/test_unified_search_graph.py -v`

### Step 8: Run full test suite
`uv run pytest -m "unit or integration or e2e" tests/ -v > /tmp/test_suite_ch_8mu.out 2>&1`

## Success Criteria
- [x] `_build_resolution_query` SELECT includes `c.id AS chunk_id`
- [x] Graph chunks from `expand()` contain `chunk_id` key
- [x] `unified_search()` calls `GraphWalkExpander.expand()` after semantic search
- [x] Graph expansion empty → semantic results pass through unchanged
- [x] Semantic search empty → no graph expansion attempted
- [x] `get_chunk_id()` works on graph chunks (returns real chunk_id)
- [x] Step 6 dedup correctly handles both semantic and graph chunks
- [x] Graph expansion failure degrades gracefully (warning logged, semantic results returned)
- [x] Full test suite passes

## Anti-Patterns
- NO changing MultiHopStrategy behavior — graph expansion is additive alongside it
- NO hard dependency on graph data — if symbols/edges tables are empty, expander returns `[]` gracefully
- NO LLM calls during graph expansion — it's deterministic DuckDB queries
- NO breaking the existing `_search_structural` MCP tool path — adding `chunk_id` is additive
- NO changing step 6's dedup logic — graph chunks just need `chunk_id` to participate

## Key Considerations
- Simple concatenation + step 6 dedup is preferred over `merge_chunk_lists`. Both work, but concatenation is simpler and step 6 already handles dedup by chunk_id. `merge_chunk_lists` adds unnecessary score comparison (graph chunks have no `rerank_score`).
- The expander is instantiated fresh per `unified_search()` call — no persistent state concern.
- Event emission for graph expansion step should follow existing patterns (`emit_event("graph_expansion", ...)`) for observability in code_research logs.
- ONE insertion point after both semantic search paths converge (line 240), before "Steps 3-5" (line 242). Do NOT insert inside each semantic path — they converge to `semantic_results` before step 3.
- Graph chunks merged into `semantic_results` will feed into symbol extraction (step 3) and regex search (step 5). This is desired — graph-discovered symbols widen the regex coverage.
- The MCP `_search_structural` path (search.py:308) does NOT wrap expand() in try/except. That's acceptable there (user sees tool error). In `unified_search()`, graceful degradation is required because it's inside code_research's BFS pipeline.

### Adversarial Failure Catalog

**Input Hostility: expand() seed chunks** — `_build_overlap_query` does direct dict access on `file_path`, `start_line`, `end_line`. If a semantic result chunk has different key names, KeyError. Mitigation: structural — try/except catches and degrades. Schema is confirmed compatible via MCP `_search_structural` path using same `search_semantic()` output.

**Resource Exhaustion: large semantic result sets** — Query expansion could produce 500+ seed chunks → walk_limit of 15,000 → large resolution IN clause. Mitigation: bounded by walk_limit; in practice page_size=30 keeps seed sets small (~150 after dedup). Not blocking; cap on seed chunk count is a future optimization if profiling shows issues.

**Temporal Betrayal: concurrent symbol population** — Background LSP population may be writing while expand() reads. Consequence: incomplete expansion (missing symbols/edges). Mitigation: acceptable by design — expansion is best-effort enrichment, read-only, no corruption risk.

## Log

- [2026-04-05T03:43:43Z] [Seth] Debrief: Clean implementation — 2 production files changed (13 lines in unified_search.py, 2 lines in graph_walk_expander.py), 6 new tests. Adversarial testing found concatenation order bug: step 6 semantic loop is last-wins, so graph chunks must prepend semantic results (graph_chunks + semantic_results) to preserve semantic priority on chunk_id collision. Fixed and regression tested. Reflections: skeleton's 'simple concatenation + step 6 dedup handles it' was almost right but missed last-wins semantics. SRE's error handling addition was essential (try/except wasn't in original skeleton). No user corrections needed.
