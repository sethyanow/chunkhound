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

### Group D failure catalog (adversarial-planning, 2026-04-25 Checkpoint 2)

**OpenAI `_embed_batch_with_extras` refactor**
- *Input Hostility — `extra_body` shape*: Assumption: callers pass `None` (OpenAI path) or a small `dict[str, str]` (TEI path). Betrayal: caller passes non-dict, or a dict with values the OpenAI SDK can't JSON-serialize. Consequence: cryptic httpx TypeError deep in retry loop; OR OpenAI path silently sends `extra_body=null` that some Azure/compat gateways reject with 400. Mitigation: type the param as `Mapping[str, Any] | None`. CRITICAL — when `extra_body is None`, OMIT the kwarg from `client.embeddings.create(...)` entirely (don't pass `extra_body=None`). Construct kwargs conditionally: `kwargs={}; if extra_body is not None: kwargs["extra_body"]=extra_body`. Guarantees byte-identical request payload for OpenAI path. Test: assert `client.embeddings.create` was called WITHOUT `extra_body` key in kwargs when `extra_body is None`.
- *Temporal Betrayal — refactor reorders side effects*: Assumption: refactor is pure code motion. Betrayal: `validate_task(task)` and `await self._ensure_client()` accidentally migrate from `_embed_batch_internal` into `_embed_batch_with_extras`, OR usage_stats updates double up if both methods touch them. Consequence: silent behavior drift in the most-tested provider. Mitigation: structural — `validate_task` and `_ensure_client` STAY in `_embed_batch_internal`. `_embed_batch_with_extras` ONLY contains the retry loop + `client.embeddings.create` + response sorting + usage_stats updates. `_embed_batch_internal` becomes a thin wrapper that does NOT touch usage_stats. Test: `_usage_stats["requests_made"]` increments exactly once per `embed_batch` call (run path with mocked client; assert counter delta is 1, not 0 or 2).
- *Dependency Treachery — SDK kwarg drift*: Assumption: OpenAI SDK accepts `extra_body=None` identically to its absence. Betrayal: SDK version drift between minor versions; OR Azure gateway rejects `null` extras. Consequence: existing OpenAI/Azure path breaks across upgrades. Mitigation: same as Input Hostility — never pass `extra_body=None` explicitly. Tested by asserting `extra_body` key absent from `mock_create.call_args.kwargs` for OpenAI path.
- *Resource Exhaustion — retry loop count*: Assumption: refactored method has the same retry loop bounds as the original. Betrayal: refactor accidentally introduces double retry (loop in both wrapper and inner). Consequence: 9 attempts on transient 503 → API ban. Mitigation: ONLY ONE retry loop in the codepath, lives in `_embed_batch_with_extras`. Test: mock `client.embeddings.create` to fail twice then succeed; assert `mock_create.call_count == 3` exactly (not 6 or 9).
- *Skipped*: Encoding boundaries (extra_body is JSON-stable), State corruption (per-instance method).

**TEIEmbeddingProvider `__init__` override**
- *Input Hostility — `dims` type/value*: Assumption: caller passes valid positive int. Betrayal: `dims=None` (relying on default), `dims="1024"` (str), `dims=0`, `dims=-1`, `dims=1.5`, `dims=True` (bool, an int subclass in Python). Consequence: silently stored, propagates to `@property dims`, breaks DB table key tuple `(provider, model, dims)` → embeddings keyed under wrong dims become unreachable (silent corruption — worst kind of bug). Mitigation: `dims` is REQUIRED kwarg (no default). Type-validate at top of `__init__`: `if not isinstance(dims, int) or isinstance(dims, bool) or dims <= 0: raise ValueError(f"TEI dims must be positive int, got {dims!r}")`. Test parametrizes over `[None, "1024", 0, -1, 1.5, True, False]`.
- *Temporal Betrayal — init ordering*: Assumption: parent `__init__` doesn't access `self.dims` during execution. Betrayal: future parent refactor invokes `self.dims` (e.g., during rerank validation), and TEI sets `self._dims` AFTER `super().__init__(...)` — AttributeError. Consequence: TEI uninstantiable on parent drift. Mitigation: structural ordering — set `self._dims = dims` BEFORE calling `super().__init__(...)`. Verified parent at lines 195–266 doesn't touch `self.dims` today, but defensive ordering survives future drift. Test: instantiate TEI with all-valid args, assert no AttributeError.
- *Dependency Treachery — `**kwargs` blackbox*: Assumption: parent's signature stays stable. Betrayal: parent gains a new kwarg name that conflicts with TEI's expectations, OR TEI uses `**kwargs` blackbox forwarding and silently drops a kwarg the user passed. Consequence: brittle subclass; user-passed config silently ignored. Mitigation: TEI `__init__` declares EXPLICIT signature for the kwargs it accepts (no `**kwargs` catch-all). Specifically: `dims: int, base_url: str, model: str, api_key: str | None = None, batch_size: int = 100, timeout: int = 30, retry_attempts: int = 3, ...`. Pass curated set to `super().__init__(...)`. Trade-off: TEI signature must update when parent gains functionality, but failure mode becomes "TypeError at instantiation" (loud) instead of "silent kwarg drift" (silent).
- *Skipped*: Encoding boundaries (int passthrough), State corruption (single instance), Resource exhaustion (init is bounded).

