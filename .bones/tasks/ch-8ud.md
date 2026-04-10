---
id: ch-8ud
title: Redesign live-API embedding tests with frozen corpus and algorithm-level assertions
status: open
type: task
priority: 2
---

## Requirements

Replace `test_dynamic_expansion_real.py` and `test_multi_hop_semantic_search.py` with tests that verify algorithm behavior, not embedding model quality.

## Context

Both files share the same broken pattern:
1. Index real ChunkHound source files → any formatting/refactor shifts embeddings
2. Generate embeddings via live API → non-deterministic, rate-limited, model-version-dependent
3. Assert on hardcoded component counts, score thresholds, min_hops → brittle

Additionally, Pyright shows these tests have: protocol mismatches (DuckDBProvider vs DatabaseProvider), possibly unbound variables (content_analysis from fixture yield inside loop), None attribute access (search/rerank on optional fields).

Remarked from `integration` to `e2e` in this commit to unblock pre-commit hook.

## Design direction

- **Frozen synthetic corpus**: Purpose-built test files with known semantic relationships, stored as fixtures. Never change unless the test intent changes.
- **Algorithm assertions, not quality assertions**: Did multi-hop expand? Did it find results from files not in initial set? Did reranking change ordering? No `min_hops: 2` or score thresholds.
- **FakeEmbeddingProvider for unit coverage**: The deterministic path already exists but uses `relaxed_component_discovery` to weaken assertions. Should test different things instead.
- **Fix all Pyright errors**: Protocol conformance, variable binding, optional access.

## Success Criteria

- [ ] No test depends on live API calls for pass/fail in integration tier
- [ ] Synthetic corpus is a test fixture, not live source code
- [ ] Algorithm behavior tested: expansion occurred, result set grew, reranking permuted
- [ ] All Pyright errors in both files resolved
- [ ] Existing FakeEmbeddingProvider deterministic tests preserved
