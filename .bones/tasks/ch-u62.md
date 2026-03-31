---
id: ch-u62
title: Semantic search path scoping leaks through MCP tool layer
status: active
type: bug
priority: 1
---



## Context
Discovered during Phase 1 acceptance walkthrough (ch-1j2). The DuckDB provider-level path scoping fix (ch-jsj) uses strict prefix matching, but the MCP tool layer (`search_semantic`) still returns results outside the specified path prefix.

**Reproduction:**
```
search_semantic(query="server configuration", path="chunkhound/lsp/", page_size=5)
```
Result 5: `tests/test_lsp_client.py` — does NOT start with `chunkhound/lsp/`.
Regex search with same path filter is clean (no leakage).

**Root cause (confirmed via SRE code trace):** Two DuckDB provider methods use substring LIKE (`%path%`) instead of prefix LIKE (`path%`):
- `_executor_find_similar_chunks` at line 2447: `f"%{escaped_path}%"`
- `_executor_search_by_embedding` at line 2555: `f"%{escaped_path}%"`

The multi-hop search strategy calls `find_similar_chunks` for expansion rounds (`multi_hop_strategy.py:209`), so neighbor expansion leaks results outside the path scope. The primary `search_semantic` (line 2136) and `search_regex` (line 2261) already use correct prefix match.

Additionally, `find_similar_chunks` doesn't accept `fuzzy_path` — it hardcodes substring match regardless of caller intent.

## Requirements
- Semantic search via MCP `search_semantic` tool must return ONLY results whose `file_path` starts with the specified `path` prefix
- Must not regress regex search path scoping (already working)
- `find_similar_chunks` and `search_by_embedding` must use prefix match by default, substring only when `fuzzy_path=True`

## Implementation
1. `chunkhound/providers/database/duckdb_provider.py` — `_executor_find_similar_chunks` (line ~2440-2447):
   - Add `fuzzy_path: bool = False` parameter to both `find_similar_chunks` and `_executor_find_similar_chunks`
   - Change path pattern from `f"%{escaped_path}%"` to `f"{escaped_path}%"` (prefix) when `fuzzy_path=False`
   - Keep `f"%{escaped_path}%"` (substring) when `fuzzy_path=True`
2. `chunkhound/providers/database/duckdb_provider.py` — `_executor_search_by_embedding` (line ~2548-2555):
   - Add `fuzzy_path: bool = False` parameter to both `search_by_embedding` and `_executor_search_by_embedding`
   - Same prefix/substring logic as above
3. `chunkhound/services/search/multi_hop_strategy.py` (line ~209):
   - Pass `fuzzy_path=fuzzy_path` to `find_similar_chunks` call

## Success Criteria
- [ ] `search_semantic(path="chunkhound/lsp/")` returns zero results from outside `chunkhound/lsp/`
- [ ] Existing path scoping tests still pass (`test_path_prefix_scoping.py`)
- [ ] Regex search path scoping unaffected
- [ ] `find_similar_chunks` uses prefix match by default, substring only with `fuzzy_path=True`
- [ ] `search_by_embedding` uses prefix match by default, substring only with `fuzzy_path=True`

## Anti-Patterns
- Do NOT add a post-query Python filter — fix the SQL LIKE pattern at the source
- Do NOT change `_validate_and_normalize_path_filter` — it's correct
- Do NOT change `search_semantic` or `search_regex` — they already use prefix match
- Do NOT remove the `fuzzy_path` capability — just make it opt-in, not default

## Key Considerations
- `_validate_and_normalize_path_filter` skips trailing slash when last segment has a dot (line 2040) — this is for file-level filters, not a bug
- Multi-hop `_single_hop_search` already passes `fuzzy_path` correctly (line 123) — only `find_similar_chunks` is missing it
- Existing test `test_semantic_search_path_prefix_no_nested_leakage` covers the exact substring-vs-prefix scenario but only tests single_hop — need a multi-hop variant

### Adversarial Findings

**Parameter propagation: `find_similar_chunks` / `search_by_embedding`**
- Assumption: Positional args in `_execute_in_db_thread_sync` call match executor signature order
- Betrayal: Add `fuzzy_path` at wrong position — value silently lands in wrong parameter
- Consequence: Corrupted SQL queries (e.g. `fuzzy_path=True` becomes `threshold`)
- Mitigation: Add `fuzzy_path` as last parameter in both signatures. Test both `fuzzy_path=True` and `fuzzy_path=False` paths.

**Fragile param insertion: `_executor_search_by_embedding`**
- Assumption: `query_params.insert(-1, path_pattern)` places pattern before `limit`
- Betrayal: Adding params shifts the -1 offset
- Consequence: SQL parameter binding misalignment
- Mitigation: Build params list explicitly. Verify with a path-filtered test.

**Keyword safety: `multi_hop_strategy` → `find_similar_chunks`**
- Assumption: `find_similar_chunks` accepts `fuzzy_path`
- Betrayal: Param added to wrong position or forgotten in executor
- Consequence: Silently ignored if positional, TypeError if keyword
- Mitigation: Use keyword argument `fuzzy_path=fuzzy_path` in the call — fails loudly if missing.
