"""Tests for graph walk expansion integration in UnifiedSearch.unified_search().

Verifies that GraphWalkExpander is called after semantic search (step 2.5),
its results are merged into the pipeline, and failures degrade gracefully.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.services.research.shared.models import ResearchContext
from chunkhound.services.research.shared.unified_search import UnifiedSearch

pytestmark = pytest.mark.unit

# Target module path for patching GraphWalkExpander where it will be imported
_GWE_PATH = "chunkhound.services.research.shared.unified_search.GraphWalkExpander"


def _make_unified_search(
    semantic_results: list[dict],
    semantic_pagination: dict | None = None,
) -> tuple[UnifiedSearch, MagicMock]:
    """Create UnifiedSearch with mocked services returning given semantic results.

    Returns (search_instance, mock_search_service) so tests can inspect calls.
    """
    mock_search_service = AsyncMock()
    mock_search_service.search_semantic.return_value = (
        semantic_results,
        semantic_pagination or {"total": len(semantic_results)},
    )

    mock_db_services = MagicMock()
    mock_db_services.search_service = mock_search_service
    mock_db_services.provider = MagicMock()

    # Disable reranking to simplify test assertions
    mock_emb_provider = MagicMock()
    mock_emb_provider.supports_reranking.return_value = False
    mock_emb_mgr = MagicMock()
    mock_emb_mgr.get_provider.return_value = mock_emb_provider

    search = UnifiedSearch(db_services=mock_db_services, embedding_manager=mock_emb_mgr)
    return search, mock_search_service


def _make_context(query: str = "test query") -> ResearchContext:
    return ResearchContext(root_query=query)


class TestGraphExpansionIntegration:
    """unified_search() includes graph-expanded chunks alongside semantic results."""

    @pytest.mark.asyncio
    async def test_unified_search_includes_graph_expanded_chunks(self) -> None:
        """Both semantic and graph-expanded chunks appear in the result."""
        semantic = [
            {
                "chunk_id": 1,
                "file_path": "a.py",
                "content": "def func_a(): pass",
                "start_line": 1,
                "end_line": 5,
                "symbol": "func_a",
            }
        ]
        search, _ = _make_unified_search(semantic)

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_expander.expand.return_value = [
                {
                    "chunk_id": 99,
                    "file_path": "b.py",
                    "content": "def func_b(): pass",
                    "start_line": 10,
                    "end_line": 15,
                }
            ]
            mock_gwe_cls.return_value = mock_expander

            result = await search.unified_search("test query", _make_context())

        chunk_ids = {c.get("chunk_id") or c.get("id") for c in result}
        assert 1 in chunk_ids, "semantic chunk missing"
        assert 99 in chunk_ids, "graph-expanded chunk missing"


class TestGraphExpansionIndependence:
    """Semantic and graph expanders are independent — either can return empty."""

    @pytest.mark.asyncio
    async def test_graph_empty_returns_semantic_only(self) -> None:
        """expand() returns [] → result contains only semantic chunks."""
        semantic = [
            {
                "chunk_id": 1,
                "file_path": "a.py",
                "content": "def func_a(): pass",
                "start_line": 1,
                "end_line": 5,
                "symbol": "func_a",
            }
        ]
        search, _ = _make_unified_search(semantic)

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_expander.expand.return_value = []
            mock_gwe_cls.return_value = mock_expander

            result = await search.unified_search("test query", _make_context())

        chunk_ids = {c.get("chunk_id") or c.get("id") for c in result}
        assert chunk_ids == {1}, f"expected only semantic chunk, got {chunk_ids}"

    @pytest.mark.asyncio
    async def test_semantic_empty_skips_graph_expansion(self) -> None:
        """Empty semantic results → expand() not called (nothing to expand)."""
        search, _ = _make_unified_search([])

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_gwe_cls.return_value = mock_expander

            result = await search.unified_search("test query", _make_context())

            # Either expand() not called, or called with [] and returns []
            if mock_expander.expand.called:
                args = mock_expander.expand.call_args[0][0]
                assert args == [], "expand() called with non-empty input on empty semantic"

        assert result == []


class TestGraphExpansionErrorHandling:
    """Graph expansion errors degrade gracefully — never crash the pipeline."""

    @pytest.mark.asyncio
    async def test_expand_raises_returns_semantic_only(self) -> None:
        """expand() raises Exception → semantic results returned, no crash."""
        semantic = [
            {
                "chunk_id": 1,
                "file_path": "a.py",
                "content": "def func_a(): pass",
                "start_line": 1,
                "end_line": 5,
                "symbol": "func_a",
            }
        ]
        search, _ = _make_unified_search(semantic)

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_expander.expand.side_effect = RuntimeError("symbols table missing")
            mock_gwe_cls.return_value = mock_expander

            # Should NOT raise — graceful degradation
            result = await search.unified_search("test query", _make_context())

        chunk_ids = {c.get("chunk_id") or c.get("id") for c in result}
        assert 1 in chunk_ids, "semantic chunk should survive graph expansion failure"


class TestAdversarialRedundant:
    """Adversarial: duplicate chunk_ids between semantic and graph results."""

    @pytest.mark.asyncio
    async def test_duplicate_chunk_id_keeps_semantic_version(self) -> None:
        """Graph chunk with same chunk_id as semantic → semantic version kept."""
        semantic = [
            {
                "chunk_id": 1,
                "file_path": "a.py",
                "content": "semantic version",
                "start_line": 1,
                "end_line": 5,
                "symbol": "func_a",
            }
        ]
        search, _ = _make_unified_search(semantic)

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_expander.expand.return_value = [
                {
                    "chunk_id": 1,  # Same chunk_id as semantic
                    "file_path": "a.py",
                    "content": "graph version",
                    "start_line": 1,
                    "end_line": 5,
                }
            ]
            mock_gwe_cls.return_value = mock_expander

            result = await search.unified_search("test query", _make_context())

        # Step 6 dedup keeps first occurrence (semantic)
        assert len(result) == 1
        assert result[0]["content"] == "semantic version"


class TestAdversarialSparseMetadata:
    """Adversarial: graph chunks missing metadata keys that downstream steps use."""

    @pytest.mark.asyncio
    async def test_graph_chunks_without_symbol_key_no_crash(self) -> None:
        """Graph chunks lack 'symbol' metadata → symbol extraction skips them, no crash."""
        semantic = [
            {
                "chunk_id": 1,
                "file_path": "a.py",
                "content": "def func_a(): pass",
                "start_line": 1,
                "end_line": 5,
                "symbol": "func_a",
            }
        ]
        search, _ = _make_unified_search(semantic)

        with patch(_GWE_PATH, create=True) as mock_gwe_cls:
            mock_expander = AsyncMock()
            mock_expander.expand.return_value = [
                {
                    "chunk_id": 99,
                    "file_path": "b.py",
                    "content": "def func_b(): pass",
                    "start_line": 10,
                    "end_line": 15,
                    # No 'symbol' key — graph chunks don't have this
                }
            ]
            mock_gwe_cls.return_value = mock_expander

            # Should not crash during symbol extraction (step 3)
            result = await search.unified_search("test query", _make_context())

        chunk_ids = {c.get("chunk_id") or c.get("id") for c in result}
        assert 1 in chunk_ids, "semantic chunk missing"
        assert 99 in chunk_ids, "graph chunk should survive despite missing symbol key"
