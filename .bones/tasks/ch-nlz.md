---
id: ch-nlz
title: 'Task 5: search(type: structural) — semantic + graph walk expansion'
status: closed
type: task
priority: 1
owner: Seth
depends_on: [ch-lic]
parent: ch-zyz
---







## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. Fifth task — ch-lic (symbols + type_filter + get_stats) just closed.
Phase 3's final implementation criterion: `search(type: structural)` does semantic search + graph walk expansion + unified rerank.

**Blocked by:** ch-lic (closed)
**Unlocks:** Phase 3 acceptance task, then Phase 4

## Requirements
From parent epic R5: `search` gains `type: structural`. From sub-epic criterion: semantic search + graph walk expansion + unified rerank.

Phase 3 builds the MCP tool surface (`_search_structural` in tools.py). Phase 5 (R7) later extracts graph walk into a reusable `GraphWalkExpander` component for code_research's BFS pipeline.

## Design
**Cohesion seam:** Single addition to the search tool — extends the existing `search_impl` dispatch with a "structural" branch. Reuses existing semantic search, graph walk CTE pattern, and type_filter post-filter.

**Key decisions:**
- `search(type: structural)` runs semantic search FIRST, then enriches with graph-discovered chunks
- Symbol lookup from semantic results: file_path + line range overlap against `symbols` table
- Graph walk: multi-seed recursive CTE adapted from `_graph_walk` (tools.py:1114) — depth 2, all edge types
- Chunk resolution: walked symbols → chunks via file_id + range overlap (established pattern, no FK)
- Deduplication by `(file_path, start_line, end_line)` tuple — semantic results take priority
- `type_filter` reuses `_apply_type_filter` (centralized post-filter from ch-lic)
- Requires embeddings — same gate as semantic search

**Reused infrastructure:**
- `search_impl` (tools.py:421) — add "structural" to Literal + dispatch branch
- `services.search_service.search_semantic()` — first stage of structural search
- `_graph_walk` CTE pattern (tools.py:1137) — adapted for multi-seed walk
- `_apply_type_filter` (tools.py) — cross-type post-filter from ch-lic
- `_escape_like` (tools.py:912) — LIKE escaping for any path/query parameters
- `_build_filtered_tool_dicts` (base.py:428) — no change needed: "structural" requires embeddings, so excluded from no-embeddings enum alongside "semantic"

## Implementation

### Step 1: Write failing tests
- File: `tests/lsp/test_tool_search_structural.py` (new)
- Tests:
  - `test_structural_returns_semantic_plus_graph_chunks`: Mock `search_service.search_semantic` returning 2 chunk results. Mock `execute_query` for: (a) symbol lookup — find symbols overlapping chunks, (b) multi-seed graph walk — find neighbor symbols 1-2 hops, (c) chunk resolution — resolve walked symbols to chunks. Assert result contains BOTH original semantic chunks AND graph-discovered chunks (len > semantic-only count).
  - `test_structural_deduplicates`: Same chunk found by both semantic and graph walk appears only once. Mock both paths returning overlapping file_path+range tuples. Assert no duplicates in result.
  - `test_structural_with_type_filter`: Structural results + type_filter narrows output. Mock combined pool, mock symbols join for type_filter. Assert filtered results.
  - `test_structural_requires_embeddings`: When embedding_manager is None, raises ValueError with clear message.
  - `test_structural_no_symbols_falls_back`: When symbol lookup returns [] (empty symbols table), returns semantic results unchanged — no crash, no empty results.
  - `test_structural_graph_walk_empty`: Symbols found but no edges in symbol_edges. Returns semantic results only (graph walk adds nothing). Assert result count equals semantic count.
- Run: → fails (structural not recognized)

