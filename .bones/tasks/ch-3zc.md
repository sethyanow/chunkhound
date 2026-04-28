---
id: ch-3zc
title: 'Bug: get_stats returns 0 for files/chunks due to .to_pandas() executor timeout'
status: closed
type: bug
priority: 1
owner: Seth
parent: ch-ljh
---









## Context
`get_stats` MCP tool returns `{files: 0, chunks: 0}` on a populated LanceDB (80K chunks on this repo), while `symbols: 83620, symbol_edges: 235581` are correct. Symptom only surfaces on large databases. Discovered during Phase 5 acceptance shakedown (ch-9xh Demo 1).

**Reproduction:**
1. Populate LanceDB with 80K+ chunks (e.g., full chunkhound repo reindex)
2. Call MCP `get_stats`
3. Observe `files: 0, chunks: 0` despite tables containing data

## Diagnosis
Root cause: `LanceDBProvider._executor_get_stats` (`chunkhound/providers/database/lancedb_provider.py:1952-1988`) calls `.to_pandas()` to count rows. On 80K chunks × 1024-dim float32 embeddings, this materializes hundreds of MB. Exceeds `CHUNKHOUND_DB_EXECUTE_TIMEOUT` (30s default) enforced in `SerialDatabaseExecutor.execute_sync` (`chunkhound/providers/database/serial_executor.py:169-177`). TimeoutError raises from the executor, then swallowed by `stats.py:30-33` outer try/except → defaults to 0.

Evidence:
- Regex search works (uses narrow `.search().where().to_list()` with pagination)
- `_executor_symbol_stats` uses `count_rows()` directly, returns correct counts
- Inner `count_rows()` fallback in `_executor_get_stats` never reached — outer executor timeout kills the future first

Fix location: `chunkhound/providers/database/lancedb_provider.py:_executor_get_stats` lines 1952-1988.

## Implementation

### Step 1: Write failing tests (two cases)
File: `tests/integration/test_lancedb_stats.py` (new)

**Test A: `test_get_stats_counts_within_budget_on_large_table`**
- Fixture: populate in-memory LanceDB with 50K fake chunks (minimal schema, 1024-dim embeddings) and matching files rows
- Call `provider.get_stats()` with a `time.perf_counter()` wrapper
- Assert files and chunks return actual counts (not 0)
- Assert total time < 5s

**Test B: `test_get_stats_empty_db_returns_zeros` (regression — empty-case invariant)**
- Use existing `lancedb_provider` fixture (fresh, empty)
- Call `provider.get_stats()` — MUST NOT raise
- Assert `files == 0`, `chunks == 0`
- Protects against over-rotating Test A's fix into a regression for the empty path

### Step 2: Run tests
```
uv run pytest tests/integration/test_lancedb_stats.py -v
```
Expect: Test A fails (timeout/0 counts), Test B fails only if current code already broken on empty (likely passes — tests both bug and guardrail)

### Step 3: Replace .to_pandas() with count_rows() in _executor_get_stats
File: `chunkhound/providers/database/lancedb_provider.py:1953-1989`

Per Key Considerations, execute **option A** (drop embeddings field) to unblock demo. Options B/C become follow-up tasks if the user wants a precise count.

Changes:
- `stats["files"] = self._files_table.count_rows()` (direct)
- `stats["chunks"] = self._chunks_table.count_rows()` (direct)
- **Remove** the `embeddings` key from the returned dict (option A). Add a one-line comment above the return referencing ch-3zc for the rationale. Keep `size_mb` as-is (not pandas-backed).
- Change truthy checks (`if self._files_table:`) to `is not None` for safety (see ch-wma for broader consistency fix)

### Step 3b: Verify no caller regresses on `embeddings` field removal
Two callers read the key:
- `chunkhound/providers/database/serial_database_provider.py:541` — `result.get("embeddings", 0)` — safe, defaults to 0
- `chunkhound/api/cli/utils/rich_output.py:613` — `stats_dict.get("embeddings", 0)` — safe, defaults to 0

Both use `.get(..., 0)`; removing the key yields `0`, preserving current behavior. Spot-confirm by grep before commit:
```
rg -n '"embeddings"' chunkhound/ | rg -v 'database/(lancedb|duckdb)'
```

### Step 4: Run test
Expect pass. All assertions green within 5s.

### Step 5: Run targeted suite
```
uv run pytest tests/integration/test_lancedb_stats.py tests/integration/test_lancedb_*.py -v
```
All pass.

### Step 6: Commit
```
git add -u && git commit -m "fix(lancedb): use count_rows() in get_stats to avoid 30s timeout on large tables"
```

