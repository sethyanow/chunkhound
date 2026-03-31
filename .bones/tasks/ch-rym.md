---
id: ch-rym
title: Decompose test_lsp_population.py (3k+ lines) into cohesion-based test files
status: open
type: task
priority: 1
parent: ch-0um
---




## Context
`tests/test_lsp_population.py` has 68 tests across ~25 classes covering unrelated concerns (core population, edges, workspace symbols, type signatures, wiring, resilience). Grew across multiple tasks without decomposition.

## Requirements
1. Split along cohesion seams — each file covers one concern
2. Shared helpers extracted to a common module (no duplication across files)
3. All tests pass after split (zero behavior change)

## Success Criteria
- [ ] Each test file covers a single cohesion seam
- [ ] Shared helpers live in one place, imported by all
- [ ] All tests pass after split
- [ ] `uv run pytest tests/test_lsp_population*.py -v` collects all tests

## Anti-Patterns
- NO behavior changes — pure code motion
- NO renaming test classes or methods (breaks git blame)
