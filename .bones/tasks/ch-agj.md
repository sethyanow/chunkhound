---
id: ch-agj
title: Plumb passage/query task hint through embedding interface
status: open
type: task
priority: 1
---



## Goal

Add an optional asymmetric-retrieval `task` hint to ChunkHound's embedding interface so:
1. Voyage queries get `input_type="query"` (currently hardcoded to `"document"` for ALL calls — real quality bug at `voyageai_provider.py:306`)
2. A new TEIEmbeddingProvider is wired in so jina-v5-text-small / jina-v5-text-nano can be tested via TEI on private repos
3. OpenAI provider accepts the arg but ignores it (no input_type semantics)
4. Existing Voyage embeddings on this repo are NOT invalidated — cache identity stays `(chunk_id, provider, model)`, no task in fingerprint

## Codebase Verification (done before planning)

**Confirmed:**
- Interface at `chunkhound/interfaces/embedding_provider.py:33-276`. Methods to update: `embed` (line 76), `embed_single` (90), `embed_batch` (104), `embed_streaming` (119)
- Voyage hardcoded `input_type="document"` at `voyageai_provider.py:306` inside `_embed_single_batch_locked`
- OpenAI `_embed_batch_internal` at `openai_provider.py:697`. Recursive fallback at line 751-758 calls `handle_token_limit_error(embed_function=self._embed_batch_internal, ...)` — drops kwargs unless threaded via closure
- `handle_token_limit_error` at `batch_utils.py:13-84` takes `embed_function: Callable[[list[str]], Awaitable[list[list[float]]]]` — single-arg; threading needs `lambda` or `functools.partial`
- 6 passage call sites: `embedding_service.py:487`, `indexing_coordinator.py:1596`, `gap_detection.py:253`, `clustering_service.py:97/199/396`
- 2 query call sites: `single_hop_strategy.py:66`, `gap_detection.py:461`
- Cache identity at `embedding_service.py:413-417` uses `(chunk_id, provider, model)` — task NOT in fingerprint
- TEI rerank already implemented via httpx in `openai_provider.py:1235+`. NO TEI-specific embedding path exists
- Provider factory at `chunkhound/core/config/embedding_factory.py:34` (`create_provider`) and `:229` (`create_provider_from_legacy_args`)
- OpenAI Python SDK supports `extra_body` kwarg on `client.embeddings.create(...)` for non-standard fields
- Jina v3/v5 task values are exactly `"retrieval.query"` and `"retrieval.passage"` (top-level field on jina cloud API; via `extra_body` for TEI's OpenAI-compat endpoint)

**Decisions:**
- TEI provider subclasses OpenAI provider rather than rewriting — keeps retry, token-limit fallback, batching for free
- Extract a shared `_embed_batch_with_extras(texts, extra_body=None)` base method on OpenAIEmbeddingProvider so TEI override avoids retry-loop duplication
- TEI provider always sends task via `extra_body` when caller provides one. Server expected to ignore unknown extras for non-jina models on TEI (OpenAI-compat layer is permissive)
- Only support `passage` + `query` task values now. `separation`/`classification`/`text-matching` are jina extras not currently used by ChunkHound's retrieval pipeline (KISS)
- Python target: `pyproject.toml` declares `>=3.10,<3.14`. Plain `EmbeddingTask = Literal["passage", "query"] | None` works on 3.10 AND 3.13 — no PEP 695 `type` statement needed
- Tests live flat in `tests/unit/` (no subdirs) — matches existing convention (`tests/unit/test_voyageai_provider.py` exists)
- pytest-asyncio mode is `"auto"` (`pyproject.toml:203`) — async tests do NOT need `@pytest.mark.asyncio` decorator
- Shared validator placed in `chunkhound/interfaces/embedding_provider.py` (not deferred per Rule of Three) — explicit user direction

**Schema/registry plumbing (additional to provider class work):**
- `EmbeddingConfig.provider` at `embedding_config.py:97` is `Literal["openai", "voyageai"]` — Pydantic will REJECT `"tei"` until literal is widened. Multiple if/elif chains reference provider name (lines 206, 307, 319, 344, 367, 456) and need audit.
- Factory at `embedding_factory.py:34` uses explicit if/elif dispatch with `_create_openai_provider` and `_create_voyageai_provider` static helpers — TEI needs a parallel `_create_tei_provider` helper, not just a case branch
- `chunkhound/embeddings.py:177` defines `create_openai_provider(...)` factory function — TEI needs a parallel `create_tei_provider(...)` factory function exported from the same module
- `OpenAIEmbeddingProvider.name` is `@property` at `openai_provider.py:431-437` (returns `"azure_openai"` or `"openai"`) — TEI subclass can override with `@property` returning `"tei"` cleanly

**Cross-cut behavior (positive confirmation):**
- DB tables are keyed by `(provider, model, dims)`. Switching provider config from `voyageai` to `tei` writes new embeddings to a separate table; existing voyage embeddings in this repo stay intact and become "dormant" (not deleted, not regenerated). This satisfies "don't disturb" automatically — no code change needed for it.

## Implementation Steps

### Group A — Type infrastructure + shared validator

**Step 1: Add `EmbeddingTask` alias + shared `validate_task` validator**
- File: `chunkhound/interfaces/embedding_provider.py`
- Add `from typing import Literal, Any` to imports
- Module-level: `EmbeddingTask = Literal["passage", "query"] | None`
- Module-level function `def validate_task(task: Any) -> EmbeddingTask`:
  - Accepts `None` and the two literals; raises `ValueError(f"Unknown embedding task {task!r}; expected 'passage', 'query', or None")` otherwise
  - Google-style docstring with Args/Returns/Raises
- Verify: `uv run mypy chunkhound/interfaces/embedding_provider.py` clean

**Step 2: Failing tests for validator and protocol shape**
- File: `tests/unit/test_embedding_provider_protocol.py` (new)
- Tests (AAA structure with `# Arrange / # Act / # Assert` comments):
  - `test_validate_task_accepts_none_passage_query` — `pytest.parametrize` with `pytest.param(None, None, id="none"), pytest.param("passage", "passage", id="passage"), pytest.param("query", "query", id="query")`
  - `test_validate_task_rejects_unknown_value` — parametrize `["document", "retrieval", "Q", ""]`; assert `ValueError` with `"Unknown embedding task"` substring
  - `test_embed_methods_accept_optional_task` — uses `inspect.signature` on each of 4 protocol methods; asserts `task` parameter exists with default `None`
- Run: `uv run pytest tests/unit/test_embedding_provider_protocol.py -v`
- Expected: validator tests pass (already in step 1); signature tests fail (protocol unchanged)

**Step 3: Add `task` param to protocol methods**
- File: `chunkhound/interfaces/embedding_provider.py:76, 90, 104, 119`
- Add `task: EmbeddingTask = None` to `embed`, `embed_single`, `embed_batch`, `embed_streaming`
- Update each docstring with one-line `Args: task:` describing asymmetric-retrieval hint
- Run: `uv run pytest tests/unit/test_embedding_provider_protocol.py -v`
- Expected: all pass

**Step 4: Commit**
- Message: `feat(embed): add EmbeddingTask alias, validator, and protocol task arg (ch-agj)`

### Group B — Voyage provider

**Step 5: Failing tests for Voyage task→input_type**
- File: `tests/unit/test_voyageai_provider.py` (extend existing)
- Tests:
  - `test_embed_passes_input_type_based_on_task` — parametrize `[(None, "document"), ("passage", "document"), ("query", "query")]` with named ids; mock `client.embed` (wrapped via `asyncio.to_thread`); assert `input_type=expected` in call kwargs
  - `test_embed_raises_value_error_on_unknown_task` — parametrize `["document", "retrieval.passage", "", "Q"]`; assert `ValueError` with `"Unknown embedding task"` substring
- Run: expected fail

**Step 6: Implement Voyage task mapping using shared validator**
- File: `chunkhound/providers/embeddings/voyageai_provider.py`
- Add `task: EmbeddingTask = None` to `embed`/`embed_single`/`embed_batch`/`embed_streaming`
- Thread task into `_embed_single_batch` → `_embed_single_batch_locked`
- At top of `_embed_single_batch_locked`, call shared `validate_task(task)`
- Replace hardcoded `input_type="document"` at line 306 with explicit if/elif/else (NOT `dict.get`):
  - `None` or `"passage"` → `"document"` (preserves historical default → no cache invalidation)
  - `"query"` → `"query"`
- Run tests; expected pass

**Step 7: Commit**
- Message: `feat(voyage): map task hint to input_type for query/passage asymmetry (ch-agj)`

### Group C — OpenAI provider

**Step 8: Failing tests for OpenAI accept-and-ignore**
- File: `tests/unit/test_openai_provider.py` (new — verify with `ls tests/unit/`)
- Tests:
  - `test_embed_accepts_task_arg_without_error` — `task="query"`; no TypeError
  - `test_embed_does_not_send_task_in_payload` — mock `client.embeddings.create`; assert kwargs do NOT include `task`/`input_type`/`prompt_name`/`extra_body`
  - `test_recursive_token_limit_fallback_threads_task` — patch `client.embeddings.create` to raise `BadRequestError("maximum context length...tokens")` once then succeed; patch `handle_token_limit_error` to capture the `embed_function`; call captured function and assert eventual `_embed_batch_internal` invocation included `task="passage"`
- Note: OpenAI provider does NOT validate task — silently accepts any value since it doesn't consume it. Inline comment in test file documents this
- Run: expected fail

**Step 9: Implement OpenAI task acceptance + recursion threading**
- File: `chunkhound/providers/embeddings/openai_provider.py`
- Add `task: EmbeddingTask = None` to public `embed` (586), `embed_single` (639), `embed_batch` (644), `embed_streaming` (691), `_embed_batch_internal` (697)
- Body: do NOT include task in `client.embeddings.create(...)` kwargs — actual OpenAI rejects unknown fields
- Recursion fix at line 751: replace `embed_function=self._embed_batch_internal` with `embed_function=lambda batch: self._embed_batch_internal(batch, task=task)`
- Run tests; expected pass

**Step 10: Commit**
- Message: `feat(openai): accept task hint and thread through token-limit recursion (ch-agj)`

### Group D — TEI/jina provider

**Step 11: Failing tests for TEIEmbeddingProvider class shape**
- File: `tests/unit/test_tei_provider.py` (new)
- Tests:
  - `test_tei_provider_instantiates` — construct with `EmbeddingConfig(provider="tei", model="jinaai/jina-embeddings-v3", base_url="http://localhost:8080/v1", dims=1024, ...)`; assert `provider.name == "tei"`
  - `test_tei_provider_supports_async_embed` — `await provider.embed(["x"])` returns a list (mocked client)
- Run: expected fail (class doesn't exist)

**Step 12: Extract shared `_embed_batch_with_extras` and implement TEIEmbeddingProvider class shell**
- File: `chunkhound/providers/embeddings/openai_provider.py`
  - Refactor `_embed_batch_internal(self, texts, task=None)` to delegate to new private `_embed_batch_with_extras(self, texts, extra_body=None)` — moves the retry loop and `client.embeddings.create(...)` call into the new method, accepting an optional `extra_body` dict
  - The existing `_embed_batch_internal` calls `_embed_batch_with_extras(texts, extra_body=None)` (preserves OpenAI behavior)
- File: `chunkhound/providers/embeddings/tei_provider.py` (new)
- Class `TEIEmbeddingProvider(OpenAIEmbeddingProvider)`:
  - Override `@property name` to return `"tei"` (parent's `name` at `openai_provider.py:431-437` is a `@property`, so `@property` override in subclass takes effect cleanly)
  - No other method overrides yet — shell only
- Add unit test asserting `TEIEmbeddingProvider(...).name == "tei"` and `supports_reranking() is False` when constructed with embedding-only config (no `rerank_format` set) — confirms inherited rerank methods stay dormant
- Run step 11 tests; expected pass
- Commit: `refactor(openai): extract _embed_batch_with_extras for shared batch logic (ch-agj)`

**Step 13: Failing tests for TEI task→extra_body mapping**
- File: `tests/unit/test_tei_provider.py` (extend)
- Tests:
  - `test_embed_sends_task_via_extra_body` — parametrize `[("passage", {"task": "retrieval.passage"}), ("query", {"task": "retrieval.query"})]`; mock `client.embeddings.create`; assert `extra_body=expected_dict` in call kwargs
  - `test_embed_omits_extra_body_when_task_is_none` — `task=None`; assert `extra_body` either absent from kwargs OR is `None` (pick one based on actual SDK behavior; document choice in test comment)
  - `test_embed_raises_value_error_on_unknown_task` — parametrize over invalid values via shared validator
- Run: expected fail

**Step 14: Implement TEI `_embed_batch_internal` override + commit**
- File: `chunkhound/providers/embeddings/tei_provider.py`
- Override `async def _embed_batch_internal(self, texts, task=None)`:
  - Call `validate_task(task)` first
  - Build `extra_body`: if task is None, `extra_body=None`; if `"passage"`, `{"task": "retrieval.passage"}`; if `"query"`, `{"task": "retrieval.query"}`
  - Delegate to inherited `self._embed_batch_with_extras(texts, extra_body=extra_body)`
- Run tests; expected pass
- Commit: `feat(tei): add TEIEmbeddingProvider with task→extra_body mapping (ch-agj)`

**Step 15: Update `EmbeddingConfig` schema to accept `provider="tei"`**
- File: `chunkhound/core/config/embedding_config.py`
- Update `provider` field at line 97 from `Literal["openai", "voyageai"]` to `Literal["openai", "voyageai", "tei"]` and update the description at line 98 to list `tei`
- Audit and update all if/elif chains that hardcode the existing two providers — at lines 206, 307, 319, 344, 367, 456 — read each first, decide whether `"tei"` needs a branch or shares OpenAI's path
- Update `is_provider_configured`, `get_missing_config`, `get_provider_config` methods so `"tei"` is recognized: TEI requires `model`, `base_url`, and `dims` (no `api_key` requirement — TEI server may or may not require auth)
- Add a model-validator branch: when `provider == "tei"`, `base_url` MUST be set (raise ValueError otherwise — TEI server URL is non-optional)
- Update `choices=["openai", "voyageai"]` reference at line 367 to include `"tei"`

**Step 16: Failing tests for factory + registry creating TEIEmbeddingProvider**
- File: `tests/unit/test_embedding_factory.py` (verify exists with `ls tests/unit/`; if absent, create new; if existing, extend)
- Tests:
  - `test_embedding_config_accepts_tei_provider` — construct `EmbeddingConfig(provider="tei", model="jinaai/jina-embeddings-v3", base_url="http://localhost:8080/v1", dims=1024)`; no Pydantic ValidationError raised
  - `test_embedding_config_rejects_tei_without_base_url` — same construction without `base_url`; assert ValidationError raised mentioning base_url
  - `test_factory_creates_tei_provider` — call `EmbeddingProviderFactory.create_provider(<the valid config>)`; assert returned instance is `TEIEmbeddingProvider`
  - `test_create_tei_provider_factory_function_returns_tei_provider` — test the standalone `create_tei_provider` factory function exported from `chunkhound.embeddings`
- Run: expected fail (schema not yet widened, no factory case, no registry function)

**Step 17: Add `_create_tei_provider` factory helper + `create_tei_provider` registry function + factory dispatch case**
- File: `chunkhound/core/config/embedding_factory.py`
  - Add static method `_create_tei_provider(provider_config: dict[str, Any]) -> TEIEmbeddingProvider` mirroring the structure of `_create_openai_provider` at line 67+ (extract api_key/base_url/model/rerank_*/azure_* from provider_config; instantiate via the registry function)
  - Import `from chunkhound.embeddings import create_tei_provider`
  - Add dispatch case: `elif config.provider == "tei": return EmbeddingProviderFactory._create_tei_provider(provider_config)`
- File: `chunkhound/embeddings.py`
  - Add `create_tei_provider(api_key=..., base_url, model, dims, ...) -> TEIEmbeddingProvider` factory function mirroring `create_openai_provider` at line 177 (signature parallels OpenAI's; extra: `dims` is required for TEI since no model registry can auto-discover it)
  - Re-export `TEIEmbeddingProvider` from this module
- Run tests; expected pass
- Commit: `feat(tei): wire TEIEmbeddingProvider into config schema, factory, and registry (ch-agj)`

### Group E — Call sites

**Step 18: Failing tests for passage call sites**
- File: `tests/unit/test_embedding_call_sites_passage.py` (new)
- Module-level `pytest.fixture` for a mocked embedding provider with `AsyncMock`-backed `embed`/`embed_batch`
- Tests (AAA structure):
  - `test_embedding_service_passes_task_passage` — drive `embedding_service.py:487` path; assert `task="passage"` in mock kwargs
  - `test_indexing_coordinator_passes_task_passage` — drive `indexing_coordinator.py:1596` path
  - `test_gap_detection_chunk_clustering_passes_task_passage` — drive `gap_detection.py:253`
  - `test_clustering_service_passes_task_passage` — parametrize over the 3 entry methods that hit `clustering_service.py:97/199/396`
- Run: expected fail

**Step 19: Update passage call sites + commit**
- Edits (add `task="passage"` kwarg):
  - `chunkhound/services/embedding_service.py:487`
  - `chunkhound/services/indexing_coordinator.py:1596`
  - `chunkhound/services/research/shared/gap_detection.py:253`
  - `chunkhound/services/clustering_service.py:97, 199, 396`
- Run tests; expected pass
- Commit: `feat(embed): pass task='passage' from indexing and clustering paths (ch-agj)`

**Step 20: Failing tests + implementation + commit for query call sites**
- File: `tests/unit/test_embedding_call_sites_query.py` (new)
- Tests:
  - `test_single_hop_strategy_passes_task_query` — drive `single_hop_strategy.py:66` path
  - `test_gap_detection_query_embedding_passes_task_query` — drive `gap_detection.py:461` path
- Run: expected fail
- Edits:
  - `chunkhound/services/search/single_hop_strategy.py:66` — add `task="query"`
  - `chunkhound/services/research/shared/gap_detection.py:461` — add `task="query"`
- Run tests; expected pass
- Commit: `feat(embed): pass task='query' from search and gap-query paths (ch-agj)`

### Group F — Cache regression + verification

**Step 21: Cache identity regression test**
- File: `tests/unit/test_embedding_cache_identity.py` (new)
- Tests:
  - `test_get_existing_embeddings_call_signature_excludes_task` — mock `db.get_existing_embeddings`; run `_filter_existing_embeddings`; assert mock called with kwargs `{chunk_ids, provider, model}` only — no `task`, no `content_hash`
  - `test_existing_voyage_embedding_skipped_when_indexing_with_task_passage` — DB mock returns existing embedding for `(provider="voyageai", model="voyage-4")`; drive new indexing with `task="passage"`; assert `provider.embed_batch` NOT called (chunks filtered). This is the regression test that proves existing repo embeddings don't get regenerated
- Run: expected pass (verifies current behavior preserved)
- Commit: `test(embed): regression test confirms task hint excluded from cache identity (ch-agj)`

**Step 22: Full unit + integration suite + targeted mypy**
- Run: `uv run pytest -m "unit or integration" tests/ -v`
- Expected: green
- Triage: if any preexisting test breaks because it constructs a mock provider with the old signature, update the mock to accept `task=None`. Note in commit message which test files were touched purely for shape compatibility
- Run: `uv run mypy chunkhound/interfaces/embedding_provider.py chunkhound/providers/embeddings/ chunkhound/core/config/embedding_factory.py chunkhound/services/embedding_service.py chunkhound/services/indexing_coordinator.py chunkhound/services/search/single_hop_strategy.py chunkhound/services/research/shared/gap_detection.py chunkhound/services/clustering_service.py`
- Expected: clean for touched files (rest of codebase has known errors per ch-6ea — don't fix here)

**Step 23: Bones log + final commit (if needed)**
- `bn log ch-agj "Plumbing complete: protocol + 3 providers (Voyage, OpenAI, TEI) + factory + 8 call sites. Cache identity unchanged. TEI provider ready for jina-v5 testing. Voyage rerank untouched. <commit list>"`
- Final commit only if anything outstanding from step 21 triage

## Success Criteria

- [ ] `EmbeddingTask` alias + shared `validate_task` in interface module
- [ ] All 4 protocol methods accept optional `task=None`
- [ ] Voyage maps task → input_type with explicit `ValueError` on unknown values
- [ ] OpenAI silently accepts task; recursive fallback threads it through closure
- [ ] `EmbeddingConfig.provider` Literal widened to include `"tei"`; provider-specific config branches updated; model-validator enforces `base_url` required when `provider="tei"`
- [ ] `TEIEmbeddingProvider` class exists, registered in factory via `_create_tei_provider`, exported from `chunkhound/embeddings.py` via `create_tei_provider`, sends `extra_body={"task": "retrieval.{passage,query}"}` for jina models
- [ ] `TEIEmbeddingProvider.name` returns `"tei"`; `supports_reranking()` returns `False` when constructed with embedding-only config (no `rerank_format`)
- [ ] All 6 passage sites pass `task="passage"`; both query sites pass `task="query"`
- [ ] Cache regression test green — existing Voyage embeddings on this repo NOT regenerated when reindexed (DB tables keyed by `(provider, model, dims)` — voyage embeddings go dormant when switching to TEI, not deleted, not regenerated)
- [ ] Voyage `rerank` method unchanged
- [ ] `uv run pytest -m "unit or integration"` green
- [ ] Mypy clean on touched-file deltas (preexisting errors stay for ch-6ea — see Anti-Patterns)
- [ ] User can `cp .chunkhound.json` with `provider: "tei"` and a TEI deployment URL to test jina-v5 on a private repo

## Out of Scope (explicit non-goals)

- Modal-hosted rerank deploy (separate task — likely Qwen3-Reranker-4B on T4)
- TEI rerank — already works via existing http path in `openai_provider.py:1235+`
- jina-v5 dimensionality / Matryoshka tuning (model-specific, post-pilot)
- Adding `separation`/`classification`/`text-matching` task adapters — KISS, only what call sites use
- Live API tests — all tests use mocks; live runs are user-driven

## Anti-Patterns to Avoid

- DO NOT add `task` to the cache identity / `get_existing_embeddings` signature — would invalidate existing embeddings
- DO NOT touch Voyage `rerank` method — cross-encoder, no task semantics, working as-is
- DO NOT add live API tests — all tests use mocks
- DO NOT silently fallback to a default when an unknown task value is passed — fail loudly with `ValueError` from `validate_task`
- DO NOT pre-empt jina-v5-nano/small testing — that's a separate task gated on this one closing
- DO NOT swallow stderr or hide preexisting test failures during step 22; document them and proceed only if unrelated
- DO NOT fix preexisting mypy errors on `voyageai_provider.py` / `openai_provider.py` / other touched files unless they were INTRODUCED by this change. Last session (ch-3zc) ballooned from "fix get_stats" to "fix 37 mypy errors" via CLAUDE.md's resolve-on-sight rule. For ch-agj, scope discipline: note preexisting errors in commit body, leave them for ch-6ea. If you find yourself fixing more than ~3 unrelated mypy errors, STOP and ask
- DO NOT lump Group D into one giant commit — split into three per the steps: (a) `refactor(openai): extract _embed_batch_with_extras` (step 12), (b) `feat(tei): add TEIEmbeddingProvider with task→extra_body` (step 14), (c) `feat(tei): wire TEIEmbeddingProvider into config schema, factory, and registry` (step 17). Three commits, NOT one
- DO NOT add `@pytest.mark.asyncio` decorator to async tests — pytest-asyncio mode is `"auto"` repo-wide (`pyproject.toml:203`). Just write `async def test_...`
- DO NOT skip the rerank-default test in step 12 — TEIEmbeddingProvider inherits OpenAI's rerank methods; the assertion that `supports_reranking() is False` for embedding-only TEI config is a regression guard against future surprises
- DO NOT assume `name` override "just works" without verifying mechanism. Confirmed in step 12 prep: parent's `name` at `openai_provider.py:431-437` is a `@property`, so `@property` override in subclass is the correct pattern. If you find an `__init__`-set attribute instead, route around it

## Log

- [2026-04-24T23:05:40Z] [Seth Yanow] Plan written. 22 steps across 6 groups: type infra+validator, Voyage, OpenAI, TEI/jina provider (subclasses OpenAI, sends extra_body), passage call sites, query call sites, cache regression. Subclassing extracts shared _embed_batch_with_extras to avoid retry-loop duplication. Validated against design-patterns/error-handling/code-style/anti-patterns/type-safety/testing-patterns skills. Cache identity stays (chunk_id, provider, model) — existing Voyage embeddings on this repo will NOT be regenerated. Awaiting user execution; user gated on live runs.
- [2026-04-24T23:24:18Z] [Seth Yanow] Plan v3 amended: added Step 15 (EmbeddingConfig schema widening for tei), Step 16 (factory + registry tests), Step 17 (factory _create_tei_provider helper + chunkhound/embeddings.py registry function + commit). Renumbered subsequent steps. Anti-patterns expanded with mypy scope discipline, commit splitting (3 commits in Group D), pytest-asyncio mode notice, rerank-default test, name @property verification. Success criteria expanded for schema/registry/name/rerank checks. dims-keyed DB table behavior documented as positive confirmation.