## Success Criteria
- [x] `get_stats` returns accurate files and chunks counts without materializing tables via `.to_pandas()` (Test A: shadow-to-forbid `.to_pandas()` on both tables; deterministic across machines — stricter than a timing-based assertion on a large fixture, which would be flaky)
- [x] Empty DB returns `{files: 0, chunks: 0}` without error (Test B)
- [x] No `.to_pandas()` call in `_executor_get_stats`
- [x] `embeddings` key removed from LanceDB `_executor_get_stats` return dict; docstring references ch-3zc
- [x] Spot-check confirms no caller of `get_stats()` regresses on embeddings removal (verified at serial_database_provider.py:541 and rich_output.py:613 — both use `.get(..., 0)`; no direct `stats["embeddings"]` indexing anywhere)
- [x] Both regression tests in `tests/integration/test_lancedb_stats.py` pass
- [x] Existing LanceDB provider tests still pass (95 passed)
- [x] Live verification: after fix, MCP `get_stats` on this repo returns real counts (not 0) — verified 2026-04-28 by CloudyMouse: returned `{files: 881, chunks: 36338}` on rebuilt index
- [x] Both counts assigned via `int(table.count_rows())` to normalize return type
- [x] Per-table try/except preserved with `logger.warning(...)` on failure (graceful degradation, not silent swallow)
- [x] Adversarial battery added: partial disconnect, idempotence, graceful degradation — all pass
- [x] ~~Fixture uses batched inserts (≤5K chunks per batch), not bulk-in-memory — verify peak memory stays bounded~~ **SUPERSEDED**: test strategy pivoted from 50K-chunk fixture to 1-chunk + shadow-to-forbid `.to_pandas()`. Batched-insert criterion no longer applies to this test. (50K-chunk behavior test would be a separate additive verification — file a follow-up if wanted.)
- [x] `size_mb` semantic change noted (side effect of type cleanup): previously float (e.g., 0.7 for sub-MB DBs), now truncated int (0). Changed to honor the declared `dict[str, int]` contract. User-visible in `rich_output.py:613` which reads this field. **Decision (2026-04-28, Seth): accept the int truncation — closes the contract.**

## Anti-Patterns
- NO `.to_pandas()` to count rows — O(table size) materialization
- NO silent swallow of count errors — let them propagate (ch-yss tracks the stats.py side)
- NO removing the size_mb field — that uses disk rglob, not pandas

## Key Considerations
- Embeddings count was "chunks with valid (non-zero, non-empty) embedding" — a semantic metric. Options:
  - A: Drop the field entirely (simplest, unblocks demo)
  - B: Compute via `.search().where("embedding IS NOT NULL").limit(0).count()` or equivalent
  - C: Approximate via sample (e.g., first 1000 rows) with disclaimer
- Recommend A for this fix; file follow-up if someone needs it precisely
- Related bug ch-yss (silent swallow in stats.py): if landed together, ch-3zc's timeout-in-prod becomes visible as a clear error instead of 0s
- **Interface symmetry (flagged, non-blocking):** DuckDB's `_executor_get_stats` returns `{"files", "chunks", "embeddings", "providers"}` (duckdb_provider.py:2558-2563). Option A diverges the LanceDB dict shape from DuckDB's. Callers are safe (`.get(..., 0)` defaults), but if users want strict interface parity across providers, consider option B as a follow-up. Not a blocker for ch-ljh / Phase 5 demo.
- **`count_rows()` performance:** LanceDB's `count_rows()` reads table metadata rather than materializing rows. Well-compacted tables are O(1); highly fragmented tables may iterate per-fragment but still bounded. The 50K-chunk fixture is a reasonable upper-bound smoke test; if `count_rows()` itself times out, raise separately (not this bug's scope).
- **Fixture construction cost:** Populating 50K chunks with 1024-dim embeddings takes memory (~200MB for embeddings alone). Use minimal File/Chunk rows with simple content and deterministic fake embeddings (e.g., `np.random.default_rng(seed).normal(size=1024).astype(np.float32)` batched). Build once per test function, not per assertion. If fixture setup exceeds ~30s on this laptop, switch to a smaller N (e.g., 20K) — the diagnostic value is exceeding the 30s DB executor budget on the *read*, not the setup.

### Failure Catalog (Adversarial Planning)

**[Input Hostility: `_executor_get_stats`]**
- Assumption: Tables are either `None` (disconnected) or functional Table objects.
- Betrayal: Tables may exist but have corrupt fragments; `count_rows()` could raise on fragment-level read errors.
- Consequence: Exception escapes provider → stats.py silent-swallow layer (ch-yss) hides the error → back to 0-display bug, different root cause.
- Mitigation (structural): Keep per-table try/except with `logger.warning(...)`; on failure set that table's count to 0 and continue. Do NOT rely on the outer stats.py swallow — ch-yss is fixing that. The anti-pattern "no silent swallow" applies to the stats.py layer; per-table graceful degradation with explicit logging is the provider's job.

