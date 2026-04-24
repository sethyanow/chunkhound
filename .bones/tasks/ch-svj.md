---
id: ch-svj
title: Plumb task param through embedding interface (jina-v3) + fix Voyage input_type
status: open
type: task
priority: 2
---

## Requirements

Add `task: str | None = None` parameter to the embedding provider interface and thread it to all call sites so that:

- **Query** embeddings send `task="retrieval.query"` (or provider equivalent)
- **Passage** (indexing) embeddings send `task="retrieval.passage"`
- **OpenAI-compatible** providers forward it as `extra_body={"task": ...}` — lets TEI-hosted jina-embeddings-v3 select the right LoRA adapter
- **Voyage** provider maps it to its native `input_type` param (`query` / `document`) — fixes a latent quality bug where Voyage currently always gets `input_type=None` for both queries and passages
- **`task=None`** preserves exact current behavior (no regression for models that don't use task adapters)

Unblocks adopting jina-embeddings-v3 without asymmetric-adapter quality loss. Incidental win: Voyage retrieval improves on the current production config.

## Context

Verified caller map (Apr 2026):

**Interface** — `chunkhound/interfaces/embedding_provider.py:76,90,104,119`

**Passage call sites** (pass `task="retrieval.passage"`):
| File | Line | Context |
|------|------|---------|
| `services/embedding_service.py` | 487 | Main embedding service during index |
| `services/indexing_coordinator.py` | 1596 | Top-level indexing pipeline |
| `services/research/shared/gap_detection.py` | 253 | Research: embed chunk bodies |
| `services/clustering_service.py` | 97, 199, 396 | K-means file clustering |

**Query call sites** (pass `task="retrieval.query"`):
| File | Line | Context |
|------|------|---------|
| `services/search/single_hop_strategy.py` | 66 | User query embedding for semantic search |
| `services/research/shared/gap_detection.py` | 461 | Research follow-up queries |

**Multi-hop:** delegates to `_single_hop_search` (`multi_hop_strategy.py:111`) for initial retrieval, so query embedding is single-sourced. Rerank calls (`multi_hop_strategy.py:132,234`) operate on raw text — cross-encoder, no task adapter concern.

**Non-obvious complication:** `openai_provider.py:_embed_batch_internal` has a recursive token-limit fallback path (lines 742-758) that passes `self._embed_batch_internal` as a bare callable to `handle_token_limit_error`. Recursive calls from smaller-batch retries will lose `task` unless we bind it via `functools.partial` or forward through the handler's kwargs.

**Re-index cost:** Enabling task-aware embedding changes the vector semantics. Any existing index must be rebuilt to benefit. Flagged in step 13 below.

**Rerank scope:** Unchanged. Rerankers are cross-encoders; they take (query, passage) pairs and don't use per-side task adapters. Not in this patch.

## Implementation

Follow TDD: failing test → minimal code → green → commit per unit.

### Step 1: Add task param to interface

**File:** `chunkhound/interfaces/embedding_provider.py`

Add `task: str | None = None` as a kwarg on four abstract methods:
- `embed(texts, task=None) -> list[list[float]]`
- `embed_single(text, task=None) -> list[float]`
- `embed_batch(texts, batch_size=None, task=None) -> list[list[float]]`
- `embed_streaming(texts, task=None) -> AsyncIterator[list[float]]`

Contract (docstring):
- `task=None` → provider default (no behavior change from today)
- Loose `str` (not Enum) so model-specific vocabularies work (`"retrieval.query"`, `"retrieval.passage"`, `"separation"`, `"classification"`, etc.)
- Unknown values → provider decides (forward as-is for OpenAI-compat; ignore with debug log for Voyage)

### Step 2: Failing test — OpenAI provider forwards task via extra_body

**File:** `tests/providers/test_openai_embedding_task.py` (new)

Mock `AsyncOpenAI` client. Assertions:
- `await provider.embed(["x"], task="retrieval.query")` → `embeddings.create(..., extra_body={"task": "retrieval.query"})`
- `await provider.embed(["x"], task=None)` → `embeddings.create(...)` with **no** `extra_body` kwarg (backward compat)
- `await provider.embed_batch(["x", "y"], task="retrieval.passage")` → same forwarding through the batch path

Run: `uv run pytest tests/providers/test_openai_embedding_task.py -v` → expect failure.

### Step 3: Make Step 2 pass — OpenAI provider plumbing

**File:** `chunkhound/providers/embeddings/openai_provider.py`

- Add `task: str | None = None` to public methods at lines 586 (`embed`), 639 (`embed_single`), 644 (`embed_batch`), 691 (`embed_streaming`). Thread through to `_embed_batch_internal(texts, task=None)`.
- In `_embed_batch_internal` (line 697), build kwargs conditionally:
  ```
  create_kwargs = {"model": ..., "input": texts, "timeout": ...}
  if task is not None:
      create_kwargs["extra_body"] = {"task": task}
  response = await self._client.embeddings.create(**create_kwargs)
  ```

Run Step 2 test → expect pass.

### Step 4: Failing test — recursive token-limit path preserves task

**File:** same test file

Mock a sequence: first call raises `openai.BadRequestError("maximum context length exceeded")`, recursive call with smaller batch succeeds. Assert **both** calls carry the same `extra_body={"task": "retrieval.passage"}`.

### Step 5: Make Step 4 pass — fix recursion

**File:** `chunkhound/providers/embeddings/openai_provider.py` lines 742-758

Replace bare `self._embed_batch_internal` passed to `handle_token_limit_error(embed_function=...)` with a partial that binds task:
```
from functools import partial
embed_fn = partial(self._embed_batch_internal, task=task)
return await handle_token_limit_error(embed_function=embed_fn, ...)
```

(If `partial` doesn't compose with instance methods cleanly, use a local async wrapper closure instead.)

### Step 6: Failing test — Voyage maps task to input_type

**File:** `tests/providers/test_voyage_embedding_task.py` (new)

Mock Voyage SDK client. Assertions:
- `task="retrieval.query"` → Voyage call with `input_type="query"`
- `task="retrieval.passage"` → Voyage call with `input_type="document"`
- `task=None` → Voyage call with `input_type=None` (current behavior)
- `task="unknown-value"` → Voyage call with `input_type=None` + debug log (do NOT crash)

### Step 7: Make Step 6 pass — Voyage provider

**File:** `chunkhound/providers/embeddings/voyageai_provider.py`

- Add `task` kwarg to the four public methods (lines 269, 359, 364, 394).
- Add a private `_task_to_input_type(task: str | None) -> str | None` helper with the mapping.
- Forward `input_type` to the Voyage SDK call sites.

### Step 8: Plumb through EmbeddingManager wrapper

**File:** `chunkhound/embeddings.py`

- `EmbeddingManager.embed_texts()` (line 151) — add `task: str | None = None` kwarg; forward via `provider.embed(texts, task=task)`.

### Step 9: Update PASSAGE callers

Add `task="retrieval.passage"` at these five call sites:
- `chunkhound/services/embedding_service.py:487`
- `chunkhound/services/indexing_coordinator.py:1596`
- `chunkhound/services/research/shared/gap_detection.py:253`
- `chunkhound/services/clustering_service.py:97, 199, 396`

**Clustering note:** K-means is symmetric — for jina-v3 the "correct" task is `separation`. Passage is safe fallback. Mark with a TODO comment to revisit if clustering quality issues surface.

### Step 10: Update QUERY callers

Add `task="retrieval.query"`:
- `chunkhound/services/search/single_hop_strategy.py:66`
- `chunkhound/services/research/shared/gap_detection.py:461`

### Step 11: Unit/integration test suite

Run: `uv run pytest -m "unit or integration" tests/ -v`

Expected: all green. Any new failure in `test_openai_provider*` or `test_voyage_provider*` is a genuine regression — investigate before continuing. Existing embedding tests must still pass with `task=None` default.

### Step 12: Type check + lint

- `uv run mypy chunkhound`
- `uv run ruff check chunkhound`

Fix any new errors introduced by signature changes.

### Step 13: Smoke test with real Voyage config

Prerequisite for declaring the Voyage fix works. Use the current `.chunkhound.json` (Voyage provider).

- Create scratch dir, small synthetic repo (5-10 code files)
- `chunkhound index` → verify indexing completes
- Enable Voyage debug logging, confirm `input_type=document` in index requests, `input_type=query` in search requests
- `chunkhound search "something"` → verify results are returned and look reasonable

**Re-index required:** the stored vectors change semantics. Existing indexes should be rebuilt to benefit from the fix. Document this in the commit message.

### Step 14: Commit

Message outline:
```
feat(embeddings): add task param to interface; fix Voyage input_type

- Thread task through embed/embed_batch/embed_single/embed_streaming
- OpenAI-compat: forward as extra_body={"task": ...} (enables jina-v3 LoRA adapters)
- Voyage: map task -> input_type (query/document) — was silently ignored
- Preserve recursive token-limit handler through functools.partial
- Update 7 call sites with explicit task="retrieval.query" or "retrieval.passage"

Re-index required to benefit. Existing indexes continue to work with prior semantics.
```

## Success Criteria

- [ ] `task: str | None = None` added to interface (4 methods)
- [ ] OpenAI provider forwards task via `extra_body` when set; passes nothing when `task=None`
- [ ] Recursive token-limit path preserves task across retries (tested)
- [ ] Voyage provider maps task to `input_type` correctly for all three cases (query, passage, None)
- [ ] Unknown task values don't crash — logged and ignored
- [ ] All 7 passage/query call sites pass explicit task
- [ ] `uv run pytest -m "unit or integration"` passes
- [ ] `uv run mypy chunkhound` passes
- [ ] `uv run ruff check chunkhound` passes
- [ ] Smoke test with live Voyage shows `input_type` in request logs
- [ ] Existing indexes continue to read correctly (no migration needed — just stale semantics)

## Anti-Patterns

- ❌ Don't auto-detect task from call site (e.g., "if caller is in services/search/, assume query"). Explicit is better than implicit. Passes explicit task at the call site.
- ❌ Don't use an Enum for task vocabulary — different models use different task strings. Loose `str` matches the actual protocol.
- ❌ Don't bundle rerank changes in this patch. Rerankers are cross-encoders and don't use task adapters.
- ❌ Don't try to auto-re-index when the patch lands. Old vectors still work (just stale semantics). Users re-index when they choose.
- ❌ Don't silently drop `extra_body` on the non-OpenAI official endpoint path. TEI needs it; the API client supports it for all endpoints.
- ❌ Don't skip the recursion fix. Silent task loss under high-volume indexing would only surface as subtle quality degradation, not an error.
