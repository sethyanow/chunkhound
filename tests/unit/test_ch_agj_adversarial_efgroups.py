"""Adversarial stress tests for ch-agj Groups E+F (Checkpoint 3).

Probes assumptions baked into:
  - 8 production call sites (6 passage + 2 query) — mechanical kwarg additions
  - tests/unit/test_embedding_call_sites_passage.py (6 tests)
  - tests/unit/test_embedding_call_sites_query.py (2 tests)
  - tests/unit/test_embedding_cache_identity.py (3 tests)

Each test is its own RED-GREEN cycle. GREEN results get the Three-Question
Framework via inline comments. Findings outside Group E+F scope get logged
on the epic via ``bn log``.

Pattern legend (subset relevant to Groups E+F):
  EM = Empty, RD = Redundant, EB = Encoding boundaries (cross-tier),
  ST = State transitions, 2R = Second-run.
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers (shaped to match the Cycle A/B/C test files for consistency)
# ---------------------------------------------------------------------------


def _make_provider_mock(*, dims: int = 4, num_vectors: int = 1) -> MagicMock:
    """Mock provider with both embed methods present + protocol-required
    helpers used by EmbeddingService._create_token_aware_batches.
    """
    provider = MagicMock()
    fake_vector = [0.1] * dims
    provider.embed = AsyncMock(return_value=[fake_vector for _ in range(num_vectors)])
    provider.embed_batch = AsyncMock(
        return_value=[fake_vector for _ in range(num_vectors)]
    )
    provider.name = "voyageai"
    provider.model = "voyage-4"
    provider.get_recommended_concurrency = MagicMock(return_value=1)
    provider.get_max_tokens_per_batch = MagicMock(return_value=8192)
    provider.get_max_documents_per_batch = MagicMock(return_value=100)
    return provider


# ---------------------------------------------------------------------------
# E1. _filter_existing_embeddings — empty chunk_ids reaches DB with locked kwargs
# ---------------------------------------------------------------------------


class TestFilterExistingEmbeddingsEmptyInput:
    """[EM] Production has NO short-circuit for empty chunk_ids — the only
    early-return at ``_filter_existing_embeddings`` (line 397) is when
    ``self._embedding_provider`` is None. So an empty input STILL produces
    a DB cache call.

    Adversarial: even at the empty boundary, the locked kwargs set
    ``{chunk_ids, provider, model}`` MUST hold. A future change that
    threads task or content-hash only on the "non-empty" path would slip
    past Cycle C's regression test (which uses 3 chunks).
    """

    async def test_empty_input_calls_db_with_locked_kwargs(self) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()
        db = MagicMock()
        db.get_existing_embeddings = MagicMock(return_value=set())

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        # Act
        result = await service._filter_existing_embeddings([], [])

        # Assert
        assert result == []
        db.get_existing_embeddings.assert_called_once()
        kwargs = db.get_existing_embeddings.call_args.kwargs
        assert set(kwargs.keys()) == {"chunk_ids", "provider", "model"}, (
            f"Cache fingerprint must hold even at empty boundary; "
            f"got kwargs keys: {set(kwargs.keys())}"
        )
        assert kwargs["chunk_ids"] == []
        # Three-Question Framework on this GREEN:
        #   Q1: production trusts caller to handle empty input upstream;
        #       _filter_existing_embeddings makes the wasteful DB call.
        #       Locked kwargs are stable at the boundary.
        #   Q2: indexing_coordinator._generate_embeddings (line 1543+) DOES
        #       short-circuit on empty `valid_chunk_data` (line 1587) BEFORE
        #       reaching the embed call — different layering choice.
        #   Q3: empty list crosses from indexing_coordinator (early-exit) →
        #       embedding_service (no early-exit) — inconsistent layering
        #       but not a correctness issue. Logged on epic for cleanup.


# ---------------------------------------------------------------------------
# E2. ClusteringService.cluster_files — empty files dict short-circuits
# ---------------------------------------------------------------------------


class TestClusteringServiceEmptyFilesShortCircuit:
    """[EM] cluster_files raises ValueError BEFORE any embed call when
    ``files={}``. Verifies the empty-input boundary at a different
    layer — production short-circuits in this method but not in
    _filter_existing_embeddings (E1). Layering inconsistency, but the
    contract here is "no embed call on empty input."
    """

    async def test_empty_files_raises_before_embed(self) -> None:
        # Arrange
        from chunkhound.services.clustering_service import ClusteringService

        provider = _make_provider_mock()
        llm_provider = MagicMock()

        service = ClusteringService(provider, llm_provider)

        # Act / Assert
        with pytest.raises(ValueError, match="empty"):
            await service.cluster_files({}, n_clusters=2)

        # Adversarial guard — NO embed call happens
        assert provider.embed_batch.call_count == 0, (
            "Empty files dict must raise BEFORE any embed call"
        )
        assert provider.embed.call_count == 0
        # Three-Question Framework on this GREEN:
        #   Q1: cluster_files validates input, _filter_existing_embeddings
        #       does not — different services have different empty-input
        #       contracts.
        #   Q2: cluster_files_hdbscan (line 167) and cluster_files_hdbscan_bounded
        #       (line 358) also raise on empty — consistent within this service.
        #   Q3: empty files dict crosses from caller into ClusteringService —
        #       service is the boundary that validates. Good design.


# ---------------------------------------------------------------------------
# E3. _filter_existing_embeddings — duplicate chunk_ids pass through
# ---------------------------------------------------------------------------


class TestFilterExistingEmbeddingsRedundantInput:
    """[RD] Duplicate chunk_ids in input — does production deduplicate
    before the DB call? Adversarial: production passes ``chunk_ids``
    verbatim to DB; duplicates flow through unchanged. Locking this
    contract: dedupe is the DB's responsibility, not the cache-filter
    layer's.
    """

    async def test_duplicate_chunk_ids_pass_through_to_db(self) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()
        db = MagicMock()
        db.get_existing_embeddings = MagicMock(return_value=set())

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        # Act — chunk_ids has duplicates
        await service._filter_existing_embeddings(
            chunk_ids=[1, 1, 2, 2, 3], chunk_texts=["a", "b", "c", "d", "e"]
        )

        # Assert — duplicates flow through verbatim
        kwargs = db.get_existing_embeddings.call_args.kwargs
        assert kwargs["chunk_ids"] == [1, 1, 2, 2, 3], (
            f"Production passes chunk_ids verbatim; if it dedupes, the "
            f"contract has changed and the DB layer's dedupe assumption "
            f"breaks. Got: {kwargs['chunk_ids']}"
        )
        # Three-Question Framework on this GREEN:
        #   Q1: production trusts caller for unique chunk_ids — likely
        #       holds because callers build chunk_ids from list comprehension
        #       over zip in indexing_coordinator (line 1592) where uniqueness
        #       is a precondition.
        #   Q2: _generate_embeddings_in_batches.process_batch passes
        #       chunk_ids forward verbatim too (line 478-479) — same
        #       trust contract.
        #   Q3: chunk_ids cross from chunk-cache-service / indexing
        #       coordinator into embedding-service — uniqueness invariant
        #       is enforced upstream by ChunkCacheService (logged on epic
        #       for verification).


# ---------------------------------------------------------------------------
# E4. FakeEmbeddingProvider — unit-tier signature probe
# ---------------------------------------------------------------------------


class TestFakeProviderEmbedSignatureAcceptsTask:
    """[EB][cross-tier] Unit-tier probe that catches the 31-test integration
    failure at unit time. After Cycle B added ``task="query"`` to
    ``single_hop_strategy.py:66``, ``FakeEmbeddingProvider.embed`` had to
    accept the kwarg too — otherwise integration tests using the fixture
    blow up with TypeError.

    Lesson the catalog flagged: protocol-shape changes need a unit-tier
    shape test that imports every fake/stub fixture and asserts its
    signature aligns with the protocol. Without this, the failure
    surfaces only at integration tier (slow feedback).
    """

    def test_fake_embed_signature_has_task_kwarg(self) -> None:
        from tests.fixtures.fake_providers import FakeEmbeddingProvider

        sig = inspect.signature(FakeEmbeddingProvider.embed)
        assert "task" in sig.parameters, (
            "FakeEmbeddingProvider.embed must accept 'task' kwarg per ch-agj "
            "protocol; missing it caused 31 integration test failures"
        )
        assert sig.parameters["task"].default is None, (
            "FakeEmbeddingProvider.embed task kwarg must default to None"
        )

    def test_fake_embed_batch_signature_has_task_kwarg(self) -> None:
        from tests.fixtures.fake_providers import FakeEmbeddingProvider

        sig = inspect.signature(FakeEmbeddingProvider.embed_batch)
        assert "task" in sig.parameters
        assert sig.parameters["task"].default is None

    def test_validating_embed_signature_has_task_kwarg(self) -> None:
        """ValidatingEmbeddingProvider overrides embed (line 584) — the
        override must also accept task."""
        from tests.fixtures.fake_providers import ValidatingEmbeddingProvider

        sig = inspect.signature(ValidatingEmbeddingProvider.embed)
        assert "task" in sig.parameters
        assert sig.parameters["task"].default is None

    async def test_fake_embed_accepts_task_kwarg_at_runtime(self) -> None:
        """Beyond signature inspection — actually invoke with task kwarg."""
        from tests.fixtures.fake_providers import FakeEmbeddingProvider

        fake = FakeEmbeddingProvider()
        # Should not raise TypeError
        result = await fake.embed(["hello"], task="passage")
        assert isinstance(result, list)
        assert len(result) == 1
        # And again without task — backward compat
        result2 = await fake.embed(["hello"])
        assert result2 == result, (
            "task kwarg is accepted but does not vary the deterministic "
            "vector — fake provider's embedding is content-only"
        )
        # Three-Question Framework on this GREEN:
        #   Q1: shape-compat at fixture level holds; the failure mode is
        #       caught at unit tier going forward.
        #   Q2: ConstantEmbeddingProvider inherits from FakeEmbeddingProvider
        #       — picks up the new signatures for free. Other test fixtures
        #       (if any) implementing the EmbeddingProvider protocol need
        #       similar audit. Logged for ch-eun follow-up.
        #   Q3: shape changes cross from production protocol → fixture
        #       implementations. There's no compile-time check that fixtures
        #       conform — this probe is the runtime check.


# ---------------------------------------------------------------------------
# E5. EmbeddingService token-limit recursion preserves task="passage"
# ---------------------------------------------------------------------------


class TestRecursiveTokenLimitSplitPreservesTaskPassage:
    """[ST] EmbeddingService._generate_embeddings_in_batches.process_batch
    recurses on token-limit error at line 540-542 — splits the batch in
    half and re-invokes process_batch. The recursive call hits line 487
    again, which now passes ``task="passage"`` as a literal.

    Adversarial: verify the literal is preserved across recursion. Not
    the same as Group C's openai_provider.py recursion (which threads
    via lambda closure inside _embed_batch_internal); this is the
    OUTER recursion in EmbeddingService.process_batch.

    **Pre-existing deadlock note (OUT OF SCOPE for ch-agj)**: production
    uses ``asyncio.Semaphore(max_concurrent_batches)`` non-reentrantly,
    and ``process_batch`` recurses INSIDE the ``async with semaphore``
    block. With ``max_concurrent_batches=1``, the recursive call waits
    forever for the permit the outer call holds — deadlock. This test
    uses ``max_concurrent_batches=2`` (room for one outer + one
    recursive permit) to avoid the deadlock; the bone-detected issue is
    logged on the ch-agj epic for follow-up. The 10s pytest timeout
    here surfaces any future regression of this pattern in seconds, not
    in the default 300s pytest-timeout.
    """

    @pytest.mark.timeout(10)
    async def test_token_limit_split_preserves_task_passage_on_each_call(
        self,
    ) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock(num_vectors=1)
        # First call raises token-limit; subsequent split halves succeed.
        call_count = {"n": 0}

        async def embed_side_effect(texts, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("max allowed tokens for this request")
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(return_value=1)

        # max_concurrent_batches=2 sidesteps the pre-existing semaphore
        # deadlock — see class docstring.
        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=2,
        )

        # Act — 2 chunks → 1st call raises → split into 2 calls (each 1 chunk)
        # Total: 3 embed calls; first fails, last 2 succeed.
        await service._generate_embeddings_in_batches(
            [(1, "aaa"), (2, "bbb")], show_progress=False
        )

        # Assert — at least the recursion happened (>=2 calls)
        assert provider.embed.call_count >= 2, (
            f"Expected token-limit retry to recurse and call embed >= 2 times; "
            f"got {provider.embed.call_count} calls"
        )
        # And every call (failing AND successful split halves) carried task='passage'
        for i, call in enumerate(provider.embed.call_args_list):
            assert call.kwargs.get("task") == "passage", (
                f"Recursive token-limit split call #{i + 1} dropped task; "
                f"got kwargs={call.kwargs}"
            )
        # Three-Question Framework on this GREEN:
        #   Q1: task='passage' is a literal at line 487, not a parameter —
        #       it's preserved across recursion automatically because each
        #       recursion lands on the SAME source line.
        #   Q2: similar concern in OpenAIEmbeddingProvider._embed_batch_internal
        #       — already covered by Group C's lambda-closure test.
        #   Q3: recursion crosses the function-call boundary; data flowing
        #       across (the texts batch) is split in half each time. Kwarg
        #       is hardcoded so it doesn't vary. If a future refactor
        #       makes task a parameter of process_batch, lambda-closure
        #       discipline would be needed (mirror Group C's pattern).


# ---------------------------------------------------------------------------
# E6. Second-run idempotency at the call-site level
# ---------------------------------------------------------------------------


class TestClusteringServiceSecondRunThreadsTaskEachTime:
    """[2R] Calling cluster_files twice with the same input — does each
    call thread ``task="passage"`` independently? Defends against
    future caching that could memoize the first call's kwargs and skip
    them on the second.
    """

    async def test_two_runs_each_threads_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.clustering_service import ClusteringService

        provider = _make_provider_mock(num_vectors=2)
        provider.embed_batch = AsyncMock(
            return_value=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]]
        )

        llm_provider = MagicMock()
        llm_provider.estimate_tokens = MagicMock(return_value=100)

        service = ClusteringService(provider, llm_provider)
        files = {"a.py": "alpha", "b.py": "beta"}

        # Act — two runs, same input
        await service.cluster_files(files, n_clusters=2)
        await service.cluster_files(files, n_clusters=2)

        # Assert — both runs hit embed_batch with task='passage'
        assert provider.embed_batch.call_count == 2, (
            f"Expected 2 embed_batch calls (one per run); got "
            f"{provider.embed_batch.call_count}"
        )
        for i, call in enumerate(provider.embed_batch.call_args_list):
            assert call.kwargs.get("task") == "passage", (
                f"Run {i + 1} did not thread task='passage'; got kwargs={call.kwargs}"
            )
        # Three-Question Framework on this GREEN:
        #   Q1: production has no per-call caching at this layer; each
        #       call re-runs the embedding pipeline.
        #   Q2: _filter_existing_embeddings DOES cache via DB lookup, but
        #       cache fingerprint excludes task — so even a cache-hit on
        #       the second call wouldn't skip the embed call differently
        #       based on task value.
        #   Q3: state crossing between runs — provider mock accumulates
        #       call_args_list (test artifact). No production state
        #       mutates between cluster_files calls; future refactor
        #       adding caching here should re-verify this property.
