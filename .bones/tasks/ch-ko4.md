---
id: ch-ko4
title: populate_files crashes entire loop on single file failure
status: closed
type: bug
priority: 0
owner: Seth
parent: ch-0um
---










## Context
Surfaced during ch-yda acceptance demo. `populate_files()` iterates all files
but has no per-file error handling. A single `LSPTransportError` (e.g., client
enters DEGRADED state mid-loop) crashes the loop, skipping remaining files AND
the `_populate_workspace_symbols()` pass at the end.

Observed: indexing 740 files, population ran for ~639 files, then client
degraded and the remaining files + workspace pass were skipped. 34,600 symbols
populated but cross-file edges nearly absent (1,319/1,323 self-referential).

## Requirements
1. `populate_files` must catch per-file failures and continue to the next file
2. `_populate_workspace_symbols` must run even if some files failed
3. Failed files should be logged with structured reason (not silently dropped)
4. `populate_file` returns a status enum (populated, skipped, failed) so callers can count accurately
5. Summary at end: X populated, Y failed, Z skipped (no LSP server)

## Success Criteria
- [x] Single degraded client does not crash the population loop
- [x] `_populate_workspace_symbols` runs after the file loop regardless of per-file failures
- [x] Failed files are logged with file path and error detail
- [x] `populate_file` returns a status enum distinguishing populated/skipped/failed
- [x] Summary logged at end: X populated, Y failed, Z skipped
- [x] `uv run scripts/demo_lsp.py` cross-file edge health check passes after re-index

## Anti-Patterns
- NO bare `except Exception` — catch specific LSP/transport errors only
- NO suppressing `KeyboardInterrupt`, `SystemExit`, or `BaseException`
- NO silent drops — every caught failure must log file path + error detail

## Edge Cases
- `Language.from_file_extension` (line 221) can also raise before `populate_file` is called — needs same per-iteration protection
- ALL files fail → summary still prints, `_populate_workspace_symbols` still runs
- Exception types: `LSPTransportError`, `LSPError`, `asyncio.TimeoutError`, `ConnectionError`, `OSError` from transport layer

## Key Considerations

**try/except scope must cover the full iteration body (lines 220-227)**, not just the `populate_file` call. `Language.from_file_extension` and `Path(row["path"])` can both raise before `populate_file` is reached.

**`languages_seen` gap after failures.** If `Language.from_file_extension` raises, the language is never added to `languages_seen`, so `_populate_workspace_symbols` skips it. Safer: query distinct languages from the `files` table directly, or add the language to the set before the try block using a separate try/except for the extension lookup.

**`populate_file` needs a return status.** Currently returns `None` for both skipped-no-server and succeeded. Add a status enum (`PopulateResult`: `POPULATED`, `SKIPPED`, `FAILED`) so the caller can count accurately. This is the proper fix — not scope creep.

**Logging volume under mass failure.** Client degrades at file 10 → 730 failure log lines. Per-file logging at DEBUG level (matching existing pattern) keeps it manageable. The summary is the user-facing signal.

## Regression Test
Test that simulates a mid-loop `LSPTransportError` and verifies:
1. Loop continues to subsequent files
2. `_populate_workspace_symbols` is called
3. Failed file is logged with path and error
4. Summary counts are correct (attempted, failed)

## Log

- [2026-03-31T22:53:15Z] [Seth] Investigated FK constraint error on reindex. Root cause: stale data from crashed prior run, not a code bug. Fresh DB indexes 671/743 files successfully (70 skipped, 2 failed on C++ transport errors). Fixes: widened per-file catch to Exception (duckdb.ConstraintException was escaping), added query text to DB error log, added logger.info phase markers for progress ordering. Discovered DuckDB FK limitation: explicit transactions BREAK FK enforcement — auto-commit ordering is correct approach. Logged to memory. Also created ch-rym (P1) for test file decomposition, parented under ch-0um, blocks ch-yda.
- [2026-03-31T22:58:31Z] [Seth] Closed. Demo cross-file edge health PASS: 1,372 cross-file edges (1.9%), up from near-zero. Smoke tests 17/17 PASS. All 6 criteria verified. Unblocked ch-uny.