### Step 2: Implement `_search_structural` in tools.py
- File: `chunkhound/mcp_server/tools.py`
- Add "structural" to `search_impl`'s Literal: `Literal["regex", "semantic", "symbols", "structural"]`
- Update validation at line 449: add "structural" to the valid set
- Add `elif type == "structural":` branch before semantic, delegating to `_search_structural()`
- `async def _search_structural(services, embedding_manager, query, path, page_size, offset, fuzzy_path, type_filter)`:
  1. Validate embedding_manager (same check as semantic branch)
  2. Run semantic search: `services.search_service.search_semantic(query=query, ...)` with `page_size=page_size * 2` to get broader seed pool
  3. Symbol lookup: batch query `SELECT DISTINCT fqn, file_id FROM symbols WHERE (file_path = ? AND range_start <= ? AND range_end >= ?) OR ...` for each semantic result
  4. Multi-seed graph walk: recursive CTE starting from ALL seed FQNs, depth 2, all edge kinds. Adapted from `_graph_walk` but with `WHERE s.fqn IN (...)` seed instead of single FQN. MUST include LIMIT on walked nodes (carry over from `_graph_walk`'s limit parameter) — cap at `page_size * 3` to bound fan-out from dense symbol files
  5. Chunk resolution: `SELECT DISTINCT f.path as file_path, c.code as content, c.start_line, c.end_line FROM chunks c JOIN files f ON c.file_id = f.id JOIN symbols s ON s.file_id = f.id AND s.range_start >= c.start_line AND s.range_end <= c.end_line WHERE s.fqn IN (walked_fqns)`
  6. Deduplicate: build set of `(file_path, start_line, end_line)` from semantic results, skip graph chunks already present
  7. Combine: semantic results first (preserve relevance order), then graph-discovered chunks
  8. Apply `_apply_type_filter` if type_filter present
  9. Paginate combined results (offset/page_size against combined pool)
  10. Return SearchResponse
- Update SEARCH_DESCRIPTION and SEARCH_DESCRIPTION_NO_RESEARCH: add structural type option
- Run: → passes

### Step 3: Smoke test + full suite + commit
- `uv run pytest tests/lsp/test_tool_search_structural.py -v -m ""`
- `uv run pytest tests/test_smoke.py -v -n auto -m e2e`
- Full unit suite: `uv run pytest -m unit tests/ > /tmp/ch-nlz-suite.out`
- Update consistency tests if needed (search type enum now has 4 values)
- Commit and push

## Success Criteria
- [x] `search(type: structural, query="error handling")` returns semantic results + graph-discovered chunks
- [x] Graph walk finds chunks that semantic search alone misses (the defining behavior)
- [x] Deduplication: chunks found by both semantic and graph paths appear once
- [x] `type_filter` works with structural search (post-filter via `_apply_type_filter`)
- [x] Structural search requires embeddings — raises ValueError when unavailable
- [x] Graceful degradation: empty symbols table → returns semantic results only, no crash
- [x] Empty graph (symbols but no edges) → returns semantic results only
- [x] Zero regression on existing search types (regex, semantic, symbols unchanged)
- [x] `uv run pytest tests/lsp/test_tool_search_structural.py -v -m ""` → all pass
- [x] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass

## Anti-Patterns
- NO blocking the semantic search on graph availability — structural degrades gracefully to semantic-only
- NO changes to existing search behavior — structural is a new branch, additive only
- NO raw SQL column names in responses — format results with clean keys matching SearchResponse contract
- NO per-result graph walk queries — batch the multi-seed walk into a single CTE
- NO print() in MCP server code
- NO edge_kind filtering in Phase 3 — walk all edge types for maximum structural context

## Key Considerations
- The multi-seed graph walk CTE must handle empty seed sets (no symbols found) — return empty, don't error.
- Graph walk depth=2 is a balance: depth=1 is too shallow (misses indirect callers), depth=3 is too broad (too many results). Can be adjusted in Phase 5.
- `page_size * 2` for the initial semantic search gives a broader seed pool for graph enrichment. The final pagination applies to the combined pool.
- The `_build_filtered_tool_dicts` enum restriction already excludes both "semantic" and "structural" when embeddings unavailable — "structural" is not in the enum at all when it's `["regex", "symbols"]`, because the Literal generates the full enum and the restriction replaces it. No code change needed.
- Consistency tests in `test_mcp_tool_consistency.py` may need updating: the search enum with embeddings now has 4 values, and the test for without-embeddings already expects `["regex", "symbols"]`.
- Phase 5 will extract the graph walk + chunk resolution into a reusable `GraphWalkExpander` in `chunkhound/services/search/`. Phase 3 keeps it inline in tools.py for now.
- **[Adversarial: Input Hostility]** Dense symbol files (e.g., `__init__.py` re-exporting 200 names) can produce hundreds of seed FQNs from a few semantic results. The multi-seed CTE must carry a LIMIT on walked nodes (cap at `page_size * 3`) to bound fan-out. Without this, depth-2 walk from 200 seeds produces thousands of walked symbols.
- **[Adversarial: Dependency Treachery]** Graph chunk dicts from chunk resolution (step 2.5) MUST use identical keys to semantic result dicts: `file_path`, `content`, `start_line`, `end_line`. Key name mismatches cause `_apply_type_filter` and deduplication to silently fail (filter drops all graph chunks, dedup sees no overlaps).
- **[Adversarial: Temporal Betrayal]** Symbol/chunk staleness during background population lag is expected by design (R3). Semantic returns fresh chunks, symbols table may be stale. Graceful degradation handles the extreme case (no symbols → semantic only). Do NOT add synchronization — the staleness window is bounded and the design accounts for it.
