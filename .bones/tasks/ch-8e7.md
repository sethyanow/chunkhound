---
id: ch-8e7
title: LSP + Graph Intelligence Layer
status: open
type: epic
priority: 1
depends_on: [ch-7j0, ch-0um, ch-zyz, ch-dar, ch-z2o, ch-bf0]
---













## Requirements (IMMUTABLE)

R1. Standalone LSP client manager module — asyncio JSON-RPC over stdio, config-driven server registry, capability gating, connection pooling. Zero ChunkHound imports. Server configs for all languages with existing tree-sitter grammars (pyright first, then all others).

R2. DuckDB schema additions — `symbols` table (fqn, name, kind, language, file_id, file_path, range_start, range_end, type_signature, parent_fqn, confidence, lsp_server) and `symbol_edges` table (from/to symbol_id + fqn + file, edge_kind, confidence, lsp_server). Symbol-to-chunk resolution via file_id + line range overlap, no FK.

R3. Background index-time population — runs after tree-sitter chunking completes. 8 LSP operations: documentSymbol, workspaceSymbol, definition, references, implementation, incomingCalls, outgoingCalls, hover. Populates symbols + edges + type_signatures. Wired into file watcher for incremental (file changed → delete its symbols/edges → repopulate).

R4. Primitive MCP tools — `lsp(file, line, character, operation)` unified LSP tool (definition|references|implementations|callers|callees|hover|diagnostics). `graph(operation, ...)` unified graph queries (walk|reachability|boundary|overview). `symbol_context(file, line, character)` compound symbol profile. `lsp_status()` server health.

R5. Extended existing tools — `search` gains `type: symbols|structural` and `type_filter` parameter. `get_stats` gains graph + LSP data.

R6. Fusion MCP tools — `test_targeting(changed_files_or_symbols)` returns minimal test set. `impact_cascade(file, line, char, depth)` returns annotated transitive caller tree with type signatures. `cross_language_check(scope_a, scope_b)` returns binding mismatches. `semantic_diff(base, head)` returns changed behaviors + affected callers classified.

R7. Graph walk expander in search pipeline — structural expansion alongside MultiHopStrategy in UnifiedSearch. Seed chunks → lookup symbol FQNs → walk symbol_edges 1-2 hops → resolve to chunks via file_id + range overlap → feed into existing reranker. Independent of semantic expander.

R8. code_research graph-aware prompt templates — templates in `chunkhound/services/research/prompts/graph_patterns/` that code_research's BFS pulls for structural sub-questions during exploration.

R9. Skill script library — 5 executable skills following mcp-code-execution-enhanced pattern (SKILL.md + workflow.py symlink → scripts/): coverage-diff, safe-to-delete, migration-plan, translate-tests, architecture-query. Scripts call ChunkHound MCP tools via `call_mcp_tool()`. LLM-requiring steps output structured data for agent synthesis.

R10. Infrastructure fixes — semantic search path scoping tightened to strict prefix matching in DuckDBProvider.search_semantic(). Git-aware indexing in RealtimeIndexingService (index reflects committed + staged state).

## Success Criteria

- [ ] LSP client manager spawns, initializes, and queries pyright + at least 3 other language servers
- [ ] `symbols` and `symbol_edges` tables populated for a multi-language codebase
- [ ] All 4 primitive MCP tools operational (lsp, graph, symbol_context, lsp_status)
- [ ] search(type: symbols) and search(type: structural) return results with type_filter support
- [ ] All 4 fusion tools return correct results (test_targeting, impact_cascade, cross_language_check, semantic_diff)
- [ ] Graph walk expander produces chunks in code_research that semantic search alone misses
- [ ] Prompt templates used by code_research BFS for structural sub-questions
- [ ] All 5 skill scripts executable via workflow.py with SKILL.md discovery
- [ ] Path scoping fix prevents semantic search result leakage across directories
- [ ] Git-aware indexing reflects staged state
- [ ] Incremental population: file change triggers symbol/edge refresh for that file only
- [ ] All existing tests still pass — zero regression

## Anti-Patterns (FORBIDDEN)

- NO print() in MCP server code (existing rule — stdio transport)
- NO single-row DB inserts in loops (existing rule — batch symbols/edges)
- NO tree-sitter removal or modification — LSP supplements, never replaces
- NO LSP calls blocking the tree-sitter indexing pipeline — background population only
- NO tool schemas that leak implementation details — agents see clean parameters, not internal LSP JSON-RPC framing
- NO hardcoded language server paths — config-driven registry with capability gating
- NO LLM calls inside MCP tools — fusion tools are deterministic. LLM synthesis belongs in skill scripts or agent context
- NO symbol-to-chunk FK — resolution via file_id + line range overlap (symbols span chunk boundaries)

## Approach

