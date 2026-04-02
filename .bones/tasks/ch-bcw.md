---
id: ch-bcw
title: 'Registry extraction: Tool, TOOL_REGISTRY, execute_tool, response limiting'
status: open
type: task
priority: 1
depends_on: [ch-e6v]
parent: ch-mtq
---

## Context

Extract the tool framework from tools.py into its own module. This is the skeleton everything hangs off — `@register_tool`, the Tool dataclass, `execute_tool` dispatch, and response size management.

Three external import sites depend on `from chunkhound.mcp_server.tools import ...`:
- `chunkhound/mcp_server/__init__.py` → `TOOL_REGISTRY`
- `chunkhound/mcp_server/common.py` → `TOOL_REGISTRY`, `execute_tool`
- `chunkhound/mcp_server/base.py` → `TOOL_REGISTRY`

The `tools/__init__.py` must re-export these so external imports don't break.

**Blocked by:** ch-e6v (shared foundations must exist for the package structure)
**Unlocks:** ch-ei8, ch-p1r, ch-c0w (all tool rebuilds need the registry in place)

## Requirements

1. `chunkhound/mcp_server/tools/registry.py` — Tool, TOOL_REGISTRY, register_tool, execute_tool
2. `chunkhound/mcp_server/tools/response.py` — estimate_tokens, limit_response_size, PaginationInfo, SearchResponse
3. `tools/__init__.py` re-exports TOOL_REGISTRY, execute_tool, register_tool, Tool so external imports work
4. All tool functions remain in old tools.py temporarily, importing from registry — migration happens in domain tasks

## Design

**registry.py** gets:
- `Tool` dataclass (line 40)
- `TOOL_REGISTRY: dict[str, Tool]` (line 54)
- `_python_type_to_json_schema_type` (line 57)
- `_extract_param_descriptions_from_docstring` (line 111)
- `_generate_json_schema_from_signature` (line 155)
- `register_tool` decorator (line 213)
- `_convert_paths_to_native` (line 270)
- `execute_tool` (line 1724)

**response.py** gets:
- `PaginationInfo` dataclass (line 282)
- `SearchResponse` dataclass (line 292)
- `estimate_tokens` (line 299)
- `limit_response_size` (line 304)
- Constants: `MAX_RESPONSE_TOKENS`, `MIN_RESPONSE_TOKENS`, `MAX_ALLOWED_TOKENS` (lines 28-30)

**tools/__init__.py** re-exports:
```python
from .registry import Tool, TOOL_REGISTRY, register_tool, execute_tool
from .response import PaginationInfo, SearchResponse, estimate_tokens, limit_response_size
```

**Transition strategy:** Old `tools.py` gets renamed (e.g. `_legacy_tools.py`) and imports from the new modules. Domain tasks (ch-ei8, ch-p1r, ch-c0w) incrementally move tool functions out. When empty, delete.

## Implementation

### Step 1: Write registry tests
- **File:** `tests/mcp_server/test_registry.py`
- **Test:** `register_tool` decorator registers a function in TOOL_REGISTRY with correct schema
- **Test:** `execute_tool` dispatches to registered function by name
- **Test:** `execute_tool` returns error for unknown tool name
- **Test:** `_generate_json_schema_from_signature` produces correct JSON schema from type hints
- **Run:** `uv run pytest tests/mcp_server/test_registry.py -v`
- **Expected:** ImportError

### Step 2: Write response tests
- **File:** `tests/mcp_server/test_response.py`
- **Test:** `estimate_tokens` returns reasonable count for known string lengths
- **Test:** `limit_response_size` truncates when over MAX_RESPONSE_TOKENS
- **Test:** `limit_response_size` passes through when under limit
- **Run:** `uv run pytest tests/mcp_server/test_response.py -v`
- **Expected:** ImportError

### Step 3: Implement registry.py
- **File:** `chunkhound/mcp_server/tools/registry.py`
- **Extract:** Tool, TOOL_REGISTRY, register_tool, execute_tool, schema generation helpers from tools.py
- **Key:** `register_tool` uses TOOL_REGISTRY from this module, not old tools.py

### Step 4: Implement response.py
- **File:** `chunkhound/mcp_server/tools/response.py`
- **Extract:** PaginationInfo, SearchResponse, estimate_tokens, limit_response_size, constants

### Step 5: Update tools/__init__.py with re-exports
- **File:** `chunkhound/mcp_server/tools/__init__.py`
- Re-export all public names so `from chunkhound.mcp_server.tools import TOOL_REGISTRY` works

### Step 6: Rename old tools.py, update its imports
- **Rename:** `tools.py` → `tools/_legacy_tools.py`
- **Update:** internal imports to use `from .registry import register_tool` etc.
- **Verify:** `__init__.py` imports `_legacy_tools` to trigger `@register_tool` decorators

### Step 7: Verify external imports
- **Run:** `uv run python -c "from chunkhound.mcp_server.tools import TOOL_REGISTRY, execute_tool; print(len(TOOL_REGISTRY))"`
- **Expected:** prints tool count (currently 7-8 tools)

### Step 8: Run full existing test suite
- **Run:** `uv run pytest tests/lsp/ -v > /tmp/registry_tests.txt 2>&1 && tail -5 /tmp/registry_tests.txt`
- **Expected:** all pass

### Step 9: Commit
- **Message:** `refactor(mcp): extract registry and response modules from tools.py`

## Success Criteria

- [ ] `registry.py` contains Tool, TOOL_REGISTRY, register_tool, execute_tool
- [ ] `response.py` contains PaginationInfo, SearchResponse, estimate_tokens, limit_response_size
- [ ] `from chunkhound.mcp_server.tools import TOOL_REGISTRY` works (re-export)
- [ ] `from chunkhound.mcp_server.tools import execute_tool` works (re-export)
- [ ] All existing tests pass unchanged
- [ ] Committed and pushed

## Anti-Patterns

- Don't move tool functions yet — only the framework. Tool functions move in ch-ei8/ch-p1r/ch-c0w
- Don't break external imports — the re-export is critical
- Don't duplicate TOOL_REGISTRY — one source of truth in registry.py
