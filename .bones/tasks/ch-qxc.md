---
id: ch-qxc
title: 'Bug: semantic search times out on populated LanceDB (30s executor budget exceeded)'
status: open
type: bug
priority: 1
parent: ch-ljh
---


## Context
`search(type=semantic)` on a populated LanceDB times out with `Operation '_executor_search_semantic' timed out` after 30s. Blocks agents from using semantic search on large indexes. Discovered during Phase 5 acceptance shakedown (ch-9xh Demo 1).

**Reproduction:**
1. Populate LanceDB with 80K+ chunks indexed with voyage-4 embeddings
2. Call MCP `search(type=semantic, query="any query", page_size=20)`
3. Observe TimeoutError after 30s (no results)

## Diagnosis (needs fix-time instrumentation)
Suspected slow operation in `LanceDBProvider._executor_search_semantic` (`chunkhound/providers/database/lancedb_provider.py:1575-1686`). Candidates:

1. `self._chunks_table.head(min(100, chunks_count)).to_pandas()` (line ~1605) — 100 rows, but embedding deserialization may be expensive with fixed-size 1024-dim floats. Normally ~400KB, should be fast.
2. Vector search with post-filter: `.search(emb).where("provider = ... AND model = ... AND embedding IS NOT NULL")` (lines ~1630-1640) — the where clause may force a full-table scan if pushdown fails or if filter columns aren't indexed.
3. `_deserialize_metadata` on each result (line ~1668) — related to ch-7s3; NaN metadata causes TypeError which might cascade.
4. Vector index rebuild during daemon indexing — if the daemon is writing while search reads, LanceDB may serialize on the fragment.

Fix location: `chunkhound/providers/database/lancedb_provider.py:_executor_search_semantic:1575-1686`.

## Implementation

### Step 1: Diagnostic instrumentation (NO FIX YET)
File: `chunkhound/providers/database/lancedb_provider.py:_executor_search_semantic`

Add `time.perf_counter()` timers around each block:
- count_rows()
- head().to_pandas() sample
- sample embeddings_mask apply
- vector search build (`.search().where().limit()`)
- `.to_list()` materialization
- result formatting loop (including `_deserialize_metadata`)

Log each block's elapsed time at INFO level with a unique tag.

### Step 2: Reproduce with instrumented build
Make sure daemon is NOT actively indexing (to isolate read from write):
```
# Stop daemon if running
# Re-run MCP search(type=semantic) on populated DB
```
Capture log output. Identify the slow block (expected >5s).

### Step 3: Write failing test based on diagnostic finding
File: `tests/integration/test_lancedb_search_semantic.py` (new)
Test: `test_semantic_search_completes_in_budget_on_populated_db`
- Fixture: 50K-chunk LanceDB with fixed-size embeddings, a single provider/model combination
- Call `_executor_search_semantic` with a query vector
- Assert completion within 10s
- Assert non-empty results

### Step 4: Run test
Expect timeout or >10s.

### Step 5: Fix based on diagnostic finding
Most likely fixes (pick based on Step 2 evidence):

**If sample `.to_pandas()` is slow:**
- Replace sample-check with a narrow count query: `self._chunks_table.search().where(f"provider = '{provider}' AND model = '{model}' AND embedding IS NOT NULL").limit(1).to_list()` and check if non-empty
- Drops the `embeddings_mask` call entirely

**If vector `.where().to_list()` is slow (post-filter issue):**
- Investigate LanceDB filter pushdown for the (provider, model) columns
- Consider adding a scalar index on (provider, model) or a composite column
- Alternative: remove the `AND embedding IS NOT NULL` since the main path requires a vector match anyway

**If metadata deserialization is the hot loop:**
- Depends on ch-7s3 fix. Land ch-7s3 first, retest.

**If index rebuild contention:**
- Document as known limitation; user stops daemon before large queries
- Or: add a fast-path that bypasses the embeddings-exist check when we have high confidence (e.g., when chunks_count > N)

### Step 6: Run test
Expect pass.

### Step 7: Remove or gate instrumentation
Either remove the `time.perf_counter()` logs or wrap them in `if os.getenv("CHUNKHOUND_DEBUG_TIMING"):` so they stay as a diagnostic tool but don't pollute default logs.

### Step 8: Run targeted suite
```
uv run pytest tests/integration/test_lancedb_search_semantic.py tests/integration/test_lancedb_*.py -v
```

### Step 9: Commit
```
git add -u && git commit -m "fix(lancedb): unblock semantic search on populated DB by <chosen fix>"
```

## Success Criteria
- [ ] Semantic search completes within 10s on 50K-chunk fixture
- [ ] MCP `search(type=semantic)` returns results on this repo (live verification)
- [ ] Instrumentation either removed or behind env flag (not default-noisy)
- [ ] Test fixture locks in the budget (will catch future regressions)
- [ ] Existing LanceDB tests still pass

## Anti-Patterns
- NO removing the `where` clause for correctness (multi-provider coexistence)
- NO bumping `CHUNKHOUND_DB_EXECUTE_TIMEOUT` — treat the budget as hard constraint, fix the slow path
- NO guessing at the fix without instrumentation — Step 1-2 mandatory

## Key Considerations
- This bug has multiple candidate root causes. Instrumentation is mandatory — don't skip Step 1-2.
- ch-7s3 (NaN metadata) may be a blocker if deserialization is the slow step. Check ch-7s3 status before starting.
- Daemon indexing concurrency may affect reproduction. Always document whether the daemon was running during the timing measurement.
- This bug is P1 because it blocks the Phase 5 acceptance demo. Resolving it unblocks ch-9xh.

## Log

- [2026-04-20T18:40:11Z] [Seth] Diagnosis needs fix-time instrumentation. Likely candidates: head().to_pandas() sample check, .search().where() post-filter, _deserialize_metadata loop (see ch-7s3). Step 1-2 in skeleton mandatory before fix.