Tree-sitter continues driving cAST chunking (proven, fast, in-process). LSP is an additive layer providing: (1) a persistent symbol index with type signatures, (2) a pre-computed dependency edge graph, (3) live navigation via MCP tools, (4) structural expansion in the search pipeline, and (5) a skill script library for higher-level analysis.

Inspired by Muvon/octocode's GraphRAG approach but using LSP at index time (faster and more accurate than LLM-based relationship discovery) with both file-level and symbol-level edges. Octocode uses LLM for complex relationship discovery; we use LSP's compiler-grade structural analysis instead.

MCP tools are designed as a clean primitive surface area. Higher-level analysis patterns that need LLM synthesis are skill scripts following the mcp-code-execution-enhanced progressive disclosure pattern (SKILL.md + workflow.py → script). Users grow the skill library by writing new scripts that compose the same primitives.

## Architecture

### Data Flow

```
File change → tree-sitter chunking (fast, existing) → chunks + embeddings (immediate search)
           → LSP background pass (after TS completes) → symbols + edges + type_signatures
```

### Component Map

```
LSP Client Manager (standalone, zero CH imports)
  ├── Server Registry (language_id → {command, args})
  ├── Connection Pool (one process per (language, workspace_root))
  ├── Capability Cache (gate calls against initialize response)
  └── 9 operations (documentSymbol, workspaceSymbol, definition,
      references, implementation, incomingCalls, outgoingCalls, hover, diagnostics)

DuckDB (existing + new tables)
  ├── files, chunks, embeddings_<N> (existing, untouched)
  ├── symbols (fqn, kind, type_signature, range, confidence)
  └── symbol_edges (from/to fqn + symbol_id, edge_kind, confidence)

MCP Tools
  ├── search (existing, extended: symbols|structural + type_filter)
  ├── code_research (existing, enhanced: graph-aware prompt templates)
  ├── lsp (new: unified LSP operations)
  ├── graph (new: walk|reachability|boundary|overview)
  ├── symbol_context (new: compound symbol profile)
  ├── lsp_status (new: server health)
  ├── test_targeting (new: minimal test set)
  ├── impact_cascade (new: transitive caller tree)
  ├── cross_language_check (new: binding mismatches)
  └── semantic_diff (new: behavior-level PR review)

Search Pipeline
  └── UnifiedSearch
      ├── Semantic expansion (existing MultiHopStrategy)
      └── Structural expansion (new GraphWalkExpander)
          → both feed existing reranker

Skill Script Library
  ├── coverage-diff/    (SKILL.md + workflow.py → scripts/)
  ├── safe-to-delete/
  ├── migration-plan/
  ├── translate-tests/
  └── architecture-query/
```

### Symbol → Chunk Resolution

No FK. Query-time range overlap:
```sql
SELECT c.id FROM chunks c
WHERE c.file_id = :symbol_file_id
  AND c.start_line <= :symbol_range_end
  AND c.end_line >= :symbol_range_start
```

## Phases

### Phase 1: Foundation
**Scope:** R1, R2, R10
**Gate:**
- `uv run pytest tests/test_lsp_client.py -v` → all pass
- `uv run chunkhound index /path/to/project && python -c "import duckdb; conn = duckdb.connect('.chunkhound/db/chunks.db'); print(conn.execute('SELECT COUNT(*) FROM symbols').fetchone())"` → non-zero count
- Semantic search with path filter returns ONLY results within that path (no leakage)

### Phase 2: Index Population
**Scope:** R3
**Gate:**
- After indexing a multi-language project, `symbols` and `symbol_edges` tables populated for all configured languages
- File watcher change triggers incremental symbol/edge refresh (verify with before/after counts)
- `uv run pytest tests/test_lsp_population.py -v` → all pass

### Phase 3: Primitive Tools
**Scope:** R4, R5
**Gate:**
- Each of lsp, graph, symbol_context, lsp_status callable via MCP and returning correct results
- `search(type: symbols, query="parse")` returns symbol results
- `search(type: structural, query="error handling", type_filter="Result")` returns type-filtered results
- `uv run pytest tests/test_mcp_tools_lsp.py -v` → all pass

### Phase 4: Fusion Tools
**Scope:** R6
**Gate:**
- `test_targeting` returns correct test subset for known changed symbols
- `impact_cascade` returns multi-hop caller tree with type signatures
- `cross_language_check` detects known binding mismatches in a test fixture
- `semantic_diff` classifies mechanical vs logic changes for a known diff
- `uv run pytest tests/test_fusion_tools.py -v` → all pass

### Phase 5: Search Pipeline + code_research
**Scope:** R7, R8
**Gate:**
- `code_research` query on a structural topic returns chunks from graph walk that semantic-only search misses
- Prompt templates used during BFS (visible in research events/logs)
- `uv run pytest tests/test_graph_expander.py -v` → all pass

