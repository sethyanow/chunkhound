---
id: ch-21f
title: 'LSP client hardening: notifications, async deletes, typed exceptions'
status: open
type: task
priority: 2
parent: ch-0um
---



## Context
LSP client logs 30k+ "unhandled" notification lines during indexing ($/progress, window/logMessage). Delete methods block the event loop with sync DB calls inside async functions. Per-file exception catch is bare `Exception` — should be typed.

Discovered during ch-ko4 reindex investigation. code_research output + Python skill patterns documented in memory.

**Blocked by:** nothing
**Unlocks:** clean debug output, non-blocking population, proper error classification

## Requirements
1. Handle `$/progress` notifications — track begin/report/end lifecycle in `_progress` dict
2. Handle `window/logMessage` notifications — route to Python logger at correct severity
3. Convert `delete_file_edges` and `delete_file_symbols` to async DB path
4. Narrow `populate_files` per-file catch to specific exception types

## Implementation

### Part A: Notification handling

#### Step 1: Test window/logMessage routing (RED)
- File: `tests/test_lsp_client_notifications.py` (new)
- Test: call `_handle_notification("window/logMessage", {"type": N, "message": "..."})` for each type (1=Error, 2=Warn, 3=Info, 4=Log)
- Assert: correct Python log level emitted via caplog
- Run: `uv run pytest tests/test_lsp_client_notifications.py -v`

#### Step 2: Test $/progress lifecycle (RED)
- Same file
- Test begin: `_handle_notification("$/progress", {"token": "t1", "value": {"kind": "begin", "title": "Indexing"}})` → `client._progress["t1"]` exists with title
- Test report: send report with percentage → updated in dict
- Test end: send end → token removed from dict
- Test unknown token report: no KeyError

#### Step 3: Implement handlers (GREEN)
- File: `chunkhound/lsp/client.py`
- Add `self._progress: dict[str, dict[str, Any]] = {}` to `__init__` (line ~45)
- Add `elif method == "window/logMessage"` in `_handle_notification` (line 461)
  - Map type 1→ERROR, 2→WARNING, 3→INFO, default→DEBUG
  - `logger.log(level, "LSP [%s]: %s", self._config.language_id, message)`
- Add `elif method == "$/progress"`
  - begin: store `{title, percentage: 0}` in `_progress[token]`
  - report: update percentage if token exists
  - end: pop token
- Keep `else` branch for truly unknown methods

### Part B: Async deletes

#### Step 4: Add execute_query_async (RED then GREEN)
- File: `chunkhound/providers/database/serial_database_provider.py`
- Add method following existing async pattern (e.g., `delete_file_completely_async`):
  ```
  async def execute_query_async(self, query, params=None):
      return await self._execute_in_db_thread("execute_query", query, params)
  ```
- Test: call `execute_query_async` with a simple SELECT, verify returns same as sync

#### Step 5: Convert delete methods to async DB path
- File: `chunkhound/services/lsp_population.py:513-526`
- Change `self._provider.execute_query(...)` → `await self._provider.execute_query_async(...)`
- Both `delete_file_edges` and `delete_file_symbols`
- Run existing tests — must all pass (behavioral equivalence)

### Part C: Typed exceptions

#### Step 6: Test typed catch (RED)
- File: `tests/test_lsp_population.py` (existing resilience class)
- Test: raise `RuntimeError` from `populate_file` — verify it escapes the loop (NOT caught)
- Test: raise `duckdb.ConstraintException` — verify it IS caught (loop continues)
- Current `except Exception` catches both → RuntimeError test will be GREEN when it should be RED

#### Step 7: Narrow the catch (GREEN)
- File: `chunkhound/services/lsp_population.py:254`
- Change to: `except (LSPError, OSError, UnicodeDecodeError, duckdb.Error) as exc:`
- Add `import duckdb` at top of file

## Success Criteria
- [ ] `$/progress` notifications tracked in `_progress` dict (begin/report/end lifecycle)
- [ ] `window/logMessage` routed to Python logger at correct severity (Error/Warn/Info/Debug)
- [ ] No "unhandled" log lines for `$/progress` or `window/logMessage`
- [ ] `delete_file_edges` and `delete_file_symbols` use async DB path
- [ ] Per-file catch uses specific types: `(LSPError, OSError, UnicodeDecodeError, duckdb.Error)`
- [ ] All existing tests pass

## Anti-Patterns
- NO observer/registry pattern for notification dispatch — KISS, just elif branches
- NO wrapping deletes in explicit transactions — DuckDB FK enforcement breaks (see memory)
- NO catching bare `Exception` — must be typed
