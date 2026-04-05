"""Tests for search(type: structural) — semantic + graph walk expansion.

Covers Phase 3 criterion: structural search does semantic search + graph walk
expansion with deduplication. Task ch-nlz.
"""

import pytest

from tests.lsp.mcp_tool_helpers import call_search_tool, make_mock_services

from unittest.mock import AsyncMock, MagicMock

pytestmark = pytest.mark.unit


def _make_embedding_manager() -> MagicMock:
    """Create a mock embedding manager that passes validation."""
    em = MagicMock()
    em.list_providers.return_value = ["voyageai"]
    provider = MagicMock()
    provider.name = "voyageai"
    provider.model = "voyage-code-3"
    em.get_provider.return_value = provider
    return em


def _make_semantic_results(
    chunks: list[dict],
) -> tuple[list[dict], dict]:
    """Build (results, pagination) tuple matching search_semantic return."""
    return (
        chunks,
        {
            "offset": 0,
            "page_size": len(chunks),
            "has_more": False,
            "total": len(chunks),
            "next_offset": None,
        },
    )


class TestStructuralRequiresEmbeddings:
    """Structural search requires an embedding provider (same gate as semantic)."""

    @pytest.mark.asyncio
    async def test_structural_requires_embeddings(self) -> None:
        """Raises ValueError when embedding_manager is None."""
        services = make_mock_services()

        with pytest.raises(ValueError, match="[Ss]tructural.*requires.*embedding|[Ss]emantic.*requires.*embedding"):
            await call_search_tool(
                services=services,
                embedding_manager=None,
                type="structural",
                query="error handling",
            )


class TestStructuralNoSymbolsFallback:
    """When no symbols overlap semantic results, returns semantic results only."""

    @pytest.mark.asyncio
    async def test_structural_no_symbols_falls_back(self) -> None:
        """Empty symbols table → semantic results returned unchanged."""
        semantic_chunks = [
            {
                "file_path": "src/handler.py",
                "content": "def handle_error(): pass",
                "start_line": 10,
                "end_line": 15,
            },
            {
                "file_path": "src/utils.py",
                "content": "def log_error(): pass",
                "start_line": 20,
                "end_line": 25,
            },
        ]

        services = make_mock_services()
        # Symbol lookup returns empty (no symbols in table)
        services.provider.execute_query.return_value = []
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="error handling",
        )

        assert "results" in result
        assert len(result["results"]) == 2
        assert result["results"][0]["file_path"] == "src/handler.py"
        assert result["results"][1]["file_path"] == "src/utils.py"


class TestStructuralGraphWalkEmpty:
    """Symbols found but no edges — returns semantic results only."""

    @pytest.mark.asyncio
    async def test_structural_graph_walk_empty(self) -> None:
        """Symbols exist but symbol_edges is empty → semantic results only."""
        semantic_chunks = [
            {
                "file_path": "src/parser.py",
                "content": "def parse(): pass",
                "start_line": 1,
                "end_line": 5,
            },
        ]

        # Symbol lookup returns symbols, but graph walk returns empty
        symbol_rows = [{"fqn": "mod::parse", "file_id": 1}]
        # Graph walk CTE returns only the seed (no neighbors)
        walk_rows = [{"fqn": "mod::parse"}]
        # Chunk resolution returns the same chunk as semantic
        chunk_rows = [
            {
                "file_path": "src/parser.py",
                "content": "def parse(): pass",
                "start_line": 1,
                "end_line": 5,
            },
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="parse",
        )

        assert "results" in result
        # Graph walk only found the seed symbol, which resolves to the same
        # chunk as semantic. Dedup ensures no duplicates.
        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"] == "src/parser.py"


