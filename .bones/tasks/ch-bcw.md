---
id: ch-bcw
title: 'Registry extraction: Tool, TOOL_REGISTRY, execute_tool, response limiting'
status: active
type: task
priority: 1
owner: Seth
depends_on: [ch-e6v]
parent: ch-mtq
---


## Context

Extract the tool framework from tools.py into its own module. This is the skeleton everything hangs off — `@register_tool`, the Tool dataclass, `execute_tool` dispatch, and response size management.

Four external import sites depend on `from chunkhound.mcp_server.tools import ...`:
- `chunkhound/mcp_server/__init__.py` → `TOOL_REGISTRY`
- `chunkhound/mcp_server/common.py` → `TOOL_REGISTRY`, `execute_tool`
- `chunkhound/mcp_server/base.py` → `TOOL_REGISTRY`
- `scripts/demo_lsp.py` → `execute_tool` (line 974, local import inside function)

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
- `execute_tool` (line 1732)

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

**Additional import site found:** `base.py` line 439 also imports `SEARCH_DESCRIPTION_NO_RESEARCH` from `.tools` — must remain importable.

## Implementation

### Step 1: Write registry tests
- **File:** `tests/mcp_server/test_registry.py`
- **Test `register_tool`:** decorator registers function in `TOOL_REGISTRY` with name, description, and JSON schema parameters
- **Test `execute_tool`:** dispatches to registered function by name, passes `services` + arbitrary kwargs, returns result
- **Test `execute_tool` unknown:** raises `ValueError("Unknown tool: ...")` for unregistered tool name (actual behavior per line 1760-1761)
- **Test `_generate_json_schema_from_signature`:** produces `{"type": "object", "properties": {...}, "required": [...]}` from typed function with `str`, `int`, `Optional[str]` params
- **Run:** `uv run pytest tests/mcp_server/test_registry.py -v`
- **Expected:** ImportError (module doesn't exist)

### Step 2: Implement registry.py
- **File:** `chunkhound/mcp_server/tools/registry.py`
- **Extract from `tools/__init__.py`:** `Tool` dataclass (~line 40), `TOOL_REGISTRY` dict (~line 54), `_python_type_to_json_schema_type` (~line 57), `_extract_param_descriptions_from_docstring` (~line 111), `_generate_json_schema_from_signature` (~line 155), `register_tool` decorator (~line 213), `_convert_paths_to_native` (~line 270), `execute_tool` (~line 1732)
- **Key:** Copy-paste from `tools/__init__.py` then clean up (remove tool-function-specific code, keep only framework). NOT circular imports from `__init__.py`. Old copies remain in `__init__.py` for now — they shadow the re-exports during transition.
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
- The old definitions in `__init__.py` shadow these re-exports — two registries coexist during transition. The old `TOOL_REGISTRY` has tools registered; `registry.py`'s is empty. Domain tasks (ch-ei8/ch-p1r/ch-c0w) will delete old defs, making re-exports authoritative.
- **Verify (old path still works):** `uv run python -c "from chunkhound.mcp_server.tools import TOOL_REGISTRY, execute_tool; print(len(TOOL_REGISTRY))"`
- **Expected:** prints tool count (7-8 tools) — this tests the OLD definition, confirming no breakage
- **Verify (new module standalone):** `uv run python -c "from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY; print(type(TOOL_REGISTRY))"`
- **Expected:** `<class 'dict'>` (empty — tools register via old path until domain tasks migrate)

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

## Key Considerations

- `execute_tool` has special-case handling for `code_research` return type (lines 1795-1805) — this goes with `execute_tool` into `registry.py`
- `execute_tool` signature inspection maps infrastructure params (services, embedding_manager, llm_manager, scan_progress, config, lsp_client_pool, progress) — all this dispatch logic moves together
- `_convert_paths_to_native` is positionally between registry and response code but semantically a search utility — keep in registry per skeleton design, domain tasks can relocate if needed
- Two `TOOL_REGISTRY` dicts coexist during transition: `__init__.py`'s (populated) and `registry.py`'s (empty). This is the intended design — sunset when domain tasks delete old defs
- `execute_tool` needs `Config` import from `chunkhound.core.config.config` and `inspect` stdlib — verify imports compile standalone

### Failure Catalog (Adversarial Planning)

**[Temporal Betrayal]: Dual TOOL_REGISTRY during transition**
- Assumption: Code consistently reads from one TOOL_REGISTRY
- Betrayal: `from chunkhound.mcp_server.tools import TOOL_REGISTRY` reads `__init__.py`'s (populated), while `from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY` reads `registry.py`'s (empty). Mixing these imports silently gets different registries.
- Consequence: Tool lookups on empty registry fail — `execute_tool` raises ValueError for all tools
- Mitigation: Structural — no code path imports from `registry` directly until domain tasks migrate. Anti-pattern covers this. Tests verify the public import path works.

**[Dependency Treachery]: registry.py Config import**
- Assumption: `registry.py` can import `Config` for `execute_tool` type annotation without circular import
- Betrayal: If `Config` transitively imports from tools, circular ImportError
- Consequence: Module load fails entirely
- Mitigation: Use `TYPE_CHECKING` guard for `Config` since it's only in a type annotation, not used at runtime. `execute_tool` receives `config` as `Config | None` but never calls Config methods — it just passes it through to tool implementations.

**[Input Hostility]: Schema generation with missing type hints**
- Assumption: Tool function parameters have type annotations
- Betrayal: Parameter with `inspect.Parameter.empty` (no hint)
- Consequence: `_python_type_to_json_schema_type` falls to else branch → `{"type": "object"}`. Imprecise but not a crash.
- Mitigation: Existing behavior preserved. Test should verify no-annotation case returns `{"type": "object"}`.

**[State Corruption]: Partial import on decorator failure
- Assumption: All `@register_tool` decorators in `__init__.py` succeed
- Betrayal: Broken type annotation in a tool function causes import failure midway
- Consequence: Partial TOOL_REGISTRY — some tools registered, others missing
- Mitigation: Existing behavior, not new. `register_tool` decorator logic is simple dict assignment — unlikely to fail. No change needed.

**Skipped categories with reasoning:**
- *Encoding Boundaries*: All tool names are ASCII string literals in decorators. `estimate_tokens` uses `len(text)//3` heuristic (not tiktoken), handles UTF-8 naturally. No FFI or serialization boundaries.
- *Resource Exhaustion*: `limit_response_size` IS the resource guard. Registry operations are O(1) dict lookups. No exhaustion risk in the framework itself.

## Anti-Patterns

- Don't move tool functions yet — only the framework. Tool functions move in ch-ei8/ch-p1r/ch-c0w
- Don't break external imports — the re-export is critical
- Don't duplicate TOOL_REGISTRY — one source of truth in registry.py (during transition, `__init__.py`'s old copy is the active one; `registry.py`'s becomes authoritative after domain tasks)
- Don't make registry.py import from `__init__.py` — that creates circular imports
- Don't modify `execute_tool`'s error behavior — it raises `ValueError` for unknown tools, don't change to error dict
