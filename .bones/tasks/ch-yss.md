---
id: ch-yss
title: 'Bug: get_stats MCP tool silently swallows provider exceptions'
status: open
type: bug
priority: 2
parent: ch-ljh
---


## Context
The outer try/except in `chunkhound/mcp_server/tools/stats.py:30-33` and `:36-39` swallows any exception from `provider.get_stats()` and `provider.symbol_stats()`, including `TimeoutError`, leaving users/agents with misleading `{files: 0, chunks: 0}` responses. Masked the ch-3zc timeout for weeks — diagnosis would have taken seconds if this had surfaced the real error.

**Reproduction:**
1. Mock or force `provider.get_stats()` to raise (e.g., TimeoutError)
2. Call MCP `get_stats`
3. Observe silent 0-valued response — no indication of error

## Diagnosis
Root cause: `chunkhound/mcp_server/tools/stats.py:29-39`:
```python
try:
    provider_stats = services.provider.get_stats()
except Exception:
    provider_stats = {}

try:
    sym_stats = services.provider.symbol_stats()
except Exception:
    sym_stats = {"symbol_count": 0, "edge_count": 0, "languages": []}
```
Bare `except Exception`, no log, no error signal to caller.

Fix location: `chunkhound/mcp_server/tools/stats.py:29-48`.

## Implementation

### Step 1: Write failing test
File: `tests/mcp_server/test_stats_error_surface.py` (new)
Tests:
- `test_provider_get_stats_timeout_surfaces_in_response` — mock `services.provider.get_stats` to raise `TimeoutError("executor timeout")`. Call `get_stats_impl`. Assert result contains an `errors` field with the TimeoutError message AND files/chunks are 0. Logging captured by caplog at WARNING level.
- `test_provider_symbol_stats_exception_surfaces` — same pattern for `symbol_stats`.
- `test_happy_path_unchanged` — both methods succeed, result has normal keys, no `errors` field.

### Step 2: Run tests
```
uv run pytest tests/mcp_server/test_stats_error_surface.py -v
```
Expect failures.

### Step 3: Implement error surfacing
File: `chunkhound/mcp_server/tools/stats.py`
Approach (Option C — selected):
- Inside each try/except, on exception: log at WARNING level with exception type and message, collect into an `errors` dict.
- Final result includes `"errors": {...}` field when any exception was caught; omit the key on happy path (backward-compatible shape).
- Preserve existing 0-defaults for files/chunks/symbols so JSON shape stays stable.

Shape:
```python
result = {
    "files": ...,
    "chunks": ...,
    "symbols": ...,
    "symbol_edges": ...,
    "languages": [...],
    "lsp_servers": ...,
}
if errors:
    result["errors"] = errors  # {"get_stats": "TimeoutError: ...", ...}
return result
```

### Step 4: Run tests
Expect pass.

### Step 5: Run targeted suite
```
uv run pytest tests/mcp_server/ -v
```

### Step 6: Commit
```
git add -u && git commit -m "fix(mcp): surface provider exceptions in get_stats response instead of returning 0s"
```

## Success Criteria
- [ ] Provider exceptions surface in `errors` field of response
- [ ] Warnings logged (caplog-verifiable) with exception type and message
- [ ] Happy path unchanged — response shape backward-compatible when no errors
- [ ] Tests in `tests/mcp_server/test_stats_error_surface.py` pass
- [ ] Existing MCP server tests still pass

## Anti-Patterns
- NO `print()` or `sys.stderr.write` — MCP stdio contract forbids it
- NO bare `except Exception: pass` — explicit logging required
- NO re-raising unconditionally — would break clients that read partial stats

## Key Considerations
- Three strategies considered:
  - A: Add `errors` field, log warning (selected — preserves shape, visible to agents)
  - B: Let exceptions propagate, MCP returns error response (cleaner but breaks contract)
  - C: Same as A (kept for record)
- Follow-up — apply same pattern to other MCP tools that silently swallow. Audit `chunkhound/mcp_server/tools/*.py` for bare `except Exception` handlers. Track separately if found.
- Ordering: ideal to land alongside ch-3zc so the pair is coherent (ch-3zc fixes the timeout, ch-yss makes similar future failures visible).

## Log

- [2026-04-20T18:40:11Z] [Seth] Diagnosis HIGH confidence: outer bare except Exception in stats.py:30-33 and :36-39 masks TimeoutError. Fix: surface errors via 'errors' dict field + warning log.
