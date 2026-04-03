---
id: ch-ef9
title: impact_cascade — transitive caller tree with type annotations
status: open
type: task
priority: 1
parent: ch-dar
---

## Context
First Phase 4 fusion tool. Composes Phase 3 primitives (`_graph_walk` + symbols table)
to answer "what breaks if I change this symbol?" in a single MCP call. Foundation for
`test_targeting` and `semantic_diff` which both consume its output.

**Blocked by:** ch-zyz (Phase 3, closed)
**Unlocks:** `test_targeting` and `semantic_diff` (both compose `impact_cascade`)

## Requirements
From ch-dar (Phase 4 epic) success criteria:
- `impact_cascade(file, line, char, depth)` returns transitive caller tree with hop distance + type signatures
- Each node annotated with enough info for agent to classify mechanical vs logic change
- Deterministic — no LLM, no embeddings
- Structured data output, not prose

## Design

**Composition:** `_resolve_start_fqn` → `_graph_walk(edge_kind="called_by")` → `_annotate_type_signatures` → `_build_caller_tree`

**Key decisions:**
1. Compose `_graph_walk` via direct import, not `execute_tool` — same process, no dispatch overhead
2. Type signatures from `symbols.type_signature` column (populated at index time), not live LSP hover
3. Tree reconstruction in Python from flat walk results (nodes + edges)
4. Input is 1-based (per Bug #2 fix), converted to 0-based for DB range queries
5. New file `chunkhound/mcp_server/tools/fusion.py` — all fusion tools share this module

**Output shape:**
```json
{
  "root": {
    "fqn": "mod::func",
    "name": "func",
    "kind": "Function",
    "file_path": "src/mod.py",
    "hop_distance": 0,
    "type_signature": "(...) -> Result",
    "children": [
      {
        "fqn": "caller::method",
        "hop_distance": 1,
        "type_signature": "...",
        "children": [...]
      }
    ]
  },
  "total_nodes": 15,
  "max_depth": 3
}
```

## Implementation

### Step 1: Write failing test — FQN resolution from position
File: `tests/mcp_server/test_fusion_tools.py` (new)
Test: given a symbol in `symbols` table at a known position, `_resolve_start_fqn(services, file, line, char)` returns its FQN. Verify 1-based input → 0-based DB conversion. Test position outside any symbol → returns error dict.

### Step 2: Implement `_resolve_start_fqn`
File: `chunkhound/mcp_server/tools/fusion.py` (new)
Signature: `_resolve_start_fqn(services: Any, file: str, line: int, character: int) -> str | dict[str, Any]`
FQN lookup from `symbol_context_impl` pattern. Returns FQN string or error dict. Converts 1-based line to 0-based for DB.

### Step 3: Write failing test — type signature decoration
Test: given walk result nodes, `_annotate_type_signatures(services, nodes)` adds `type_signature` from DB. Nodes without signatures get `None`.

### Step 4: Implement `_annotate_type_signatures`
Signature: `_annotate_type_signatures(services: Any, nodes: list[dict]) -> list[dict]`
Query: `SELECT fqn, type_signature FROM symbols WHERE fqn IN (...)`. Merge into node dicts.

### Step 5: Write failing test — tree reconstruction
Test: given flat nodes with `depth` and edges, `_build_caller_tree(root_fqn, nodes, edges)` returns nested dict with `children` and `hop_distance`. Root at depth 0, callers at 1, transitive at 2.

### Step 6: Implement `_build_caller_tree`
Signature: `_build_caller_tree(root_fqn: str, nodes: list[dict], edges: list[dict]) -> dict[str, Any]`
Adjacency map from edges, BFS from root. Each node: `{fqn, name, kind, file_path, hop_distance, type_signature, children}`.

### Step 7: Write failing test — full tool integration
Test: mock `services.provider.execute_query` with canned data. Call `impact_cascade_impl`. Verify output shape, `@register_tool` registration in `TOOL_REGISTRY`.

### Step 8: Implement `impact_cascade_impl`
Signature: `async def impact_cascade_impl(services: Any, file: str, line: int, character: int, depth: int = 3) -> dict[str, Any]`
`@register_tool(name="impact_cascade")`. Clamp depth 1-10. Compose the 4 helpers.

### Step 9: Wire into `__init__.py`
Add `from . import fusion as fusion  # noqa: F401` to domain module imports.

### Step 10: Verify live via MCP
Call `impact_cascade` on a known central function via MCP tool. Verify tree, hop distances, type signatures.

## Success Criteria
- [ ] `_resolve_start_fqn` converts 1-based input, returns FQN or error dict
- [ ] `_annotate_type_signatures` batch-queries type_signature from symbols table
- [ ] `_build_caller_tree` reconstructs hierarchical tree from flat walk output
- [ ] `impact_cascade_impl` registered in TOOL_REGISTRY via `@register_tool`
- [ ] Output is structured tree: `{root: {fqn, hop_distance, type_signature, children}, total_nodes, max_depth}`
- [ ] Depth clamped 1-10, default 3
- [ ] Zero LLM/embedding calls — deterministic only
- [ ] All new code has failing tests before implementation
- [ ] `uv run pytest tests/mcp_server/test_fusion_tools.py -v` → all pass

## Anti-Patterns
- NO live LSP hover for type signatures — use symbols.type_signature column
- NO `execute_tool` dispatch — direct function calls for internal composition
- NO prose output — structured dicts only
- NO reimplementing walk traversal — compose `_graph_walk` from graph.py