**TEIEmbeddingProvider `@property dims` / `@property name`**
- *Temporal Betrayal — property accessed pre-init*: Assumption: property reads happen after `__init__` completes. Betrayal: pickle deserialization or `__init_subclass__` reads `dims` before `_dims` is set. Consequence: AttributeError surfaces in obscure paths. Mitigation: TEI is never pickled and has no `__init_subclass__` — accept Python's natural AttributeError as "loud enough." If this assumption breaks later, switch to `getattr(self, "_dims", None)` with a `RuntimeError` fallback.
- *Dependency Treachery — parent fallback drift*: Assumption: parent's `dims` property at line 464–469 returns `1536` as fallback. Betrayal: parent refactor changes fallback to "raise NotImplementedError" or "look up from API"; TEI's override is fine but if accidentally REMOVED, TEI silently returns 1536 for jina-v3 (correct: 1024) → all retrieval breaks invisibly. Consequence: silent quality failure post-deploy. Mitigation: regression test asserts BOTH `provider.dims == self._dims` (positive: returns configured) AND `provider.dims != 1536` for non-1536 models (negative: catches override removal). Both assertions in the same test, parametrized over `[(1024, "jinaai/jina-embeddings-v3"), (256, "jinaai/jina-embeddings-v5-text-nano"), (512, "jinaai/jina-embeddings-v5-text-small")]`.
- *Skipped*: Input hostility (no input), Encoding boundaries (stable types), State corruption (pure read), Resource exhaustion (O(1)).

**TEIEmbeddingProvider `_embed_batch_internal` override**
- *Input Hostility — validation result drops*: Assumption: `validate_task` is called for fail-fast. Betrayal: implementation calls `validate_task(task)` but ignores the return; if `validate_task` ever normalizes (e.g., lowercasing), TEI bypasses the normalization and Voyage uses it → asymmetric semantics. Consequence: forward-compat trap. Mitigation: bind the validated value back: `task = validate_task(task)`. TEI is the first provider with task-consuming logic, sets the precedent.
- *Encoding Boundaries — TEI server forwards `extra_body`*: Assumption: TEI's OpenAI-compat layer forwards `extra_body={"task": "retrieval.X"}` to the underlying jina model's encode call. Betrayal: TEI deployment drops unknown extra_body fields silently for non-jina models, OR a TEI version regression breaks pass-through. Consequence: jina-v3/v5 silently embed everything as passages → asymmetric retrieval degrades to symmetric, undetectable in unit tests. Mitigation: NOT a structural code mitigation — this is a deployment assumption. Document via inline code comment at the mapping site: `# TEI's OpenAI-compat layer must forward extra_body to model.encode(). Verify with a manual query/passage cosine test on first deploy — if scores are symmetric, TEI is dropping extras.` User's "live runs gated" framing already absorbs this; flagging here so the comment isn't omitted.
- *Dependency Treachery — parent strips `extra_body`*: Assumption: parent's `_embed_batch_with_extras` honors `extra_body` and forwards to `client.embeddings.create`. Betrayal: future parent refactor adds an OpenAI-compat allowlist filter that strips `extra_body`. Consequence: TEI thinks it sent extras; wire payload doesn't have them; silent retrieval-quality failure. Mitigation: regression test mocks `client.embeddings.create` and asserts forwarding boundary — `mock_create.call_args.kwargs["extra_body"] == {"task": "retrieval.passage"}`. Tests at the parent-method boundary, NOT just at TEI's method boundary, catches a parent refactor regression.
- *Skipped*: Temporal betrayal (single-shot, no recursion), State corruption (no shared state), Resource exhaustion (extra_body dict is bounded — 1 key).

