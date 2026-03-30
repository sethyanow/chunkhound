---
id: ch-jsj
title: Fix semantic search path scoping + git-aware indexing
status: closed
type: task
priority: 0
parent: ch-7j0
---






## Context
First task in Phase 1. Fixes broken behavior before building new features. Parent epic R10.

Two infrastructure issues identified from empirical usage:
1. `search_semantic` AND `search_regex` with `path` filter leak results across directories — both use `%{escaped_path}%` substring LIKE match instead of prefix
2. `SimpleEventHandler._should_index()` (used by `RealtimeIndexingService`) checks file patterns only — blind to git state

**SRE-verified locations:**
- Path filter: `chunkhound/providers/database/duckdb_provider.py`
  - `_validate_and_normalize_path_filter()` at line 1920
  - `_executor_search_semantic()` at line 1992, bug at line 2049: `path_like = f"%{escaped_path}%"`
  - `_executor_search_regex()` at line 2150, same bug at line 2172: `params.append(f"%{escaped_path}%")`
  - Comment at line 2058: "Use substring match so callers can pass repo-relative paths even when the database base_directory is higher (e.g., monorepo root)." — this was an intentional design choice
- Git-aware: `SimpleEventHandler._should_index()` at `chunkhound/services/realtime_indexing_service.py:101` (on SimpleEventHandler, NOT RealtimeIndexingService)
  - Called from `on_any_event` (line 61), `_process_polled_file` (line 542), `_index_directory` (line 748)
- pygit2 confirmed: `pyproject.toml` line 90: `"pygit2>=1.12.0"`

**Design decision (RESOLVED):**
Prefix match by default, substring opt-in via parameter. User confirmed — fuzzy substring is still useful but shouldn't be the default because it causes cross-directory leakage in monorepos with similar names.

## Requirements
- Semantic search path filter returns ONLY results within the specified directory prefix (no leakage)
- Regex search path filter returns ONLY results within the specified directory prefix (same bug, same fix)
- Git-aware indexing: `SimpleEventHandler._should_index()` checks git state so realtime index reflects committed + staged files, not just filesystem presence
- Non-git projects continue to work (graceful fallback when no `.git` directory)

## Implementation

### Path Scoping Fix
1. Write test: semantic search with `path="src/auth"` must return zero results from `src/payments/`
2. Write test: semantic search with `path="src/auth"` must return zero results from `src/authorization/` (prefix overlap edge case)
3. Write test: regex search with `path="src/auth"` must return zero results from `src/payments/`
4. Write test: semantic search with `path="auth"` AND `fuzzy_path=True` DOES return results containing "auth" anywhere in path (backward compat)
5. Add `fuzzy_path: bool = False` parameter to provider layer — both sync and async:
   - `SerialDatabaseProvider.search_semantic()` at `serial_database_provider.py:229`
   - `SerialDatabaseProvider.search_regex()` at `serial_database_provider.py:254`
   - `SerialDatabaseProvider.search_regex_async()` at `serial_database_provider.py:269`
   - `DuckDBProvider.search_semantic()` at `duckdb_provider.py:1965`
   - `DuckDBProvider.search_regex()` at `duckdb_provider.py:2127`
6. Fix `_executor_search_semantic()` at `duckdb_provider.py:2049` — default to prefix `f"{escaped_path}%"`, use `f"%{escaped_path}%"` only when `fuzzy_path=True`
7. Fix `_executor_search_regex()` at same file line 2172 — same logic
8. Thread `fuzzy_path` through intermediate layers (MCP tool → SearchService → strategies → provider):
   - `search_impl()` at `chunkhound/mcp_server/tools.py:420` — add optional `fuzzy_path` param
   - `SearchService.search_semantic()` at `chunkhound/services/search_service.py:56`
   - `SearchService.search_regex_async()` at `chunkhound/services/search_service.py:216`
   - `SingleHopStrategy.search()` at `chunkhound/services/search/single_hop_strategy.py:39`
   - `MultiHopStrategy.search()` at `chunkhound/services/search/multi_hop_strategy.py:60`
   Pass `fuzzy_path` at each call site: tools.py:474-481 and 484-489, search_service.py:135-145 and 149-157 and 242-247, single_hop_strategy.py:71-79
9. Update existing test `tests/test_path_filter_monorepo_mismatch.py` to pass `fuzzy_path=True` — it explicitly tests the substring matching use case which is now opt-in, not default
10. Update `_validate_and_normalize_path_filter()` (line 1920) if needed to ensure trailing slash on directory paths (already does this for extensionless segments — verify edge cases)
11. Run existing semantic search tests to verify no regression