class TestStructuralReturnsGraphChunks:
    """Core behavior: structural returns semantic + graph-discovered chunks."""

    @pytest.mark.asyncio
    async def test_structural_returns_semantic_plus_graph_chunks(self) -> None:
        """Graph walk discovers chunks that semantic search alone misses."""
        semantic_chunks = [
            {
                "file_path": "src/handler.py",
                "content": "def handle_request(): pass",
                "start_line": 10,
                "end_line": 20,
            },
        ]

        # Symbol lookup finds a symbol overlapping the semantic chunk
        symbol_rows = [{"fqn": "mod::handle_request", "file_id": 1}]
        # Graph walk finds the seed + a neighbor (caller)
        walk_rows = [
            {"fqn": "mod::handle_request"},
            {"fqn": "mod::route_dispatch"},
        ]
        # Chunk resolution returns chunks for BOTH walked symbols —
        # the semantic chunk (will be deduped) + a NEW graph-discovered chunk
        chunk_rows = [
            {
                "file_path": "src/handler.py",
                "content": "def handle_request(): pass",
                "start_line": 10,
                "end_line": 20,
            },
            {
                "file_path": "src/router.py",
                "content": "def route_dispatch(): pass",
                "start_line": 1,
                "end_line": 8,
            },
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="request handling",
        )

        assert "results" in result
        # 1 semantic + 1 graph-discovered (the semantic duplicate is deduped)
        assert len(result["results"]) == 2
        # Semantic results come first
        assert result["results"][0]["file_path"] == "src/handler.py"
        # Graph-discovered chunk comes second
        assert result["results"][1]["file_path"] == "src/router.py"


class TestStructuralDeduplicates:
    """Chunks found by both semantic and graph walk appear only once."""

    @pytest.mark.asyncio
    async def test_structural_deduplicates(self) -> None:
        """Same chunk from both paths → appears once in results."""
        semantic_chunks = [
            {
                "file_path": "src/core.py",
                "content": "def process(): pass",
                "start_line": 5,
                "end_line": 15,
            },
            {
                "file_path": "src/helper.py",
                "content": "def assist(): pass",
                "start_line": 1,
                "end_line": 10,
            },
        ]

        # Symbol lookup finds symbols for both chunks
        symbol_rows = [
            {"fqn": "mod::process", "file_id": 1},
            {"fqn": "mod::assist", "file_id": 2},
        ]
        # Graph walk finds same symbols (no new neighbors)
        walk_rows = [
            {"fqn": "mod::process"},
            {"fqn": "mod::assist"},
        ]
        # Chunk resolution returns the EXACT same chunks as semantic
        chunk_rows = [
            {
                "file_path": "src/core.py",
                "content": "def process(): pass",
                "start_line": 5,
                "end_line": 15,
            },
            {
                "file_path": "src/helper.py",
                "content": "def assist(): pass",
                "start_line": 1,
                "end_line": 10,
            },
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="processing",
        )

        assert "results" in result
        # Both graph chunks duplicate semantic — dedup keeps only 2
        assert len(result["results"]) == 2
        paths = [r["file_path"] for r in result["results"]]
        assert paths == ["src/core.py", "src/helper.py"]