**Pydantic `EmbeddingConfig` schema widening**
- *Input Hostility — partial TEI config*: Assumption: when `provider="tei"`, user provides BOTH `base_url` AND `dims`. Betrayal: user provides only one (or neither) — Pydantic accepts each individually as `None`-defaulting. Consequence: validation passes but factory crashes with confusing traceback. Mitigation: model-validator branch — when `provider == "tei"`, BOTH `base_url` AND `dims` MUST be set. Pydantic must report BOTH missing in a SINGLE ValidationError when both are missing (not just the first). Use `field_validator` would only catch one; use `@model_validator(mode="after")` accumulating into a list of errors. Test parametrizes `[(missing_only_base_url,), (missing_only_dims,), (missing_both,)]` and asserts the error message names each missing field.
- *Temporal Betrayal — validator ordering*: Assumption: TEI validator runs after all fields are populated. Betrayal: existing `validate_rerank_config` and `validate_azure_config` already exist; ordering between them affects which error surfaces first. Consequence: confusing user-facing errors when multiple validation issues coexist. Mitigation: add the new TEI validator as the LAST `@model_validator(mode="after")` in declaration order (after rerank and azure). It depends on `provider`, `base_url`, `dims` being final; it doesn't influence others. Test: construct `provider="tei", rerank_format="cohere", base_url=None, dims=None` (multiple errors expected) — assert ValidationError mentions all triggered issues, not just one.
- *Dependency Treachery — if/elif chain audit*: Assumption: agents update every `if/elif` branch in `embedding_config.py` that hardcodes the existing two providers. Betrayal: any miss → silent fall-through to else branch which treats TEI as voyageai (or worse). Sites: `get_default_model` at 296, `is_provider_configured` at 312, `get_missing_config` at 335, `get_provider_config` at 252, CLI `choices` at 367, env-fallback at 456. Consequence: TEI provider silently misbehaves in one method while passing tests in others. Mitigation: parametrize the test suite over `[("openai", ...), ("voyageai", ...), ("tei", ...)]` and assert behavior of EACH method for EACH provider. Cross-product catches missing branches: e.g., `EmbeddingConfig(provider="tei", base_url="...", dims=1024, model="jinaai/jina-embeddings-v3").is_provider_configured() is True`, `.get_missing_config() == []`, `.get_default_model()` raises (TEI has no default), `.get_provider_config()["dims"] == 1024`. Forces every if/elif to be exercised explicitly.
- *Encoding Boundaries — env var coercion*: Assumption: `CHUNKHOUND_EMBEDDING__DIMS=1024` env var coerces to int via Pydantic. Betrayal: `=1024.5`, `=hi`, `=` empty string fail with various Pydantic messages depending on version. Consequence: confusing error UX for env-driven config. Mitigation: rely on Pydantic's built-in `int | None` coercion; don't custom-handle. Test: env var with valid int parses; env var with non-int raises ValidationError mentioning "dims" or "int parsing".
- *Skipped*: State corruption (Pydantic models immutable post-init), Resource exhaustion (schema bounded).

**`embedding_factory.py` dispatch + supported list + `validate_provider_dependencies`**
- *Dependency Treachery — `openai` SDK transitive dep for TEI*: Assumption: TEI is "just an HTTP client to a TEI server." Betrayal: TEI subclasses OpenAIEmbeddingProvider, so it transitively depends on the openai SDK being installed. If `openai` is missing, `validate_provider_dependencies("tei")` would naively return True (no TEI-specific imports); but `create_provider` then fails at TEI subclass instantiation with cryptic openai-import-error. Consequence: dependency check passes, instantiation fails — opposite of fail-fast. Mitigation: in `validate_provider_dependencies("tei")`, import `TEIEmbeddingProvider` (which transitively imports openai). If openai is missing, the import fails and `validate_provider_dependencies` returns `(False, "Missing dependencies for tei provider: <openai not installed>")`. Test: monkeypatch the `openai` module to be missing → call `validate_provider_dependencies("tei")` → assert returns `(False, ...)` with informative message.
- *Temporal Betrayal — circular import*: Assumption: factory's lazy import of `create_tei_provider` at call time avoids cycles. Betrayal: `chunkhound/embeddings.py` is imported by many modules, and re-exporting `TEIEmbeddingProvider` at module load could create an import cycle if any TEI transitive dep imports back from `chunkhound.embeddings`. Consequence: import-time crashes affect ALL chunkhound imports, not just TEI. Mitigation: structural — verified TEI's import graph in advance (TEI → openai_provider + interfaces, neither imports `chunkhound.embeddings`). Use lazy imports inside `_create_tei_provider` (mirrors OpenAI/Voyage). Type hints for TEI under `TYPE_CHECKING` block at top of factory file. Smoke test: `import chunkhound.core.config.embedding_factory` clean even if TEI deps were broken.
- *Input Hostility — pre-existing `"openai_compatible"` ghost*: Assumption: `get_supported_providers()` returns providers that actually dispatch. Betrayal: PRE-EXISTING — line 198 already returns `["openai", "voyageai", "openai_compatible"]`, but `create_provider` only dispatches `openai`/`voyageai`, AND Pydantic Literal doesn't accept `"openai_compatible"`. Consequence: dead code; this task isn't responsible for fixing it, but the new TEI changes touch the same surface. Mitigation: file a follow-up bones task for the cleanup (note in commit body). For TEI: ensure NEW `"tei"` entry is added consistently across all FOUR sites — `get_supported_providers`, `validate_provider_dependencies`, `create_provider` dispatch, AND Pydantic Literal. All four MUST update together; test parametrizes over `[("openai", True), ("voyageai", True), ("tei", True), ("nonsense", False)]` for `validate_provider_dependencies` to ensure each returns the right thing.
- *Skipped*: Encoding boundaries (ASCII names), State corruption (factory is stateless), Resource exhaustion (bounded operations).

