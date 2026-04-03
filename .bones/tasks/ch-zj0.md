---
id: ch-zj0
title: test_targeting — minimal test set from changed symbols
status: open
type: task
priority: 1
parent: ch-dar
---

## Context
Second Phase 4 fusion tool. Given changed files or symbols, walks their caller
graph and intersects with test entry points to return the minimal test set.
Reuses `_graph_walk(directed=True)` and `@register_tool` patterns from ch-ef9.

**Blocked by:** ch-ef9 (impact_cascade, closed — delivers directed walk)
**Unlocks:** `semantic_diff` (which needs test_targeting to classify affected tests)

## Requirements
From ch-dar (Phase 4 epic) success criteria:
- `test_targeting(changed_files_or_symbols)` returns minimal test set by walking callers graph and intersecting with test entry points
- Identifies test files via path patterns + symbol kind, not hardcoded paths
- Deterministic — no LLM, no embeddings
- Structured data output, not prose

## Design

**Composition:** `_resolve_changed_to_fqns` → `_collect_test_fqns` → per-symbol `_graph_walk(directed=True, edge_kind="called_by")` → intersect reachable with test set → structured output

**Key decisions:**
1. Input heuristic: strings containing `"::"` are FQNs, others are file paths — file paths resolved via symbols table
2. Test identification: `kind = 'Function' AND name LIKE 'test_%'` with optional `test_scope` path filter — not hardcoded to `tests/` directory
3. Per-symbol walk: call `_graph_walk` for each changed symbol, merge reachable nodes into a union set, then intersect with test FQNs
4. Walk uses `directed=True` — only follows callers, not callees (same rationale as impact_cascade)
5. `limit=100` per walk (same as impact_cascade), `depth` clamped 1-10
6. Output includes `hop_distance` per test — closest caller chain length from any changed symbol

**Output shape:**
```json
{
  "changed_symbols": ["mod::func_a", "mod::func_b"],
  "tests": [
    {"fqn": "test_mod::test_func_a", "name": "test_func_a", "file_path": "tests/test_mod.py", "hop_distance": 2}
  ],
  "total_tests": 1,
  "walk_depth": 3
}
```

## Implementation

### Step 1: Write failing test — resolve changed inputs to FQNs
File: `tests/mcp_server/test_fusion_tools.py` (extend)
Test class: `TestResolveChangedToFqns`
- Test: file path input → queries all symbols in that file, returns FQN list
- Test: FQN string input (contains "::") → passes through unchanged
- Test: mixed list of file paths and FQNs → resolves both
- Test: file with no indexed symbols → excluded from result (no error)
Mock: `services.provider.execute_query` returns canned symbol rows

### Step 2: Implement `_resolve_changed_to_fqns`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_resolve_changed_to_fqns(services: Any, changed: list[str], workspace_root: str) -> list[str]`
Heuristic: strings containing `"::"` are FQNs, others are file paths. File paths: normalize to relative via `os.path.relpath` (same as `_resolve_start_fqn`), then `SELECT DISTINCT fqn FROM symbols WHERE file_path = ?`. Return deduplicated FQN list.

### Step 3: Write failing test — collect test entry point FQNs
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestCollectTestFqns`
- Test: returns FQNs of Function symbols with `test_` name prefix
- Test: respects `test_scope` path filter (e.g., "tests/")
- Test: excludes non-Function symbols even if name matches (e.g., Variable named `test_data`)
Mock: `services.provider.execute_query` returns mixed kinds

### Step 4: Implement `_collect_test_fqns`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `_collect_test_fqns(services: Any, test_scope: str | None = None) -> set[str]`
Query: `SELECT fqn FROM symbols WHERE kind = 'Function' AND name LIKE 'test_%'` + optional `AND file_path LIKE ?` scope filter with escaped LIKE pattern. Returns set for O(1) intersection.

### Step 5: Write failing test — full tool integration
File: `tests/mcp_server/test_fusion_tools.py`
Test class: `TestTestTargetingImpl`
- Test: changed symbol has callers that are test functions → returns those tests with hop_distance
- Test: changed symbol has no test callers → returns empty tests list
- Test: changed file → resolves to symbols, walks callers, finds tests
- Test: tool registered in TOOL_REGISTRY
Mock: sequential `execute_query` calls (resolve changed, collect tests, walk nodes per symbol, walk edges per symbol)

### Step 6: Implement `test_targeting_impl`
File: `chunkhound/mcp_server/tools/fusion.py`
Signature: `async def test_targeting_impl(services: Any, config: Any, changed: list[str], depth: int = 3, test_scope: str | None = None) -> dict[str, Any]`
`@register_tool(name="test_targeting")`. Clamp depth 1-10. Compose: resolve changed → collect test FQNs → for each changed symbol, directed walk → union all reachable FQNs → intersect with test set → build output with hop_distance (minimum across all walks).

### Step 7: Wire — update expected tool count
File: `tests/mcp_server/test_adversarial_decomp.py`
Add `"test_targeting"` to `EXPECTED_TOOLS` set.

### Step 8: Verify live via MCP
Call `test_targeting` with a known changed file. Verify output shape.

## Success Criteria
- [ ] `_resolve_changed_to_fqns` handles file paths, FQNs, and mixed input
- [ ] `_collect_test_fqns` identifies test functions by kind + name pattern, not hardcoded paths
- [ ] `_collect_test_fqns` respects optional `test_scope` path filter
- [ ] `test_targeting_impl` registered in TOOL_REGISTRY via `@register_tool`
- [ ] Output is structured: `{changed_symbols, tests: [{fqn, name, file_path, hop_distance}], total_tests, walk_depth}`
- [ ] Returns empty tests list (not error) when no test callers found
- [ ] Depth clamped 1-10, default 3
- [ ] Zero LLM/embedding calls — deterministic only
- [ ] All new code has failing tests before implementation
- [ ] `uv run pytest tests/mcp_server/test_fusion_tools.py -v` → all pass

## Key Considerations
- Input heuristic (`"::"` = FQN, else file path) is imperfect — FQNs without `::` (top-level symbols) would be treated as files. Acceptable for v1 since top-level test functions aren't typically "changed symbols" in the calling pattern.
- Walk per-symbol can be expensive for many changed symbols. `limit=100` per walk bounds individual calls, but N symbols × 100 = up to N×100 DB queries. For typical use (5-20 changed symbols), this is fine.
- `hop_distance` in output is the MINIMUM distance from any changed symbol — if test_X is at hop 2 from symbol A and hop 3 from symbol B, report 2.
- Test identification uses DB-stored `name` field from LSP `documentSymbol` — reliable for Python (`test_` prefix), may need language-specific patterns for other languages (future concern).
- Cycle detection is inherited from `_graph_walk`'s CTE visited list + `_build_caller_tree`'s visited set (ch-ef9 fix).

## Anti-Patterns
- NO hardcoded test directory paths — use `kind + name LIKE` pattern, configurable via `test_scope`
- NO live LSP calls — use indexed symbols table only
- NO LLM/embedding calls — deterministic graph + DB composition
- NO `execute_tool` dispatch — direct function calls for internal composition
- NO prose output — structured dicts only
