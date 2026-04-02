---
id: ch-c0w
title: 'LSP dispatch + stats + research: cleanup and port'
status: closed
type: task
priority: 1
owner: Seth
depends_on: [ch-bcw]
parent: ch-mtq
---




## Context

Three tool domains grouped because none is heavy alone:
- **LSP tools** (lsp_impl, lsp_status_impl, symbol_context_impl): no SQL, but the dispatch chain is a long if/elif. Formatters already extracted in ch-e6v. Reflect on the dispatch pattern.
- **Stats** (get_stats_impl): trivial SQL for counts/breakdowns. Keep raw SQL.
- **Research** (deep_research_impl): thin validation + factory delegation. Stays thin, just moves to its own file.

After this task, `tools/__init__.py` (currently 1034 lines with local copies of all infrastructure + remaining tool implementations) should become a thin re-export module (~30-50 lines).

**Blocked by:** ch-bcw (registry in place)
**Unlocks:** ch-mtq acceptance (final tool migration, monolith cleanup)

## Requirements

1. `chunkhound/mcp_server/tools/lsp_tools.py` — lsp_impl, lsp_status_impl, symbol_context_impl
2. `chunkhound/mcp_server/tools/stats.py` — get_stats_impl with raw SQL (queries are trivial COUNTs + GROUP BY)
3. `chunkhound/mcp_server/tools/research.py` — deep_research_impl
4. LSP dispatch improved (not just moved)
5. `__init__.py` cleaned up to thin re-export module — all local definitions deleted (Tool, TOOL_REGISTRY, register_tool, schema gen, execute_tool, copy-loop)
6. All existing tests pass

## Design

**lsp_tools.py** — LSP tool functions:
- `lsp_impl`: currently a big if/elif on operation string. Reflect: consider a dispatch dict `{operation_name: handler_function}` instead. Each handler is a small function that calls the LSP client method and formats the result.
- `lsp_status_impl`: straightforward, just move + use validation helpers
- `symbol_context_impl`: has internal `_safe_*` helpers (hover, definition, incoming, outgoing). These can stay nested or become module-level — decide during implementation based on readability.
- Formatters imported from `formatters.py` (already extracted in ch-e6v)
- Description constants: `LSP_DESCRIPTION`, `LSP_STATUS_DESCRIPTION`, `SYMBOL_CONTEXT_DESCRIPTION`

**stats.py** — stats tool:
- `get_stats_impl` has SQL for symbol counts, edge counts, language breakdowns
- Keep raw SQL inline — queries are trivial (4 COUNTs + 1 GROUP BY), sqlglot adds no value here
- `GET_STATS_DESCRIPTION` constant
- **Edge case:** guard against missing `symbols`/`symbol_edges` tables (pre-population state) — catch table-not-found errors gracefully, return 0 counts

**research.py** — research tool:
- `deep_research_impl` is validation + factory delegation. No SQL. Just move, use validation helpers.
- `CODE_RESEARCH_DESCRIPTION` constant — move from search.py to here (it describes research, not search)

**`__init__.py` cleanup** (the actual monolith — there is no `_legacy_tools.py`):
- After all tool functions moved to domain files, delete ALL local definitions from `__init__.py`: Tool class (line 92), TOOL_REGISTRY (line 106), schema gen functions (lines 109-263), register_tool (lines 265-314), PaginationInfo/SearchResponse/estimate_tokens/limit_response_size (lines 322-396), execute_tool (lines 940-1021), copy-loop hack (lines 1024-1034)
- Keep ONLY the re-export block (lines 1-67) + domain module imports to trigger registration
- Add `from .lsp_tools import lsp_impl, lsp_status_impl, symbol_context_impl` (new)
- Add `from .stats import get_stats_impl` (new)
- Add `from .research import deep_research_impl` (new, required — CLI imports `deep_research_impl` from package)
- Verify `@register_tool` decorators in domain files import from `registry.py`, NOT from `__init__.py`
- Verify all `@register_tool` decorators fire correctly from the new locations

