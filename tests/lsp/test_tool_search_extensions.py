"""Tests for search(type: symbols) and type_filter parameter extensions.

Covers R5 of parent epic ch-8e7: search gains type: symbols|structural
and type_filter parameter. This file tests symbols + type_filter only;
type: structural (graph walk expansion) is a separate task.
"""

import pytest

pytestmark = pytest.mark.unit

from tests.lsp.mcp_tool_helpers import call_search_tool, make_mock_services


# ---------------------------------------------------------------------------
# search(type: symbols) — basic queries against symbols table
# ---------------------------------------------------------------------------


class TestSearchSymbols:
    """search(type='symbols') queries the symbols table via provider.search_symbols.

    Post ch-nxu Step 16: search_impl calls provider.search_symbols (returning
    ``(rows, total)``) instead of two raw execute_query calls.
    """

    @pytest.mark.asyncio
    async def test_search_symbols_by_name(self) -> None:
        """Symbols search returns structured symbol dicts with expected keys."""
        symbol_rows = [
            {
                "fqn": "mod::parse_input",
                "name": "parse_input",
                "kind": "Function",
                "language": "python",
                "file_path": "src/parser.py",
                "range_start": 10,
                "range_end": 25,
                "type_signature": "(str) -> ParseResult",
            },
            {
                "fqn": "mod::parse_config",
                "name": "parse_config",
                "kind": "Function",
                "language": "python",
                "file_path": "src/config.py",
                "range_start": 5,
                "range_end": 20,
                "type_signature": "(Path) -> Config",
            },
        ]
        services = make_mock_services()
        services.provider.search_symbols.return_value = (symbol_rows, 2)

        result = await call_search_tool(
            services=services, type="symbols", query="parse",
        )

        assert "results" in result
        assert "pagination" in result
        assert len(result["results"]) == 2

        sym = result["results"][0]
        assert sym["fqn"] == "mod::parse_input"
        assert sym["name"] == "parse_input"
        assert sym["kind"] == "Function"
        assert sym["file_path"] == "src/parser.py"
        assert sym["type_signature"] == "(str) -> ParseResult"

    @pytest.mark.asyncio
    async def test_search_symbols_empty(self) -> None:
        """Empty symbols table returns empty results with pagination."""
        services = make_mock_services()
        services.provider.search_symbols.return_value = ([], 0)

        result = await call_search_tool(
            services=services, type="symbols", query="nonexistent",
        )

        assert result["results"] == []
        assert result["pagination"]["total"] == 0

    @pytest.mark.asyncio
    async def test_search_symbols_empty_query(self) -> None:
        """Empty string query returns all symbols (no name/fqn filter)."""
        symbol_rows = [
            {
                "fqn": "mod::Foo",
                "name": "Foo",
                "kind": "Class",
                "language": "python",
                "file_path": "src/foo.py",
                "range_start": 1,
                "range_end": 50,
                "type_signature": None,
            },
        ]
        services = make_mock_services()
        services.provider.search_symbols.return_value = (symbol_rows, 1)

        result = await call_search_tool(
            services=services, type="symbols", query="",
        )

        assert len(result["results"]) == 1


# ---------------------------------------------------------------------------
# type_filter — substring matching on type_signature
# ---------------------------------------------------------------------------


