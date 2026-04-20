---
id: ch-3zc
title: 'Bug: get_stats returns 0 for files/chunks due to .to_pandas() executor timeout'
status: open
type: bug
priority: 1
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

### Step 1: Write failing test
File: `tests/integration/test_lancedb_stats.py` (new)
Test: `test_get_stats_counts_within_budget_on_large_table`
- Fixture: populate in-memory LanceDB with 50K fake chunks (minimal schema, 1024-dim embeddings) and matching files rows
- Call `provider.get_stats()` with a `time.perf_counter()` wrapper
- Assert files and chunks return actual counts (not 0)
- Assert total time < 5s

### Step 2: Run test
```
uv run pytest tests/integration/test_lancedb_stats.py::test_get_stats_counts_within_budget_on_large_table -v
```
Expect failure: timeout or 0 counts.

### Step 3: Replace .to_pandas() with count_rows() in _executor_get_stats
File: `chunkhound/providers/database/lancedb_provider.py:1952-1988`
- `stats["files"] = self._files_table.count_rows()` (direct)
- `stats["chunks"] = self._chunks_table.count_rows()` (direct)
- Embeddings count: replace with narrow filter query:
  - If LanceDB SQL supports `WHERE embedding IS NOT NULL`: use a filtered count
  - Otherwise: remove the `embeddings` field from stats output entirely, add comment linking to this bug
- Change truthy checks (`if self._files_table:`) to `is not None` for safety (see ch-wma for broader consistency fix)

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
- [ ] `get_stats` on populated DB (≥50K chunks fixture) returns accurate files and chunks counts within 5s
- [ ] No `.to_pandas()` call in `_executor_get_stats`
- [ ] Regression test in `tests/integration/test_lancedb_stats.py` passes
- [ ] Existing LanceDB provider tests still pass
- [ ] Live verification: after fix, MCP `get_stats` on this repo returns real counts (not 0)

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

## Log

- [2026-04-20T18:40:10Z] [Seth] Diagnosis: .to_pandas() on 80K-chunk table exceeds 30s CHUNKHOUND_DB_EXECUTE_TIMEOUT. TimeoutError silently swallowed by stats.py outer try/except. Fix: count_rows() in _executor_get_stats. Confidence HIGH.
