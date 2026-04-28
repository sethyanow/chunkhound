---
id: ch-eoe
title: 'Bug: regex search returns total>0 but empty results array (page_size:0 in response)'
status: open
type: bug
priority: 1
parent: ch-ljh
---

## Context
MCP `search(type=regex)` reports a positive `total` but ships an empty `results` array. The pagination envelope also shows `page_size: 0` despite the caller requesting a non-zero `page_size`, and `has_more: true` paired with `next_offset: null`. Net effect: every regex search appears to find matches but returns nothing — silently breaks all regex-driven workflows on this DB.

Discovered 2026-04-28 by CloudyMouse during post-rebuild smoke test on chunkhound's own repo (`/data/projects/chunkhound`, 881 files / 36,338 chunks indexed).

**Reproduction:**
1. Populated LanceDB on chunkhound repo (881 files, 36,338 chunks, voyage-4 embeddings)
2. Call MCP:
   ```
   search(type="regex", query="class EmbeddingService", page_size=10)
   ```
3. Observed response:
   ```json
   {
     "results": [],
     "pagination": {
       "offset": 0,
       "page_size": 0,
       "has_more": true,
       "total": 4,
       "next_offset": null
     }
   }
   ```
4. Same shape for `page_size=5`. `total` consistent at 4.
5. `find_references`-style ground truth: `class EmbeddingService` appears in `chunkhound/services/embedding_service.py` and downstream — count of 4 is plausible.

## Diagnosis (needs instrumentation — multiple candidate root causes)

The response carries **three** anomalies, any of which could be the primary fault, and they may compound:

1. **`page_size: 0` in response despite request `page_size>0`** — the formatter is computing `page_size` from `len(results)` rather than echoing the request param, OR the request param is being clobbered en route to the executor.
2. **`results: []` while `total > 0`** — every match is being filtered/dropped between counting and serialization. This is the user-facing symptom.
3. **`has_more: true` with `next_offset: null`** — pagination state is inconsistent; if `has_more` is true, the caller has no way to advance.

### Suspected root causes (ranked by likelihood, all need probe)

**A. ch-7s3 NaN cascade in result formatter (HIGH likelihood — also flagged in ch-qxc as a co-conspirator)**
The regex search formatter loop calls `_deserialize_metadata` (`chunkhound/providers/database/lancedb_provider.py:173-175`). Per ch-7s3 diagnosis, pandas NaN in the `metadata` column raises `TypeError` from `json.loads(nan)`. If the formatter wraps the loop in a try/except that drops the entire batch on first failure, `total` (computed before the loop) stays correct while `results` ends up empty. This bug then disappears when ch-7s3 ships — but the formatter's swallow-on-error behavior would still be a latent reliability hazard.

**B. Pagination math drops all results structurally**
The regex executor (`_executor_search_regex` in `chunkhound/providers/database/lancedb_provider.py`) may slice `results[offset:offset+page_size]` after a count step that ran on a different result set. If `offset=0`, `page_size=10`, but the slice indexes are computed wrong (e.g., page_size becomes 0 from a min() with an empty intermediate variable), the slice is `[0:0]` → `[]`. The `page_size: 0` in the response would corroborate this.

**C. `page_size` request not threaded to executor**
MCP tool layer extracts `page_size` from the request, but doesn't pass it to the underlying `search_regex(...)` provider call. Provider defaults `page_size=0` (or unset, then min/max clamps it to 0), counts everything, returns nothing. The response `page_size: 0` is the provider's view, not the request's.

### Investigation order
1. Trace the `page_size` parameter from MCP tool entry → executor → provider. Identify where it goes from `10` to `0` (or never gets passed).
2. If parameter threading is correct, instrument the formatter loop with `time.perf_counter` and exception logs to detect per-row failures (parallels ch-qxc Step 1).
3. If exceptions are silently swallowed, check whether they're NaN-derived (→ ch-7s3 dependency) or something else.

## Implementation

### Step 1: Diagnostic instrumentation (NO FIX YET)
File: `chunkhound/providers/database/lancedb_provider.py:_executor_search_regex` (and any MCP-layer wrapper involved in regex routing — discover via LSP `findReferences` on `search_regex`).

- Log the inbound `page_size` and `offset` at the executor entry.
- Log `len(results)` after counting / before slicing.
- Wrap the formatter loop's try/except (if any) to log the exception type + message before swallowing. Do NOT change behavior in this step — just observe.

### Step 2: Reproduce with instrumented build
- Run MCP `search(type=regex, query="class EmbeddingService", page_size=10)` against the repo's DB.
- Capture log output. Identify which of root causes A/B/C is firing (or whether multiple compound).

### Step 3: Write failing tests based on diagnostic finding
File: `tests/integration/test_lancedb_search_regex.py` (new, or extend if it exists)

**Test A: `test_regex_search_returns_results_when_total_positive`**
- Fixture: small LanceDB with at least 5 chunks whose content matches a known regex.
- Call `_executor_search_regex(query, offset=0, page_size=10)`.
- Assert `len(results) == total` (or `len(results) == min(total, page_size)`).
- Assert `pagination["page_size"]` equals the requested `page_size` (echo invariant).
- Assert `pagination["has_more"]` is `False` when all results fit; if `True`, `next_offset` MUST not be `None`.

**Test B: `test_regex_search_with_nan_metadata_does_not_drop_batch` (depends on root cause)**
Skip if Step 2 rules out the NaN cascade. Otherwise:
- Fixture: insert chunks where one row has NaN in `metadata`.
- Call regex search that would match across NaN and non-NaN rows.
- Assert results contain the non-NaN matches (per-row swallow, not batch swallow).
- Assert the NaN row is logged but doesn't poison the batch.

