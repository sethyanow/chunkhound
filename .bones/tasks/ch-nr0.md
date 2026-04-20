---
id: ch-nr0
title: 'Bug: watchdog recursive limit falls back to polling on chunkhound''s own tree (610 dirs > 500 threshold)'
status: open
type: bug
priority: 3
parent: ch-ljh
---


## Context
The realtime indexing service's watchdog refuses to use recursive monitoring on trees with more than 500 directories, falling back to polling mode. On the chunkhound repo itself (610 dirs currently, after stale-worktree removal), this fallback triggers and the polling loop logs a WARNING every ~3 seconds for its entire lifetime: `Polling checked 5100 files, skipping rest`. Log spam on the core project is a bad look and obscures real warnings.

**Reproduction:**
1. On chunkhound repo's clean working tree (610+ dirs including `.git`, `.venv`, `.specstory`, `.claude`, etc.)
2. Start daemon
3. Check `.chunkhound/daemon.log`
4. Observe watchdog fallback + polling warnings spamming every ~3s

## Diagnosis
Two root causes in one bug:

**(a) Dir count threshold too low.** `_MAX_RECURSIVE_DIRS = 500` at `chunkhound/services/realtime_indexing_service.py:474`. Counted naively via `rglob("*")` — does not apply the exclude patterns from `.chunkhound.json`. Post-worktree-removal this repo has 610 dirs; most of them (e.g., inside `.git`, `.venv`) should be excluded from indexing entirely.

**(b) Polling loop log spam.** `_polling_monitor` at `chunkhound/services/realtime_indexing_service.py:534+` has a hardcoded 5000-file limit. When exceeded (every poll cycle on this tree), it logs at WARNING with no cooldown. Result: constant spam.

Evidence:
- `find . -type d -not -path '*/.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' | wc -l` → 610 (already excluding obvious dirs)
- Total dir count including those is much higher
- `_count_directories` (or equivalent) does not consult exclude patterns

Fix location:
- `chunkhound/services/realtime_indexing_service.py:441-470` (`_setup_watchdog` / `_start_fs_monitor`)
- `chunkhound/services/realtime_indexing_service.py:474` (`_MAX_RECURSIVE_DIRS`)
- `chunkhound/services/realtime_indexing_service.py:534-590` (`_polling_monitor` log spam)

## Implementation

### Step 1: Write failing tests
File: `tests/integration/test_realtime_indexing_watchdog.py` (or existing file if present)

- `test_dir_count_respects_exclude_patterns` — fixture repo with 600 dirs, 400 of them inside `.venv`, `.git` (default excludes). Count should be ~200, not 600.
- `test_watchdog_uses_recursive_on_excluded_tree` — same fixture; `_setup_watchdog` should succeed (use recursive), not fall back to polling.
- `test_polling_log_spam_rate_limited` — if polling IS used, the WARNING log fires at most once per N cycles (e.g., once per minute).

### Step 2: Run tests
```
uv run pytest tests/integration/test_realtime_indexing_watchdog.py -v
```
Expect failures.

### Step 3: Fix dir counting to respect excludes
File: `chunkhound/services/realtime_indexing_service.py`
- Locate the dir count logic (likely in `_start_fs_monitor` or a helper)
- Pass or load the exclude patterns from config
- Apply pattern matching (fnmatch or pathspec) before incrementing count
- Also use same excludes when setting up the recursive observer.schedule (so watchdog doesn't need to watch excluded dirs)

### Step 4: Fix polling log spam
File: `chunkhound/services/realtime_indexing_service.py:570-574`
- Track last-logged-time for the "Polling checked N files, skipping rest" warning
- Rate-limit to at most once per 60 seconds
- Alternative: log DEBUG after the first warning, so info is available but not spammy

### Step 5: (Optional) Raise threshold
Only if Step 3 alone doesn't solve it on the chunkhound repo:
- Increase `_MAX_RECURSIVE_DIRS` from 500 to 2000 (or make env-configurable: `CHUNKHOUND_WATCHDOG_MAX_DIRS`)
- Verify kernel fd limits aren't an issue on macOS/Linux

### Step 6: Run tests
Expect pass.

### Step 7: Live verification
Restart daemon on chunkhound repo. Tail `.chunkhound/daemon.log` for 5 minutes. Confirm: either watchdog is used (no polling warnings), or polling is rate-limited (≤5 warnings in 5 minutes).

### Step 8: Run full realtime indexing suite
```
uv run pytest tests/integration/test_realtime_*.py tests/integration/test_watchdog*.py -v
```

### Step 9: Commit
```
git add -u && git commit -m "fix(watchdog): respect excludes in dir count + rate-limit polling log spam"
```

## Success Criteria
- [ ] Dir count respects `.chunkhound.json` exclude patterns
- [ ] On this repo's tree, daemon uses watchdog (not polling) — OR — polling log spam rate-limited
- [ ] Regression tests pass; existing indexing tests still pass
- [ ] Daemon log clean after 5 minutes of idle operation (live check)

## Anti-Patterns
- NO silencing the warning without fixing the root cause (polling fallback is a real signal)
- NO arbitrary threshold bump without understanding the original fd concern
- NO removing polling as a fallback entirely — watchdog can legitimately fail on some environments

## Key Considerations
- The 500-dir threshold exists because watchdog's `observer.schedule(recursive=True)` walks the tree synchronously to register per-directory kernel watches. On slow filesystems this blocks startup. The threshold guards against this.
- After Step 3 (excludes in dir count), this may not need Step 5 — chunkhound's effective dir count (minus `.git`, `.venv`, `node_modules`, etc.) is well under 500.
- Default exclude patterns to add to `.chunkhound.json`'s `indexing.exclude` if not already present: `.git/**`, `.venv/**`, `node_modules/**`, `.claude/worktrees/**`. Check current defaults first.
- Related: the polling loop's 5000-file limit isn't itself the bug — it's a reasonable safety. The log spam is the bug.

## Log

- [2026-04-20T18:40:11Z] [Seth] Diagnosis HIGH confidence: _MAX_RECURSIVE_DIRS=500 triggered by 610-dir count (doesn't apply excludes). Polling loop has unrate-limited WARNING. Fix: apply excludes + rate-limit log.