### Git-Aware Indexing
1. Write test: file that is `git rm`'d but still on filesystem should not be indexed
2. Write test: untracked file (WT_NEW) should not be indexed
3. Write test: staged new file (INDEX_NEW) SHOULD be indexed
4. Write test: non-git project (no `.git` dir) falls back to current behavior (file patterns only)
5. Write test: file path with non-ASCII characters in a git repo still resolves correctly
6. In `SimpleEventHandler._should_index()` at `chunkhound/services/realtime_indexing_service.py:101`: add git status check AFTER the existing pattern/config checks (pattern check is cheaper — short-circuit first)
7. Use pygit2 (`Repository`, `status_file()`) to check file git state. Cache the `Repository` object on the handler instance — do NOT create per-file
8. Status logic: `status_file()` returns `0` for tracked/clean. Exclude if `status & (GIT_STATUS_WT_NEW | GIT_STATUS_IGNORED)`. Allow everything else (CURRENT, INDEX_NEW, INDEX_MODIFIED, WT_MODIFIED).
9. Path conversion: `status_file()` takes RELATIVE paths from repo root. Convert `file_path` to relative via `os.path.relpath(file_path, repo.workdir)` or `file_path.relative_to(repo_root)`.
10. Polling optimization: In `_polling_monitor`, call `repo.status()` ONCE per cycle (returns dict of non-CURRENT files). Check dict membership per file instead of per-file `status_file()` calls.
11. Graceful fallback: if pygit2 raises on `Repository()` (not a git repo), set `self._git_repo = None`, log once, and skip git filtering for all subsequent calls

## Success Criteria
- [x] Test proves semantic search path scoping no longer leaks across directories (tested through SearchService, not just provider)
- [x] Test proves regex search path scoping no longer leaks across directories (tested through SearchService, not just provider)
- [x] Test proves prefix overlap handled correctly (e.g., `src/auth` vs `src/authorization`)
- [x] Test proves `fuzzy_path=True` restores substring matching for backward compat
- [x] Test proves git-rm'd files are excluded from realtime indexing
- [x] Test proves untracked files are excluded from realtime indexing
- [x] Test proves non-git-repo projects still work (graceful fallback)
- [x] Test proves staged new file (INDEX_NEW) IS included in indexing
- [x] Test proves git index lock (rebase in progress) falls back gracefully, not crash
- [x] Existing `test_path_filter_monorepo_mismatch.py` updated to use `fuzzy_path=True` and still passes
- [x] All existing tests still pass (`uv run pytest tests/test_smoke.py -v -n auto`)

## Anti-Patterns
- NO changes to existing DuckDB schema
- NO changes to search result format — only which results are returned
- NO shelling to git if pygit2 can do it (pygit2 is already a dependency)
- NO creating pygit2 Repository object per-file — cache on handler instance
- NO crashing if project is not a git repo — graceful fallback to current behavior

## Key Considerations
- The substring LIKE match was intentional for monorepo support. Prefix match is correct for path scoping — verify monorepo callers are not broken by checking all `search_semantic` and `search_regex` call sites with `path_filter` parameter.
- `SimpleEventHandler` runs in a watchdog thread (OS-level filesystem events). pygit2 thread safety must be verified or guarded.
- `_should_index` is called from three sites: `on_any_event`, `_process_polled_file`, `_index_directory`. All three inherit the fix.
- Existing test `tests/realtime/test_realtime_repo_boundary.py` tests `.gitignore` boundary behavior — NOT git state. New tests needed.
- Initial bulk indexing pipeline (outside `RealtimeIndexingService`) may have the same git-blindness issue — out of scope for this task but should be tracked.

## Failure Catalog (Adversarial Planning)

### Path Filter LIKE Change (`_executor_search_semantic` + `_executor_search_regex`)

**Input Hostility: Directory names with dots**
- Assumption: `_validate_and_normalize_path_filter` correctly adds trailing slash for directories
- Betrayal: Directory named `src/my.module/` — last segment has a dot, so heuristic at line 1956 treats it as a file and does NOT add trailing slash. Prefix match `src/my.module%` would match `src/my.module.backup/file.py`.
- Consequence: Cross-directory leakage for dot-containing directory names — the exact bug we're fixing
- Mitigation: The trailing-slash heuristic is best-effort for bare names. Callers that pass a known directory path should include the trailing slash. Document this in the method docstring. Test explicitly with dot-containing directory names.

