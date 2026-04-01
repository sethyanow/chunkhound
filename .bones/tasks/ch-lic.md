---
id: ch-lic
title: 'Task 4: search(type: symbols), type_filter parameter, get_stats MCP tool'
status: open
type: task
priority: 1
depends_on: [ch-t3j]
parent: ch-zyz
---




## Context
Parent epic ch-8e7, Phase 3 sub-epic ch-zyz. Fourth task — ch-1ed (lsp + lsp_status), ch-dlx (graph), ch-r4a (symbol_context) are closed.
Phase 3 Tasks 1-3 delivered all 4 primitive tools. This task extends existing tools per R5.

**Blocked by:** None (ch-r4a closed)
**Unlocks:** search(type: structural) task, then Phase 3 acceptance

## Requirements
From parent epic R5: `search` gains `type: symbols|structural` and `type_filter` parameter. `get_stats` gains graph + LSP data.

This task covers: `type: symbols`, `type_filter`, and `get_stats` MCP tool registration. `type: structural` (graph walk expansion) is a separate task.

## Design
**Cohesion seam:** Three related additions to the MCP tool surface — all R5 scope, all extend existing tools.py infrastructure. No new modules.

**Key decisions:**
- `search(type: symbols)` queries `symbols` table directly via `execute_query` — not the existing SearchService which handles chunks
- `type_filter` parameter applies to ALL search types: symbols (filter by type_signature directly), regex/semantic (post-filter by joining results with symbols table via file_path + line range overlap)
- `get_stats` is a NEW @register_tool (not in TOOL_REGISTRY currently). Queries file/chunk/symbol/edge counts + per-language breakdown + optional LSP status summary
- `get_stats` needs `services` (DB queries) and optionally `lsp_client_pool` (server status). Same signature inspection pattern as other tools.

**Reused infrastructure (to verify during SRE):**
- `search_impl` (tools.py:415) — extend Literal type and add type_filter param
- `_generate_json_schema_from_signature` — auto-generates schema from updated signature
- `_make_mock_services` (test helper) — sequential execute_query results
- `execute_tool` — already maps `services` and `lsp_client_pool`
- `_build_filtered_tool_dicts` — auto-picks up new tool from TOOL_REGISTRY

## Implementation

### Step 1: Write failing tests — search(type: symbols), type_filter, get_stats
- File: `tests/test_mcp_tools_lsp.py` (extend with `TestSearchExtensions` and `TestGetStatsTool`)
- `TestSearchExtensions`:
  - `test_search_symbols_by_name`: `search(type="symbols", query="parse")` → mock `services.provider.execute_query` returning symbol rows. Assert result has symbol dicts with `fqn`, `name`, `kind`, `file_path`, `type_signature` keys. Assert pagination present.
  - `test_search_symbols_with_type_filter`: `search(type="symbols", query="parse", type_filter="Result")` → filters by type_signature substring match. Assert results only contain symbols whose type_signature includes "Result".
  - `test_search_symbols_with_path_filter`: `search(type="symbols", query="parse", path="src/auth")` → file_path prefix filter. Assert results only from that path.
  - `test_search_symbols_empty`: query returns [] → empty results with pagination.
  - `test_search_regex_with_type_filter`: existing regex search + `type_filter="int"` → post-filters results by type_signature. Mock both search_service.search_regex_async (returns chunk results) and execute_query (symbols join). Assert filtered results.
  - `test_search_semantic_with_type_filter`: same pattern as regex but with semantic search path.
- `TestGetStatsTool`:
  - `test_get_stats`: mock execute_query for counts + language breakdown → result has `files`, `chunks`, `symbols`, `symbol_edges`, `languages` keys.
  - `test_get_stats_with_lsp`: pass lsp_client_pool mock → result includes `lsp_servers` key with count and status.
  - `test_get_stats_no_lsp`: lsp_client_pool=None → result has stats but `lsp_servers: null`.
- Run: → fails (symbols search type not recognized, get_stats not registered)

