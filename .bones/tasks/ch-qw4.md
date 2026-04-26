---
id: ch-qw4
title: EmbeddingService.process_batch deadlocks on token-limit recursion when max_concurrent_batches=1
status: active
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
- New adversarial test: schedule N initial batches with `max_concurrent_batches=N`, force token-limit on every initial batch, verify completion AND that split recursion actually executed (`provider.embed.call_count > N` AND `total_generated > 0`). Without the fix this hangs even at large N; with a broken mock it would silent-pass without exercising the fix.
- Full unit + integration suite green.
- No new mypy errors in `chunkhound/services/embedding_service.py` (pre-existing errors stay for ch-6ea).

## Anti-Patterns to Avoid

- Bumping `max_concurrent_batches` default as a workaround — no static value is safe given gather scheduling.
- Touching the `task='passage'` literal at line 487 — that's ch-agj's contract.
- Fixing pre-existing mypy errors in this file — they belong to ch-6ea.

## Key Considerations (Failure Catalog)

Failure modes pre-identified during planning. Implementer should hold these in working memory while writing code.

**A1. `process_batch` (refactored outer) — Dependency Treachery: error catch breadth**
- Assumption: `_attempt_batch` swallows non-retryable errors and returns 0, matching today's `process_batch` `except Exception` breadth.
- Betrayal: If `_attempt_batch` lacks the same broad `except Exception` clause, exceptions bubble to `gather(..., return_exceptions=True)` (line 601) and follow the gather error path (lines 612-643), changing log output and metrics shape.
- Consequence: Previously-silent return-0 batches become loudly-logged failures; functional behavior preserved (count is still 0) but observability changes.
- Mitigation: `_attempt_batch` MUST have an `except Exception` clause matching today's breadth — token-limit branch returns `_SPLIT_SIGNAL` (when `len(batch) > 1` and `retry_depth < 3`); other paths log via the existing debug-file mechanism and return 0. The new outer `process_batch` is `try`/`finally` only — no `except`.

**A2. `process_batch` (refactored outer) — Resource Exhaustion: split fan-out under gather concurrency**
- Assumption: `retry_depth < 3` bound prevents unbounded recursion.
- Betrayal: With `max_concurrent_batches=N` permits and all N initial batches hitting token-limit at depth 0, fan-out is 2N awaitables at depth 1, 4N at depth 2, 8N at depth 3 — all competing for N permits. For N=4 that is 32 pending awaitables at peak.
- Consequence: Tens of pending awaitables in the queue under stress, but bounded — no unbounded recursion, no thread/process spawn.
- Mitigation: Existing `retry_depth < 3` check in the split branch already structurally bounds the fan-out. Don't relax it.

**A3. `process_batch` (refactored outer) — Temporal Betrayal: lexical lock invariant after refactor**
- Assumption: No `await process_batch(...)` is lexically inside `async with semaphore:` after the refactor.
- Betrayal: A future edit could re-introduce a recursive call inside the lock (e.g., adding a fast-path retry "while still holding the permit").
- Consequence: Re-introduces the original deadlock silently — the regression tests would catch it, but only at full-suite time.
- Mitigation: Post-refactor verification step: `rg -n "await process_batch" chunkhound/services/embedding_service.py` and visually confirm no recursive call sits inside the lock block. The 3 timeout-bounded tests are the behavioral guard.

**B1. `_attempt_batch` — State Corruption: partial timing markers on early raise**
- Assumption: Each `BatchTiming` records a clean span (`mark_embed_api_start` always followed by `mark_embed_api_end`).
- Betrayal: If `provider.embed` raises after `mark_embed_api_start` but before `mark_embed_api_end`, timing has start without end — `embed_api_ms` returns 0.0 (per `BatchTiming.embed_api_ms` guard at `batch_metrics.py:46`).
- Consequence: Same risk exists today; refactor introduces no regression.
- Mitigation: Pre-existing behavior preserved by `BatchTiming.embed_api_ms` guard. Not in ch-qw4 scope to fix.

**C1. Test 1 (`test_max_one_permit_with_token_limit_completes`) — Temporal Betrayal: timeout headroom**
- Assumption: Single batch of 2 chunks under max=1 with 1 fail + 2 success completes in <10s.
- Betrayal: Realistic asyncio test runner overhead is <100ms; mock calls are zero-cost.
- Consequence: 10s has ~100x headroom; near-timeout completion would indicate real perf regression, not flake.
- Mitigation: Treat any flake at the 10s boundary as a real bug, not a test issue.

