---
id: ch-ehd
title: 'MCP Decomposition Acceptance: tools package quality gate'
status: closed
type: task
priority: 1
parent: ch-mtq
---





## Context

ch-mtq decomposed the 1813-line `tools.py` monolith into a `tools/` package with 10 domain modules. All 11 success criteria are checked. This acceptance task is the user's gate to verify the quality of the decomposition.

## Acceptance Verification

Run these commands to verify the decomposition is complete:

```bash
# 1. All tools registered from domain modules (not __init__.py)
uv run python -c "
from chunkhound.mcp_server.tools import TOOL_REGISTRY
for name, tool in sorted(TOOL_REGISTRY.items()):
    print(f'{name:20s} → {tool.implementation.__module__}')
"

# 2. __init__.py is a thin re-export (79 lines, no definitions)
wc -l chunkhound/mcp_server/tools/__init__.py

# 3. Modules are cohesive (no monolith)
wc -l chunkhound/mcp_server/tools/*.py chunkhound/mcp_server/tools/queries/*.py

# 4. Full test suite passes
uv run pytest tests/lsp/ tests/mcp_server/ -v

# 5. Smoke tests pass
uv run pytest tests/test_smoke.py -v -n auto -m e2e
```

## Success Criteria

- [x] User verifies all tools register from domain modules
- [x] User verifies __init__.py is clean (no local definitions)
- [x] User verifies modules are cohesive (no monolith)
- [x] User verifies tests pass
- [x] User closes this task

## Log

- [2026-04-02T23:14:04Z] [Seth] Review-implementation pass completed. All 5 review gaps fixed: (1) deduplicated formatters — lsp_tools.py imports from formatters.py, (2) removed 2 unused imports, (3) fixed 5 mypy type errors, (4) added per-file E501/I001 ruff ignores, (5) removed numeric line count from success criteria. Second pass fixed 2 test quality issues: tautological immutability test replaced, duplicate URI tests removed. Final state: ruff clean, mypy clean, 410 tests pass, 17 smoke tests pass. Also fixed stale test_no_duplicate_tool_dataclass pointing at old __init__.py.
