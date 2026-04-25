---
id: ch-agj
title: Plumb passage/query task hint through embedding interface
status: active
type: task
priority: 1
owner: Seth Yanow
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

## SRE Findings (2026-04-25 fresh-eyes review)

**Gaps filled in place** (see updated steps):
- Step 6 — explicit Voyage threading at lines 287, 290, 361, 295 (was "Thread task into _embed_single_batch → _embed_single_batch_locked" without listing sites)
- Step 9 — explicit OpenAI threading at lines 595, 641, 661/669/676/686 (4 calls inside `embed_batch` body), 694 (was only the recursion fix at 751)
- Step 12 — TEIEmbeddingProvider must override `__init__` to accept `dims: int` REQUIRED, override `@property dims` to return `self._dims`. Parent `OpenAIEmbeddingProvider.__init__` does NOT accept `dims`; parent's `dims` @property hardcodes 1536 for non-OpenAI models — would return wrong value for jina-v3 (1024), jina-v5-nano (256), jina-v5-small (512)
- Step 15 — added `dims` field to `EmbeddingConfig` Pydantic schema (required for tei); `--dims` CLI arg; model-validator now enforces both `base_url` AND `dims` for tei
- Step 17 — factory also updates `get_supported_providers()` (line 198) and `validate_provider_dependencies()` (line 201); `get_provider_info()` wizard-facing metadata is OUT OF SCOPE
- All new test files: add `pytestmark = pytest.mark.unit` at module top (matches existing convention — see `tests/unit/test_voyageai_provider.py:29`)

**Informational — no action required in this task**:
- `EmbeddingConfig` name collision: dataclass at `chunkhound/interfaces/embedding_provider.py:16-30` (used by `OpenAIEmbeddingProvider.config` property) vs Pydantic `BaseSettings` at `chunkhound/core/config/embedding_config.py:72` (user-facing config). Both are legitimate; agent must keep them straight
- File size: `openai_provider.py` (1450 lines) and `voyageai_provider.py` (725 lines) both exceed CLAUDE.md 500-line threshold. This task adds ~50 lines to each. A follow-up refactor task should be filed (e.g., extract rerank module, extract batching module) — out of scope here

**Granularity concern — user-decidable**:
22 steps in ONE bones task conflicts with executing-plans' "ONE task per turn" rule. Groups A-F could each be their own sub-task (with ch-agj as a parent epic). User chose the monolithic shape in two plan-writing sessions; ask before restructuring. See decision flagged in Log entry 2026-04-25.

## Key Considerations (failure catalog from adversarial-planning, 2026-04-25)

Failures grouped by component. Mitigations are structural — design prevents the problem, not "add a try/catch."

**`validate_task` validator**
- Assumption: callers pass `None`, `"passage"`, or `"query"`.
- Betrayal: callers pass `True` (bool, an `int` subtype), `0`, `"PASSAGE"`, `" passage"`, `["passage"]`.
- Consequence: typo silently embeds with wrong/no asymmetric hint → silent retrieval-quality degradation, worst kind of bug.
- Mitigation: error message uses `f"Unknown embedding task {task!r}"` so callers can distinguish list-vs-string-vs-bool. Test parametrization explicitly includes bool/int/list/whitespace-padded values (Steps 5 and 8).

**Validation symmetry across providers**
- Assumption (initial skeleton): Voyage validates, OpenAI silently accepts because it doesn't consume the value.
- Betrayal: typo `task="quary"` raises ValueError on Voyage, silently passes on OpenAI → inconsistent fail-fast, hard-to-debug quality drops.
- Consequence: AI-agent-written code at call sites can drift between providers; users can't rely on "task validation works."
- Mitigation: **DECISION (2026-04-25): validate at every provider's deepest internal method.** Voyage validates in `_embed_single_batch_locked`. OpenAI validates in `_embed_batch_internal`. TEI (Group D) validates in its `_embed_batch_internal` override. Single shared `validate_task` helper, one invocation per provider class. Consistent behavior, one code path, agent-friendly.