**D1. Test 2 (`test_n_initial_batches_with_n_permits_completes`) — Dependency Treachery: assertion strength**
- Assumption: Test passes iff `_generate_embeddings_in_batches` returns within 10s.
- Betrayal: A broken mock (e.g., raises a non-token-limit exception on first call) short-circuits every batch via the swallow path. Function returns 0 quickly — test passes — but split recursion was never exercised. The fix is unverified.
- Consequence: Silent green — the worst kind. Test would still pass with the deadlock bug present if the mock pre-empts the recursion path.
- Mitigation: Add explicit assertions beyond mere completion: (a) `provider.embed.call_count > N` to confirm split halves were attempted; (b) `total_generated > 0` to confirm split halves succeeded; (c) optionally assert at least one call had `len(texts) < INITIAL_SIZE` to confirm split-sized calls happened.

**D2. Test 2 — Temporal Betrayal: mock state under concurrent gather**
- Assumption: Mock side_effect is invoked concurrently from N gather tasks.
- Betrayal: Stateful mocks (counter incremented on each call) can race under concurrent invocation; even though asyncio is single-threaded per loop, ordering is non-deterministic across N concurrent awaits, so a counter-based "first call fails" pattern fails on the wrong batch.
- Consequence: Test becomes order-dependent and flaky.
- Mitigation: Use stateless discrimination by `len(texts) == INITIAL_SIZE` per SRE Clarification 3. Initial-sized calls always raise; split-sized calls always succeed. No shared mutable state in the mock.

**Categories skipped with reason**
- **Encoding Boundaries** for all components: this is a control-flow refactor of an `async with` block; no data encoding/serialization/FFI boundary changes.
- **Input Hostility** for `process_batch`: input shape contract (list of `(ChunkId, str)`, batch_num, retry_depth) is unchanged from today; `len(batch) > 1` guard prevents zero-split pathology.

## Implementation Plan

**Files**

- `chunkhound/services/embedding_service.py` — refactor `process_batch` (current lines 463-571).
- `tests/unit/test_ch_qw4_semaphore_deadlock.py` — new regression file.
- `tests/unit/test_ch_agj_adversarial_efgroups.py` — re-lock at line 328; update class docstring (lines 289-298); drop workaround comments at lines 323-324.

**Seam shape**

Extract the API-call / length-validation / DB-insert / per-attempt timing markers into a nested helper:

```
async def _attempt_batch(batch, batch_num, retry_depth, timing) -> int | _SplitSignal
```

Outer `process_batch` shape: `try: async with semaphore: outcome = await _attempt_batch(...) ; if outcome signals split → recurse outside the lock ; finally: end_batch(timing)`. Sentinel form (singleton vs typed wrapper) is the executing agent's choice.

**Invariants the agent must preserve**

- `start_batch` / `end_batch` lifecycle stays at the OUTER level (one timing per top-level batch, as today).
- `mark_embed_*` / `mark_db_*` markers stay inside `_attempt_batch` (under the semaphore).
- Recursive `process_batch` calls happen AFTER `async with semaphore:` exits.
- `task="passage"` literal at line 487 — untouched (ch-agj contract).
- Pre-existing mypy errors — untouched (ch-6ea owns them).
- `return 0` swallow on non-retryable error — unchanged (separate concern).
- Debug-file logging at lines 551-562 and token-limit string match at lines 522-527 — unchanged.

**Steps (TDD)**

1. Write test `test_max_one_permit_with_token_limit_completes` in the new file. Intent: regression for the canonical `max=1` deadlock — single batch, first attempt forced to token-limit, splits succeed, `@pytest.mark.timeout(10)`.
2. Run the new test → expect `Failed: Timeout >10.0s` (not `AssertionError`).
3. Write test `test_n_initial_batches_with_n_permits_completes` (N=4 is fine). Intent: regression for the gather-concurrency angle. Monkeypatch `service._create_token_aware_batches` to return N pre-formed batches so the test controls fan-out; force token-limit on first attempt of every batch. Same 10s timeout.
4. Run the second test → expect timeout.
5. Re-lock `TestRecursiveTokenLimitSplitPreservesTaskPassage` to `max_concurrent_batches=1` at line 328. Rewrite the "OUT OF SCOPE for ch-agj" paragraph (lines 289-298) to a one-line note that ch-qw4 fixed the deadlock. Remove the workaround comments at lines 323-324. Keep `@pytest.mark.timeout(10)`.
6. Run the re-locked test → expect timeout.
7. Refactor `process_batch` per Seam Shape. The fix is correct iff no recursive `process_batch` call is ever awaited inside `async with semaphore:`.
8. Run the three previously-red tests → expect all green.
9. `uv run pytest -m unit` → green.
10. `uv run pytest -m "unit or integration"` → green (the pre-commit gate).
11. `uv run mypy chunkhound/services/embedding_service.py` → no NEW errors vs baseline.
12. Commit on `dev`: `fix(embed): break process_batch semaphore deadlock on token-limit recursion (ch-qw4)`.
13. `bn log ch-qw4` with summary; `bn close ch-qw4`.

