# ChunkHound Knowledge Store

Durable codebase learnings. **For task status and session history: see `.bones/`.**
Things here should still be useful six months from now.

---

## Architecture

### Two `EmbeddingConfig` classes — name collision
- `chunkhound/interfaces/embedding_provider.py:16-30` → `@dataclass EmbeddingConfig` (simple dataclass; returned by `OpenAIEmbeddingProvider.config` property and held internally by providers)
- `chunkhound/core/config/embedding_config.py:72` → `EmbeddingConfig(BaseSettings)` (Pydantic v2 settings; user-facing, loaded from env/CLI/files)
- Both exist by design. When editing, name the module path to disambiguate.

### Cache identity (how re-indexing decides what to skip)
- `embedding_service.py:413-417` → `get_existing_embeddings(chunk_ids, provider, model)`
- Fingerprint is `(chunk_id, provider, model)` only. Does NOT include `task`, `dims`, or content hash.
- **DB tables are keyed by `(provider, model, dims)`**. Switching providers (e.g., voyageai → tei) writes to a new table; old embeddings stay dormant, not deleted, not regenerated. "Don't disturb existing data" is automatic.

### Embedding `dims` is bounded by the DB, not the provider
- TEI / OpenAI provider classes accept any positive `int` for dims (Python int is unbounded).
- LanceDB and DuckDB vector columns are fixed-size at table-creation time. Astronomical dims (e.g., `2**31`) would fail at DB schema creation, NOT at provider construction — the failure mode is a ValueError or panic in the DB layer, far from the cause.
- Practical embedding dims are typically `≤ 4096`; any model claiming larger should be sanity-checked.

### Embedding provider anatomy (where retry lives)
- `VoyageAIEmbeddingProvider._embed_single_batch_locked()` (voyageai_provider.py:~297) — semaphore-protected, retry loop with `asyncio.to_thread(self._client.embed, ...)` (Voyage SDK is sync)
- `OpenAIEmbeddingProvider._embed_batch_internal()` (openai_provider.py:~697) — async retry loop, handles RateLimitError/BadRequestError/APITimeoutError. Token-limit BadRequestError triggers `handle_token_limit_error` recursive fallback
- These are "the deepest internal entry points" for each provider. Input validation belongs here to fail-fast before retry/network I/O.

### Protocol vs implementation
- `EmbeddingProvider` is a `typing.Protocol` (structural typing, no inheritance required).
- Adding methods or kwargs to the Protocol does NOT break existing impls at runtime — only mypy sees the drift. Structurally-compatible providers keep working until callers actually pass the new kwarg.

### OpenAI client initialization is lazy
- `_ensure_client()` creates `AsyncOpenAI` / `AsyncAzureOpenAI` in async context.
- Creating in `__init__` caused TaskGroup errors on Ubuntu when no event loop is running — leave this alone unless you reproduce the bug.

### `chunkhound.embeddings` re-exports use PEP 562 `__getattr__` to dodge a cycle
- Eager `from chunkhound.providers.embeddings.X_provider import XProvider` at module level in `chunkhound/embeddings.py` creates a circular import:
  `chunkhound.embeddings → providers.embeddings.X_provider → chunkhound.providers.__init__ → DuckDBProvider → chunkhound.embeddings (mid-load, EmbeddingManager not yet defined)`
- Fixed for `TEIEmbeddingProvider` via PEP 562 module-level `__getattr__` that lazy-imports on first access. Same pattern works for any future re-export.
- **Unit-tier smoke tests don't catch this** — by the time pytest runs unit tests, sibling modules are already loaded (warm). The cycle only triggers from a fresh process. Integration tests that spawn `chunkhound index` via subprocess hit the cold path and surface the bug.
- Lesson: any test for a circular-import fix must run under `subprocess` (integration tier) to exercise the cold-import case.

---

## Conventions

### Test organization
- All unit tests live flat in `tests/unit/` — no subdirectories.
- Every test module declares `pytestmark = pytest.mark.unit` (or the relevant marker) at module top.
- pytest-asyncio mode is `"auto"` (`pyproject.toml:203`) — write `async def test_...`, do **not** add `@pytest.mark.asyncio` decorators.

