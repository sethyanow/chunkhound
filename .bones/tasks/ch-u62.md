---
id: ch-u62
title: Semantic search path scoping leaks through MCP tool layer
status: open
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

**Likely cause:** MCP tool's `search_semantic` may route through a different code path than `DuckDBProvider.search_semantic()`, or the path filter is applied post-retrieval with substring instead of prefix matching.

## Requirements
- Semantic search via MCP `search_semantic` tool must return ONLY results whose `file_path` starts with the specified `path` prefix
- Must not regress regex search path scoping (already working)

## Success Criteria
- [ ] `search_semantic(path="chunkhound/lsp/")` returns zero results from outside `chunkhound/lsp/`
- [ ] Existing path scoping tests still pass
- [ ] Regex search path scoping unaffected
