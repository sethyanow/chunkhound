---
id: ch-mtq
title: MCP Tools Decomposition & Quality Gate
status: open
type: epic
priority: 1
depends_on: [ch-e6v, ch-bcw, ch-ei8, ch-p1r, ch-c0w, ch-s0x]
parent: ch-zyz
---













## Context

Phase 3 acceptance shakedown (ch-tox) surfaced ch-s0x: graph(walk) returned 0 edges due to an outbound-only CTE. Root cause: 1813-line `tools.py` mixes param validation, query construction, and result formatting in fat functions. Mocked tests can't catch SQL logic bugs because they sidestep `execute_query` entirely. Structural search has the same outbound-only bug (second location).

The fix exposed a design gap: agent-built code was never decomposed for testability. Before Phase 4 builds on top of this, the foundation needs to be solid.

**Approach:** sqlglot (DuckDB dialect) for query construction. Decompose `tools.py` into domain modules with shared foundations. Test query builders as pure functions. Reflect and rebuild, not code motion.

**Dependency:** sqlglot 30.2.1 already added. All DuckDB patterns verified: recursive CTEs, LIST_CONCAT/LIST_CONTAINS, LIKE ESCAPE, UNION ALL, `?` placeholders.

## Requirements

1. `chunkhound/mcp_server/tools.py` (1813 lines) decomposed into `tools/` package along domain seams
2. Shared foundations: validation, formatters, sqlglot query fragments tested independently
3. Registry extraction preserving external import paths (`__init__.py`, `common.py`, `base.py`)
4. Graph query builders ported to sqlglot with bidirectional edge pattern as reusable fragment
5. Search query builders ported to sqlglot; structural search outbound-only bug fixed
6. LSP tool dispatch rethought; stats SQL ported; research unchanged
7. Query builder tests validate SQL structure via sqlglot round-trip parsing
8. All existing tool tests pass, all smoke tests pass

## Success Criteria

- [x] `tools.py` replaced by `tools/` package — no file over 500 lines (largest: lsp_tools.py 458)
- [x] `queries/common.py` provides `bidirectional_edges()`, `scope_filter()`, `visited_tracking()` as tested sqlglot fragments
- [x] `validation.py` and `formatters.py` extracted with tests
- [x] `registry.py` extracted; `from chunkhound.mcp_server.tools import TOOL_REGISTRY, execute_tool` still works
- [x] Graph query builders are pure functions returning `(sql, params)` via sqlglot
- [x] Search query builders are pure functions; structural search walks bidirectionally
- [x] LSP dispatch chain cleaned up; stats ported (raw SQL — trivial queries)
- [x] Query builder tests use sqlglot parse round-trip for structural assertions
- [x] `uv run pytest tests/lsp/ -v` — all pass (395 passed across lsp/ + mcp_server/)
- [x] `uv run pytest tests/test_smoke.py -v -n auto -m e2e` — all pass (17 passed)
- [x] ch-s0x closed (SQL fix committed, verified via live MCP)

## Anti-Patterns

- Code motion without reflection — moving functions to new files without improving them
- Testing SQL by mocking execute_query — the bug that started this
- Pre-writing implementation in skeletons — plans describe what to build, not the code
- Overengineering the query builder — sqlglot IS the abstraction, don't layer on top
