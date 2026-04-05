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
    """search(type='symbols') queries the symbols table directly."""

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
        # First call: symbol query, second call: count query
        count_rows = [{"total": 2}]
        services = make_mock_services([symbol_rows, count_rows])

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
        services = make_mock_services([[], [{"total": 0}]])

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
        services = make_mock_services([symbol_rows, [{"total": 1}]])

        result = await call_search_tool(
            services=services, type="symbols", query="",
        )

        assert len(result["results"]) == 1


# ---------------------------------------------------------------------------
# type_filter — substring matching on type_signature
# ---------------------------------------------------------------------------


class TestTypeFilter:
    """type_filter parameter filters results by type_signature content."""

    @pytest.mark.asyncio
    async def test_search_symbols_with_type_filter(self) -> None:
        """type_filter narrows symbols by type_signature substring."""
        # Only symbols whose type_signature contains "Result" should be returned
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
        services = make_mock_services([symbol_rows, [{"total": 1}]])

        result = await call_search_tool(
            services=services,
            type="symbols",
            query="parse",
            type_filter="Result",
        )

        assert len(result["results"]) == 1
        assert "Result" in result["results"][0]["type_signature"]

    @pytest.mark.asyncio
    async def test_search_symbols_with_path_filter(self) -> None:
        """path parameter filters symbols by file_path prefix."""
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
        services = make_mock_services([symbol_rows, [{"total": 1}]])

        result = await call_search_tool(
            services=services,
            type="symbols",
            query="validate",
            path="src/auth",
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"].startswith("src/auth")

    @pytest.mark.asyncio
    async def test_search_symbols_type_filter_escapes_like(self) -> None:
        """LIKE metacharacters in type_filter are escaped — no wildcard injection."""
        # type_filter "Result[int]" — brackets are safe in DuckDB LIKE, but
        # underscores need escaping. Use "my_type" to test underscore escaping.
        symbol_rows = [
            {
                "fqn": "mod::func",
                "name": "func",
                "kind": "Function",
                "language": "python",
                "file_path": "src/mod.py",
                "range_start": 1,
                "range_end": 10,
                "type_signature": "(my_type) -> None",
            },
        ]
        services = make_mock_services([symbol_rows, [{"total": 1}]])

        result = await call_search_tool(
            services=services,
            type="symbols",
            query="func",
            type_filter="my_type",
        )

        # Verify the SQL was called with escaped LIKE pattern
        calls = services.provider.execute_query.call_args_list
        # The symbols query should contain the escaped type_filter
        symbols_query = calls[0][0][0]
        assert "LIKE" in symbols_query
        # The parameter should have escaped underscores
        params = calls[0][0][1]
        # Find the type_filter param — should have !-escaped underscore
        type_filter_params = [p for p in params if isinstance(p, str) and "my" in p]
        assert len(type_filter_params) >= 1
        # Escaped underscore: my!_type
        assert "my!_type" in type_filter_params[0]


# ---------------------------------------------------------------------------
# type_filter on regex/semantic — post-filter via symbols join
# ---------------------------------------------------------------------------


class TestTypeFilterCrossType:
    """type_filter works with regex and semantic search types via post-filter."""

    @pytest.mark.asyncio
    async def test_search_regex_with_type_filter(self) -> None:
        """Regex search + type_filter post-filters by symbols join."""
        # Mock regex search returning chunk results
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

        # Symbols join: only src/parser.py has a symbol with "int" in type_signature
        # The batch query returns matching (file_path, range_start, range_end) tuples
        services.provider.execute_query.return_value = [
            {
                "file_path": "src/util.py",
                "range_start": 5,
                "range_end": 15,
            },
        ]

        result = await call_search_tool(
            services=services,
            type="regex",
            query="def.*->",
            type_filter="int",
        )

        # Only the chunk overlapping a symbol with "int" in type_signature survives
        assert len(result["results"]) == 1
        assert result["results"][0]["file_path"] == "src/util.py"

    @pytest.mark.asyncio
    async def test_search_semantic_with_type_filter(self) -> None:
        """Semantic search + type_filter post-filters by symbols join."""
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

        # Symbols join: only handler.py has a symbol with "Handler" in type_signature
        services.provider.execute_query.return_value = [
            {
                "file_path": "src/handler.py",
                "range_start": 1,
                "range_end": 50,
            },
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

        # Only handler.py survives — logger.py has no matching symbol type
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
    """Adversarial battery for _search_symbols and _apply_type_filter."""

    @pytest.mark.asyncio
    async def test_query_with_percent_sign(self) -> None:
        """LIKE metacharacter % in query is escaped — doesn't match everything."""
        services = make_mock_services([[], [{"total": 0}]])

        result = await call_search_tool(
            services=services, type="symbols", query="100%",
        )

        # Verify the SQL parameter has escaped percent
        calls = services.provider.execute_query.call_args_list
        params = calls[0][0][1]
        name_param = params[0]  # First LIKE param
        assert "!%" in name_param

    @pytest.mark.asyncio
    async def test_query_with_underscore(self) -> None:
        """Underscore in query is escaped — doesn't match single-char wildcard."""
        services = make_mock_services([[], [{"total": 0}]])

        result = await call_search_tool(
            services=services, type="symbols", query="my_func",
        )

        calls = services.provider.execute_query.call_args_list
        params = calls[0][0][1]
        name_param = params[0]
        assert "!_" in name_param

    @pytest.mark.asyncio
    async def test_path_with_underscore_escaped(self) -> None:
        """Underscore in path filter is escaped for LIKE."""
        services = make_mock_services([[], [{"total": 0}]])

        result = await call_search_tool(
            services=services, type="symbols", query="x", path="my_module",
        )

        calls = services.provider.execute_query.call_args_list
        params = calls[0][0][1]
        # Path param should have !-escaped underscore
        path_params = [p for p in params if isinstance(p, str) and "module" in p]
        assert any("!_" in p for p in path_params)

    @pytest.mark.asyncio
    async def test_large_offset_beyond_total(self) -> None:
        """Offset beyond total returns empty results with correct pagination."""
        services = make_mock_services([[], [{"total": 5}]])

        result = await call_search_tool(
            services=services, type="symbols", query="x", offset=1000,
        )

        assert result["results"] == []
        assert result["pagination"]["total"] == 5
        assert result["pagination"]["has_more"] is False

    @pytest.mark.asyncio
    async def test_type_filter_empty_string_treated_as_no_filter(self) -> None:
        """Empty type_filter string should not add a LIKE clause."""
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
        services = make_mock_services([symbol_rows, [{"total": 1}]])

        result = await call_search_tool(
            services=services, type="symbols", query="foo", type_filter="",
        )

        # Empty type_filter should not filter — symbol with None type_signature still returned
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
        # Symbols join returns NOTHING — no matching type signatures
        services.provider.execute_query.return_value = []

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
        # Both chunks have matching symbols
        services.provider.execute_query.return_value = [
            {"file_path": "src/a.py", "range_start": 1, "range_end": 5},
            {"file_path": "src/b.py", "range_start": 10, "range_end": 15},
        ]

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

        # No type_filter — execute_query for symbols join should NOT be called
        services.provider.execute_query.assert_not_called()
        assert len(result["results"]) == 1