**Dependency Treachery: DuckDB LIKE with ESCAPE**
- Assumption: `escape_like_pattern()` correctly escapes `%` and `_` in user-provided paths
- Betrayal: If `escape_like_pattern` misses an edge case (e.g., backslash itself in the path), the LIKE clause could match unintended rows or fail to match intended ones
- Consequence: Silent wrong results — security-adjacent (directory traversal via LIKE injection)
- Mitigation: Write a test with paths containing `%`, `_`, and `\` to verify `escape_like_pattern` correctness. Read the function before relying on it.

### `SimpleEventHandler._should_index()` Git Check

**Temporal Betrayal: Stale status during polling cycle**
- Assumption: File git status is stable during a polling cycle
- Betrayal: File committed mid-cycle after `repo.status()` snapshot — cycle still sees it as WT_NEW, skips indexing. Or: file `git rm`'d mid-cycle, already in the dict as CURRENT (absent from bulk status), gets indexed.
- Consequence: One-cycle latency on git state changes. File not indexed until next cycle.
- Mitigation: Acceptable — bounded staleness (0.5-3 seconds per cycle). The NEXT cycle corrects it. Event handler path uses per-file `status_file()` for fresher reads. Document the eventual-consistency guarantee.

**Dependency Treachery: pygit2 `status_file()` path format**
- Assumption: We pass an absolute path or Path object
- Betrayal: `status_file()` requires a RELATIVE path from repo root. Passing absolute path raises `ValueError` or returns wrong status.
- Consequence: Git check fails for every file — either crashes or incorrectly skips all files
- Mitigation: Convert via `file_path.relative_to(Path(repo.workdir))`. Handle `ValueError` from `relative_to` (file outside repo root) as "not tracked, skip."

**Dependency Treachery: pygit2 bulk `status()` semantics**
- Assumption: Absent from `status()` dict means tracked/clean
- Betrayal: Confirmed correct via live test — `status()` only returns non-CURRENT entries. Tracked clean files are NOT in the dict. Untracked files ARE in the dict with WT_NEW flag.
- Consequence: Logic inverted if assumption wrong — but it's verified correct.
- Mitigation: Test asserts `tracked_clean_file NOT in repo.status()` and `untracked_file IN repo.status()`.

**State Corruption: Locked git index**
- Assumption: pygit2 can always read git status
- Betrayal: `.git/index.lock` exists during rebase/merge. pygit2 may raise `GitError` on `status()` or `status_file()`.
- Consequence: All files fail the git check — either crash loop or nothing gets indexed during rebase
- Mitigation: Catch `pygit2.GitError` in the status check. On error, fall back to current behavior (skip git filtering for this cycle, log warning). Do NOT cache the error — retry next cycle, the lock may be released.

**Resource Exhaustion: Per-file status in polling monitor**
- Assumption: `status_file()` per file is fast enough
- Betrayal: 5000 files × `status_file()` = 5000 disk reads per cycle. On spinning disk or network filesystem, this could take seconds.
- Consequence: Polling cycle takes 10x longer than expected, delays change detection
- Mitigation: Use bulk `repo.status()` once per polling cycle. Returns dict of ~100 dirty files. Dict lookup per file is O(1). Only the event handler path uses per-file `status_file()` (infrequent).

**Input Hostility: Symlinks**
- Assumption: `file_path` is a real file path
- Betrayal: `file_path` is a symlink. `status_file()` follows or doesn't follow — depends on git config `core.symlinks`.
- Consequence: Symlinked files may be incorrectly skipped or included
- Mitigation: Low risk for initial implementation. Document as known limitation. Git's own behavior with symlinks is platform-dependent — matching git's behavior is correct.

### pygit2 Repository Caching

**State Corruption: `.git` directory disappears**
- Assumption: Repository object remains valid for handler lifetime
- Betrayal: User deletes or corrupts `.git` after handler init. Subsequent `status()` calls fail.
- Consequence: Every file fails git check — crash or full skip depending on error handling
- Mitigation: Wrap every `status()` / `status_file()` call in try/except. On persistent failure (3 consecutive errors), set `_git_repo = None` and log. Fall back to pattern-only filtering.

## Blocked by
None — first task.

## Unlocks
Clean foundation for all subsequent Phase 1 work (LSP client, schema additions).

## Log

- [2026-03-30T03:40:44Z] [Seth] SRE review complete. Key corrections: (1) file path was wrong (chunkhound/providers/database/ not chunkhound/db/), (2) _should_index is on SimpleEventHandler not RealtimeIndexingService, (3) regex search has same substring bug, (4) user decided: prefix default + fuzzy_path=True opt-in for substring. Failure catalog added with 8 entries. Success criteria expanded 3→10.
- [2026-03-30T04:09:53Z] [Seth] Fresh-session SRE review (13-cat). Two critical findings: (1) fuzzy_path threading plan missed 5 intermediate method signatures (SearchService.search_semantic, SearchService.search_regex_async, SingleHopStrategy.search, MultiHopStrategy.search, SerialDatabaseProvider.search_regex_async) — updated impl steps 5+8. (2) Existing test test_path_filter_monorepo_mismatch.py explicitly validates substring matching — will break on prefix default, must be updated with fuzzy_path=True — added to success criteria. Also strengthened criteria 1+2 to require SearchService-level testing, not just provider. All architecture claims and line numbers verified via LSP. Failure catalog validated — 8 entries sound, no new runtime failure modes. APPROVED with amendments applied to skeleton.
- [2026-03-30T07:00:23Z] [Seth] Debrief: (1) Subrepo boundary regression caught by existing test — discover_repository per-dir fixed it. (2) GIT_STATUS_IGNORED removed from check because ignore engine already handles .gitignore. (3) KeyError from status_file returns True (allow) for files unknown to git. (4) fuzzy_path NOT threaded through find_similar_chunks or code_research — both use path as scope limiter where prefix default is correct. Reflections: skeleton's one-repo-cache was wrong for subrepos, 5 missing intermediate layers caught by SRE. Memory: git check design decisions saved.