### Step 4: Run tests
Expect failure on Test A (and Test B if applicable).

### Step 5: Fix based on diagnostic finding
Apply the minimal structural fix. Do NOT bundle multiple anomalies into one fix unless Step 2 proves they share a single root cause. Prefer ordering by independence:
- **Root cause C (param threading):** wire `page_size` end-to-end. Most likely a single missing kwarg in the MCP tool → provider call.
- **Root cause B (pagination math):** correct the slice / clamp logic. Add a unit test for the math itself.
- **Root cause A (formatter swallow):** narrow the try/except to per-row scope with explicit log; do NOT swallow the entire batch on one row's failure. If the deserialize failure is NaN-specific, this becomes a follow-up that depends on ch-7s3.

### Step 6: Run tests
Expect pass.

### Step 7: Run targeted suite
```
uv run pytest tests/integration/test_lancedb_search_regex.py tests/integration/test_lancedb_*.py -v
```

### Step 8: Live verification
Re-run MCP `search(type=regex, query="class EmbeddingService", page_size=10)` and confirm 4 results returned with `page_size: 10` in the envelope.

### Step 9: Remove or gate instrumentation
Either delete the `time.perf_counter` / probe logs, or wrap them in `if os.getenv("CHUNKHOUND_DEBUG_TIMING"):` (matches ch-qxc's convention).

### Step 10: Commit
```
git add -u && git commit -m "fix(lancedb): <chosen root cause> — regex search drops results on populated DB (ch-eoe)"
```

## Success Criteria
- [ ] Live verification: MCP `search(type=regex, query="class EmbeddingService", page_size=10)` returns 4 results on this repo's index, with `pagination.page_size == 10` (echoed) and `pagination.has_more` consistent with `next_offset` (both null OR both populated)
- [ ] Failing test (Test A) added that pins the `len(results) == min(total, page_size)` invariant for regex search
- [ ] Pagination invariants codified: `page_size` echoes request; `has_more=true` ⟹ `next_offset is not None`
- [ ] Step 2 instrumentation findings logged into this skeleton (which root cause(s) fired and how confirmed)
- [ ] If NaN cascade (root cause A) fires: link to ch-7s3 status; document whether ch-eoe fix lands first (per-row swallow) or waits on ch-7s3 (NaN-tolerant deserialize)
- [ ] Instrumentation either removed or gated on `CHUNKHOUND_DEBUG_TIMING` (no default-noisy logs)
- [ ] Existing LanceDB regex tests still pass

## Anti-Patterns
- NO swallowing batch-level exceptions to hide per-row failures — narrow the try/except to one row, log the row identifier, continue.
- NO computing `page_size` in the response from `len(results)` — echo the requested value; let `len(results)` speak for itself.
- NO `has_more=true` paired with `next_offset=null` — these two MUST move together. Add the invariant as an assertion at the boundary if needed.
- NO guessing the fix without Step 1-2 instrumentation. The three anomalies could be one bug or three; only evidence picks.
- NO bumping `CHUNKHOUND_DB_EXECUTE_TIMEOUT` or other budget knobs — fix the slow/broken path.

## Key Considerations
- **Discovered post-fix for ch-3zc.** ch-3zc's diagnosis explicitly stated "Regex search works (uses narrow `.search().where().to_list()` with pagination)" as evidence that the get_stats bug was specific to `.to_pandas()`. Either the regex path regressed since that diagnosis was written (2026-04-20), or the regex code path looked fine in isolation but broke in combination with something landed since. Worth a `git log -p` on `_executor_search_regex` between 2026-04-20 and now during Step 1.
- **Linkage to ch-7s3 and ch-qxc:** if Step 2 confirms NaN cascade is the root cause, ch-7s3 becomes a hard dependency for ch-eoe (and is already noted as a candidate root cause in ch-qxc). Promoting ch-7s3 to P1 may be warranted.
- **MCP envelope contract:** the inconsistency between `has_more: true` and `next_offset: null` suggests either the pagination wrapper is returning a partial envelope, or the underlying provider returns `None` for `next_offset` and the wrapper doesn't recompute. Worth checking `chunkhound/mcp/tools.py` (or wherever the regex tool returns) for envelope assembly logic.
- **DuckDB symmetry:** verify whether `DuckDBProvider._executor_search_regex` exhibits the same anomaly. If only LanceDB is affected, the bug is provider-local; if both, it's in the MCP tool layer or the SerialDatabaseProvider wrapper.
- **Symbols=0 / LSP servers=0** observed in the same `get_stats` call (`symbols: 0, symbol_edges: 0, languages: [], lsp_servers: 0`). Not certain whether that's expected post-rebuild (LSP enrichment hasn't run) or a separate regression. Out of scope for ch-eoe — file separately if it doesn't resolve once LSP enrichment completes.

## Log

- [2026-04-28T21:10Z] [CloudyMouse] Discovered during post-rebuild MCP smoke test on chunkhound's own DB (881 files, 36338 chunks). Regex search reproducibly returns `{results: [], pagination: {page_size: 0, has_more: true, total: 4, next_offset: null}}` for `class EmbeddingService` and `def __init__`. Three anomalies cataloged (results-empty, page_size-zero, has_more+next_offset inconsistency) with three candidate root causes (NaN cascade per ch-7s3, pagination-math slice bug, page_size param not threaded). Diagnosis confidence MEDIUM — needs instrumentation per Step 1-2.
