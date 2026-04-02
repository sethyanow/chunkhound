---
id: ch-ei8
title: 'Graph tools: query builder tests + sqlglot rebuild'
status: active
type: task
priority: 1
owner: Seth
depends_on: [ch-bcw]
parent: ch-mtq
---


## Context

Graph operations (walk, reachability, boundary, overview) are the most SQL-heavy tools. Walk already has the bidirectional fix (ch-s0x) as a reference. This task ports all four to sqlglot query builders and rebuilds the tool functions to be thin.

Full walk query spike completed and verified during planning — see conversation context for the exact sqlglot API usage.

**Blocked by:** ch-bcw (registry in place, package structure ready)
**Unlocks:** ch-mtq acceptance (graph portion)

## Requirements

1. `chunkhound/mcp_server/tools/queries/graph.py` — query builders for all four graph operations
2. `chunkhound/mcp_server/tools/graph.py` — thin tool functions: validate → build → execute → format
3. Query builders are pure functions returning `(str, list[Any])` via sqlglot
4. All builders use shared fragments from `queries/common.py` (bidirectional_edges, scope_filter, visited_tracking)
5. Existing graph tests still pass
6. New query builder tests validate SQL structure via round-trip parsing

## Design

**queries/graph.py** — four builder functions:
- `build_walk_query(symbol, depth, edge_kind, limit) -> tuple[str, list[Any]]`
- `build_walk_edges_query(fqns, edge_kind) -> tuple[str, list[Any]]` (the second query that fetches edges between discovered nodes)
- `build_reachability_query(scope, limit) -> tuple[str, list[Any]]` (outbound-only CTE, correct for directional reachability)
- `build_boundary_query(scope, limit) -> tuple[str, list[Any]]`
- `build_overview_query(scope, limit) -> tuple[str, list[Any]]` (uses bidirectional edge counting pattern)
- `build_overview_breakdown_query(fqns) -> tuple[str, list[Any]]`

**graph.py** — thin tool layer:
- `graph_impl` dispatches to operations (existing pattern, but using validation helpers)
- `_graph_walk` → validate with `require_param` → `build_walk_query` → `execute_query` → `format_node`/`format_edge`
- Same pattern for reachability, boundary, overview
- `GRAPH_DESCRIPTION` constant lives here
- `_escape_like` calls move to `queries.common.escape_like`

**Key decision:** reachability stays outbound-only. It answers "what's reachable from entry points?" which is directional by design. Walk is bidirectional ("what's connected?"). These are different semantics.

## Implementation

### Step 1: Write query builder tests
- **File:** `tests/mcp_server/test_queries_graph.py`
- **Test `build_walk_query`:** round-trip parse, assert recursive CTE, assert bidirectional UNION ALL in subquery, assert LIST_CONTAINS, placeholder count = 3 (no edge_kind) / 4 (with edge_kind), param ordering
- **Test `build_walk_query` with edge_kind:** extra placeholder, edge_kind in WHERE
- **Test `build_reachability_query`:** NOT bidirectional (intentionally outbound-only), has scope LIKE ESCAPE
- **Test `build_boundary_query`:** scope filtering with LIKE/NOT LIKE, edge output columns
- **Test `build_overview_query`:** bidirectional UNION ALL for edge counting, GROUP BY
- **Test helpers:** `parse_duckdb(sql)` wrapper, `assert_placeholder_count(sql, n)`
- **Run:** `uv run pytest tests/mcp_server/test_queries_graph.py -v`
- **Expected:** ImportError

### Step 2: Implement queries/graph.py
- **File:** `chunkhound/mcp_server/tools/queries/graph.py`
- **Pattern:** each function builds sqlglot AST → `.sql(dialect="duckdb")` → return (sql, params)
- **Compose:** `bidirectional_edges()` and `visited_tracking_columns()` from common.py
- **Walk query:** reference the spike from this conversation (tested, verified, correct)
- **Run:** query builder tests pass

### Step 3: Write thin graph tool tests
- **File:** update `tests/lsp/test_tool_graph.py` or create `tests/mcp_server/test_graph_tool.py`
- **Intent:** verify the thin functions compose correctly: mock `execute_query`, verify builders are called, formatting is applied
- **Key test:** `test_walk_calls_builder_and_formats` — services.provider.execute_query is called with SQL from builder, result goes through format_node/format_edge

### Step 4: Implement graph.py tool functions
- **File:** `chunkhound/mcp_server/tools/graph.py`
- **Move:** `graph_impl`, `_graph_walk`, `_graph_reachability`, `_graph_boundary`, `_graph_overview`, `GRAPH_DESCRIPTION` from `__init__.py` (the legacy monolith, 1832 lines)
- **Rebuild:** each function uses `require_param`/`clamp` from validation.py, query builders from queries/graph.py, formatters from formatters.py
- **Remove:** inline SQL strings, inline formatting, inline param validation
- **Register:** `@register_tool` from registry.py
- **Wire:** `__init__.py` must import `graph.py` (e.g., `from .graph import graph_impl`) so `@register_tool` fires and the tool appears in TOOL_REGISTRY

### Step 5: Remove graph functions from __init__.py
- **Delete:** graph_impl, _graph_walk, _graph_reachability, _graph_boundary, _graph_overview, GRAPH_DESCRIPTION, _escape_like (now in queries/common)
- **Update test imports:** `tests/lsp/test_tool_graph.py` line 15 imports `_escape_like` from `chunkhound.mcp_server.tools` — update to `from chunkhound.mcp_server.tools.queries.common import escape_like`
- **Verify:** `__init__.py` shrinks significantly

### Step 6: Run full graph test suite
- **Run:** `uv run pytest tests/lsp/test_tool_graph.py tests/mcp_server/test_queries_graph.py -v`
- **Expected:** all pass

### Step 7: Run smoke tests
- **Run:** `uv run pytest tests/test_smoke.py -v -n auto -m e2e > /tmp/graph_smoke.txt 2>&1 && tail -10 /tmp/graph_smoke.txt`
- **Expected:** all pass

### Step 8: Commit
- **Message:** `refactor(graph): port graph tools to sqlglot query builders`

## Success Criteria

- [ ] `queries/graph.py` has builders for walk, reachability, boundary, overview
- [ ] All builders return `(sql, params)` via sqlglot DuckDB dialect
- [ ] Walk builder uses `bidirectional_edges()` from common.py
- [ ] Reachability stays outbound-only (directional by design)
- [ ] Overview uses bidirectional edge counting
- [ ] `graph.py` tool functions are thin: validate → build → execute → format
- [ ] Query builder tests validate structure via round-trip parsing
- [ ] Existing graph tests pass
- [ ] Smoke tests pass
- [ ] Committed and pushed

## Key Considerations

- sqlglot normalizes `list_contains` → `array_contains` in DuckDB dialect — assert on `array_contains` in query builder tests
- Overview breakdown query uses OR-join (`e.from_fqn = s.fqn OR e.to_fqn = s.fqn`) — acceptable for small N (post-filter on top symbols). Top-symbol query uses UNION ALL for performance.
- After creating `graph.py`, forgetting to import it in `__init__.py` → tool silently absent → existing tests will catch this (SC8)

## Anti-Patterns

- Don't change reachability to bidirectional — it's intentionally directional
- Don't inline SQL in the tool functions — that's what we're fixing
- Don't skip the round-trip parse in tests — string matching is fragile