class TestStructuralWithTypeFilter:
    """type_filter narrows structural results via _apply_type_filter."""

    @pytest.mark.asyncio
    async def test_structural_with_type_filter(self) -> None:
        """type_filter filters combined semantic+graph results."""
        semantic_chunks = [
            {
                "file_path": "src/handler.py",
                "content": "def handle() -> Result: pass",
                "start_line": 10,
                "end_line": 20,
            },
            {
                "file_path": "src/utils.py",
                "content": "def helper() -> str: pass",
                "start_line": 1,
                "end_line": 5,
            },
        ]

        # Symbol lookup
        symbol_rows = [
            {"fqn": "mod::handle", "file_id": 1},
            {"fqn": "mod::helper", "file_id": 2},
        ]
        # Graph walk finds a new neighbor
        walk_rows = [
            {"fqn": "mod::handle"},
            {"fqn": "mod::helper"},
            {"fqn": "mod::validate"},
        ]
        # Chunk resolution returns graph-discovered chunk
        chunk_rows = [
            {
                "file_path": "src/handler.py",
                "content": "def handle() -> Result: pass",
                "start_line": 10,
                "end_line": 20,
            },
            {
                "file_path": "src/utils.py",
                "content": "def helper() -> str: pass",
                "start_line": 1,
                "end_line": 5,
            },
            {
                "file_path": "src/validator.py",
                "content": "def validate() -> Result: pass",
                "start_line": 30,
                "end_line": 40,
            },
        ]
        # type_filter query: only handler.py and validator.py have Result type
        type_filter_matches = [
            {"file_path": "src/handler.py", "range_start": 10, "range_end": 20},
            {"file_path": "src/validator.py", "range_start": 30, "range_end": 40},
        ]

        services = make_mock_services(
            [symbol_rows, walk_rows, chunk_rows, type_filter_matches],
        )
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="handling",
            type_filter="Result",
        )

        assert "results" in result
        # type_filter keeps only chunks with matching type_signature
        # handler.py (semantic) + validator.py (graph) match; utils.py filtered out
        assert len(result["results"]) == 2
        paths = [r["file_path"] for r in result["results"]]
        assert "src/handler.py" in paths
        assert "src/validator.py" in paths
        assert "src/utils.py" not in paths


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestStructuralAdversarial:
    """Adversarial tests for _search_structural edge cases."""

    @pytest.mark.asyncio
    async def test_empty_semantic_results(self) -> None:
        """Semantic search returns 0 results → empty response, no crash."""
        services = make_mock_services()
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results([]),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="nonexistent concept",
        )

        assert "results" in result
        assert len(result["results"]) == 0
        assert result["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_singular_pipeline(self) -> None:
        """Single semantic result → single symbol → single edge → single graph chunk."""
        semantic_chunks = [
            {
                "file_path": "src/a.py",
                "content": "def a(): pass",
                "start_line": 1,
                "end_line": 3,
            },
        ]
        symbol_rows = [{"fqn": "mod::a", "file_id": 1}]
        walk_rows = [{"fqn": "mod::a"}, {"fqn": "mod::b"}]
        chunk_rows = [
            {"file_path": "src/a.py", "content": "def a(): pass", "start_line": 1, "end_line": 3},
            {"file_path": "src/b.py", "content": "def b(): pass", "start_line": 1, "end_line": 2},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="a",
        )

        assert len(result["results"]) == 2
        assert result["results"][0]["file_path"] == "src/a.py"
        assert result["results"][1]["file_path"] == "src/b.py"

    @pytest.mark.asyncio
    async def test_self_referential_symbol_edge(self) -> None:
        """Symbol with self-loop edge → CTE cycle detection prevents infinite walk."""
        semantic_chunks = [
            {
                "file_path": "src/recursive.py",
                "content": "def recurse(): recurse()",
                "start_line": 1,
                "end_line": 3,
            },
        ]
        symbol_rows = [{"fqn": "mod::recurse", "file_id": 1}]
        # Walk returns only the seed (self-loop detected by list_contains)
        walk_rows = [{"fqn": "mod::recurse"}]
        chunk_rows = [
            {"file_path": "src/recursive.py", "content": "def recurse(): recurse()", "start_line": 1, "end_line": 3},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="recurse",
        )

        # Self-loop doesn't cause infinite expansion — dedup keeps 1 result
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_redundant_symbols_same_fqn(self) -> None:
        """Multiple semantic results mapping to same FQN → no duplicate seeds."""
        semantic_chunks = [
            {
                "file_path": "src/mod.py",
                "content": "def parse(): pass",
                "start_line": 1,
                "end_line": 5,
            },
            {
                "file_path": "src/mod.py",
                "content": "class Parser: pass",
                "start_line": 6,
                "end_line": 15,
            },
        ]
        # Both chunks overlap the same symbol
        symbol_rows = [
            {"fqn": "mod::parse", "file_id": 1},
            {"fqn": "mod::parse", "file_id": 1},
        ]
        walk_rows = [{"fqn": "mod::parse"}, {"fqn": "mod::caller"}]
        chunk_rows = [
            {"file_path": "src/mod.py", "content": "def parse(): pass", "start_line": 1, "end_line": 5},
            {"file_path": "src/mod.py", "content": "class Parser: pass", "start_line": 6, "end_line": 15},
            {"file_path": "src/caller.py", "content": "def call(): parse()", "start_line": 1, "end_line": 3},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="parse",
        )

        # 2 semantic + 1 graph-discovered (the 2 semantic chunks are already in seen set)
        assert len(result["results"]) == 3
        paths = [r["file_path"] for r in result["results"]]
        assert paths.count("src/caller.py") == 1

    @pytest.mark.asyncio
    async def test_pagination_with_small_page_size(self) -> None:
        """page_size=1 with combined pool of 3 → returns 1, has_more=True."""
        semantic_chunks = [
            {"file_path": "src/a.py", "content": "a", "start_line": 1, "end_line": 2},
            {"file_path": "src/b.py", "content": "b", "start_line": 1, "end_line": 2},
        ]
        symbol_rows = [{"fqn": "mod::a", "file_id": 1}]
        walk_rows = [{"fqn": "mod::a"}, {"fqn": "mod::c"}]
        chunk_rows = [
            {"file_path": "src/a.py", "content": "a", "start_line": 1, "end_line": 2},
            {"file_path": "src/c.py", "content": "c", "start_line": 1, "end_line": 2},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="test",
            page_size=1,
        )

        assert len(result["results"]) == 1
        assert result["pagination"]["has_more"] is True
        assert result["pagination"]["total"] == 3
        assert result["pagination"]["next_offset"] == 1

    @pytest.mark.asyncio
    async def test_dense_graph_chunks_paginated_correctly(self) -> None:
        """Many graph-discovered chunks correctly paginated with page_size=2."""
        semantic_chunks = [
            {"file_path": "src/seed.py", "content": "seed", "start_line": 1, "end_line": 3},
        ]
        symbol_rows = [{"fqn": "mod::seed", "file_id": 1}]
        walk_rows = [{"fqn": f"mod::n{i}"} for i in range(5)]
        chunk_rows = [
            {"file_path": f"src/n{i}.py", "content": f"n{i}", "start_line": 1, "end_line": 3}
            for i in range(5)
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="dense",
            page_size=2,
        )

        # 1 semantic + 5 graph = 6 total, page_size=2 → first 2 returned
        assert len(result["results"]) == 2
        assert result["pagination"]["total"] == 6
        assert result["pagination"]["has_more"] is True
        assert result["pagination"]["next_offset"] == 2

    @pytest.mark.asyncio
    async def test_unicode_file_paths_dedup_correctly(self) -> None:
        """Unicode file paths dedup correctly between semantic and graph results."""
        semantic_chunks = [
            {"file_path": "src/données.py", "content": "data", "start_line": 1, "end_line": 5},
        ]
        symbol_rows = [{"fqn": "mod::données", "file_id": 1}]
        walk_rows = [{"fqn": "mod::données"}, {"fqn": "mod::処理"}]
        chunk_rows = [
            # Same unicode path as semantic — should be deduped
            {"file_path": "src/données.py", "content": "data", "start_line": 1, "end_line": 5},
            # Different unicode path — should survive
            {"file_path": "src/処理.py", "content": "process", "start_line": 1, "end_line": 3},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="unicode",
        )

        assert len(result["results"]) == 2
        paths = [r["file_path"] for r in result["results"]]
        assert "src/données.py" in paths
        assert "src/処理.py" in paths

    @pytest.mark.asyncio
    async def test_offset_beyond_combined_pool(self) -> None:
        """Offset exceeding total combined results returns empty with correct pagination."""
        semantic_chunks = [
            {"file_path": "src/a.py", "content": "a", "start_line": 1, "end_line": 2},
        ]
        symbol_rows = [{"fqn": "mod::a", "file_id": 1}]
        walk_rows = [{"fqn": "mod::a"}]
        chunk_rows = [
            {"file_path": "src/a.py", "content": "a", "start_line": 1, "end_line": 2},
        ]

        services = make_mock_services([symbol_rows, walk_rows, chunk_rows])
        services.search_service = MagicMock()
        services.search_service.search_semantic = AsyncMock(
            return_value=_make_semantic_results(semantic_chunks),
        )

        em = _make_embedding_manager()
        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="structural",
            query="offset",
            offset=100,
        )

        # 1 total, offset=100 → empty page
        assert len(result["results"]) == 0
        assert result["pagination"]["total"] == 1
        assert result["pagination"]["has_more"] is False
        assert result["pagination"]["next_offset"] is None