## SRE Refinement Notes (2026-04-26)

Findings from fresh-session SRE review. None alter the seam-shape design choice; all are gap-fills the executing agent needs.

**Spot-check: code claims verified.** `chunkhound/services/embedding_service.py` lines confirmed exact match: `process_batch` def @463, `async with semaphore:` @469, `task="passage"` literal @487, recursive `process_batch` calls @540-541 (return sum @542), token-limit string match @522-527, debug-file logging @551-562, `asyncio.Semaphore(self._max_concurrent_batches)` @461. `asyncio.gather` @601 (the log entry's "lines 654-659" is wrong — those lines are inside `_create_token_aware_batches`; gather is at 600-601). Test file `tests/unit/test_ch_agj_adversarial_efgroups.py`: docstring @289-298, workaround comments @323-324, `max_concurrent_batches=2` @328 — all confirmed.

**Clarification 1 — `start_batch` placement under the semaphore.** `BatchTiming.start_time` is set to `time.perf_counter()` AT CONSTRUCTION (`chunkhound/core/diagnostics/batch_metrics.py:67`), and `total_latency_ms = end_time - start_time`. Today, `start_batch` is called INSIDE `async with semaphore:` (line 473-474), so wall-clock excludes permit-wait time. To preserve current metrics semantics, keep `start_batch` inside the semaphore in the new design (timing is created if `retry_depth == 0` right after acquiring the permit). The `try`/`finally` for `end_batch` must wrap the entire process_batch body so timing is closed even if `_attempt_batch` raises an unexpected exception. Sketch:
```
async def process_batch(batch, batch_num, retry_depth=0):
    timing = None
    try:
        async with semaphore:
            if self._metrics_collector and retry_depth == 0:
                timing = self._metrics_collector.start_batch(batch_num, len(batch))
            outcome = await _attempt_batch(batch, batch_num, retry_depth, timing)
        # OUTSIDE the semaphore
        if isinstance(outcome, _SplitSignal):
            mid = len(batch) // 2
            r1 = await process_batch(batch[:mid], batch_num, retry_depth + 1)
            r2 = await process_batch(batch[mid:], batch_num, retry_depth + 1)
            return r1 + r2
        return outcome
    finally:
        if timing and self._metrics_collector:
            self._metrics_collector.end_batch(timing)
```

**Clarification 2 — Sentinel form.** Recommend a module-private singleton sentinel: `_SPLIT_SIGNAL: Final = object()` (or a frozen dataclass if you prefer typing) and return-type annotation `int | _SplitSignal` where `_SplitSignal = type(_SPLIT_SIGNAL)`. The reason for a typed return (not `Optional[int]` with `None` meaning "split"): zero is a valid `int` return today (the `return 0` swallow path on non-retryable errors). Conflating zero/None with "split" is unsafe.

**Clarification 3 — Test 2 mock strategy under concurrent gather.** With `max_concurrent_batches=N` and N initial batches launched via `gather`, all N call `embed` concurrently in non-deterministic order. Discriminate first-attempt vs split by `len(texts)`:
```
INITIAL_SIZE = 4  # e.g., 4 chunks per initial batch
async def embed_side_effect(texts, **kwargs):
    if len(texts) == INITIAL_SIZE:
        raise Exception("max allowed tokens for this request")
    return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
```
This is robust to concurrent ordering — every initial-sized call fails, every split-sized call succeeds. To control fan-out, monkeypatch `service._create_token_aware_batches` to return exactly N pre-formed batches each of size `INITIAL_SIZE`.

**Clarification 4 — Test 1 dynamics.** With `max_concurrent_batches=1` and 1 batch of 2 chunks, the parent acquires the only permit, embed raises token-limit, the parent (under the fix) releases the permit, then split halves run sequentially under the single permit. Expected: 3 embed calls (1 fail + 2 success). Without the fix: the recursive call waits forever for the permit the parent holds — `gather` stalls, pytest-timeout fires at 10s.

**Clarification 5 — Verification post-refactor.** After Step 7, before declaring GREEN, grep for any remaining recursive call inside the lock: the structural test is "no `await process_batch(` line is lexically inside the `async with semaphore:` block in the refactored function." A simple `rg -n "process_batch" chunkhound/services/embedding_service.py` plus visual inspection of the new shape suffices. The 3 timeout tests passing is the behavioral confirmation.

**Clarification 6 — `_attempt_batch` error handling boundary.** `_attempt_batch` swallows non-retryable errors and returns `0` (preserves current `return 0` invariant), and returns `_SPLIT_SIGNAL` for token-limit-with-room-to-split. It should NOT raise — the outer `process_batch` already has no `except` clause in the new shape, only `finally`. If `_attempt_batch` raises an unexpected exception, it bubbles up through `gather(..., return_exceptions=True)` exactly as today.

## Log

- [2026-04-25T21:58:15Z] [Seth Yanow] Found via ch-agj Checkpoint 3 adversarial battery (2026-04-25). EmbeddingService at chunkhound/services/embedding_service.py:461 creates asyncio.Semaphore(self._max_concurrent_batches); process_batch recurses INSIDE the 'async with semaphore' block at line 540-542 when a token-limit error triggers a batch split. asyncio.Semaphore is non-reentrant — with max_concurrent_batches=1 the recursive call waits forever for the permit held by the outer call. Real production default is 8 (or provider-recommended) so the bug is latent in normal use. Adversarial test at tests/unit/test_ch_agj_adversarial_efgroups.py::TestRecursiveTokenLimitSplitPreservesTaskPassage uses max=2 to bypass + 10s pytest timeout — keep that workaround in place until the deadlock is fixed. Fix candidates: (a) release semaphore before recursion via finally + re-acquire after split; (b) move recursion OUTSIDE the semaphore block (process_batch returns 'needs_split' sentinel, caller re-invokes); (c) document max_concurrent_batches >= 2 as a precondition (worst option — hidden footgun stays). Investigate which split halves still need rate-limiting; if rate-limit applies only to API call not retry, option (b) is cleanest.
- [2026-04-25T22:53:29Z] [Seth Yanow] Codex rescue (task-moew6xgt-rrloxj, xhigh effort, 8m34s) returned 4-point analysis. Confirmed deadlock mechanics. Two corrections to my analysis: (1) 'max>=4' is NOT a safe floor — gather scheduling at lines 654-659 kicks off ALL initial batches at once, so multiple top-level batches can occupy every permit before any split child competes; numerical floors are illusory, the bug is structural hold-and-wait. (2) DB write IS inside semaphore scope, but the recursive split path is reached on the API-exception BEFORE any outer _db.insert_embeddings_batch — releasing the semaphore before recursing does NOT skip an outer DB write, no half-state concern. Fix selected: hybrid-(a), lexically scope async-with-semaphore around one attempt (API + validation + DB insert + per-attempt metrics); token-limit split recursion happens AFTER scope exit. Lower-impact than (b) sentinel/worker-queue refactor; (c) documented floor rejected because no static N is safe. Test plan adds multi-initial-batch split case to catch the gather-concurrency angle. Skeleton updated.
- [2026-04-26T06:42:17Z] [Seth Yanow] Implementation plan written via /fixing-bugs (writing-plans skill). TDD steps 1-13 cover regression tests (max=1 + N-batch gather-concurrency), seam-shape refactor, pre-commit gate, mypy-unchanged check. Ready for next session: executing-plans → SRE refinement → TDD execution.
- [2026-04-26T19:19:19Z] [Seth Yanow] SRE refinement (fresh session): 6 clarifications added to plan body — start_batch placement (preserve wall-clock semantics by keeping inside semaphore), sentinel form (typed singleton, not None — zero is a valid return), Test 2 mock pattern (discriminate by len(texts) for concurrent gather), Test 1 dynamics (3 embed calls expected), post-refactor verification (grep for await process_batch inside lock), and _attempt_batch error boundary (swallow non-retryable, return 0). All code line-claims spot-checked accurate. No design changes — gap-fills only.
- [2026-04-26T19:22:04Z] [Seth Yanow] Adversarial planning: 6 failure-catalog entries added to Key Considerations covering process_batch error breadth (A1), split fan-out under gather (A2), lexical lock invariant (A3), partial timing markers (B1), Test 1 timeout headroom (C1), Test 2 assertion strength (D1), Test 2 mock state (D2). Strengthened Test 2 success criterion to require asserting split recursion actually executed (call_count > N AND total_generated > 0) — guards against silent green from broken mocks. Encoding-boundaries and most input-hostility categories skipped with stated reason (control-flow refactor, no data shape changes).