### Phase 6: Skill Script Library
**Scope:** R9
**Gate:**
- All 5 skill scripts executable via `workflow.py --help` (shows params)
- Each SKILL.md discoverable by Claude Code skill system
- At least one skill (coverage-diff) produces correct structured output against a test fixture
- `uv run pytest tests/test_skill_scripts.py -v` → all pass

## Agent Failure Mode Catalog

### Phase 1
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Hardcode pyright-only, defer other languages | "Get it working first" | R1 explicitly requires all tree-sitter-supported languages. Config is mechanical. |
| Skip path scoping fix | "We'll fix it later, not related to LSP" | R10 is in Phase 1 scope. Gate predicate tests for leakage. |
| Use tree-sitter for symbol extraction instead of LSP | "It's faster and already available" | Anti-pattern: tree-sitter supplements, not replaces. LSP provides type_signature, FQN, cross-file resolution. |

### Phase 2
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Skip hover at index time | "Only needed at query time" | R3 explicitly lists hover. type_signature column depends on it. |
| Inline LSP calls in tree-sitter pipeline | "More efficient" | Anti-pattern: NO LSP calls blocking tree-sitter pipeline. Background only. |
| Skip incremental wiring | "Full reindex works" | Gate tests incremental refresh with before/after counts. |

### Phase 3
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Expose raw LSP JSON-RPC in tool responses | "It's what the server returns" | Anti-pattern: no implementation details in tool schemas. Clean structured responses. |
| Skip type_filter on search | "Agents can filter client-side" | R5 requires type_filter parameter. Gate tests it explicitly. |

### Phase 4
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Use LLM in fusion tools | "Better classification" | Anti-pattern: fusion tools are deterministic. LLM belongs in skill scripts. |
| Return raw graph data without type annotation | "Agent can call hover separately" | R6 specifies type signatures in impact_cascade output. |

### Phase 5
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Skip prompt templates, just add graph calls to search | "Same effect" | R8 requires templates in graph_patterns/. code_research BFS uses them for structural sub-questions specifically. |

### Phase 6
| Shortcut | Rationalization | Pre-block |
|----------|----------------|-----------|
| Write skills as Claude Code plugin skills (markdown only) | "Simpler" | R9 requires executable scripts following mcp-code-execution-enhanced pattern. SKILL.md + workflow.py symlink. |
| Skip SKILL.md discovery metadata | "Agent can read the script" | Progressive disclosure: SKILL.md is ~60 tokens to discover. Script internals never enter agent context. |

## Seam Contracts

### Phase 1 → Phase 2
**Delivers:** LSP client manager with all language configs, DuckDB tables created, infrastructure fixes applied
**Assumes:** Phase 2 can call `lsp_client.document_symbols(file)` etc. and write to `symbols`/`symbol_edges` tables
**If wrong:** Population service can't write data. Rework is schema changes + client API changes.

### Phase 2 → Phase 3
**Delivers:** Populated `symbols` and `symbol_edges` tables with data from LSP
**Assumes:** Phase 3 MCP tools query these tables and call the LSP client for live operations
**If wrong:** Tools return empty results. Rework is query logic, not schema.

### Phase 3 → Phase 4
**Delivers:** Working primitive MCP tools (lsp, graph, symbol_context, search extensions)
**Assumes:** Phase 4 fusion tools compose primitives internally
**If wrong:** Fusion tools can't gather data. Rework is primitive tool parameter design.

### Phase 4 → Phase 5
**Delivers:** Full MCP tool surface (primitives + fusion)
**Assumes:** Phase 5 graph walk expander uses same graph queries as `graph` tool. code_research has access to graph data.
**If wrong:** Expander needs different query patterns. Rework is expander logic, not tools.

### Phase 5 → Phase 6
**Delivers:** Enhanced search pipeline + code_research with graph awareness
**Assumes:** Phase 6 skill scripts call MCP tools externally via `call_mcp_tool()`
**If wrong:** Tool API mismatch. Rework is script call signatures.

## Design Rationale

### Problem
Agents bounce between LSP (structural: types, call chains, references) and semantic search (conceptual: patterns by meaning), manually cross-referencing results. Neither alone answers the cross-cutting questions agents actually ask during autonomous coding.

### Research Findings
**Codebase:** Tree-sitter deeply embedded across TreeSitterEngine, ConceptExtractor, UniversalParser (3+ layers). MCP tools registered via @register_tool decorator in tools.py. DuckDB schema: files, chunks, embeddings_<N>. Indexing pipeline: file watcher → process_file → tree-sitter parsing (CPU-parallel) → DuckDB write → embedding generation. Research service: multi-hop vector expansion + BFS exploration + gap detection with reranker requirement.

