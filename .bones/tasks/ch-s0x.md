---
id: ch-s0x
title: 'Bug: graph(walk) returns 0 edges — CTE only follows outbound, but edges are inbound'
status: open
type: bug
priority: 2
---


## Requirements

Fix the graph walk operation so it traverses edges in both directions (inbound and outbound), returning connected symbols regardless of edge directionality.

## Context

**Reproduction:** `graph(walk, symbol="LSPClient::start")` returns 0 edges despite `graph(overview)` showing 28 edges for the same symbol.

**Root cause (from handoff diagnosis):** The walk CTE joins only on `from_fqn` (outbound edges), but LSP population via `findReferences` creates edges where references point TO the symbol (`to_fqn = 'LSPClient::start'`), not FROM it. The walk never sees inbound edges.

**Scope:** Affects every symbol, not just LSPClient. Any symbol whose edges are primarily inbound (which is the common case for `findReferences`-sourced edges) returns 0 or near-0 results from walk.

**Discovered during:** Phase 3 acceptance demo run (ch-tox).

## Success Criteria

- [ ] `graph(walk, symbol=X)` traverses both inbound and outbound edges
- [ ] Regression test: walk on a symbol with known inbound edges returns those edges
- [ ] Existing graph tests still pass

## Log

- [2026-04-02T05:16:39Z] [Seth] Diagnosis: walk CTE (tools.py:1347) joins e.from_fqn = r.fqn only. Edge population (lsp_population.py:456-461) puts the source symbol as from_fqn and reference targets as to_fqn. Walking from a reference site never finds its definition. Fix: bidirectional UNION in the recursive CTE step. Confidence: HIGH.
