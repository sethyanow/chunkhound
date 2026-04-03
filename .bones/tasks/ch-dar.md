---
id: ch-dar
title: 'Phase 4: Fusion MCP Tools'
status: open
type: epic
priority: 1
depends_on: [ch-zyz, ch-ef9, ch-zj0, ch-wo0]
parent: ch-8e7
---











## Context
Parent epic ch-8e7, Phase 4. Depends on Phase 3 (ch-zyz).
Phase 3 delivers the primitive MCP tools. This phase builds fusion tools that combine multiple primitives internally to answer common agent questions in a single call. All fusion tools are deterministic — no LLM.

## Requirements
Scoped to parent epic R6:
- R6: Fusion MCP tools (test_targeting, impact_cascade, cross_language_check, semantic_diff)

## Success Criteria
- [x] `test_targeting(changed_files_or_symbols)` returns minimal test set by walking callers graph and intersecting with test entry points
- [x] `test_targeting` correctly identifies test files via path patterns + symbol kind, not hardcoded paths
- [x] `impact_cascade(file, line, char, depth)` returns transitive caller tree with hop distance + type signatures at each node
- [x] `impact_cascade` annotates each node with enough info for agent to classify mechanical vs logic change
- [x] `cross_language_check(scope_a, scope_b)` compares exported symbols across scopes, returns mismatches in name/signature
- [x] `cross_language_check` works across different languages (e.g., Python bindings vs C extensions)
- [ ] `semantic_diff(base, head)` identifies changed symbols, walks their impact, classifies affected callers
- [ ] `semantic_diff` distinguishes between signature changes and body-only changes
- [ ] All fusion tools return structured data (not prose), suitable for agent consumption or skill script input
- [ ] All fusion tools are deterministic — no LLM calls, no embedding calls
- [ ] `uv run pytest tests/test_fusion_tools.py -v` → all pass
- [ ] `uv run pytest tests/test_smoke.py -v -n auto` → all pass

## Anti-Patterns
Inherited from parent epic, plus:
- NO LLM or embedding calls in fusion tools — they are deterministic graph + LSP compositions
- NO returning unstructured text — fusion tools return typed data structures
- NO reimplementing graph traversal — fusion tools compose the `graph` primitive internally

## Key Considerations
- `test_targeting` needs a way to identify test entry points. Convention: files matching `test_*` or `*_test` patterns, symbols with `test` in name or decorated with test markers. Make this configurable, not hardcoded.
- `impact_cascade` depth parameter prevents runaway traversal. Default depth=3, max depth=10.
- `semantic_diff` needs git integration to determine changed files/symbols between base and head refs. Use pygit2 or git CLI to get the diff, then map changed lines to symbols via range overlap.
- `cross_language_check` compares type_signatures across languages — these may use different type syntax. Comparison is structural (name + arity), not string equality.

## Acceptance Requirements
**Agent Documentation:**
- [ ] CLAUDE.md updated: fusion tool descriptions, example usage
- [ ] AGENTS.md updated: tool descriptions for LLM routing

**User Walkthrough Must Cover:**
- Change a function in a test project → `test_targeting` returns correct subset of tests
- Call `impact_cascade` on a widely-used function → returns multi-level caller tree
- Set up a cross-language fixture (e.g., Python + C extension) → `cross_language_check` finds a deliberate mismatch
- Create a branch with known changes → `semantic_diff` correctly classifies them
