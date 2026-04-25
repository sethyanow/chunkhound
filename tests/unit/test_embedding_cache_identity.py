"""Cycle C — cache identity regression test for ch-agj.

Locks the contract that adding ``task`` (or any other field) to the
embedding cache fingerprint would invalidate every existing user's
voyage embeddings. The cache fingerprint at
``chunkhound/services/embedding_service.py:413-417`` is
``(chunk_id, provider, model)`` and MUST stay that way.

Per Group E+F failure catalog (Checkpoint 3 adversarial planning):
- Assertion is EXHAUSTIVE key-set equality
  (``set(call_args.kwargs.keys()) == {"chunk_ids", "provider", "model"}``),
  NOT membership (``"task" not in kwargs``). Membership lets unrelated
  kwarg additions slip through; key-set equality catches any future drift.
- Two-test layered defense:
  - **Unit-level** (test 1): direct assertion on
    ``db.get_existing_embeddings`` kwargs.
  - **End-to-end** (test 2): drives ``generate_embeddings_for_chunks``
    with a DB mock that reports every chunk as already-existing; asserts
    NO embed call is made. Catches a future wrapper-around-cache that
    swallows ``task`` upstream of the DB call (which the unit test
    alone would not detect).

This test is a regression-locker. It SHOULD pass on first run since it
verifies current behavior. If it fails, production has accidentally
added something to the cache fingerprint and is about to invalidate
every user's existing embeddings.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chunkhound.services.embedding_service import EmbeddingService

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service_for_filter(
    *, existing_chunk_ids: set[int] | None = None
) -> tuple[EmbeddingService, MagicMock, MagicMock]:
    """Build an ``EmbeddingService`` with mocked db + voyage-shaped provider.

    Returns the service plus the inner mocks for assertion access.

    DB mock returns a ``set[int]`` matching the production return type
    contract documented at ``embedding_service.py:420`` (``existing_chunk_ids =
    set()`` fallback). A ``list[int]`` would also work for ``chunk_id not
    in`` checks today, but if production ever switches to
    set-difference, a list mock would mask the change.
    """
    if existing_chunk_ids is None:
        existing_chunk_ids = set()

    db = MagicMock()
    db.get_existing_embeddings = MagicMock(return_value=existing_chunk_ids)
    db.insert_embeddings_batch = MagicMock(return_value=0)

    provider = MagicMock()
    provider.embed = AsyncMock(return_value=[])
    provider.embed_batch = AsyncMock(return_value=[])
    provider.name = "voyageai"
    provider.model = "voyage-4"
    provider.get_recommended_concurrency = MagicMock(return_value=1)
    provider.get_max_tokens_per_batch = MagicMock(return_value=8192)
    provider.get_max_documents_per_batch = MagicMock(return_value=100)

    service = EmbeddingService(
        database_provider=db,
        embedding_provider=provider,
        max_concurrent_batches=1,
        metrics_collector=None,
    )
    return service, db, provider


# ===========================================================================
# Test 1: unit-level — exhaustive kwargs equality on the cache call
# ===========================================================================


class TestFilterExistingEmbeddingsCallSignature:
    """Drives ``EmbeddingService._filter_existing_embeddings`` (line 385) and
    asserts the kwargs to ``db.get_existing_embeddings`` (line 413) are
    EXACTLY ``{"chunk_ids", "provider", "model"}`` — the cache fingerprint.

    Adding any new key (``task``, ``content_hash``, anything) would change
    the table key under the hood and invalidate user data.
    """

    async def test_kwargs_keys_are_exactly_chunk_ids_provider_model(self) -> None:
        # Arrange
        service, db, _provider = _make_service_for_filter()

        # Act
        await service._filter_existing_embeddings(
            chunk_ids=[1, 2, 3],
            chunk_texts=["a", "b", "c"],
        )

        # Assert — EXHAUSTIVE key-set equality, not membership
        db.get_existing_embeddings.assert_called_once()
        kwargs = db.get_existing_embeddings.call_args.kwargs
        assert set(kwargs.keys()) == {"chunk_ids", "provider", "model"}, (
            f"Cache fingerprint must be exactly (chunk_ids, provider, model). "
            f"Adding any field — including 'task', 'dims', 'content_hash' — "
            f"would invalidate every existing user's embeddings on next "
            f"reindex. Got kwargs keys: {set(kwargs.keys())}"
        )
        # Sanity: the right values flow through
        assert kwargs["chunk_ids"] == [1, 2, 3]
        assert kwargs["provider"] == "voyageai"
        assert kwargs["model"] == "voyage-4"

    async def test_second_run_kwargs_are_identical(self) -> None:
        """Idempotency lock: calling _filter_existing_embeddings twice with
        the same input produces identical cache-call kwargs. Defends
        against a future memoization or stateful caching that might cause
        the second call to pass different kwargs (e.g., a stale-data
        check that adds a timestamp).
        """
        # Arrange
        service, db, _provider = _make_service_for_filter()

        # Act
        await service._filter_existing_embeddings(
            chunk_ids=[1, 2, 3],
            chunk_texts=["a", "b", "c"],
        )
        first_kwargs = dict(db.get_existing_embeddings.call_args.kwargs)

        await service._filter_existing_embeddings(
            chunk_ids=[1, 2, 3],
            chunk_texts=["a", "b", "c"],
        )
        second_kwargs = dict(db.get_existing_embeddings.call_args.kwargs)

        # Assert
        assert db.get_existing_embeddings.call_count == 2, (
            "Second call must reach the DB (no internal memoization)"
        )
        assert first_kwargs == second_kwargs, (
            f"Second-run kwargs must equal first-run kwargs. "
            f"first={first_kwargs}; second={second_kwargs}"
        )


# ===========================================================================
# Test 2: end-to-end — existing voyage embeddings skipped on reindex
# ===========================================================================


class TestExistingVoyageEmbeddingsSkippedOnReindex:
    """End-to-end: drives the public
    ``EmbeddingService.generate_embeddings_for_chunks`` flow with a DB mock
    that reports every chunk as already-having-an-embedding for the
    current ``(provider="voyageai", model="voyage-4")`` key. Asserts that
    NO embed API call is made — chunks are filtered out, existing voyage
    embeddings stay dormant, no regeneration burns user credits.

    This is the user-visible regression contract. The unit test above
    locks the cache-call signature, but a future refactor that wraps the
    cache call in a helper ``_lookup_cache(provider, model, task)`` —
    where the helper SWALLOWS ``task`` upstream of the DB call — would
    pass the unit test (DB still sees only 3 kwargs) yet break the
    user contract (helper used ``task`` to invalidate the wrong rows).
    This end-to-end assertion catches that.

    Naming note: the test's "with_task_passage" framing is semantic —
    after Cycle A, the production line at ``embedding_service.py:487``
    threads ``task="passage"`` when it calls ``provider.embed(...)``. The
    contract being tested is that this task hint does NOT participate in
    cache lookup, so chunks already keyed under
    ``(voyageai, voyage-4, dims)`` stay valid regardless of new task
    plumbing.
    """

    async def test_embed_not_called_when_all_chunks_have_existing_embeddings(
        self,
    ) -> None:
        # Arrange
        chunk_ids = [101, 102, 103]
        existing = {101, 102, 103}  # DB says all of them already embedded
        service, _db, provider = _make_service_for_filter(
            existing_chunk_ids=existing
        )

        # Act — public entry point, mirrors what indexing flow would call
        result = await service.generate_embeddings_for_chunks(
            chunk_ids=chunk_ids,
            chunk_texts=["aaa", "bbb", "ccc"],
            show_progress=False,
        )

        # Assert
        assert result == 0, (
            f"All chunks pre-existing; generate_embeddings_for_chunks should "
            f"return 0 (no new embeddings); got {result}"
        )
        assert provider.embed.call_count == 0, (
            f"Existing voyage embeddings present; provider.embed must NOT "
            f"be called — Cycle A threaded task='passage' here, but cache "
            f"hit must short-circuit before any API call. Got "
            f"call_count={provider.embed.call_count}"
        )
        assert provider.embed_batch.call_count == 0, (
            f"Same as above for embed_batch — no API call regardless of "
            f"which method the call site uses. Got "
            f"call_count={provider.embed_batch.call_count}"
        )