**External:** Muvon/octocode builds GraphRAG with rule-based import parsing + optional LLM at index time, LSP at query time only. We use LSP at both index and query time — faster and more accurate than LLM for relationship discovery.

### Approaches Considered

#### 1. LSP supplements tree-sitter (selected)
**Chosen because:** Zero risk to existing chunking quality. LSP provides additive capabilities (type signatures, FQN, cross-file edges) that tree-sitter doesn't. Both data sources are maintained independently.

#### 2. Replace tree-sitter with LSP
**Why explored:** Research brief proposed it as simpler architecture (one parsing backend).
**REJECTED BECAUSE:** Tree-sitter provides AST structure, S-expression queries, and sibling merge data that LSP documentSymbol doesn't. The cAST algorithm fundamentally relies on tree structure for greedy merge. Quality regression when LSP unavailable (heuristic line-window fallback loses all semantic structure).
**DO NOT REVISIT UNLESS:** LSP gains AST-level tree structure responses beyond documentSymbol.

#### 3. LLM-based relationship discovery (octocode pattern)
**Why explored:** Octocode uses optional LLM to discover complex relationships (dependency injection, factory patterns).
**REJECTED BECAUSE:** LSP is faster, cheaper, deterministic, and provides compiler-grade accuracy for the same relationship types. LLM adds latency and cost at index time.
**DO NOT REVISIT UNLESS:** Relationship types needed that LSP cannot detect (e.g., runtime-only patterns, convention-based coupling).

### Scope Boundaries
**In scope:** LSP client, symbol/edge tables, MCP tools (primitive + fusion), graph walk in search, code_research enhancement, skill script library, path scoping fix, git-aware indexing, all language server configs
**Out of scope:** Changes to embedding providers/logic, changes to existing MCP tools' behavior, tree-sitter modifications, AI memory/commit tooling, graph community detection or LLM-over-graph summarization

### Open Questions
- Exact `call_mcp_tool()` integration pattern for skill scripts (depends on how ChunkHound's MCP server is exposed to local scripts)
- Whether skill scripts should live in ChunkHound's repo or a companion plugin repo
- Specific language server commands/args for less common languages (will be resolved during Phase 1)

## Design Discovery

### Key Decisions Made
| Question | Answer | Implication |
|----------|--------|-------------|
| Replace or supplement tree-sitter? | Supplement | No swap point needed. No fallback heuristic. No quality regression risk. |
| Graph granularity? | Both file-level and symbol-level | Richer edges than octocode's file-only approach. Leverages ChunkHound's existing symbol-level chunking. |
| Relationship discovery method? | LSP over LLM | Deterministic, compiler-grade, faster. No LLM cost at index time. |
| Index-time strategy? | Background pass after tree-sitter | Users get search immediately. Graph data populates async. |
| Symbol-to-chunk relationship? | Range overlap query, no FK | Handles symbols spanning multiple chunks. No stale references. Independent pipelines. |
| hover at index time? | Yes, for type_signature | Rust/TS/Go type trees pre-indexed. Agents don't deep-dive tracing types. |
| Tool architecture? | Primitives (MCP) + recipes (skill scripts) | Clean tool surface, growing skill library, progressive disclosure. |
| What's a tool vs skill? | Deterministic = tool, LLM-requiring = skill | 8 new MCP tools handle basic structural questions. 5 skills handle analytical synthesis. |
| Fork vs branch? | Personal fork, already set up | Work in existing repo. |

### Dead-End Paths
- **Full tree-sitter replacement:** Explored because research brief proposed it. Abandoned when codebase audit revealed tree-sitter is used at 3+ layers (AST parsing, query compilation, cAST merge decisions), not a single swap point. LSP documentSymbol doesn't provide the tree structure cAST needs.
- **LSP at index time for every symbol's definition + references (research brief proposal):** Would mean 500K+ LSP round-trips for large codebases. Revised to background pass with all 8 operations, but still scoped to one documentSymbol call per file + targeted definition/references/hover per symbol.
- **25 individual MCP tools:** Original design had 11 primitives + 8 fusion + 6 analytical. Revised to minimal primitive surface (4 new + 2 extended + 4 fusion) + skill library for analytical patterns. Progressive disclosure principle.

### Open Concerns
- LSP server startup time may delay background population. Mitigation: population service waits for server readiness, doesn't block user-facing operations.
- Cross-language edge quality depends on language server accuracy for cross-file resolution. Some servers (rust-analyzer, pyright) are excellent; others may be partial. Confidence field captures this.
- Skill script `call_mcp_tool()` pattern needs a local MCP client or HTTP endpoint. ChunkHound already has HTTP server mode (`chunkhound mcp http --port 5173`), so scripts can call it via HTTP.
