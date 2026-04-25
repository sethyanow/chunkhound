"""Cycle A — passage call sites for ch-agj.

Verifies that production call sites in indexing/clustering paths thread
``task="passage"`` through to the embedding provider's mock at the EXACT
method production calls.

Per Group E+F failure catalog (Checkpoint 3 adversarial planning):
- Each test mocks the EXACT method production calls (``embed`` vs
  ``embed_batch``); production lines verified against source.
- Adversarial guard: assert the UNUSED method has ``call_count == 0`` —
  catches mock-at-wrong-method bugs that would otherwise produce silent
  false-positive GREEN.
- Test names embed file:line of the production line being driven so
  pytest output enumerates every site.
- Mocks are set up at the SAME level production accesses (direct
  ``self._embedding_provider`` vs ``self._embedding_manager.get_provider()``
  manager indirection).

Production sites driven by this file:
- ``embedding_service.py:487``           via ``.embed``
- ``indexing_coordinator.py:1596``       via ``.embed_batch``
- ``gap_detection.py:253``               via ``.embed_batch`` (manager indirection)
- ``clustering_service.py:97/199/396``   via ``.embed_batch``
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import numpy as np
import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_mock(
    *, dims: int = 4, num_vectors: int = 1
) -> MagicMock:
    """Build a mock embedding provider with BOTH ``embed`` and ``embed_batch``
    as AsyncMocks.

    Returning both methods lets each test apply the adversarial guard:
    "the UNUSED method must have ``call_count == 0``." That assertion
    catches the failure mode where a test mocks ``embed_batch`` while
    production actually calls ``embed`` (or vice versa) — without the guard
    the mock auto-creates the called attribute and the assertion vacuously
    passes.
    """
    provider = MagicMock()
    fake_vector = [0.1] * dims
    provider.embed = AsyncMock(return_value=[fake_vector for _ in range(num_vectors)])
    provider.embed_batch = AsyncMock(
        return_value=[fake_vector for _ in range(num_vectors)]
    )
    provider.name = "openai"
    provider.model = "text-embedding-3-small"
    provider.get_recommended_concurrency = MagicMock(return_value=1)
    # _create_token_aware_batches in EmbeddingService reads these
    provider.get_max_tokens_per_batch = MagicMock(return_value=8192)
    provider.get_max_documents_per_batch = MagicMock(return_value=100)
    return provider


# ===========================================================================
# Site 1: embedding_service.py:487  →  .embed(texts)
# ===========================================================================


class TestEmbeddingServiceAt487:
    """Drives ``EmbeddingService._generate_embeddings_in_batches`` whose inner
    closure calls ``self._embedding_provider.embed(texts)`` at line 487.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.embedding_service import EmbeddingService

        provider = _make_provider_mock(dims=4, num_vectors=1)
        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(return_value=1)

        service = EmbeddingService(
            database_provider=db,
            embedding_provider=provider,
            max_concurrent_batches=1,
            metrics_collector=None,
        )

        # Act
        await service._generate_embeddings_in_batches(
            [(1, "hello world")], show_progress=False
        )

        # Assert — production line 487 calls .embed (NOT .embed_batch)
        provider.embed.assert_called_once()
        assert provider.embed.call_args.kwargs.get("task") == "passage", (
            f"embedding_service.py:487 must thread task='passage'; "
            f"got args={provider.embed.call_args.args}, "
            f"kwargs={provider.embed.call_args.kwargs}"
        )
        # Adversarial guard — unused method must NOT be called
        assert provider.embed_batch.call_count == 0, (
            "embedding_service.py:487 calls .embed; if .embed_batch was "
            "called, this test mocked the wrong method"
        )


# ===========================================================================
# Site 2: indexing_coordinator.py:1596  →  .embed_batch(texts)
# ===========================================================================


