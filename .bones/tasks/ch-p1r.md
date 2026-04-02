---
id: ch-p1r
title: 'Search tools: query builder tests + sqlglot rebuild + structural walk fix'
status: open
type: task
priority: 1
depends_on: [ch-bcw]
parent: ch-mtq
---

## Context

Search has four modes: regex (delegated to service, no SQL in tools.py), semantic (same), symbols (direct SQL), structural (8-stage pipeline with SQL stages 2-4). The SQL lives in `_search_structural` and `_search_symbols` plus `_apply_type_filter`.

**Critical fix:** structural search at ~line 633 has a recursive CTE with `e.from_fqn = r.fqn` — same outbound-only bug as ch-s0x. This is the second location. The builder must use `bidirectional_edges()` from common.py.

**Blocked by:** ch-bcw (registry in place)
**Unlocks:** ch-mtq acceptance (search portion)

## Requirements

1. `chunkhound/mcp_server/tools/queries/search.py` — query builders for symbol search, symbol overlap, graph walk (structural), chunk resolution, type filter
2. `chunkhound/mcp_server/tools/search.py` — thin tool functions with 8-stage pipeline orchestration
3. Structural search graph walk uses bidirectional edges (bug fix)
4. Query builders tested via round-trip parsing
5. Existing search tests pass

## Design

**queries/search.py** — builder functions:
- `build_symbol_search_query(query, path, type_filter, limit, offset) -> tuple[str, list[Any]]` — LIKE-based search on name/fqn
- `build_symbol_overlap_query(chunks) -> tuple[str, list[Any]]` — find FQNs overlapping semantic result ranges (stage 2)
- `build_structural_walk_query(seed_fqns, depth, limit) -> tuple[str, list[Any]]` — recursive CTE from seeds, MUST use bidirectional_edges() (stage 3)
- `build_chunk_resolution_query(fqns) -> tuple[str, list[Any]]` — resolve FQNs to chunks (stage 4)
- `build_type_filter_query(results, type_filter) -> tuple[str, list[Any]]` — batch symbol lookup for type_signature overlap

**search.py** — tool layer:
- `search_impl` dispatches on `type` parameter (existing pattern)
- Regex/semantic paths delegate to search service unchanged — no SQL to port
- Symbol path: validate → `build_symbol_search_query` → execute → format
- Structural path: 8-stage pipeline with builders for SQL stages, Python for dedup/filter/paginate
- `SEARCH_DESCRIPTION`, `SEARCH_DESCRIPTION_NO_RESEARCH`, `CODE_RESEARCH_DESCRIPTION` constants live here
- `_apply_type_filter` uses `build_type_filter_query` internally

**Key decision:** structural walk depth stays at 2, walk limit stays at `page_size * 3`. These are tuning knobs, not bugs.

## Implementation

### Step 1: Write query builder tests
- **File:** `tests/mcp_server/test_queries_search.py`
- **Test `build_symbol_search_query`:** LIKE on name and fqn, optional path filter with ESCAPE, optional type_filter, placeholder count varies
- **Test `build_structural_walk_query`:** recursive CTE, MUST assert bidirectional UNION ALL (this is the regression test for the second outbound-only bug), cycle detection, seed FQNs as IN clause
- **Test `build_symbol_overlap_query`:** file_path + range overlap conditions
- **Test `build_chunk_resolution_query`:** FQN IN clause, joins chunks/symbols/files
- **Test `build_type_filter_query`:** type_signature LIKE with overlap conditions
- **Run:** `uv run pytest tests/mcp_server/test_queries_search.py -v`
- **Expected:** ImportError

### Step 2: Implement queries/search.py
- **File:** `chunkhound/mcp_server/tools/queries/search.py`
- **Compose:** `bidirectional_edges()` in structural walk, `scope_filter()` in symbol search, `escape_like()` for type_filter
- **Structural walk:** same pattern as graph walk builder but seeds from multiple FQNs (IN clause) instead of single symbol
- **Run:** query builder tests pass

### Step 3: Write thin search tool tests
- **File:** update existing `tests/lsp/test_tool_search_extensions.py` and `test_tool_search_structural.py` or create new
- **Intent:** verify orchestration — mock execute_query, verify builders called correctly, Python stages (dedup, paginate) still work

### Step 4: Implement search.py tool functions
- **File:** `chunkhound/mcp_server/tools/search.py`
- **Move+rebuild:** `search_impl`, `_search_structural`, `_search_symbols`, `_apply_type_filter` from _legacy_tools.py
- **Port:** SQL stages to query builders, keep Python orchestration stages
- **Register:** `@register_tool` for search, code_research description constant

### Step 5: Remove search functions from _legacy_tools.py
- **Verify:** corresponding functions deleted from legacy file

### Step 6: Run full search test suite
- **Run:** `uv run pytest tests/lsp/test_tool_search_extensions.py tests/lsp/test_tool_search_structural.py tests/mcp_server/test_queries_search.py -v`
- **Expected:** all pass

### Step 7: Verify structural walk fix via live MCP
- **After reinstall:** call `search(type="structural", query="error handling")` and verify results include graph-walked symbols from both edge directions
- This is the second outbound-only bug fix — verify it works

### Step 8: Commit
- **Message:** `refactor(search): port search tools to sqlglot query builders, fix structural walk direction`

## Success Criteria

- [ ] `queries/search.py` has builders for all search SQL operations
- [ ] Structural walk builder uses `bidirectional_edges()` (outbound-only bug FIXED)
- [ ] Query builder tests assert bidirectional UNION ALL in structural walk
- [ ] Symbol search uses LIKE ESCAPE via shared `scope_filter()`
- [ ] 8-stage structural pipeline preserved with SQL stages using builders
- [ ] Existing search tests pass
- [ ] Committed and pushed

## Anti-Patterns

- Don't change the 8-stage pipeline structure — it works, just port the SQL stages
- Don't try to push embedding similarity into SQL — it's correctly in the service layer
- Don't forget the structural walk bidirectional fix — it's half the point of this task