**Final `__init__.py` contents (~30-50 lines):**
```
Re-exports from registry.py: Tool, TOOL_REGISTRY, execute_tool, register_tool
Re-exports from response.py: PaginationInfo, SearchResponse, estimate_tokens, limit_response_size, MAX_*
Re-exports from search.py: search_impl, SEARCH_DESCRIPTION, SEARCH_DESCRIPTION_NO_RESEARCH
Re-exports from research.py: deep_research_impl, CODE_RESEARCH_DESCRIPTION
Domain module imports: graph, search, lsp_tools, stats, research (trigger registration)
```

## Implementation

### Step 1: Write LSP dispatch tests
- **File:** `tests/mcp_server/test_lsp_dispatch.py`
- **Intent:** test that the dispatch pattern routes correctly to each operation handler
- **Test:** valid operation → correct handler called
- **Test:** invalid operation → error response
- **Test:** missing required params → error dict

### Step 2: Write stats tool tests
- **File:** `tests/mcp_server/test_stats_tool.py`
- **Test:** stats returns correct structure with symbol count, edge count, language breakdown
- **Test:** stats handles empty/missing tables gracefully (returns 0 counts)
- **Run:** ImportError expected

### Step 3: Implement lsp_tools.py
- **File:** `chunkhound/mcp_server/tools/lsp_tools.py`
- **Rebuild:** lsp_impl with dispatch dict pattern replacing if/elif
- **Import:** formatters from `formatters.py`, validation from `validation.py`
- **Move:** lsp_status_impl, symbol_context_impl
- **Register:** all three with `@register_tool`

### Step 4: Implement stats.py
- **File:** `chunkhound/mcp_server/tools/stats.py`
- **Keep:** raw SQL inline — trivial COUNTs + GROUP BY
- **Add:** error guard for missing tables (pre-population state)
- **Register:** `@register_tool`

### Step 5: Implement research.py
- **File:** `chunkhound/mcp_server/tools/research.py`
- **Move:** deep_research_impl (thin, mostly stays as-is)
- **Use:** validation helpers for the gate checks
- **Register:** `@register_tool`

### Step 6: Clean up `__init__.py`
- **Delete:** all local definitions (Tool, TOOL_REGISTRY, register_tool, schema gen, response types, execute_tool, copy-loop)
- **Keep:** re-export block + domain module imports only
- **Add:** re-exports for new modules (lsp_tools, stats, research)
- **Verify:** `uv run python -c "from chunkhound.mcp_server.tools import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY.keys()))"` — all tools registered
- **Verify:** `uv run python -c "from chunkhound.mcp_server.tools import deep_research_impl, search_impl, execute_tool"` — CLI imports work

### Step 7: Run full test suite
- **Run:** `uv run pytest tests/lsp/ tests/mcp_server/ -v > /tmp/final_decomp.txt 2>&1 && tail -20 /tmp/final_decomp.txt`
- **Expected:** all pass

### Step 8: Run smoke tests
- **Run:** `uv run pytest tests/test_smoke.py -v -n auto -m e2e > /tmp/final_smoke.txt 2>&1 && tail -10 /tmp/final_smoke.txt`
- **Expected:** all pass

### Step 9: Verify file sizes
- **Run:** `wc -l chunkhound/mcp_server/tools/*.py chunkhound/mcp_server/tools/queries/*.py`
- **Expected:** no file over 500 lines

### Step 10: Commit
- **Message:** `refactor(mcp): complete tools decomposition — LSP, stats, research; delete legacy tools.py`

## Success Criteria

- [x] `lsp_tools.py` has improved dispatch pattern (not if/elif chain) — LSP_DISPATCH dict, 7 handlers
- [x] `stats.py` has get_stats_impl with raw SQL, handles missing tables gracefully — _safe_count/_safe_language_breakdown
- [x] `research.py` has deep_research_impl — CODE_RESEARCH_DESCRIPTION moved from search.py
- [x] `__init__.py` is a thin re-export module (79 lines) — no local Tool/TOOL_REGISTRY/register_tool/schema gen/execute_tool/copy-loop
- [x] All tools registered correctly — 7 tools via TOOL_REGISTRY, all from domain modules
- [x] CLI imports work (`from chunkhound.mcp_server.tools import deep_research_impl, search_impl, execute_tool`)
- [x] No file in `tools/` exceeds 500 lines — largest is lsp_tools.py at 458
- [x] All existing tests pass — 395 passed (tests/lsp/ + tests/mcp_server/)
- [x] Smoke tests pass — 17 passed
- [x] Committed and pushed — c53473db