**`chunkhound/embeddings.py` `create_tei_provider` registry function + re-export**
- *Input Hostility — bypass-Pydantic path*: Assumption: callers go through Pydantic-validated `EmbeddingConfig` → factory → `create_tei_provider`. Betrayal: legacy path `create_provider_from_legacy_args(provider="tei", ...)` or direct calls to `create_tei_provider(base_url=None, ...)` bypass Pydantic. Consequence: legacy callers get worse error messages than Pydantic-validated callers. Mitigation: in `create_tei_provider`, validate `base_url` (must start with `http://` or `https://`) AND `dims` (positive int) BEFORE constructing `TEIEmbeddingProvider`. Reuse `validate_base_url` field-validator logic (or import from config). Test: `create_tei_provider(base_url=None, ...)` raises ValueError; `create_tei_provider(base_url="not-a-url", ...)` raises ValueError; `create_tei_provider(base_url="http://localhost:8080/v1", model="...", dims=0)` raises ValueError.
- *Dependency Treachery — re-export creates cycle*: Assumption: `from chunkhound.providers.embeddings.tei_provider import TEIEmbeddingProvider` at module-level in `chunkhound/embeddings.py` doesn't create a cycle. Betrayal: TEI's transitive imports (openai_provider, interfaces) DON'T import `chunkhound.embeddings` today, but a future refactor adds such an import → cycle. Consequence: `ImportError` at module load propagates to every importer of `chunkhound.embeddings`. Mitigation: add a smoke test in `tests/unit/` — `def test_chunkhound_embeddings_re_exports_tei(): import chunkhound.embeddings; assert hasattr(chunkhound.embeddings, "TEIEmbeddingProvider")`. Catches a cycle-introducing refactor immediately.
- *Skipped*: Encoding boundaries (string params normal), Temporal betrayal (pure factory), State corruption (no state), Resource exhaustion (bounded).

### Group E+F failure catalog (adversarial-planning, 2026-04-25 Checkpoint 3)

Spot-check before cataloging — production call shapes differ across the 8 sites:
- `embedding_service.py:487` → `embed(texts)`; `indexing_coordinator.py:1596` → `embed_batch(texts)`; `gap_detection.py:253` → `embed_batch(chunk_contents)`; `clustering_service.py:97/199/396` → `embed_batch(file_contents)`; `single_hop_strategy.py:66` → `embed([query])`; `gap_detection.py:461` → `embed(queries)`. **Mixed `embed` / `embed_batch` across sites — test fixtures MUST mock the exact method per site.** A test that mocks `embed_batch` while production calls `embed` produces silent false-positive GREEN.
- `embedding_service.py:413-417` cache call shape verified: `get_existing_embeddings(chunk_ids=[...], provider=..., model=...)` — three kwargs, EXACT names.

**Call site edits (Steps 19-20: 6 passage + 2 query)**
- *Input Hostility — typo in task literal*: Assumption: agent types `task="passage"` correctly at all 8 sites. Betrayal: typo `"Passage"`, `"passages"`, `"PASSAGE"` at any site. Consequence: `validate_task` raises ValueError at provider's deepest entry — but only when the path is exercised in a test/run; an untested call site silently ships the typo, then raises ValueError mid-indexing in user-land far from the call site. Mitigation: structural — every call site has a corresponding test that drives that exact path with a real `task="passage"` literal. Test failure surfaces at design time, not in user's reindex run.
- *Input Hostility — forgotten site*: Assumption: agent edits all 8 sites. Betrayal: agent edits 7 of 8 — most likely candidate is one of the three near-identical lines in `clustering_service.py` (97/199/396) where grep-based edits skip a duplicate. Consequence: voyage `input_type="document"` silently applies to the missed path. If it's a query site (`single_hop_strategy.py:66` or `gap_detection.py:461`), retrieval quality drops invisibly — no error, just worse results. Mitigation: structural — pre-edit, run `grep -n "embedding_provider.embed" chunkhound/services/clustering_service.py` and verify edit count matches grep count. Tests parametrize over all 8 sites with file:line labels (e.g., `pytest.param(..., id="clustering_service.py:97")`) so pytest output enumerates every site explicitly — a dropped param is visible in the test list.
- *Input Hostility — positional-arg drift*: Assumption: agent uses `task="passage"` kwarg form per the locked convention. Betrayal: agent uses positional form `embed_batch(texts, "passage")` — works today because `task` is the only trailing param, but if any provider gains a new trailing kwarg before `task`, the literal lands in the wrong slot silently. Consequence: silent semantic drift on future signature evolution. Mitigation: structural — convention is `task=` kwarg always (already locked in Group A). Post-edit verification grep: `grep -rn 'task="passage"\|task="query"' chunkhound/services/ | wc -l` should equal 8.
- *Dependency Treachery — `gap_detection.py` mixed-task module*: Assumption: agent edits the right line for the right task. Betrayal: `gap_detection.py` has BOTH a passage site (line 253, clustering chunk content) AND a query site (line 461, gap query embedding). Agent confuses the two — adds `task="passage"` to line 461 OR `task="query"` to line 253. Consequence: query gaps embedded as passages → asymmetric retrieval inverted, the WORST case (semantically valid, silently wrong). Mitigation: structural — both lines tested in the same test session (passage test imports the gap-detection passage path; query test imports the gap-query path). Test names include line numbers. The two tests live in different files (`test_embedding_call_sites_passage.py` vs `_query.py`), so a copy-paste error between them is visible at file boundary.
- *Skipped*: Encoding boundaries (`task` is a Python literal, no encoding crosses), Temporal betrayal (static code edit, no runtime ordering), State corruption (no shared state), Resource exhaustion (kwarg adds no resources).

