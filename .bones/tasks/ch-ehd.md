---
id: ch-ehd
title: 'MCP Decomposition Acceptance: tools package quality gate'
status: open
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

# 3. No file exceeds 500 lines
wc -l chunkhound/mcp_server/tools/*.py chunkhound/mcp_server/tools/queries/*.py

# 4. Full test suite passes
uv run pytest tests/lsp/ tests/mcp_server/ -v

# 5. Smoke tests pass
uv run pytest tests/test_smoke.py -v -n auto -m e2e
```

## Success Criteria

- [ ] User verifies all tools register from domain modules
- [ ] User verifies __init__.py is clean (no local definitions)
- [ ] User verifies file sizes are under 500 lines
- [ ] User verifies tests pass
- [ ] User closes this task
