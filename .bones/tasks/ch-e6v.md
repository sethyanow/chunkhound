---
id: ch-e6v
title: 'Shared foundations: validation, formatters, query fragments'
status: closed
type: task
priority: 1
owner: Seth
parent: ch-mtq
---



## Context

First task in the decomposition. Creates the shared modules that every subsequent tool rebuild depends on. These are zero-caller modules initially — building blocks consumed by ch-ei8, ch-p1r, ch-c0w.

**Blocked by:** nothing
**Unlocks:** ch-bcw (registry extraction), and transitively all tool rebuilds

## Requirements

1. `chunkhound/mcp_server/tools/validation.py` — param checking, range clamping, error dict construction
2. `chunkhound/mcp_server/tools/formatters.py` — node, edge, location, call_item, diagnostic formatting
3. `chunkhound/mcp_server/tools/queries/__init__.py` + `common.py` — sqlglot shared fragments
4. All three modules tested independently

## Design

**validation.py** extracts the repeated pattern from every tool function:
- `require_param(name, value) -> dict | None` — returns error dict if missing/empty, None if ok
- `clamp(value, min_val, max_val) -> int` — range clamping (used for depth, limit)
- Error dict format: `{"error": "missing_parameter", "message": "..."}`

Current patterns to extract from (tools.py):
- `_graph_walk` lines 1319-1323: symbol required check
- `graph_impl` lines 1273-1308: depth/limit clamping (1-20, 1-100)
- `_graph_reachability` lines 1400-1410: scope required check
- `_graph_boundary` lines 1454-1464: scope required check

**formatters.py** consolidates result formatting:
- `format_node(row) -> dict` — `{fqn, name, kind, file_path, depth}` from raw DB row
- `format_edge(row) -> dict` — `{from_symbol, to_symbol, edge_kind, from_file, to_file}` (renames from_fqn/to_fqn)
- Existing LSP formatters to move: `_location_to_dict` (line 876), `_call_item_to_dict` (line 887), `_diagnostic_to_dict` (line 901)

**queries/common.py** provides sqlglot reusable fragments:
- `bidirectional_edges() -> exp.Union` — the UNION ALL subquery, tested in spike
- `scope_filter(scope: str) -> tuple[exp.Expression, list[Any]]` — LIKE ESCAPE pattern with placeholder
- `visited_tracking_columns(table: str) -> tuple[exp.Expression, exp.Expression]` — list_concat append + list_contains cycle check
- `escape_like(value: str) -> str` — moves from tools.py `_escape_like` (line 1260)

## Implementation

### Step 1: Write validation tests
- **File:** `tests/mcp_server/test_validation.py` (create directory + file)
- **Test class:** `TestRequireParam` — missing returns error dict, present returns None, empty string treated as missing
- **Test class:** `TestClamp` — below min clamps up, above max clamps down, in-range unchanged
- **Run:** `uv run pytest tests/mcp_server/test_validation.py -v`
- **Expected:** ImportError (module doesn't exist)

### Step 2: Implement validation.py
- **File:** `chunkhound/mcp_server/tools/validation.py` (create `tools/` as package first)
- **Create:** `chunkhound/mcp_server/tools/__init__.py` (empty initially)
- **Create:** `chunkhound/mcp_server/tools/queries/__init__.py` (empty initially)
- **Signatures:** `require_param(name: str, value: Any) -> dict[str, str] | None`, `clamp(value: int, min_val: int, max_val: int) -> int`
- **Run:** tests pass

### Step 3: Write formatter tests
- **File:** `tests/mcp_server/test_formatters.py`
- **Test:** `format_node` maps raw DB dict to clean response dict
- **Test:** `format_edge` renames from_fqn→from_symbol, to_fqn→to_symbol
- **Test:** LSP formatters produce expected shapes from Location/CallItem/Diagnostic dicts
- **Run:** `uv run pytest tests/mcp_server/test_formatters.py -v`
- **Expected:** ImportError

### Step 4: Implement formatters.py
- **File:** `chunkhound/mcp_server/tools/formatters.py`
- **Move:** `_location_to_dict`, `_call_item_to_dict`, `_diagnostic_to_dict` from tools.py (extract, don't copy — these become the single source)
- **Add:** `format_node`, `format_edge` (new, currently inline in every graph function)
- **Run:** tests pass

### Step 5: Write query fragment tests
- **File:** `tests/mcp_server/test_query_common.py`
- **Test `bidirectional_edges`:** generates UNION ALL with src/dst aliases, round-trip parses as valid DuckDB
- **Test `scope_filter`:** generates LIKE with ESCAPE and placeholder, returns correct param
- **Test `escape_like`:** %, _, backslash all escaped correctly (existing test_tool_graph.py TestEscapeLike covers this — move or reference)
- **Test `visited_tracking_columns`:** LIST_CONCAT and LIST_CONTAINS expressions generate correct DuckDB SQL
- **Run:** `uv run pytest tests/mcp_server/test_query_common.py -v`
- **Expected:** ImportError

### Step 6: Implement queries/common.py
- **File:** `chunkhound/mcp_server/tools/queries/common.py`
- **Implement:** `bidirectional_edges()`, `scope_filter()`, `visited_tracking_columns()`, `escape_like()`
- **Pattern:** each returns sqlglot expressions, validated by spike in this conversation
- **Run:** tests pass

### Step 7: Run existing test suite
- **Run:** `uv run pytest tests/lsp/test_tool_graph.py tests/lsp/test_tool_search_extensions.py tests/lsp/test_tool_search_structural.py -v`
- **Expected:** all pass (tools.py unchanged at this point, foundations are additive)

### Step 8: Commit
- **Message:** `feat(mcp): add shared foundations — validation, formatters, sqlglot query fragments`

## Success Criteria

- [x] `validation.py` exists with `require_param`, `clamp`, tested
- [x] `formatters.py` exists with `format_node`, `format_edge`, LSP formatters, tested
- [x] `queries/common.py` exists with `bidirectional_edges`, `scope_filter`, `visited_tracking_columns`, `escape_like`, tested
- [x] `tools/` package directory created with `__init__.py`
- [x] Existing tests unaffected
- [x] Committed and pushed

## Anti-Patterns

- Don't remove functions from tools.py yet — that happens in the domain tasks (ch-ei8, ch-p1r, ch-c0w)
- Don't create abstractions without tests proving they work
- Don't over-generalize formatters — only extract what's currently repeated
