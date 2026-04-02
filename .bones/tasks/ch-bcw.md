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

**Transition strategy:** `tools/__init__.py` (the former `tools.py`, moved by ch-e6v) keeps all tool functions. New `registry.py` and `response.py` extract the framework. `__init__.py` gets re-exports at the top — old definitions stay until domain tasks (ch-ei8, ch-p1r, ch-c0w) clean up.

**Additional import site found:** `base.py` line 434 also imports `SEARCH_DESCRIPTION_NO_RESEARCH` from `.tools` — must remain importable.

## Implementation

### Step 1: Write registry tests
- **File:** `tests/mcp_server/test_registry.py`
- **Test `register_tool`:** decorator registers function in `TOOL_REGISTRY` with name, description, and JSON schema parameters
- **Test `execute_tool`:** dispatches to registered function by name, passes `services` + arbitrary kwargs, returns result
- **Test `execute_tool` unknown:** returns error dict `{"error": "unknown_tool"}` for unregistered tool name
- **Test `_generate_json_schema_from_signature`:** produces `{"type": "object", "properties": {...}, "required": [...]}` from typed function with `str`, `int`, `Optional[str]` params
- **Run:** `uv run pytest tests/mcp_server/test_registry.py -v`
- **Expected:** ImportError (module doesn't exist)

### Step 2: Implement registry.py
- **File:** `chunkhound/mcp_server/tools/registry.py`
- **Extract from `tools/__init__.py`:** `Tool` dataclass (~line 40), `TOOL_REGISTRY` dict (~line 54), `_python_type_to_json_schema_type` (~line 57), `_extract_param_descriptions_from_docstring` (~line 111), `_generate_json_schema_from_signature` (~line 155), `register_tool` decorator (~line 213), `_convert_paths_to_native` (~line 270), `execute_tool` (~line 1724)
- **Key:** Fresh implementations, not imports from `__init__.py`. Old copies remain in `__init__.py` for now.
- **Run:** tests pass

### Step 3: Write response tests
- **File:** `tests/mcp_server/test_response.py`
- **Test `estimate_tokens`:** "hello world" → ~3 tokens (tiktoken ~4 chars/token), empty string → 0
- **Test `limit_response_size` truncation:** result with 100k chars exceeds MAX_RESPONSE_TOKENS, returns truncated + pagination warning
- **Test `limit_response_size` passthrough:** result under limit returned unchanged
- **Run:** `uv run pytest tests/mcp_server/test_response.py -v`
- **Expected:** ImportError

### Step 4: Implement response.py
- **File:** `chunkhound/mcp_server/tools/response.py`
- **Extract from `tools/__init__.py`:** `MAX_RESPONSE_TOKENS` (~line 28), `MIN_RESPONSE_TOKENS` (~line 29), `MAX_ALLOWED_TOKENS` (~line 30), `PaginationInfo` (~line 282), `SearchResponse` (~line 292), `estimate_tokens` (~line 299), `limit_response_size` (~line 304)
- **Run:** tests pass

### Step 5: Update tools/__init__.py with re-exports
- **File:** `chunkhound/mcp_server/tools/__init__.py`
- Add at top (before existing code): `from .registry import Tool, TOOL_REGISTRY, register_tool, execute_tool` and `from .response import PaginationInfo, SearchResponse, estimate_tokens, limit_response_size`
- The old definitions in `__init__.py` shadow these re-exports — that's fine because they're identical. Domain tasks will delete old defs, making re-exports authoritative.
- **Verify:** `uv run python -c "from chunkhound.mcp_server.tools import TOOL_REGISTRY, execute_tool; print(len(TOOL_REGISTRY))"`
- **Expected:** prints tool count (7-8 tools)

### Step 6: Run existing test suite
- **Run:** `uv run pytest tests/lsp/ tests/mcp_server/ -v`
- **Expected:** all pass

### Step 7: Commit
- **Message:** `refactor(mcp): extract registry and response modules from tools`

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