**Call site test files (Steps 18, 20: `test_embedding_call_sites_passage.py` + `_query.py`)**
- *Input Hostility — mock at wrong abstraction level*: Assumption: mock `provider.embed`/`embed_batch` and assert on `call_args.kwargs`. Betrayal: production accesses provider via different paths — `embedding_service.py:487` uses `self._embedding_provider.embed(...)` (direct attribute), `gap_detection.py:253` uses `self._embedding_manager.get_provider().embed_batch(...)` (manager indirection), `clustering_service.py` uses `self._embedding_provider` (direct), `single_hop_strategy.py:66` uses `self._embedding_provider`. Test that mocks at the manager level when production accesses directly (or vice versa) produces false-positive GREEN. Consequence: test passes; production threading is broken. Mitigation: structural — each test reads the production call line first and mocks at the SAME level production accesses. Test docstring cites the production line being driven so reviewers can verify the mock matches.
- *Input Hostility — mocked method mismatch*: Assumption: production uses `embed`. Betrayal: 5 of 8 sites use `embed_batch`, only 3 use `embed`. Test that mocks `provider.embed = AsyncMock(...)` while production calls `embed_batch` results in `embed_batch` being a MagicMock auto-attribute that returns a MagicMock — test asserts on the wrong mock and passes vacuously. Consequence: silent test pass while production threading is broken. Mitigation: structural — each test mocks the EXACT method the production line calls (verified per call-site list above). Adversarial assertion: in addition to `mock_embed.assert_called_with(..., task="passage")`, also assert `mock_provider.embed_batch.call_count == 0` (the unused method must NOT be called) — and vice versa for embed_batch sites. Catches mock-level confusion.
- *Temporal Betrayal — test asserts on its own wiring, not production*: Assumption: test drives the production code path. Betrayal: test imports a function and calls `provider.embed_batch(["x"], task="passage")` directly — that's testing the test's own kwarg, not production threading. Consequence: GREEN with zero coverage of the actual call site. Mitigation: structural — every test invokes a public service entry point (e.g., `EmbeddingService._generate_embeddings_in_batches(...)`, the gap-detection clustering method, etc.) and asserts the inner mock was called with `task="passage"`. Test name in the form `test_<service_method>_passes_task_passage_at_<file>_<line>` makes the production line under test explicit.
- *Dependency Treachery — partial mocks leak real I/O*: Assumption: provider mock isolates the test from real network/DB. Betrayal: services like `EmbeddingService` also touch `self._db.get_existing_embeddings(...)` and `self._metrics_collector.start_batch(...)`; partial mock leaves these unmocked, real DB/metrics calls happen. Consequence: tests slow, flaky, or fail in CI without DB. Mitigation: structural — fixture provides a fully-mocked service at the boundary (provider + db + metrics_collector); `pytestmark = pytest.mark.unit` enforces the no-I/O contract via the unit-tier classification.
- *State Corruption — fixture sharing across tests*: Assumption: fixture is fresh per test. Betrayal: module-scoped `mocked_provider` fixture accumulates `call_args_list` across tests; test order affects assertions. Consequence: flaky test ordering, hard to debug. Mitigation: structural — function-scoped fixtures (pytest default), one fresh `AsyncMock` per test.
- *Skipped*: Encoding boundaries (Python literals only), Resource exhaustion (mock-only tests bounded).