class TestIndexingCoordinatorAt1596:
    """Drives ``IndexingCoordinator._generate_embeddings`` whose body calls
    ``self._embedding_provider.embed_batch(texts)`` at line 1596.

    Construction note: ``IndexingCoordinator.__init__`` requires many heavy
    dependencies (parsers, chunk-cache, file utilities). For a unit test of
    one method, we use ``__new__`` to skip ``__init__`` and assign only the
    attributes the method touches. Method body verified to access only
    ``self._embedding_provider`` and ``self._db.insert_embeddings_batch``.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.indexing_coordinator import IndexingCoordinator

        provider = _make_provider_mock(dims=4, num_vectors=1)

        db = MagicMock()
        db.insert_embeddings_batch = MagicMock(return_value=1)

        coordinator = IndexingCoordinator.__new__(IndexingCoordinator)
        coordinator._embedding_provider = provider
        coordinator._db = db

        # Realistic chunk shape — _generate_embeddings reads "code" via
        # normalize_content + format_chunk_for_embedding.
        chunks: list[dict[str, Any]] = [
            {"code": "def hello():\n    return 1", "file_path": "x.py", "language": "python"}
        ]
        chunk_ids = [42]

        # Act
        await coordinator._generate_embeddings(chunk_ids, chunks)

        # Assert — production line 1596 calls .embed_batch (NOT .embed)
        provider.embed_batch.assert_called_once()
        assert provider.embed_batch.call_args.kwargs.get("task") == "passage", (
            f"indexing_coordinator.py:1596 must thread task='passage'; "
            f"got args={provider.embed_batch.call_args.args}, "
            f"kwargs={provider.embed_batch.call_args.kwargs}"
        )
        # Adversarial guard
        assert provider.embed.call_count == 0, (
            "indexing_coordinator.py:1596 calls .embed_batch; if .embed was "
            "called, this test mocked the wrong method"
        )


# ===========================================================================
# Site 3: gap_detection.py:253  →  .embed_batch(chunk_contents)
# (Manager indirection: production accesses provider via
#  self._embedding_manager.get_provider().)
# ===========================================================================


class TestGapDetectionAt253:
    """Drives ``GapDetectionService._cluster_chunks_kmeans`` whose body binds
    ``embedding_provider = self._embedding_manager.get_provider()`` then
    calls ``embedding_provider.embed_batch(chunk_contents)`` at line 253.

    Mock at the manager level — production uses manager indirection here,
    NOT direct attribute access. This is the abstraction-level mismatch the
    failure catalog flagged.

    Token budget: production short-circuits on line 245 when
    ``total_tokens <= GAP_CLUSTER_TOKEN_BUDGET (50_000)``; mock
    ``estimate_tokens`` to return enough that total exceeds the budget so
    the call site is reached.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.research.shared.gap_detection import (
            GapDetectionService,
        )

        provider = _make_provider_mock(dims=4, num_vectors=2)

        embedding_manager = MagicMock()
        embedding_manager.get_provider = MagicMock(return_value=provider)

        llm_provider = MagicMock()
        # Returning 30_000 per chunk × 2 chunks = 60_000 > 50_000 budget,
        # forcing the embed_batch path.
        llm_provider.estimate_tokens = MagicMock(return_value=30_000)
        llm_manager = MagicMock()
        llm_manager.get_utility_provider = MagicMock(return_value=llm_provider)

        service = GapDetectionService.__new__(GapDetectionService)
        service._embedding_manager = embedding_manager
        service._llm_manager = llm_manager

        chunks = [
            {"code": "x" * 100, "file_path": "a.py"},
            {"code": "y" * 100, "file_path": "b.py"},
        ]

        # Act
        await service._cluster_chunks_kmeans(chunks)

        # Assert — production line 253 calls .embed_batch (NOT .embed)
        provider.embed_batch.assert_called_once()
        assert provider.embed_batch.call_args.kwargs.get("task") == "passage", (
            f"gap_detection.py:253 must thread task='passage'; "
            f"got args={provider.embed_batch.call_args.args}, "
            f"kwargs={provider.embed_batch.call_args.kwargs}"
        )
        # Adversarial guard
        assert provider.embed.call_count == 0, (
            "gap_detection.py:253 calls .embed_batch; if .embed was called, "
            "this test mocked the wrong method"
        )


# ===========================================================================
# Site 4: clustering_service.py:97  →  .embed_batch(file_contents)  [k-means]
# ===========================================================================


