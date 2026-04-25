---
id: ch-qw4
title: EmbeddingService.process_batch deadlocks on token-limit recursion when max_concurrent_batches=1
status: open
type: bug
priority: 2
owner: Seth Yanow
---



## Context

`EmbeddingService._generate_embeddings_in_batches` (in `chunkhound/services/embedding_service.py`) creates `asyncio.Semaphore(self._max_concurrent_batches)`. The inner `process_batch` acquires the permit with `async with semaphore:`, performs the API call, then on token-limit error splits the batch in half and recursively awaits two more `process_batch` calls — all while still inside the `async with` block. `asyncio.Semaphore` is non-reentrant, so the recursive calls block waiting for a permit the parent owns. The semaphore scope also covers `_db.insert_embeddings_batch`, but the recursive path is reached on the API-exception branch before any outer DB insert, so there is no partial DB state to preserve when the lock is released.

No static `max_concurrent_batches` value is safe. The scheduler kicks off all initial batches with a single `asyncio.gather`, so N initial batches all hitting token-limit with `max=N` deadlocks regardless of `retry_depth` cap arithmetic. The bug is hold-and-wait, not sizing.

Latent because production gets `max_concurrent_batches` from `provider.get_recommended_concurrency()` (typically 8) and token-limit retry rarely fires; the existing suite never combined `max=1` with a forced token-limit. Surfaced by `tests/unit/test_ch_agj_adversarial_efgroups.py::TestRecursiveTokenLimitSplitPreservesTaskPassage`, currently working around it with `max_concurrent_batches=2` plus a 10s `@pytest.mark.timeout`.

## Approach

Scope `async with semaphore:` around exactly one attempt (API call + result validation + DB insert + per-attempt metrics). Perform token-limit split recursion after the scope exits, so no recursive `process_batch` is ever awaited while its parent holds a permit.

## Success Criteria

- `process_batch` no longer awaits recursive calls inside `async with semaphore:`.
- Existing adversarial test re-locked to `max_concurrent_batches=1`; the `max=2` workaround removed; 10s `@pytest.mark.timeout` retained.
- New adversarial test: schedule N initial batches with `max_concurrent_batches=N`, force token-limit on every initial batch, verify completion. Without the fix this hangs even at large N.
- Full unit + integration suite green.
- No new mypy errors in `chunkhound/services/embedding_service.py` (pre-existing errors stay for ch-6ea).

## Anti-Patterns to Avoid

- Bumping `max_concurrent_batches` default as a workaround — no static value is safe given gather scheduling.
- Touching the `task='passage'` literal at line 487 — that's ch-agj's contract.
- Fixing pre-existing mypy errors in this file — they belong to ch-6ea.

## Log

- [2026-04-25T21:58:15Z] [Seth Yanow] Found via ch-agj Checkpoint 3 adversarial battery (2026-04-25). EmbeddingService at chunkhound/services/embedding_service.py:461 creates asyncio.Semaphore(self._max_concurrent_batches); process_batch recurses INSIDE the 'async with semaphore' block at line 540-542 when a token-limit error triggers a batch split. asyncio.Semaphore is non-reentrant — with max_concurrent_batches=1 the recursive call waits forever for the permit held by the outer call. Real production default is 8 (or provider-recommended) so the bug is latent in normal use. Adversarial test at tests/unit/test_ch_agj_adversarial_efgroups.py::TestRecursiveTokenLimitSplitPreservesTaskPassage uses max=2 to bypass + 10s pytest timeout — keep that workaround in place until the deadlock is fixed. Fix candidates: (a) release semaphore before recursion via finally + re-acquire after split; (b) move recursion OUTSIDE the semaphore block (process_batch returns 'needs_split' sentinel, caller re-invokes); (c) document max_concurrent_batches >= 2 as a precondition (worst option — hidden footgun stays). Investigate which split halves still need rate-limiting; if rate-limit applies only to API call not retry, option (b) is cleanest.
- [2026-04-25T22:53:29Z] [Seth Yanow] Codex rescue (task-moew6xgt-rrloxj, xhigh effort, 8m34s) returned 4-point analysis. Confirmed deadlock mechanics. Two corrections to my analysis: (1) 'max>=4' is NOT a safe floor — gather scheduling at lines 654-659 kicks off ALL initial batches at once, so multiple top-level batches can occupy every permit before any split child competes; numerical floors are illusory, the bug is structural hold-and-wait. (2) DB write IS inside semaphore scope, but the recursive split path is reached on the API-exception BEFORE any outer _db.insert_embeddings_batch — releasing the semaphore before recursing does NOT skip an outer DB write, no half-state concern. Fix selected: hybrid-(a), lexically scope async-with-semaphore around one attempt (API + validation + DB insert + per-attempt metrics); token-limit split recursion happens AFTER scope exit. Lower-impact than (b) sentinel/worker-queue refactor; (c) documented floor rejected because no static N is safe. Test plan adds multi-initial-batch split case to catch the gather-concurrency angle. Skeleton updated.