**Cache identity regression test (Step 21: `test_embedding_cache_identity.py`)**
- *Input Hostility — narrow assertion lets future drift through*: Assumption: a future change adding `task` to the cache fingerprint would break this test. Betrayal: test asserts `"task" not in mock.call_args.kwargs` — passes today AND passes if a future change adds `extra_field="x"` instead of `task`. Membership assertions don't catch unrelated additions. Consequence: regression test rots; users get cache-invalidation surprises despite the "passing" test. Mitigation: structural — assert EXHAUSTIVE equality of the kwargs set: `assert set(mock.call_args.kwargs.keys()) == {"chunk_ids", "provider", "model"}` (key set, not value set since chunk_ids varies). Any new kwarg fails the equality. New success criterion locks this assertion shape.
- *Input Hostility — mock at wrong layer skips the cache call*: Assumption: test mocks `db.get_existing_embeddings`. Betrayal: a future refactor wraps the DB call in a helper (e.g., `_lookup_cache(provider, model, task)`) and the test still mocks the lower DB level — wrapper SWALLOWS `task` and only forwards `provider/model` to DB; DB mock sees what the test expects, test passes, wrapper has invalidated the cache silently because IT used `task` for some other lookup. Consequence: false confidence. Mitigation: structural — second test case (the skip-existing-on-reindex test) drives the END-TO-END filtering flow with `task="passage"` and asserts `provider.embed_batch.assert_not_called()`. End-to-end fingerprint preservation is the user-visible contract; signature equality is the unit-level proxy. BOTH tests required, not just one.
- *Temporal Betrayal — second-run idempotency*: Assumption: filtering twice produces the same skip set. Betrayal: `_filter_existing_embeddings` mutates state (caches result, drains an iterator). Consequence: second call returns wrong filter set, breaks reindex flows. Mitigation: structural — call `_filter_existing_embeddings` twice in one test, assert identical return values AND that `db.get_existing_embeddings` is called twice with identical kwargs (no memoization sneaking in). The current implementation is stateless per `embedding_service.py:395-438` but locking the property prevents future drift.
- *Dependency Treachery — DB return type contract*: Assumption: `db.get_existing_embeddings` returns a `set[int]` (used in `chunk_id not in existing_chunk_ids` at line 426; fallback at line 420 is `set()`). Betrayal: mock returns a `list[int]` — `not in` works for both, but if production switches to set-based intersection (`set(chunk_ids) - existing`), a list mock fails with `TypeError`. Consequence: tests pass against the mock but production breaks. Mitigation: structural — mock returns `set` matching the real return type; document in test docstring: "DB returns set per embedding_service.py:420 fallback; mock matches."
- *State Corruption — service instance shared across tests*: Assumption: each test gets fresh service. Betrayal: shared `EmbeddingService` across tests — second test sees first test's mock state. Consequence: test order dependence. Mitigation: structural — function-scoped fixture builds a fresh `EmbeddingService` with fresh mocks per test.
- *Skipped*: Encoding boundaries (integers and strings only, no encoding crosses), Resource exhaustion (mock-bounded).

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
- [x] `EmbeddingConfig.provider` Literal widened to include `"tei"`; provider-specific config branches updated; model-validator enforces `base_url` AND `dims` required when `provider="tei"` (single ValidationError names BOTH missing fields when both omitted) (commit 5ab7dfe)
- [x] `TEIEmbeddingProvider` class exists, registered in factory via `_create_tei_provider`, exported from `chunkhound/embeddings.py` via `create_tei_provider`, sends `extra_body={"task": "retrieval.{passage,query}"}` for jina models (commits a7502f0, 328cd83, 5ab7dfe)
- [x] `TEIEmbeddingProvider.name` returns `"tei"`; `supports_reranking()` returns `False` when constructed with embedding-only config (no `rerank_format`) (commit a7502f0)
- [x] OpenAI `_embed_batch_with_extras` refactor preserves: (a) single retry loop, (b) single `_usage_stats["requests_made"]` increment per call, (c) `validate_task` runs before any network I/O, (d) `extra_body` kwarg OMITTED from `client.embeddings.create` when `extra_body is None` (not passed as `extra_body=None`) (commit a7502f0; usage_stats single-increment test in test_openai_provider.py + existing extra_body-absent test)
- [x] TEI `__init__` rejects `dims` of types/values `[None, "1024", 0, -1, 1.5, True, False]` with `ValueError` before instantiation completes (commit a7502f0)
- [x] TEI `dims` regression: `provider.dims` returns configured value AND `provider.dims != 1536` for non-1536 models — both assertions, parametrized over jina v3/v5-nano/v5-small dims (commit a7502f0)
- [x] TEI `_embed_batch_internal` boundary test: parent's `_embed_batch_with_extras` receives `extra_body={"task": "retrieval.passage"}` — asserted at the parent-method mock boundary, not just at the TEI-method boundary, to catch parent-refactor regressions (commit 328cd83)
- [x] `validate_provider_dependencies("tei")` returns `(False, ...)` when `openai` SDK is unavailable (transitively required by TEI subclass) (commit 5ab7dfe)
- [x] `create_tei_provider` registry function fail-fast validates `base_url` scheme (http/https) and `dims > 0` BEFORE instantiating TEI, so legacy callers get same UX as Pydantic-validated callers (commit 5ab7dfe)
- [x] Re-export smoke test: `import chunkhound.embeddings; chunkhound.embeddings.TEIEmbeddingProvider` resolves without ImportError (commit 5ab7dfe — uses PEP 562 `__getattr__` to break a circular import discovered during integration testing; warm-import smoke test plus integration suite covers cold-import case)
- [x] All 6 passage sites pass `task="passage"`; both query sites pass `task="query"` (commits 327cc94, c414ec9)
- [x] All 8 call sites use `task=` kwarg form (NOT positional). Post-edit `grep -rn 'task="passage"\|task="query"' chunkhound/services/ | wc -l` returns exactly 8 (verified post-Cycle B)
- [x] Each call-site test enumerates with file:line label so pytest output names every site (test class names embed file:line, e.g. `TestEmbeddingServiceAt487`, `TestClusteringServiceAt97`) — a dropped site is visible in the test list (commits 327cc94, c414ec9)
- [x] Each call-site test mocks the EXACT method production calls at that line (`embed` vs `embed_batch` — verified per-site against production source). Adversarial guard: assert the UNUSED method has `call_count == 0`, not just that the used method received `task=...` (commits 327cc94, c414ec9)
- [x] Mixed-task module guard: `gap_detection.py` passage site (line 253) AND query site (line 461) BOTH tested in their respective test files; cross-contamination (passage → 461 or query → 253) would fail at least one test (commits 327cc94, c414ec9)
- [x] Cache regression test green — existing Voyage embeddings on this repo NOT regenerated when reindexed (DB tables keyed by `(provider, model, dims)` — voyage embeddings go dormant when switching to TEI, not deleted, not regenerated) (commit a9c339d)
- [x] Cache identity assertion is EXHAUSTIVE: `assert set(mock.call_args.kwargs.keys()) == {"chunk_ids", "provider", "model"}` (key-set equality, not `"task" not in kwargs` membership) — catches future kwarg drift, not just a `task` regression (commit a9c339d)
- [x] Cache regression has BOTH a unit-level signature test (`db.get_existing_embeddings` kwargs locked) AND an end-to-end skip-on-reindex test (`provider.embed_batch.assert_not_called()` when DB has the chunk) — wrapper-around-cache refactors that swallow `task` upstream of the DB call would pass the unit test but fail the end-to-end test (commit a9c339d)
- [x] Voyage `rerank` method unchanged (untouched throughout; verified by absence in `git diff main..HEAD chunkhound/providers/embeddings/voyageai_provider.py`'s `rerank` method)
- [x] `uv run pytest -m "unit or integration"` green — 3052 passed, 44 skipped, 99 deselected (post fake-provider shape-compat fix, commit 15487fa)
- [x] Mypy clean on touched-file deltas (preexisting errors stay for ch-6ea — see Anti-Patterns). All errors at lines NOT touched by Checkpoint 3 commits.
- [x] User can `cp .chunkhound.json` with `provider: "tei"` and a TEI deployment URL to test jina-v5 on a private repo (Pydantic schema widened in commit 5ab7dfe; factory dispatch + registry function in place; user-facing capability ready)

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
- [2026-04-25T03:45:08Z] [Seth Yanow] Adversarial planning for Group D (Checkpoint 2). Failure catalog appended to Key Considerations covering 7 components: OpenAI _embed_batch_with_extras refactor, TEI __init__/dims/name/_embed_batch_internal overrides, Pydantic schema widening, factory dispatch+validate_deps, registry function+re-export. 10 new success criteria added for structural guarantees. Key risks identified: (1) extra_body=None must be OMITTED from client.embeddings.create not passed explicitly (Azure-compat); (2) TEI dims type validation includes bool exclusion (Python bool is int subclass); (3) TEI requires openai SDK transitively, validate_provider_dependencies must reflect this; (4) Pydantic must report base_url+dims missing in single ValidationError; (5) regression test for parent _embed_batch_with_extras receiving extra_body kwarg catches future parent-refactor regression. Pre-existing 'openai_compatible' ghost in get_supported_providers flagged as follow-up. Three commits planned for Group D: (a) refactor _embed_batch_with_extras + TEI class shell; (b) TEI _embed_batch_internal task->extra_body; (c) wire schema+factory+registry.
- [2026-04-25T04:24:04Z] [Seth Yanow] Checkpoint 2 complete (Group D, Steps 11-17). Three commits: a7502f0 (refactor _embed_batch_with_extras + TEI class shell with dims/name overrides + 16 unit tests), 328cd83 (TEI _embed_batch_internal task->extra_body override + 11 unit tests), 5ab7dfe (Pydantic schema widening + factory _create_tei_provider + chunkhound/embeddings.py create_tei_provider with PEP 562 __getattr__ for cycle break + 27 unit tests). Total: 54 new unit tests added, 3021 unit+integration tests pass. Critical learning: adversarial planning's 'circular import' Temporal Betrayal mitigation was insufficient with unit-tier smoke test only — modules are warm at unit-test runtime so cold-import cycles don't trigger. Integration tests caught the cycle via subprocess CLI invocation. Fixed with module-level __getattr__; pattern recorded in MEMORY.md. 7 of 11 checkpoint-specific success criteria checked off (5 of original 13 plus 7 new from adversarial planning). Mypy clean on touched-file deltas (pre-existing errors at create_provider_from_legacy_args stay for ch-6ea, untouched). Group D ready for user review; Checkpoint 3 (Groups E+F = 8 call sites + cache regression) blocked on user approval to proceed.
- [2026-04-25T04:36:04Z] [Seth Yanow] Adversarial stress test (Groups A-D) — 20/20 GREEN; Three-Question Framework applied to each. Out-of-scope findings logged for follow-up: (1) Pydantic EmbeddingConfig is mutable post-construction by default — model_validator runs at __init__ only, so config.base_url=None after construction silently bypasses validate_tei_required_fields; downstream is_provider_configured() still catches it, but a frozen=True hardening would require auditing all callers for mutation. (2) TEI accepts empty/whitespace model name at construction — fails loudly at TEI server's first API call, but validate_tei_required_fields could fail-fast on construction; minor exposure. (3) Subtle: extra_body forwarded by reference (not copied) into the OpenAI SDK call; defensive copy not needed today since TEI builds a fresh dict each call, but a future caller mutating between handoff and SDK call would see the mutation. (4) Downstream constraint: TEI dims accepts unbounded Python int but LanceDB/DuckDB vector columns are fixed-size — huge dims would fail at DB schema creation, not at provider construction; worth noting in MEMORY.md. Process smell check: no dead stubs, no happy-path-only test coverage gap, scope was right-sized. Port/equivalence check: N/A (original code, not a port). All findings minor; none block Group D. Adversarial tests committed at tests/unit/test_ch_agj_adversarial.py for regression durability.
- [2026-04-25T06:42:15Z] [Seth Yanow] Adversarial planning for Checkpoint 3 (Groups E+F = Steps 18-21). Failure catalog appended to Key Considerations covering 3 components: call site edits (8 sites, mixed embed/embed_batch shapes), call site test files (mock-at-wrong-level + method-mismatch + assert-on-own-wiring risks), cache identity regression test (exhaustive-equality vs membership-assertion + BOTH unit and end-to-end coverage). Spot-check verified production call shapes differ across 8 sites — 5 use embed_batch, 3 use embed; tests must mock the EXACT method per site. 6 new success criteria added: kwarg-form convention enforcement, file:line parametrize labels, exact-method mock match with unused-method call_count==0 guard, mixed-task module guard for gap_detection.py, exhaustive kwargs equality on cache call, dual unit+end-to-end coverage on cache regression. Key risks: (1) typo in literal silently ships if path untested; (2) forgotten edit at one of clustering_service.py 97/199/396 (three near-identical sites in one file); (3) gap_detection.py mixes passage:253 and query:461 — copy-paste between them could invert asymmetry; (4) cache test with membership assertion would rot — exhaustive equality is the lock. Catalog ready for Checkpoint 3 TDD cycles.
- [2026-04-25T08:02:52Z] [Seth Yanow] Adversarial stress test for Groups E+F (Checkpoint 3) — 9/9 GREEN; Three-Question Framework applied to each. Out-of-scope findings logged for follow-up: (1) Pre-existing semaphore deadlock in EmbeddingService.process_batch — production uses asyncio.Semaphore(max_concurrent_batches) non-reentrantly and recurses INSIDE the async with semaphore block at line 540-542 (token-limit batch split). With max_concurrent_batches=1 the recursive call deadlocks waiting for the permit the outer call holds. Real production default is 8 or provider-recommended so the bug is normally latent, but if anyone explicitly sets max=1 they hit it. Worth a separate bones task. (2) Empty-input layering inconsistency: EmbeddingService._filter_existing_embeddings has NO short-circuit for empty chunk_ids and makes a wasteful DB call (line 413), while IndexingCoordinator._generate_embeddings short-circuits at line 1587. Different services, different empty-input contracts — minor cleanup. (3) Lesson for the protocol-shape playbook: the 31-test integration failure (FakeEmbeddingProvider missing task kwarg) could have been caught at unit tier via inspect.signature probes. test_ch_agj_adversarial_efgroups.py::TestFakeProviderEmbedSignatureAcceptsTask now adds that probe retroactively — future protocol-shape changes should add a similar unit-tier probe FIRST. Process smell check: no dead stubs, no happy-path-only gaps, scope was right-sized. Port/equivalence check: N/A. All 9 adversarial tests pass with tight timeouts.
- [2026-04-25T08:04:07Z] [Seth Yanow] Checkpoint 3 complete (Groups E+F, Steps 18-23). Five commits: 327cc94 (passage cycle: 6 production sites + 6 unit tests), c414ec9 (query cycle: 2 production sites + 2 unit tests), a9c339d (cache identity regression: 3 unit tests with exhaustive kwargs equality + end-to-end skip-on-reindex), 15487fa (fake provider shape-compat: FakeEmbeddingProvider/ValidatingEmbeddingProvider accept task kwarg — fixes 31 integration test failures from task threading), [adversarial battery commit pending]. Total: 17 new unit tests + 9 adversarial probes = 26 new tests. Full suite: 3052 unit+integration green (was 2998 before C3). Targeted mypy: no errors introduced at touched lines (487, 1596, 253, 461, 66, 97, 199, 396); all 125 errors in scope of ch-6ea (pre-existing). All 12 Group E+F success criteria checked off. Voyage rerank untouched. Cache identity preserved. ch-agj task plumbing COMPLETE — ready for live runs.
