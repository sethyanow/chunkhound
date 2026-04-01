---
id: ch-21f
title: 'LSP client hardening: notifications, async deletes, typed exceptions'
status: closed
type: task
priority: 2
owner: Seth
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
- Test params=None: `_handle_notification("$/progress", None)` → no crash, no state change
- Test params=None for logMessage: `_handle_notification("window/logMessage", None)` → no crash
- Test missing token: `_handle_notification("$/progress", {"value": {"kind": "begin"}})` → no crash, no `_progress[None]` entry

#### Step 3: Implement handlers (GREEN)
- File: `chunkhound/lsp/client.py`
- Add `self._progress: dict[str, dict[str, Any]] = {}` to `__init__` (line ~45)
- Add `elif method == "window/logMessage"` in `_handle_notification` (method at line 451, `else` branch at line 461)
  - Map type 1→ERROR, 2→WARNING, 3→INFO, default→DEBUG
  - Use `params.get("type", 4)` — missing type defaults to Log (4) → DEBUG
  - Use `params.get("message", "")` — defensive against missing message
  - `logger.log(level, "LSP [%s]: %s", self._config.language_id, message)`
- Add `elif method == "$/progress"`
  - Extract `token = params.get("token")` and `value = params.get("value", {})` — defensive access
  - Extract `kind = value.get("kind")` — may be missing, skip if so
  - begin: store `{title, percentage: 0}` in `_progress[token]`
  - report: update percentage if token exists (use `.get()` on `_progress` to avoid KeyError)
  - end: pop token (use `.pop(token, None)` to avoid KeyError)
- Keep `else` branch for truly unknown methods

### Part B: Async deletes

#### Step 4: Add execute_query_async (RED then GREEN)
- File: `chunkhound/providers/database/serial_database_provider.py`
- Add method following existing async pattern (e.g., `delete_file_completely_async` at line 292):
  ```
  async def execute_query_async(self, query, params=None):
      return await self._execute_in_db_thread("execute_query", query, params)
  ```
- Note: `_executor_execute_query` already exists on `DuckDBProvider` (duckdb_provider.py:2798) — only the async wrapper is needed, not a new executor
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
- [x] `$/progress` notifications tracked in `_progress` dict (begin/report/end lifecycle)
- [x] `window/logMessage` routed to Python logger at correct severity (Error/Warn/Info/Debug)
- [x] No "unhandled" log lines for `$/progress` or `window/logMessage`
- [x] `_handle_notification` with `params=None` does not crash (guard before `.get()` access)
- [x] `delete_file_edges` and `delete_file_symbols` use async DB path
- [x] Per-file catch uses specific types: `(LSPError, OSError, UnicodeDecodeError, duckdb.Error)`
- [x] All existing tests pass

## Key Considerations
- **CRITICAL: `params` can be `None`** — `_handle_notification` signature is `(method: str, params: dict | None)`. Both new handlers MUST guard with `if not params: return` before calling `params.get()`, matching the existing `publishDiagnostics` pattern (`and params` at line 453). Without this guard, `AttributeError` crashes the transport read loop.
- **`$/progress` token can be `None`** — `params.get("token")` returns `None` if missing. `_progress[None]` creates an un-cleanable garbage entry. Skip if `token is None`.
- `window/logMessage` params may be missing `type` or `message` keys — use `.get()` with safe defaults (type=4→DEBUG, message="")
- `$/progress` value dict may be missing `kind` key — skip processing if no kind
- **Orphaned progress tokens** — if server crashes before sending `end`, token stays in `_progress`. Not critical (in-memory only, cleared on client restart). Document as known behavior.
- `window/showMessage` is another common LSP notification (not in scope for this task) — it will still log as "unhandled," which is acceptable per criteria scoping to only `$/progress` and `window/logMessage`
- Delete methods (`delete_file_edges`, `delete_file_symbols`) return DuckDB row-count results from `fetchall()` — the async path via `_executor_execute_query` handles this correctly
- **Python 3.10 timeout note** — `asyncio.TimeoutError` is NOT a subclass of `OSError` on 3.10 (it is on 3.11+). Not actionable now (LSP client has no timeouts), but if timeouts are added later, they should use `LSPTimeoutError(LSPError)` which is in the catch list
- Step 6 TDD note: the `duckdb.ConstraintException` test will be GREEN from the start (current `except Exception` already catches it). The RuntimeError test drives the change (it will be RED because current code catches it but it should escape)

## Anti-Patterns
- NO observer/registry pattern for notification dispatch — KISS, just elif branches
- NO wrapping deletes in explicit transactions — DuckDB FK enforcement breaks (see memory)
- NO catching bare `Exception` — must be typed

## Log

- [2026-04-01T02:27:38Z] [Seth] Debrief: Clean implementation — 25 lines production code, 150 lines tests, 26 unit tests + 2 typed-catch tests all pass. SRE caught params=None crash vector and missing executor location note before implementation. No workarounds. Adversarial battery: 13 structural tests all GREEN, Three-Question Framework traced no concerns. Reflections: Skeleton was accurate (minor line number drift caught by SRE). User corrected to load Python skills before implementation — saved to feedback memory. Epic criteria still accurate. execute_query_async now available for future async DB paths.
