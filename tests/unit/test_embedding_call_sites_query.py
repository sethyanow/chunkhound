"""Cycle B — query call sites for ch-agj.

Verifies that production call sites in search/gap-query paths thread
``task="query"`` through to the embedding provider's mock at the EXACT
method production calls.

Per Group E+F failure catalog (Checkpoint 3 adversarial planning):
- Each test mocks the EXACT method production calls (``embed`` —
  both query sites use ``.embed``, neither uses ``.embed_batch``).
- Adversarial guard: assert the UNUSED method (``embed_batch``) has
  ``call_count == 0`` — catches mock-at-wrong-method bugs.
- Test names embed file:line of the production line being driven.

**Mixed-task module guard (catalog Dependency Treachery)**: ``gap_detection.py``
contains BOTH a passage site (line 253, exercised by
``test_embedding_call_sites_passage.py::TestGapDetectionAt253``) AND a
query site (line 461, exercised here). A copy-paste error between the two
test files would assign the wrong literal at one of those lines and fail
at least one of the two tests. Splitting passage/query into separate
files makes that bug visible at file boundary.

Production sites driven by this file:
- ``single_hop_strategy.py:66`` via ``.embed``
- ``research/shared/gap_detection.py:461`` via ``.embed`` (manager indirection)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chunkhound.services.research.shared.gap_models import GapCandidate

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider_mock(*, dims: int = 4, num_vectors: int = 1) -> MagicMock:
    """Build a mock embedding provider with BOTH ``embed`` and ``embed_batch``
    as AsyncMocks, so each test can apply the adversarial guard
    "the UNUSED method must have ``call_count == 0``."
    """
    provider = MagicMock()
    fake_vector = [0.1] * dims
    provider.embed = AsyncMock(return_value=[fake_vector for _ in range(num_vectors)])
    provider.embed_batch = AsyncMock(
        return_value=[fake_vector for _ in range(num_vectors)]
    )
    provider.name = "openai"
    provider.model = "text-embedding-3-small"
    return provider


# ===========================================================================
# Site 1: single_hop_strategy.py:66  →  .embed([query])
# ===========================================================================


class TestSingleHopStrategyAt66:
    """Drives ``SingleHopStrategy.search`` (public) whose body calls
    ``self._embedding_provider.embed([query])`` at line 66.
    """

    async def test_passes_task_query(self) -> None:
        # Arrange
        from chunkhound.services.search.single_hop_strategy import SingleHopStrategy

        provider = _make_provider_mock(dims=4, num_vectors=1)
        db = MagicMock()
        # search_semantic returns (results, pagination_metadata) per
        # DatabaseProvider.search_semantic interface
        db.search_semantic = MagicMock(return_value=([], {}))

        strategy = SingleHopStrategy(
            database_provider=db,
            embedding_provider=provider,
        )

        # Act
        await strategy.search(
            query="test query",
            page_size=10,
            offset=0,
            threshold=None,
            provider="openai",
            model="text-embedding-3-small",
            path_filter=None,
        )

        # Assert — production line 66 calls .embed (NOT .embed_batch)
        provider.embed.assert_called_once()
        assert provider.embed.call_args.kwargs.get("task") == "query", (
            f"single_hop_strategy.py:66 must thread task='query'; "
            f"got args={provider.embed.call_args.args}, "
            f"kwargs={provider.embed.call_args.kwargs}"
        )
        # Adversarial guard
        assert provider.embed_batch.call_count == 0, (
            "single_hop_strategy.py:66 calls .embed; if .embed_batch was "
            "called, this test mocked the wrong method"
        )


# ===========================================================================
# Site 2: gap_detection.py:461  →  .embed(queries)
# (Manager indirection: production accesses provider via
#  self._embedding_manager.get_provider().)
# ===========================================================================


class TestGapDetectionAt461:
    """Drives ``GapDetectionService._embed_gap_queries`` whose body binds
    ``embedding_provider = self._embedding_manager.get_provider()`` then
    calls ``embedding_provider.embed(queries)`` at line 461.

    Mock at the manager level — production uses manager indirection here,
    NOT direct attribute access.

    Mixed-task module note: this is the QUERY partner to
    ``test_embedding_call_sites_passage.py::TestGapDetectionAt253`` which
    drives the PASSAGE site at line 253 in the same file. The two tests
    live in separate files so a copy-paste error would surface at file
    boundary.
    """

    async def test_passes_task_query(self) -> None:
        # Arrange
        from chunkhound.services.research.shared.gap_detection import (
            GapDetectionService,
        )

        provider = _make_provider_mock(dims=4, num_vectors=2)

        embedding_manager = MagicMock()
        embedding_manager.get_provider = MagicMock(return_value=provider)

        service = GapDetectionService.__new__(GapDetectionService)
        service._embedding_manager = embedding_manager

        gaps = [
            GapCandidate(
                query="how does X work",
                rationale="missing coverage",
                confidence=0.9,
                source_shard=0,
            ),
            GapCandidate(
                query="why is Y broken",
                rationale="needs investigation",
                confidence=0.8,
                source_shard=1,
            ),
        ]

        # Act
        await service._embed_gap_queries(gaps)

        # Assert — production line 461 calls .embed (NOT .embed_batch)
        provider.embed.assert_called_once()
        assert provider.embed.call_args.kwargs.get("task") == "query", (
            f"gap_detection.py:461 must thread task='query'; "
            f"got args={provider.embed.call_args.args}, "
            f"kwargs={provider.embed.call_args.kwargs}"
        )
        # Adversarial guard
        assert provider.embed_batch.call_count == 0, (
            "gap_detection.py:461 calls .embed; if .embed_batch was called, "
            "this test mocked the wrong method"
        )