**`handle_token_limit_error` callback signature coupling**
- Assumption: `handle_token_limit_error(embed_function=...)` invokes `embed_function(batch)` — single positional arg.
- Betrayal: `batch_utils.py` is updated to pass extra kwargs (e.g., `embed_function(batch, retry_count=N)`) → the lambda `lambda batch: self._embed_batch_internal(batch, task=task)` raises TypeError.
- Consequence: token-limit fallback breaks loudly (acceptable — fail-fast), but the failure happens deep in a retry path that's hard to test live.
- Mitigation: code-comment at the lambda site documents the coupling. `handle_token_limit_error`'s signature is locked at `Callable[[list[str]], Awaitable[list[list[float]]]]` per `batch_utils.py:13-84`; if that ever drifts, this lambda must update too.

**Cache identity preservation**
- Assumption: existing Voyage embeddings (indexed pre-this-change with implicit `input_type="document"`) remain valid for retrieval after this change.
- Betrayal: a future change adds `task` to the cache fingerprint at `embedding_service.py:413-417`, invalidating all existing user data on next reindex.
- Consequence: user reindexes their entire repo, burning Voyage credits and an afternoon.
- Mitigation: structural — cache identity is `(chunk_id, provider, model)`, no task. Group F Step 21 regression test asserts `get_existing_embeddings` is called with exactly those kwargs. **Checkpoint 1 addition**: inline code-comment at Voyage's `_embed_single_batch_locked` mapping site noting `# task affects API call's input_type but NOT cache identity (see ch-agj Step 21 regression test)`.

**Signature ordering convention**
- Assumption: agents append `task: EmbeddingTask = None` at the end of every signature (after all existing params, e.g., after `batch_size: int | None = None` on `embed_batch`).
- Betrayal: agent inserts `task` in the middle of the signature, shifting positional indices. Third-party or test-fixture callers using positional args silently land arguments in the wrong slot.
- Consequence: type errors at boundaries — or worse, silent wrong-arg-passing if types align (e.g., a `str` lands in a `str | None` slot).
- Mitigation: **CONVENTION (lock this)** — `task` is appended at the end of every signature, never inserted. Tests should always call with `task=` kwarg form, never positional. The Protocol and impls all use the same trailing-arg convention.

**Skipped categories (no real failure mode):**
- *Encoding boundaries* — `task` is a Python literal `str`, SDKs accept literals, no serialization.
- *Temporal betrayal* — validator is pure, retry loops capture `task` correctly via closure (function-param scope, not loop-variable scope).
- *Resource exhaustion* — type aliases are zero-cost; lambda closures bounded by token-split tree depth (< 5 levels).

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
  - `test_embed_raises_value_error_on_unknown_task` — parametrize `["document", "retrieval.passage", "", "Q", "PASSAGE", " passage", True, 0, ["passage"]]`; assert `ValueError` with `"Unknown embedding task"` substring AND that the error message includes `repr(task)` (so `[1, 2]` shows as `[1, 2]`, not `1 2`)
- Run: expected fail

**Step 6: Implement Voyage task mapping using shared validator**
- File: `chunkhound/providers/embeddings/voyageai_provider.py`
- Add `task: EmbeddingTask = None` to `embed` (line 269), `embed_single` (line 359), `embed_batch` (line 364), `embed_streaming` (line 394)
- Add `task: EmbeddingTask = None` to `_embed_single_batch` (line 292) and `_embed_single_batch_locked` (line 297)
- Thread `task=task` at ALL internal call sites (explicit list — don't miss any):
  - `embed` body: `_embed_single_batch(sub_batch, task=task)` at line 287 AND `_embed_single_batch(validated_texts, task=task)` at line 290
  - `embed_single` body: `self.embed([text], task=task)` at line 361
  - `embed_batch` body: every call to `_embed_single_batch` — read the body first (starts at line 364) and thread `task=task` at each invocation
  - `embed_streaming` body: thread `task=task` at every call to `embed`/`embed_single` (verify actual call sites)
  - `_embed_single_batch` body: `self._embed_single_batch_locked(texts, task=task)` at line 295
- At top of `_embed_single_batch_locked`, call shared `validate_task(task)`
- Replace hardcoded `input_type="document"` at line 306 with explicit if/elif/else (NOT `dict.get`):
  - `None` or `"passage"` → `"document"` (preserves historical default → no cache invalidation)
  - `"query"` → `"query"`
- Run tests; expected pass

**Step 7: Commit**
- Message: `feat(voyage): map task hint to input_type for query/passage asymmetry (ch-agj)`

### Group C — OpenAI provider

**Step 8: Failing tests for OpenAI accept + validate + recursion threading**
- File: `tests/unit/test_openai_provider.py` (new — verify with `ls tests/unit/`)
- Tests:
  - `test_embed_accepts_task_arg_without_error` — `task="query"`; no TypeError; mocked client returns embeddings normally
  - `test_embed_does_not_send_task_in_payload` — mock `client.embeddings.create`; assert kwargs do NOT include `task`/`input_type`/`prompt_name`/`extra_body` (OpenAI rejects unknown fields; we accept the arg but never forward it)
  - `test_embed_raises_value_error_on_unknown_task` — parametrize `["document", "retrieval.passage", "", "Q", "PASSAGE", " passage", True, 0, ["passage"]]`; assert `ValueError` with `"Unknown embedding task"` substring. Validates symmetric fail-fast across providers (matches Voyage Step 5 test)
  - `test_recursive_token_limit_fallback_threads_task` — patch `client.embeddings.create` to raise `BadRequestError("maximum context length...tokens")` once then succeed; patch `handle_token_limit_error` to capture the `embed_function`; call captured function and assert eventual `_embed_batch_internal` invocation included `task="passage"` (verifies lambda closure captures task correctly across recursion)
- Run: expected fail

**Step 9: Implement OpenAI task acceptance + validation + recursion threading**
- File: `chunkhound/providers/embeddings/openai_provider.py`
- Add `task: EmbeddingTask = None` to public `embed` (586), `embed_single` (639), `embed_batch` (644), `embed_streaming` (691), `_embed_batch_internal` (697)
- At the TOP of `_embed_batch_internal` (line 697, before `await self._ensure_client()`): call `validate_task(task)`. This is the single validation point for OpenAI — fail-fast on typos at the deepest internal entry. Symmetric with Voyage's validation at `_embed_single_batch_locked`. Public methods thread `task` mechanically; only the inner method validates
- Import: `from chunkhound.interfaces.embedding_provider import EmbeddingTask, validate_task`
- Body: do NOT include task in `client.embeddings.create(...)` kwargs — actual OpenAI rejects unknown fields
- Thread `task=task` at ALL internal call sites (explicit list — don't miss any):
  - `embed` body: `self.embed_batch(validated_texts, task=task)` at line 595
  - `embed_single` body: `self.embed([text], task=task)` at line 641
  - `embed_batch` body: pass `task=task` to EVERY `_embed_batch_internal(...)` call — lines 661, 669, 676, 686 (4 total)
  - `embed_streaming` body: `self.embed_single(text, task=task)` at line 694
- Recursion fix at line 751: replace `embed_function=self._embed_batch_internal` with `embed_function=lambda batch: self._embed_batch_internal(batch, task=task)`. Add a code-comment one line above the lambda: `# Lambda closure captures task across token-limit recursion. handle_token_limit_error's signature (batch_utils.py:13) is Callable[[list[str]], Awaitable[...]] — if that ever changes, this lambda must change too.`
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
  - Override `__init__`: accept `dims: int` as a REQUIRED keyword arg (not optional — the parent can't infer it for custom models), store as `self._dims`, then call `super().__init__(**other_kwargs)` with remaining kwargs. Parent `OpenAIEmbeddingProvider.__init__` at `openai_provider.py:190-207` does NOT accept `dims` — you must pop/intercept before calling super
  - Override `@property dims` to return `self._dims`. Parent's `dims` at `openai_provider.py:460-464` returns from `_model_config` dict if model matches (openai only) else hardcoded 1536 — wrong for jina-v3=1024, jina-v5-nano=256, jina-v5-small=512
  - Override `@property name` to return `"tei"` (parent's `name` at `openai_provider.py:431-437` is a `@property`, so `@property` override in subclass takes effect cleanly)
  - No other method overrides yet — shell only
- Add unit test asserting `TEIEmbeddingProvider(dims=1024, model="jinaai/jina-embeddings-v3", ...).dims == 1024` — verifies dims override actually takes effect (regression guard against the parent's 1536 default leaking through)
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
- Add NEW `dims: int | None = Field(default=None, description="Embedding dimensions (REQUIRED for tei, auto-detected for openai/voyageai)")` — needed because TEI serves custom models whose dims cannot be inferred from the model name
- Audit and update all if/elif chains that hardcode the existing two providers — at lines 206, 307, 319, 344, 367, 456 — read each first, decide whether `"tei"` needs a branch or shares OpenAI's path. Note: the `__repr__` at line 524-539 does NOT branch on provider so needs no change
- Update `is_provider_configured` (line 312), `get_missing_config` (line 335), `get_provider_config` (line 252) methods so `"tei"` is recognized: TEI requires `model`, `base_url`, and `dims` (no `api_key` requirement — TEI server may or may not require auth)
- Add a model-validator branch: when `provider == "tei"`, `base_url` MUST be set AND `dims` MUST be set (raise ValueError with specific message for each missing requirement)
- `get_default_model` at line 296 — no sensible TEI default (user must specify); raise ValueError if `provider == "tei"` and `self.model is None`
- Update `choices=["openai", "voyageai"]` at line 367 (argparse `--provider`) to include `"tei"`
- `EmbeddingConfig.add_cli_arguments` at line 362: consider adding `--dims` CLI arg so users can pass `--provider tei --dims 1024 --model jinaai/jina-embeddings-v3 --base-url http://...` (see `extract_cli_overrides` at line 475 for the threading pattern)

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
  - Add static method `_create_tei_provider(provider_config: dict[str, Any]) -> TEIEmbeddingProvider` mirroring the structure of `_create_openai_provider` at line 67+ (extract api_key/base_url/model/dims/rerank_*/azure_* from provider_config; pass `dims` explicitly; instantiate via the registry function)
  - Import `from chunkhound.embeddings import create_tei_provider`
  - Add dispatch case at `create_provider` (line 57-62): `elif config.provider == "tei": return EmbeddingProviderFactory._create_tei_provider(provider_config)`
  - Update `get_supported_providers()` at line 198 — add `"tei"` to returned list
  - Update `validate_provider_dependencies()` at line 201 — add `elif provider == "tei"` branch importing `TEIEmbeddingProvider` (mirrors the `elif provider == "voyageai"` branch at lines 219-221)
  - `get_provider_info()` at line 280+ — adding a TEI branch here is OUT OF SCOPE (setup-wizard concern); leave for a follow-up
- File: `chunkhound/embeddings.py`
  - Add `create_tei_provider(base_url: str, model: str, dims: int, api_key: str | None = None, ...) -> TEIEmbeddingProvider` factory function mirroring `create_openai_provider` at line 177 (signature parallels OpenAI's; `dims` is REQUIRED, `base_url` is REQUIRED, `api_key` optional; pass through rerank_* kwargs)
  - Re-export `TEIEmbeddingProvider` from this module (add to module-level import, not just TYPE_CHECKING)
- Also update `EmbeddingConfig.get_provider_config()` at `embedding_config.py:252` — ensure `dims` is threaded into `base_config` dict when `self.provider == "tei"`. Pydantic `EmbeddingConfig` does NOT currently have a `dims` field — you must add it to the schema in Step 15 (`dims: int | None = Field(default=None, ...)`) and include it in `base_config` here so the factory can read it
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

- [x] `EmbeddingTask` alias + shared `validate_task` in interface module (commit 8edc107)
- [x] All 4 protocol methods accept optional `task=None` (commit 8edc107)
- [x] Voyage maps task → input_type with explicit `ValueError` on unknown values (commit 705a9a8)
- [x] OpenAI validates task at `_embed_batch_internal` entry (symmetric fail-fast with Voyage per adversarial-planning decision 2026-04-25); recursive token-limit fallback threads task through lambda closure (commit 49b631b)
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
- [2026-04-25T02:19:42Z] [Seth Yanow] SRE fresh-eyes review (2026-04-25). Skeleton claims all verified against codebase. Filled 5 critical gaps in place: (1) Step 6 Voyage threading explicit at lines 287/290/295/361; (2) Step 9 OpenAI threading explicit at lines 595/641/661/669/676/686/694 — the 4 calls inside embed_batch were missing; (3) Step 12 TEIEmbeddingProvider MUST override __init__ to accept dims (required kwarg) AND override @property dims — parent hardcodes 1536 for non-OpenAI models, wrong for jina-v3=1024/jina-v5-nano=256/jina-v5-small=512; (4) Step 15 add 'dims' field to Pydantic EmbeddingConfig + CLI '--dims' arg + model-validator enforces both base_url AND dims for tei; (5) Step 17 factory also updates get_supported_providers() and validate_provider_dependencies(); get_provider_info wizard metadata explicitly OUT OF SCOPE. Informational: EmbeddingConfig name collision (dataclass vs Pydantic), openai_provider.py 1450 lines / voyageai 725 lines already exceed CLAUDE.md 500-line threshold — follow-up refactor ticket warranted. Granularity concern (22 steps, one task) flagged for user decision.
- [2026-04-25T02:50:07Z] [Seth Yanow] Adversarial planning (Checkpoint 1: A+B+C). Failure catalog added to Key Considerations. Design decision: validate_task called at the DEEPEST internal method of every provider (Voyage _embed_single_batch_locked, OpenAI _embed_batch_internal, TEI _embed_batch_internal override). Drops the Voyage-validates/OpenAI-silently-accepts asymmetry. One helper, one invocation per provider class, symmetric fail-fast. User framing: AI agents write code here — consistency over cleverness. Three C's lens: Clarity (same pattern everywhere), Cohesion (validator owns contract), Coupling (providers depend on validator only, not each other). Step 8 test plan extended with test_embed_raises_value_error_on_unknown_task (parametrized over bool/int/list/whitespace edge cases); Step 5 Voyage test extended with same set. Step 9 adds validate_task(task) at _embed_batch_internal top + lambda-coupling comment.
- [2026-04-25T03:11:43Z] [Seth Yanow] Checkpoint 1 complete (Groups A+B+C). 3 commits: 8edc107 (protocol surface + validator + 21 tests), 705a9a8 (Voyage task→input_type mapping + threading + validation + 9 tests, 4 pre-existing fake signatures updated mechanically), 49b631b (OpenAI accept+validate+recursion lambda + 13 tests including 2 closure-introspection regression guards). Total: 43 new unit tests green. Full unit suite: 2057 pass (22 more than baseline). Validate-everywhere design shipped symmetrically — Voyage at _embed_single_batch_locked entry, OpenAI at _embed_batch_internal entry. Mypy: no errors introduced (pre-existing errors tracked in ch-6ea). Side note: fixed a local-only pre-commit hook syntax bug (.git/hooks/trauma-guard-precommit.py used Rust-style \u{1f525} which is invalid Python; replaced with \U0001F525). Deferred adversarial stress test to end of Checkpoint 3 per user's checkpoint-cadence framing.