### Test tiers contract (`CLAUDE.md` has the detail)
- `unit`: no subprocess, no I/O beyond `tmp_path`, no network
- `integration`: loopback OK, outbound network blocked by autouse fixture (Python-level only — subprocess escapes the guard)
- `acceptance`: VCR cassette playback
- `e2e`: live APIs, opt-in

### Task hint (`ch-agj` convention, 2026-04-25)
- `EmbeddingTask = Literal["passage", "query"] | None` alias at `chunkhound/interfaces/embedding_provider.py`
- `validate_task()` at same module — **call it at the deepest internal method of every provider** (not at each public method). Symmetric fail-fast across Voyage/OpenAI/TEI even though OpenAI doesn't consume the value.
- `task` parameter is **always appended at the end of signatures**. Never inserted mid-signature — positional calls would shift.

### Provider file hygiene
- Pre-existing mypy errors on `voyageai_provider.py` and `openai_provider.py` are tracked by `ch-6ea`. **Don't fix them mid-task** (past sessions ballooned scope this way). Scope discipline: leave untouched unless INTRODUCED by the current change.

---

## Tooling Quirks

### mypy narrowing through tuple `in` checks
- `if x not in (None, "a", "b"): raise` does NOT narrow `x` for mypy.
- Use `cast(TargetType, x)` after the guard to document the invariant.

### Lambda closure testing
- `inspect.getclosurevars(fn).nonlocals` → dict of captured variables by name.
- Use this to assert a lambda closes over a specific kwarg:
  ```python
  assert closure_vars.nonlocals.get("task") == "passage"
  ```
- Combined with `fn.__name__ == "<lambda>"`, catches regressions where someone reverts to a bare method reference that drops kwargs.

### `handle_token_limit_error` signature coupling
- At `chunkhound/providers/embeddings/batch_utils.py:13-84`.
- Takes `embed_function: Callable[[list[str]], Awaitable[list[list[float]]]]` — **single positional arg only**.
- Threading extra kwargs requires `lambda batch: self._method(batch, task=task)`. If the helper ever starts passing more args, the lambda must update too. There's an inline comment at the call site in `openai_provider.py` noting this coupling.

### `hatch-vcs` dynamic versioning
- Version derived from git tags. **Never manually edit version strings.** Use `uv run scripts/update_version.py X.Y.Z`.

### DB path footguns (detail in CLAUDE.md)
- `--db` wants the **directory** (e.g., `--db .chunkhound/db`), not the file path. Passing the full `chunks.db` path silently creates nested `chunks.db/chunks.db`.
- Wrong `--db` subpath returns 0 results with no error.

---

## Known Tech Debt (address after embeddings pilot)

- **Oversized provider files**. Both are well over CLAUDE.md's 500-line threshold and each gained ~50 lines from `ch-agj`:
  - `chunkhound/providers/embeddings/openai_provider.py` (~1476 lines) — candidate extractions: rerank HTTP handler, Qwen model config, batch sizing helpers
  - `chunkhound/providers/embeddings/voyageai_provider.py` (~755 lines) — candidate extractions: rerank HTTP handler, retry classification logic
- **`ch-6ea` mypy sweep**: 851 typing errors across 92 files, including pre-existing issues on the provider files above.
- **`ch-eun` integration test reclassification**: tests mismarked as unit that actually use subprocess/network.
- **Pre-commit hook generation bug**: `.git/hooks/trauma-guard-precommit.py` (installed by some CASS memory / `cm` tooling) uses Rust-style `\u{1f525}` unicode escape which is invalid Python. Fixed locally; the upstream generator should switch to `\U0001F525`.

---

## Useful Command Pointers

- `bn` (bones) commands in CLAUDE.md's REQUIRED_SKILLS section; status + task history live in `.bones/`, not this file.
- `uv run pytest -m "unit or integration"` is the pre-commit gate.
- `uv run mypy <file>` is the only sanctioned way to check typing — incremental daemon sometimes lies about cached state.