class TestTypeFilter:
    """type_filter parameter filters results by type_signature content.

    Post ch-nxu Step 16: LIKE-escape semantics for query / path / type_filter
    are provider internals and tested in
    tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
    """

    @pytest.mark.asyncio
    async def test_search_symbols_with_type_filter(self) -> None:
        """type_filter is forwarded to the provider method."""
        symbol_rows = [
            {
                "fqn": "mod::parse_input",
                "name": "parse_input",
                "kind": "Function",
                "language": "python",
                "file_path": "src/parser.py",
                "range_start": 10,
                "range_end": 25,
                "type_signature": "(str) -> Result[ParseOutput, Error]",
            },
        ]
        services = make_mock_services()
        services.provider.search_symbols.return_value = (symbol_rows, 1)

        result = await call_search_tool(
            services=services,
            type="symbols",
            query="parse",
            type_filter="Result",
        )

        assert len(result["results"]) == 1
        assert "Result" in result["results"][0]["type_signature"]
        # Provider call received the type_filter arg verbatim
        kwargs = services.provider.search_symbols.call_args.kwargs
        assert kwargs["type_filter"] == "Result"

    @pytest.mark.asyncio
    async def test_search_symbols_with_path_filter(self) -> None:
        """path parameter is forwarded to the provider method."""
        symbol_rows = [
            {
                "fqn": "auth::validate",
                "name": "validate",
                "kind": "Function",
                "language": "python",
                "file_path": "src/auth/validate.py",
                "range_start": 1,
                "range_end": 30,
                "type_signature": "(Token) -> bool",
            },
        ]
        services = make_mock_services()
        services.provider.search_symbols.return_value = (symbol_rows, 1)

        result = await call_search_tool(
            services=services,
            type="symbols",
            query="validate",
            path="src/auth",
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"].startswith("src/auth")
        kwargs = services.provider.search_symbols.call_args.kwargs
        assert kwargs["path"] == "src/auth"


# ---------------------------------------------------------------------------
# type_filter on regex/semantic — post-filter via symbols join
# ---------------------------------------------------------------------------


class TestTypeFilterCrossType:
    """type_filter works with regex and semantic search types via post-filter."""

    @pytest.mark.asyncio
    async def test_search_regex_with_type_filter(self) -> None:
        """Regex search + type_filter post-filters via filter_chunks_by_symbol_type_signature."""
        chunk_results = [
            {
                "file_path": "src/parser.py",
                "content": "def parse_input(s: str) -> Result:\n    ...",
                "start_line": 10,
                "end_line": 25,
            },
            {
                "file_path": "src/util.py",
                "content": "def helper() -> int:\n    ...",
                "start_line": 5,
                "end_line": 15,
            },
        ]
        pagination = {"offset": 0, "page_size": 10, "has_more": False, "total": 2}

        services = make_mock_services()
        services.search_service.search_regex_async = pytest.importorskip(
            "unittest.mock"
        ).AsyncMock(return_value=(chunk_results, pagination))
        # Provider retains only the util.py chunk
        services.provider.filter_chunks_by_symbol_type_signature.return_value = [
            chunk_results[1],
        ]

        result = await call_search_tool(
            services=services,
            type="regex",
            query="def.*->",
            type_filter="int",
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"] == "src/util.py"
        services.provider.filter_chunks_by_symbol_type_signature.assert_called_once()

    @pytest.mark.asyncio
    async def test_search_semantic_with_type_filter(self) -> None:
        """Semantic search + type_filter post-filters via filter_chunks_by_symbol_type_signature."""
        from unittest.mock import MagicMock

        chunk_results = [
            {
                "file_path": "src/handler.py",
                "content": "class RequestHandler:\n    ...",
                "start_line": 1,
                "end_line": 50,
            },
            {
                "file_path": "src/logger.py",
                "content": "def setup_logging() -> None:\n    ...",
                "start_line": 1,
                "end_line": 20,
            },
        ]
        pagination = {"offset": 0, "page_size": 10, "has_more": False, "total": 2}

        services = make_mock_services()
        services.search_service.search_semantic = pytest.importorskip(
            "unittest.mock"
        ).AsyncMock(return_value=(chunk_results, pagination))
        services.provider.filter_chunks_by_symbol_type_signature.return_value = [
            chunk_results[0],
        ]

        # Need embedding_manager mock for semantic search
        em = MagicMock()
        provider_mock = MagicMock()
        provider_mock.name = "voyageai"
        provider_mock.model = "voyage-code-3"
        em.list_providers.return_value = ["voyageai"]
        em.get_provider.return_value = provider_mock

        result = await call_search_tool(
            services=services,
            embedding_manager=em,
            type="semantic",
            query="request handling",
            type_filter="Handler",
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"] == "src/handler.py"


# ---------------------------------------------------------------------------
# _build_filtered_tool_dicts — symbols always available
# ---------------------------------------------------------------------------


class TestBuildFilteredToolDicts:
    """_build_filtered_tool_dicts includes 'symbols' in type enum without embeddings."""

    def test_symbols_available_without_embeddings(self) -> None:
        """When embeddings unavailable, search type enum includes 'regex' and 'symbols'."""
        from unittest.mock import MagicMock

        from chunkhound.mcp_server.base import MCPServerBase

        server = MagicMock(spec=MCPServerBase)
        server.embedding_manager = None
        server.llm_manager = None

        tool_dicts = MCPServerBase._build_filtered_tool_dicts(server)

        # Find the search tool
        search_tool = next(
            (t for t in tool_dicts if t["name"] == "search"), None
        )
        assert search_tool is not None

        type_enum = search_tool["inputSchema"]["properties"]["type"]["enum"]
        assert "regex" in type_enum
        assert "symbols" in type_enum
        assert "semantic" not in type_enum


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestAdversarialSearchSymbols:
    """Adversarial battery for _search_symbols and _apply_type_filter.

    Post ch-nxu Step 16: LIKE-escape metacharacter handling is provider-
    internal. The DuckDB / LanceDB contract tests in
    tests/integration/test_{duckdb,lancedb}_symbol_protocol.py cover query,
    path, and type_filter escape semantics on real databases.
    """

    @pytest.mark.asyncio
    async def test_query_forwarded_verbatim_to_provider(self) -> None:
        """User-supplied query metacharacters are passed to the provider as-is;
        the provider owns LIKE escaping."""
        services = make_mock_services()
        services.provider.search_symbols.return_value = ([], 0)

        await call_search_tool(
            services=services, type="symbols", query="100%",
        )

        kwargs = services.provider.search_symbols.call_args.kwargs
        assert kwargs["query"] == "100%"

    @pytest.mark.asyncio
    async def test_path_forwarded_verbatim_to_provider(self) -> None:
        """Path filter is forwarded to the provider verbatim."""
        services = make_mock_services()
        services.provider.search_symbols.return_value = ([], 0)

        await call_search_tool(
            services=services, type="symbols", query="x", path="my_module",
        )

        kwargs = services.provider.search_symbols.call_args.kwargs
        assert kwargs["path"] == "my_module"

    @pytest.mark.asyncio
    async def test_large_offset_beyond_total(self) -> None:
        """Offset beyond total returns empty results with correct pagination."""
        services = make_mock_services()
        services.provider.search_symbols.return_value = ([], 5)

        result = await call_search_tool(
            services=services, type="symbols", query="x", offset=1000,
        )

        assert result["results"] == []
        assert result["pagination"]["total"] == 5
        assert result["pagination"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_type_filter_empty_string_passed_to_provider(self) -> None:
        """Empty type_filter passed to provider as empty string — provider
        decides how to treat it (DuckDB matches any non-NULL signature)."""
        symbol_rows = [
            {
                "fqn": "mod::foo",
                "name": "foo",
                "kind": "Function",
                "language": "python",
                "file_path": "src/mod.py",
                "range_start": 1,
                "range_end": 10,
                "type_signature": None,
            },
        ]
        services = make_mock_services()
        services.provider.search_symbols.return_value = (symbol_rows, 1)

        result = await call_search_tool(
            services=services, type="symbols", query="foo", type_filter="",
        )

        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_apply_type_filter_no_matches_filters_all(self) -> None:
        """When no symbols match type_filter, all chunk results are removed."""
        from unittest.mock import AsyncMock

        chunk_results = [
            {
                "file_path": "src/a.py",
                "content": "def foo(): ...",
                "start_line": 1,
                "end_line": 5,
            },
            {
                "file_path": "src/b.py",
                "content": "def bar(): ...",
                "start_line": 1,
                "end_line": 5,
            },
        ]
        pagination = {"offset": 0, "page_size": 10, "has_more": False, "total": 2}

        services = make_mock_services()
        services.search_service.search_regex_async = AsyncMock(
            return_value=(chunk_results, pagination),
        )
        services.provider.filter_chunks_by_symbol_type_signature.return_value = []

        result = await call_search_tool(
            services=services, type="regex", query="def", type_filter="NonExistentType",
        )

        assert len(result["results"]) == 0

    @pytest.mark.asyncio
    async def test_apply_type_filter_all_match(self) -> None:
        """When all chunks have matching symbols, all survive filtering."""
        from unittest.mock import AsyncMock

        chunk_results = [
            {
                "file_path": "src/a.py",
                "content": "def foo() -> int: ...",
                "start_line": 1,
                "end_line": 5,
            },
            {
                "file_path": "src/b.py",
                "content": "def bar() -> int: ...",
                "start_line": 10,
                "end_line": 15,
            },
        ]
        pagination = {"offset": 0, "page_size": 10, "has_more": False, "total": 2}

        services = make_mock_services()
        services.search_service.search_regex_async = AsyncMock(
            return_value=(chunk_results, pagination),
        )
        services.provider.filter_chunks_by_symbol_type_signature.return_value = chunk_results

        result = await call_search_tool(
            services=services, type="regex", query="def", type_filter="int",
        )

        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_regex_without_type_filter_unchanged(self) -> None:
        """Regex search without type_filter returns all results — zero regression."""
        from unittest.mock import AsyncMock

        chunk_results = [
            {
                "file_path": "src/mod.py",
                "content": "class Foo: ...",
                "start_line": 1,
                "end_line": 10,
            },
        ]
        pagination = {"offset": 0, "page_size": 10, "has_more": False, "total": 1}

        services = make_mock_services()
        services.search_service.search_regex_async = AsyncMock(
            return_value=(chunk_results, pagination),
        )

        result = await call_search_tool(
            services=services, type="regex", query="class",
        )

        # No type_filter — filter_chunks_by_symbol_type_signature must not be called
        services.provider.filter_chunks_by_symbol_type_signature.assert_not_called()
        assert len(result["results"]) == 1
