"""Regression tests for ch-qw4: EmbeddingService.process_batch deadlock.

The bug: ``EmbeddingService._generate_embeddings_in_batches`` creates
``asyncio.Semaphore(self._max_concurrent_batches)`` and the inner
``process_batch`` recursively awaits two more ``process_batch`` calls on
token-limit error — all while still inside the ``async with semaphore``
block. ``asyncio.Semaphore`` is non-reentrant, so the recursive calls
block waiting for a permit the parent owns.

These tests exercise two angles:
  - **Test 1**: canonical ``max=1`` deadlock — single batch, first call
    forced to token-limit, splits succeed. With max=1, the single permit
    is the only resource and the recursive call deadlocks without any
    concurrent fan-out needed.
  - **Test 2**: gather-concurrency angle — N initial batches with
    ``max=N`` permits, every initial batch hits token-limit, splits
    succeed. Without the fix all permits are held by parents and split
    children wait forever regardless of how large N is. The mock
    explicitly yields to the event loop before raising so all N parents
    can acquire their permits before any of them attempts to recurse —
    otherwise asyncio runs them sequentially and the deadlock condition
    never forms.

Both tests use ``@pytest.mark.timeout(10)`` so the deadlock surfaces in
seconds, not in pytest-timeout's 300s default.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


def _make_provider_mock(*, dims: int = 4, num_vectors: int = 1) -> MagicMock:
    """Mock provider with both embed methods plus protocol-required helpers
    used by ``EmbeddingService._create_token_aware_batches``.
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
# Test 1: canonical max=1 deadlock
# ---------------------------------------------------------------------------


