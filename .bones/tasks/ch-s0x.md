---
id: ch-s0x
title: 'Bug: graph(walk) returns 0 edges — CTE only follows outbound, but edges are inbound'
status: closed
type: bug
priority: 2
parent: ch-mtq
---







## Requirements

Fix the graph walk operation so it traverses edges in both directions (inbound and outbound), returning connected symbols regardless of edge directionality.

## Context

**Reproduction:** `graph(walk, symbol="LSPClient::start")` returns 0 edges despite `graph(overview)` showing 28 edges for the same symbol.

**Root cause (from handoff diagnosis):** The walk CTE joins only on `from_fqn` (outbound edges), but LSP population via `findReferences` creates edges where references point TO the symbol (`to_fqn = 'LSPClient::start'`), not FROM it. The walk never sees inbound edges.

**Scope:** Affects every symbol, not just LSPClient. Any symbol whose edges are primarily inbound (which is the common case for `findReferences`-sourced edges) returns 0 or near-0 results from walk.

**Discovered during:** Phase 3 acceptance demo run (ch-tox).

## Success Criteria

- [x] `graph(walk, symbol=X)` traverses both inbound and outbound edges (inline UNION ALL, committed f0522c44, verified via live MCP: 20 nodes, 42 edges for LSPClient::start)
- [x] Regression coverage: query builder tests in ch-ei8 will validate bidirectional structure via sqlglot round-trip; broader MCP tool testing gap tracked by ch-mtq
- [x] Existing graph tests pass (mock-based, unaffected by SQL change)

## Log

- [2026-04-02T05:16:39Z] [Seth] Diagnosis: walk CTE (tools.py:1347) joins e.from_fqn = r.fqn only. Edge population (lsp_population.py:456-461) puts the source symbol as from_fqn and reference targets as to_fqn. Walking from a reference site never finds its definition. Fix: bidirectional UNION in the recursive CTE step. Confidence: HIGH.
- [2026-04-02T10:47:41Z] [Seth] Diagnosis verified in new session. Walk CTE base case finds the symbol (depth 0) but recursive step finds 0 outbound edges. Overview shows 28 edges — all inbound (to_fqn=LSPClient::start). Fix: bidirectional join in recursive CTE step. Also: edge query at line 1374 only checks from_fqn/to_fqn within discovered fqn set — that's secondary to the walk direction bug but needs same bidirectional treatment.
- [2026-04-02T16:54:00Z] [Seth] SQL fix committed f0522c44, verified via live MCP (20 nodes, 42 edges). Regression coverage deferred to ch-ei8 query builder tests + ch-mtq broader testing gap. Structural search has same bug — tracked in ch-p1r.