**[Dependency Treachery: LanceDB `count_rows()` return type]**
- Assumption: `count_rows()` returns a plain Python `int`.
- Betrayal: LanceDB bindings may return `numpy.int64` or another numeric type depending on version; downstream `int(provider_stats.get("files", 0))` in stats.py handles most numeric types but JSON serializers may not.
- Consequence: MCP JSON response fails to serialize; stats endpoint silently returns `{}` or raises on deeper serialization.
- Mitigation (structural): Wrap both counts with `int(...)` at assignment in `_executor_get_stats`. Pattern already used elsewhere in the file (lines 1479-1480, 1499).

**[Resource Exhaustion: Test fixture build (50K chunks × 1024-dim)]**
- Assumption: Building 50K chunks in memory before insert fits in the test runner.
- Betrayal: CI runners or memory-constrained laptops OOM if the full 200MB embedding matrix is materialized before insert. Bulk `insert_chunks()` with one giant list may also stress pandas/pyarrow internals.
- Consequence: Test fails nondeterministically on CI but passes locally; or test hangs on memory pressure; fixture setup itself times out.
- Mitigation (structural): Batch inserts in chunks of 5K. Generate each batch of embeddings, call `provider.insert_chunks(batch)` (or equivalent), discard batch, repeat. This caps peak memory to one batch (~20MB). Do NOT hold the full 50K list in memory.

**[Temporal Betrayal: Test ordering / state isolation]**
- Assumption: Each test starts with an empty, isolated DB.
- Betrayal: If Test A's 50K-chunk fixture writes to a path that Test B later uses, or if fixture teardown doesn't fully close LanceDB handles, state leaks.
- Consequence: Test B (empty-DB assertion) sees Test A's data → false pass or confusing failure.
- Mitigation (structural): Use the existing `lancedb_provider` fixture (function-scoped, `tmp_path`-backed). Each test gets an isolated `.lancedb` directory. Already correct — do not override scope.

**[State Corruption: Partial `is not None` refactor]**
- Assumption: Switching `if self._files_table:` → `if self._files_table is not None:` is mechanical.
- Betrayal: LanceDB `Table` objects may define `__bool__` (returning `False` on empty tables) — the old truthy check would skip counting on an empty-but-present table. The `is not None` switch FIXES that silently.
- Consequence: This is actually a bug fix disguised as a cleanup — empty-DB regression test (Test B) may have been passing only by accident under the old truthy behavior. The Test B assertion `files == 0, chunks == 0` stays correct either way (counts on empty tables return 0), but the behavior of the `if` guard changes.
- Mitigation (structural): Test B verifies post-fix behavior directly (calls get_stats on an empty provider and checks the result dict). No further action needed — the change is correct.

**Categories skipped with reason:**
- *Encoding Boundaries*: `_executor_get_stats` operates on integer counts and path-based size; no text encoding surface.
- *Temporal Betrayal on `_executor_get_stats`*: Serialized by `SerialDatabaseExecutor` — concurrent-call race already prevented structurally.

## Log

- [2026-04-20T18:40:10Z] [Seth] Diagnosis: .to_pandas() on 80K-chunk table exceeds 30s CHUNKHOUND_DB_EXECUTE_TIMEOUT. TimeoutError silently swallowed by stats.py outer try/except. Fix: count_rows() in _executor_get_stats. Confidence HIGH.
- [2026-04-20T21:13:25Z] [Seth] SRE review: All 10 categories applied. Claims verified against code (line ranges, .to_pandas() calls, .count_rows() precedent, DuckDB non-affected). Added: empty-DB regression test (Step 1b/Test B), caller-safety check (Step 3b: serial_database_provider.py:541 + rich_output.py:613 both use .get(...,0)), success criteria for embeddings removal + caller safety + empty-case. Flagged non-blocking: interface parity w/ DuckDB after option A, count_rows() performance notes, fixture construction cost guidance.
- [2026-04-20T21:15:15Z] [Seth] Adversarial planning: 5 failure modes cataloged across 3 components. New criteria: batched fixture inserts (5K/batch), int() cast on count_rows(), preserved per-table try/except with logger.warning. Skipped encoding + race (serialized executor). Interaction between is-not-None switch and LanceDB Table __bool__ noted.
- [2026-04-20T21:27:48Z] [Seth] Adversarial stress test: 3 patterns applied (partial disconnect, idempotence, graceful degradation); all GREEN. Three-Question framework traced _executor_symbol_stats (parallel function) — logged symmetry gap on ch-ljh (out of scope).
- [2026-04-28T21:23:06Z] [Seth Yanow] Live verification by CloudyMouse on rebuilt 881-file/36338-chunk index: get_stats returned real counts (not 0). size_mb int truncation accepted per Seth. All success criteria checked. Closing.