class TestMaxOnePermitWithTokenLimit:
    """With ``max_concurrent_batches=1`` and a forced token-limit on the
    first attempt, the parent holds the only permit and the recursive
    split halves wait forever for it (without the fix).
    """

    @pytest.mark.timeout(10)
    async def test_max_one_permit_with_token_limit_completes(self) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()
        call_count = {"n": 0}

        async def embed_side_effect(texts, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise Exception("max allowed tokens for this request")
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(return_value=1)

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        # Act — 2 chunks → 1st call raises → split into 2 calls (1 chunk
        # each). With the fix: 3 embed calls total (1 fail + 2 success).
        # Without the fix: only 1 embed call ever happens; gather hangs
        # waiting on the recursive permits → pytest-timeout fires at 10s.
        await service._generate_embeddings_in_batches(
            [(1, "aaa"), (2, "bbb")], show_progress=False
        )

        # Assert — split recursion actually executed
        assert provider.embed.call_count >= 3, (
            f"Expected token-limit retry to recurse and call embed at "
            f"least 3 times (1 fail + 2 split halves); got "
            f"{provider.embed.call_count} calls"
        )


# ---------------------------------------------------------------------------
# Test 2: gather-concurrency angle — N initial batches with max=N permits
# ---------------------------------------------------------------------------


class TestNInitialBatchesWithNPermits:
    """With ``max_concurrent_batches=N`` and N initial batches all hitting
    token-limit at depth 0, ``gather`` schedules all N concurrently and
    every parent holds a permit; recursive split halves then wait
    forever for any of those permits — total deadlock regardless of how
    large N is. Defends against the illusion that a "safe" static
    ``max_concurrent_batches`` exists; the bug is hold-and-wait, not
    sizing.
    """

    @pytest.mark.timeout(10)
    async def test_n_initial_batches_with_n_permits_completes(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        N = 4
        INITIAL_SIZE = 4  # chunks per initial batch

        provider = _make_provider_mock()

        async def embed_side_effect(texts, **kwargs):
            # Stateless discrimination per SRE Clarification 3:
            # initial-sized calls raise, split halves (smaller) succeed.
            if len(texts) == INITIAL_SIZE:
                # Yield to the event loop BEFORE raising so all N
                # parents can acquire their permits concurrently.
                # Without this yield, asyncio runs each parent's full
                # recursion to completion before scheduling the next —
                # permits are released between parents and the
                # deadlock condition never forms.
                await asyncio.sleep(0)
                raise Exception("max allowed tokens for this request")
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(
            side_effect=lambda data, batch_size: len(data)
        )

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=N,
        )

        # Pre-formed batches: N batches of INITIAL_SIZE chunks each.
        chunks_per_batch = [
            [
                (b * INITIAL_SIZE + i, f"chunk{b}-{i}")
                for i in range(INITIAL_SIZE)
            ]
            for b in range(N)
        ]
        all_chunks = [c for batch in chunks_per_batch for c in batch]

        monkeypatch.setattr(
            service,
            "_create_token_aware_batches",
            lambda chunk_data: chunks_per_batch,
        )

        # Act — N initial batches all token-limit at depth 0; all permits
        # held by parents. Without the fix: gather hangs → 10s timeout.
        # With the fix: parents release permits before recursing, split
        # halves run under available permits.
        total_generated = await service._generate_embeddings_in_batches(
            all_chunks, show_progress=False
        )

        # Assert per D1 (catalog) — guard against silent green from a
        # broken mock that short-circuits before recursion.
        assert provider.embed.call_count > N, (
            f"Expected split halves beyond initial N={N} calls; got "
            f"{provider.embed.call_count} calls — split recursion did "
            f"not execute"
        )
        assert total_generated > 0, (
            f"Expected split halves to succeed and store embeddings; "
            f"got total_generated={total_generated}"
        )


# ---------------------------------------------------------------------------
# Adversarial battery — stress-test the seam refactor under structural
# patterns: singular, multi-level recursion, second run.
# ---------------------------------------------------------------------------


class TestSingularBatchTokenLimitSwallows:
    """[Singular] 1-chunk batch hitting token-limit must NOT split (guard
    ``len(batch) > 1``). The swallow path runs and ``process_batch`` returns
    0. Verifies the seam handles the no-split outcome correctly under
    ``max=1`` (no deadlock concern, but adversarial coverage of the
    sentinel-not-returned branch).
    """

    @pytest.mark.timeout(10)
    async def test_singular_batch_token_limit_swallows_under_max_one(
        self,
    ) -> None:
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()

        async def embed_side_effect(texts, **kwargs):
            raise Exception("max allowed tokens for this request")

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(return_value=1)

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        # Single chunk → cannot split (guard) → swallow path → return 0
        total = await service._generate_embeddings_in_batches(
            [(1, "single")], show_progress=False
        )

        assert total == 0, f"Expected swallow path to return 0; got {total}"
        assert provider.embed.call_count == 1, (
            f"Expected exactly 1 embed call (no split for 1-chunk batch); "
            f"got {provider.embed.call_count}"
        )
        db.insert_embeddings_batch.assert_not_called()


class TestMultiLevelSplitRecursion:
    """[Dense + State transitions] Multi-level token-limit recursion: an
    8-chunk batch fails initial → splits to 4+4 → each 4-chunk also fails
    → splits to 2+2 → each 2-chunk succeeds. Tests that the fix scales to
    deeper recursion without the lock being held by an ancestor when a
    descendant tries to acquire it.
    """

    @pytest.mark.timeout(10)
    async def test_multi_level_recursion_completes_under_max_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()

        async def embed_side_effect(texts, **kwargs):
            # Sizes 4 and 8 fail; size 2 (depth-2 splits) succeeds.
            if len(texts) >= 4:
                raise Exception("max allowed tokens for this request")
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(side_effect=lambda data, _: len(data))

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        chunks = [(i, f"chunk{i}") for i in range(8)]
        # Force a single 8-chunk initial batch (default token-aware batching
        # would split this differently).
        monkeypatch.setattr(
            service,
            "_create_token_aware_batches",
            lambda chunk_data: [chunks],
        )

        # depth 0: 1 call (size 8) → fail → split 4+4
        # depth 1: 2 calls (size 4) → both fail → split 2+2 each
        # depth 2: 4 calls (size 2) → all succeed (4 DB inserts of 2 each)
        # Total: 7 embed calls, 8 chunks stored
        total = await service._generate_embeddings_in_batches(
            chunks, show_progress=False
        )

        assert total == 8, (
            f"Expected 8 chunks stored after 2-level recursion; got {total}"
        )
        assert provider.embed.call_count == 7, (
            f"Expected 7 embed calls (1 + 2 + 4 across 3 depths); "
            f"got {provider.embed.call_count}"
        )


class TestSecondRunWithSameService:
    """[Second run] Calling ``_generate_embeddings_in_batches`` twice on
    the same service instance with token-limit recursion in each run must
    leave no stuck state — the semaphore must be reusable, the metrics
    collector (when present) must accumulate cleanly, and the second run's
    recursion must not deadlock from any leaked permit from run 1.
    """

    @pytest.mark.timeout(10)
    async def test_second_run_with_same_service_completes(self) -> None:
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock()

        async def embed_side_effect(texts, **kwargs):
            # Initial batch (size 2) fails; split halves (size 1) succeed.
            if len(texts) == 2:
                raise Exception("max allowed tokens for this request")
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        provider.embed = AsyncMock(side_effect=embed_side_effect)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(side_effect=lambda data, _: len(data))

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
        )

        chunks = [(1, "aaa"), (2, "bbb")]

        # Run 1: 3 embed calls (1 fail + 2 split halves succeed)
        total1 = await service._generate_embeddings_in_batches(
            chunks, show_progress=False
        )
        assert total1 == 2, f"Run 1: expected 2 stored; got {total1}"

        # Run 2: same path on same service — must not deadlock and must
        # produce identical outcome
        total2 = await service._generate_embeddings_in_batches(
            chunks, show_progress=False
        )
        assert total2 == 2, f"Run 2: expected 2 stored; got {total2}"

        # Each run: 1 fail + 2 success = 3 calls. Two runs = 6 calls.
        assert provider.embed.call_count == 6, (
            f"Expected 6 embed calls across 2 runs (3 per run); "
            f"got {provider.embed.call_count}"
        )