## Anti-Patterns

- Don't gold-plate the LSP dispatch — a dict is enough, no need for a registry pattern
- Don't refactor research internals — it's already thin, just move it
- Don't leave local definitions in `__init__.py` — the whole point is eliminating the monolith, not just moving functions out while leaving infrastructure behind
- Don't import `register_tool` from `__init__.py` in domain files — import from `registry.py` to avoid circular deps
- Don't port stats SQL to sqlglot — raw SQL for trivial queries is correct here
- Don't leave CODE_RESEARCH_DESCRIPTION in search.py — it describes the research tool, move it to research.py

## Key Considerations

### lsp_tools.py — dispatch dict

**[Input Hostility]: Operation routing**
- Assumption: `operation` string matches a dispatch dict key
- Betrayal: MCP schema enforcement is client-side. At runtime, any string can arrive.
- Consequence: `dict[key]` → KeyError; `dict.get()` → None → downstream AttributeError
- Mitigation: `handler = DISPATCH.get(operation)` with explicit None check returning error dict. The dict pattern itself is the structural fix over if/elif.

**[Dependency Treachery]: graph._graph_walk cross-import in symbol_context_impl**
- Assumption: `from .graph import _graph_walk` succeeds
- Betrayal: graph.py internal restructure breaks this import
- Consequence: graph_neighborhood silently None — data loss, not crash
- Mitigation: Already structural — try/except with None fallback. Acceptable degradation.

### stats.py — raw SQL

**[Temporal Betrayal]: Missing tables pre-population**
- Assumption: `symbols` and `symbol_edges` tables exist when stats queries run
- Betrayal: Indexing hasn't completed or failed mid-way → DuckDB CatalogException
- Consequence: Unhandled exception propagates to MCP client
- Mitigation: Catch CatalogException for symbol/edge queries, return 0 counts. File/chunk tables always exist (created during DB init).

### research.py — description ownership

**[Dependency Treachery]: CODE_RESEARCH_DESCRIPTION lives in wrong module**
- Assumption: research.py imports its tool description from search.py
- Betrayal: search.py modification accidentally changes research tool description. Confusing ownership.
- Consequence: Research tool description drifts from research tool behavior
- Mitigation: Move CODE_RESEARCH_DESCRIPTION to research.py. Update __init__.py re-export source.

### __init__.py — registration wiring

**[Dependency Treachery]: Silent registration failure**
- Assumption: Importing domain modules triggers @register_tool, populating TOOL_REGISTRY
- Betrayal: If a domain module import fails (missing dep, circular import), that tool silently doesn't register — no error, just fewer entries in TOOL_REGISTRY
- Consequence: MCP client doesn't see the tool; execute_tool raises ValueError
- Mitigation: Verification step (`print(sorted(TOOL_REGISTRY.keys()))`) catches this. But should also be a test assertion.

**[Temporal Betrayal]: Dual registry incomplete cleanup**
- Assumption: After cleanup, only registry.py's TOOL_REGISTRY exists
- Betrayal: Partial cleanup leaves some tools registering into a dead local dict
- Consequence: Tool works in direct-import tests but fails via MCP dispatch (which uses registry.py's TOOL_REGISTRY)
- Mitigation: Delete local TOOL_REGISTRY entirely. Any stale reference → immediate ImportError. Verification step confirms all tools reachable.

**[Dependency Treachery]: Circular imports in domain files**
- Assumption: Domain files import only from registry.py, response.py, validation.py, formatters.py
- Betrayal: If a domain file imports from __init__.py (even indirectly), and __init__.py imports the domain file → circular import
- Consequence: ImportError or partially initialized module
- Mitigation: Anti-pattern already blocks this. Domain files never import from `__init__.py`.

## Log

- [2026-04-02T22:20:45Z] [Seth] Debrief: Clean decomposition — LSP dispatch dict, _safe_count guard, CODE_RESEARCH_DESCRIPTION moved to research.py. One external test coupling fixed (test_tool_wiring.py imported private fn from __init__). SRE caught _legacy_tools.py factual error before any wasted effort. Reflections: skeleton accuracy was good except for the wrong file name (6 references). User confirmed raw SQL for stats. All ch-mtq criteria now checked.