### Step 2: Implement search(type: symbols) + type_filter in search_impl
- File: `chunkhound/mcp_server/tools.py`
- Extend search_impl's `type` parameter: `Literal["regex", "semantic", "symbols"]`
- Add `type_filter: str | None = None` parameter to search_impl signature
- Add `elif type == "symbols":` branch after semantic branch:
  - Build parameterized SQL: `SELECT fqn, name, kind, language, file_path, range_start, range_end, type_signature FROM symbols WHERE (name LIKE ? OR fqn LIKE ?)`
  - Add path_filter: `AND file_path LIKE ?` with `{path}%` prefix
  - Add type_filter: `AND type_signature LIKE ?` with `%{type_filter}%`
  - Pagination: `ORDER BY name LIMIT ? OFFSET ?`
  - Count query for pagination total
  - Format as SearchResponse (results list + pagination dict)
- For regex/semantic + type_filter: after getting chunk results from existing pipeline, post-filter by joining with symbols table: `SELECT DISTINCT s.type_signature FROM symbols s WHERE s.file_path = ? AND s.range_start <= ? AND s.range_end >= ? AND s.type_signature LIKE ?` — drop chunks with no matching symbol
- Update SEARCH_DESCRIPTION and SEARCH_DESCRIPTION_NO_RESEARCH to mention `type: symbols` option
- Run: → passes

### Step 3: Implement get_stats MCP tool
- File: `chunkhound/mcp_server/tools.py`
- Add `GET_STATS_DESCRIPTION` constant describing the stats tool
- Register: `@register_tool(description=GET_STATS_DESCRIPTION, name="get_stats")`
- Signature: `async def get_stats_impl(services: Any, lsp_client_pool: Any = None) -> dict[str, Any]`
- Query counts: files (`SELECT COUNT(*) as count FROM files`), chunks, symbols, symbol_edges — each via `execute_query`
- Query per-language breakdown: `SELECT language, COUNT(*) as count FROM symbols GROUP BY language ORDER BY count DESC`
- If lsp_client_pool is not None: summarize pool state (total servers, ready count, degraded count) — reuse pattern from lsp_status_impl
- Return combined dict with all stats
- Run: → passes

### Step 4: Smoke test + full suite + commit
- `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""`
- `uv run pytest tests/test_smoke.py -v -n auto -m e2e`
- Full unit suite: `uv run pytest -m unit tests/`
- Commit and push

## Success Criteria
- [ ] `search(type: symbols, query="parse")` returns matching symbols from symbols table
- [ ] `search(type: symbols, type_filter="Result")` filters by type_signature substring
- [ ] `search(type: symbols, path="src/auth")` filters by file_path prefix
- [ ] `type_filter` works with regex and semantic search types (post-filter via symbols join)
- [ ] `get_stats` tool registered with @register_tool, callable via execute_tool
- [ ] `get_stats` returns file, chunk, symbol, edge counts + per-language breakdown
- [ ] `get_stats` includes LSP server status when pool available, null when not
- [ ] All existing search behavior unchanged (zero regression on regex/semantic without type_filter)
- [ ] `uv run pytest tests/test_mcp_tools_lsp.py -v -m ""` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` → all pass

## Anti-Patterns
- NO changes to existing search behavior — type_filter is additive, symbols is a new branch
- NO raw SQL column names in responses — format symbol results with clean keys
- NO print() in MCP server code
- NO separate type_filter handling per search type — centralize the symbols-join post-filter
- NO hardcoded count queries — use parameterized SQL for all stats queries

## Key Considerations
- The `symbols` search type bypasses SearchService entirely — it's a direct SQL query against the symbols table. This is intentional: SearchService handles chunks + embeddings, not symbols.
- `type_filter` on regex/semantic searches requires a join between chunk results and the symbols table. The join key is file_path + line range overlap (same pattern as symbol_context's FQN lookup). This may be expensive for large result sets — apply after pagination, not before.
- `get_stats` doesn't need embeddings or LLM — set `requires_embeddings=False`, `requires_llm=False` so it's always available.
- `_build_filtered_tool_dicts` in base.py has special handling for the "search" tool (restricts type enum when embeddings unavailable). After adding "symbols" to the Literal, the enum restriction logic needs updating: "symbols" should always be available (doesn't require embeddings), while "semantic" requires embeddings.
- The `search` tool description strings (SEARCH_DESCRIPTION, SEARCH_DESCRIPTION_NO_RESEARCH) need updating to include the `symbols` type and `type_filter` parameter.
