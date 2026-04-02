---
id: ch-c0w
title: 'LSP dispatch + stats + research: cleanup and port'
status: open
type: task
priority: 1
depends_on: [ch-bcw]
parent: ch-mtq
---

## Context

Three tool domains grouped because none is heavy alone:
- **LSP tools** (lsp_impl, lsp_status_impl, symbol_context_impl): no SQL, but the dispatch chain is a long if/elif. Formatters already extracted in ch-e6v. Reflect on the dispatch pattern.
- **Stats** (get_stats_impl): some SQL for counts/breakdowns. Port to sqlglot.
- **Research** (deep_research_impl): thin validation + factory delegation. Stays thin, just moves to its own file.

After this task, `_legacy_tools.py` should be empty and deletable.

**Blocked by:** ch-bcw (registry in place)
**Unlocks:** ch-mtq acceptance (final tool migration, legacy file deletion)

## Requirements

1. `chunkhound/mcp_server/tools/lsp_tools.py` — lsp_impl, lsp_status_impl, symbol_context_impl
2. `chunkhound/mcp_server/tools/stats.py` — get_stats_impl with sqlglot query builders
3. `chunkhound/mcp_server/tools/research.py` — deep_research_impl
4. LSP dispatch improved (not just moved)
5. `_legacy_tools.py` empty and deleted
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
- Port these queries to sqlglot builders in `queries/stats.py` or inline if small enough
- `GET_STATS_DESCRIPTION` constant

**research.py** — research tool:
- `deep_research_impl` is validation + factory delegation. No SQL. Just move, use validation helpers.
- `CODE_RESEARCH_DESCRIPTION` constant (or this may live with search — check where it's registered)

**Legacy cleanup:**
- After all functions moved, `_legacy_tools.py` should be empty
- Delete it
- Verify `tools/__init__.py` doesn't reference it anymore
- Verify all `@register_tool` decorators fire correctly from the new locations

## Implementation

### Step 1: Write LSP dispatch tests
- **File:** `tests/mcp_server/test_lsp_dispatch.py`
- **Intent:** test that the dispatch pattern routes correctly to each operation handler
- **Test:** valid operation → correct handler called
- **Test:** invalid operation → error response
- **Test:** missing required params → error dict

### Step 2: Write stats query builder tests
- **File:** `tests/mcp_server/test_queries_stats.py` (if builders extracted) or `tests/mcp_server/test_stats_tool.py`
- **Test:** stats queries generate correct SQL for symbol count, edge count, language breakdown
- **Run:** ImportError expected

### Step 3: Implement lsp_tools.py
- **File:** `chunkhound/mcp_server/tools/lsp_tools.py`
- **Rebuild:** lsp_impl with dispatch dict pattern replacing if/elif
- **Import:** formatters from `formatters.py`, validation from `validation.py`
- **Move:** lsp_status_impl, symbol_context_impl
- **Register:** all three with `@register_tool`

### Step 4: Implement stats.py
- **File:** `chunkhound/mcp_server/tools/stats.py`
- **Port:** get_stats_impl SQL to sqlglot or keep inline if queries are simple/few
- **Use:** validation helpers, formatters as needed
- **Register:** `@register_tool`

### Step 5: Implement research.py
- **File:** `chunkhound/mcp_server/tools/research.py`
- **Move:** deep_research_impl (thin, mostly stays as-is)
- **Use:** validation helpers for the gate checks
- **Register:** `@register_tool`

### Step 6: Delete _legacy_tools.py
- **Verify:** no functions remain
- **Delete:** the file
- **Update:** `tools/__init__.py` if it imports from _legacy_tools
- **Run:** `uv run python -c "from chunkhound.mcp_server.tools import TOOL_REGISTRY; print(sorted(TOOL_REGISTRY.keys()))"` — verify all tools registered

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

- [ ] `lsp_tools.py` has improved dispatch pattern (not if/elif chain)
- [ ] `stats.py` has get_stats_impl with sqlglot or clean inline SQL
- [ ] `research.py` has deep_research_impl
- [ ] `_legacy_tools.py` deleted — no more monolith
- [ ] All tools registered correctly (verify via TOOL_REGISTRY)
- [ ] No file in `tools/` exceeds 500 lines
- [ ] All existing tests pass
- [ ] Smoke tests pass
- [ ] Committed and pushed

## Anti-Patterns

- Don't gold-plate the LSP dispatch — a dict is enough, no need for a registry pattern
- Don't refactor research internals — it's already thin, just move it
- Don't leave _legacy_tools.py around "just in case" — delete it when empty
