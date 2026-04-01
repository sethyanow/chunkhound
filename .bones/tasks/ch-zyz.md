---
id: ch-zyz
title: 'Phase 3: Primitive MCP Tools'
status: open
type: epic
priority: 1
depends_on: [ch-0um, ch-1ed, ch-dlx, ch-r4a]
parent: ch-8e7
---











## Context
Parent epic ch-8e7, Phase 3. Depends on Phase 2 (ch-0um).
Phase 2 delivers populated symbols and symbol_edges tables. This phase exposes that data (and live LSP) as MCP tools agents can call directly.

## Requirements
Scoped to parent epic R4, R5:
- R4: Primitive MCP tools (lsp, graph, symbol_context, lsp_status)
- R5: Extended existing tools (search gains symbols|structural + type_filter, get_stats gains graph data)

## Success Criteria
- [x] `lsp` tool callable with all 7 operations (definition, references, implementations, callers, callees, hover, diagnostics)
- [x] `lsp` tool returns clean structured responses (no raw JSON-RPC framing)
- [x] `lsp` tool capability-gates: calling an unadvertised operation returns a clear error, not crash
- [x] `graph` tool supports walk, reachability, boundary, overview operations
- [x] `graph(walk)` returns symbols + edges traversed, respects depth and edge_kind filters
- [x] `graph(reachability)` identifies unreachable symbols from specified roots
- [x] `graph(boundary)` returns cross-scope violations
- [x] `graph(overview)` returns most-connected types with relationship summary
- [ ] `symbol_context` returns hover + definition + callers + callees + graph neighborhood in one response
- [x] `lsp_status` returns per-server state, capabilities, readiness
- [ ] `search(type: symbols)` queries symbols table with kind, language, path, fqn pattern filters
- [ ] `search(type: structural)` does semantic search + graph walk expansion + unified rerank
- [ ] `search` `type_filter` parameter filters results by type_signature content
- [ ] `get_stats` includes symbol count, edge count, per-language breakdown, LSP server status
- [ ] All tools follow existing `@register_tool` pattern with proper schema generation
- [ ] All existing MCP tools unchanged in behavior (zero regression)
- [ ] `uv run pytest tests/test_mcp_tools_lsp.py -v` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
Inherited from parent epic, plus:
- NO raw LSP JSON-RPC in tool responses — translate to clean domain types
- NO tool that requires agents to understand LSP protocol details
- NO changes to existing tool signatures (search regex|semantic, code_research, etc.)

## Key Considerations
- `lsp` tool needs the LSP client manager injected. Add `lsp_manager` to `execute_tool()` signature inspection alongside existing `embedding_manager`, `llm_manager`.
- `graph` operations query DuckDB only (no live LSP). Fast, deterministic.
- `search(type: structural)` is the first integration point between semantic search and the graph — it calls the existing search pipeline then enriches with graph data.
- `type_filter` does substring matching on `symbols.type_signature`. Agents pass type names like "Result", "GraphRead", etc.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: new MCP tools documented with parameter descriptions
- [ ] AGENTS.md updated: tool descriptions for LLM routing

**User Walkthrough Must Cover:**
- Call `lsp` with definition operation on a known symbol → returns correct file + range
- Call `graph(walk)` from a function → returns callers/callees with edge kinds
- Call `search(type: symbols, query="parse")` → returns matching symbols
- Call `search(type: structural, query="error handling", type_filter="Result")` → returns type-filtered results
- Call `symbol_context` on a function → returns compound profile in one call
