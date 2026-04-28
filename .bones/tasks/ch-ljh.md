---
id: ch-ljh
title: Phase 5 acceptance shakedown bugfixes
status: open
type: epic
priority: 1
depends_on: [ch-3zc, ch-wma, ch-yss, ch-qxc, ch-7s3, ch-nr0, ch-eoe]
---





## Context
Discovered during ch-9xh (Phase 5 acceptance) Demo 1. Attempting to demo `search(type=semantic)` vs `search(type=structural)` on a freshly reindexed LanceDB surfaced multiple issues. ch-9xh cannot close until these are resolved or triaged.

All six bugs trace back to the LanceDBProvider / indexer surface area. None are in Phase 5's own code (GraphWalkExpander + prompt templates) — the acceptance demo just exercised the surrounding infrastructure heavily enough to expose them.

## Children
- **ch-3zc** P1 — `get_stats` returns 0 for files/chunks due to `.to_pandas()` executor timeout (blocks accurate stats on any large DB)
- **ch-qxc** P1 — `search(type=semantic)` times out on populated LanceDB (blocks demo directly)
- **ch-7s3** P2 — `_deserialize_metadata` fails on pandas NaN (observed in daemon log; possible contributor to ch-qxc)
- **ch-yss** P2 — `get_stats` MCP tool silently swallows provider exceptions (hid ch-3zc for weeks)
- **ch-wma** P3 — LanceDBProvider disconnect asymmetry (latent; footgun)
- **ch-nr0** P3 — Watchdog polling fallback + log spam on chunkhound's own tree (DX issue)

## Suggested Order
1. **ch-3zc + ch-yss together** — they are paired (fix + observability of fix). Small, focused.
2. **ch-7s3** — narrow, may be a prerequisite for ch-qxc's instrumentation step.
3. **ch-qxc** — requires instrumentation (Step 1-2 mandatory) to diagnose properly. Unblocks ch-9xh.
4. **ch-wma** — wider refactor; benefits from ch-3zc landing first so truthy-check cleanup doesn't conflict.
5. **ch-nr0** — DX polish; lowest priority.

## Success Criteria
- [ ] All 6 child bugs closed
- [ ] ch-9xh (Phase 5 Acceptance) demo can run end-to-end without timeouts or 0-stats
- [ ] Live verification: `get_stats`, `search(type=semantic)`, `search(type=structural)` all return correct results on the current indexed DB
- [ ] Daemon log quiet under idle operation (no polling spam)

## Anti-Patterns
- NO batch fixing across unrelated files in one PR — each bug ships independently
- NO relaxing the 30s executor timeout — it's a hard budget, not a tunable
- NO reindexing the DB to "fix" the problems — the existing 14-hour LanceDB index is valid data; fixes are read-path improvements

## Key Considerations
- User's 14-hour LanceDB index is precious — DO NOT drop or recreate it during these fixes. All fixes are read-path changes or small write-path improvements that don't invalidate existing data.
- Stale subagent worktree was removed by user before this epic opened; dir count is now 610 (still > threshold for ch-nr0).
- Phase 5 acceptance task (ch-9xh) is blocked by this epic's outcomes — once ch-3zc, ch-qxc, ch-7s3 are done, the demo should work.

## Log

- [2026-04-20T18:40:11Z] [Seth] Parent epic for 6 bugs found during ch-9xh Phase 5 acceptance Demo 1. Blocks ch-9xh closure. Suggested order in skeleton.
- [2026-04-20T21:27:48Z] [Seth] Adversarial finding (ch-3zc scope): _executor_symbol_stats at lancedb_provider.py:2413-2416 has no per-table try/except around count_rows(). Inconsistent with improved _executor_get_stats pattern. Fragment read errors propagate instead of degrading gracefully with logger.warning. Non-blocking for Phase 5 demo; consider aligning when ch-yss work lands for symmetric provider-side robustness.