class TestClusteringServiceAt97:
    """Drives ``ClusteringService.cluster_files`` (public, k-means) whose body
    calls ``self._embedding_provider.embed_batch(file_contents)`` at
    line 97.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.clustering_service import ClusteringService

        provider = _make_provider_mock(dims=4, num_vectors=2)
        # Override embed_batch to return realistic shapes for k-means.
        provider.embed_batch = AsyncMock(return_value=[[0.1, 0.2, 0.3, 0.4], [0.5, 0.6, 0.7, 0.8]])

        llm_provider = MagicMock()
        # Make sure n_clusters > 1 path is taken: total > 0 and 2 files
        llm_provider.estimate_tokens = MagicMock(return_value=100)

        service = ClusteringService(
            embedding_provider=provider,
            llm_provider=llm_provider,
        )

        # 2 files + n_clusters=2 forces the line-97 embed_batch path
        files = {"a.py": "alpha", "b.py": "beta"}

        # Act
        await service.cluster_files(files, n_clusters=2)

        # Assert
        provider.embed_batch.assert_called_once()
        assert provider.embed_batch.call_args.kwargs.get("task") == "passage", (
            f"clustering_service.py:97 must thread task='passage'; "
            f"got args={provider.embed_batch.call_args.args}, "
            f"kwargs={provider.embed_batch.call_args.kwargs}"
        )
        assert provider.embed.call_count == 0, (
            "clustering_service.py:97 calls .embed_batch; if .embed was called, "
            "this test mocked the wrong method"
        )


# ===========================================================================
# Site 5: clustering_service.py:199  →  .embed_batch(file_contents)  [HDBSCAN]
# ===========================================================================


class TestClusteringServiceAt199:
    """Drives ``ClusteringService.cluster_files_hdbscan`` (public, HDBSCAN)
    whose body calls ``self._embedding_provider.embed_batch(file_contents)``
    at line 199.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.clustering_service import ClusteringService

        provider = _make_provider_mock(dims=4, num_vectors=3)
        # HDBSCAN needs more points than min_cluster_size (default 2) plus
        # signal — give 3 distinct vectors.
        provider.embed_batch = AsyncMock(
            return_value=[[0.1, 0.2, 0.3, 0.4], [0.9, 0.8, 0.7, 0.6], [0.5, 0.4, 0.3, 0.2]]
        )

        llm_provider = MagicMock()
        llm_provider.estimate_tokens = MagicMock(return_value=100)

        service = ClusteringService(
            embedding_provider=provider,
            llm_provider=llm_provider,
        )

        files = {"a.py": "alpha content", "b.py": "beta content", "c.py": "gamma content"}

        # Act
        await service.cluster_files_hdbscan(files, min_cluster_size=2)

        # Assert
        provider.embed_batch.assert_called_once()
        assert provider.embed_batch.call_args.kwargs.get("task") == "passage", (
            f"clustering_service.py:199 must thread task='passage'; "
            f"got args={provider.embed_batch.call_args.args}, "
            f"kwargs={provider.embed_batch.call_args.kwargs}"
        )
        assert provider.embed.call_count == 0, (
            "clustering_service.py:199 calls .embed_batch; if .embed was called, "
            "this test mocked the wrong method"
        )


# ===========================================================================
# Site 6: clustering_service.py:396  →  .embed_batch(file_contents)
# (cluster_files_hdbscan_bounded)
# ===========================================================================


class TestClusteringServiceAt396:
    """Drives ``ClusteringService.cluster_files_hdbscan_bounded`` (public)
    whose body calls ``self._embedding_provider.embed_batch(file_contents)``
    at line 396.
    """

    async def test_passes_task_passage(self) -> None:
        # Arrange
        from chunkhound.services.clustering_service import ClusteringService

        provider = _make_provider_mock(dims=4, num_vectors=3)
        provider.embed_batch = AsyncMock(
            return_value=[[0.1, 0.2, 0.3, 0.4], [0.9, 0.8, 0.7, 0.6], [0.5, 0.4, 0.3, 0.2]]
        )

        llm_provider = MagicMock()
        llm_provider.estimate_tokens = MagicMock(return_value=100)

        service = ClusteringService(
            embedding_provider=provider,
            llm_provider=llm_provider,
        )

        files = {"a.py": "alpha", "b.py": "beta", "c.py": "gamma"}

        # Act — give wide bounds so bounds enforcement is a no-op,
        # focus is solely on the embed_batch call site
        await service.cluster_files_hdbscan_bounded(
            files,
            min_cluster_size=2,
            min_tokens_per_cluster=1,
            max_tokens_per_cluster=10_000_000,
        )

        # Assert
        provider.embed_batch.assert_called_once()
        assert provider.embed_batch.call_args.kwargs.get("task") == "passage", (
            f"clustering_service.py:396 must thread task='passage'; "
            f"got args={provider.embed_batch.call_args.args}, "
            f"kwargs={provider.embed_batch.call_args.kwargs}"
        )
        assert provider.embed.call_count == 0, (
            "clustering_service.py:396 calls .embed_batch; if .embed was called, "
            "this test mocked the wrong method"
        )
