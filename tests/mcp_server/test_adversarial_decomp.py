"""Adversarial stress tests for tools decomposition (ch-c0w).

Structural patterns applied to the dispatch dict, URI handling,
stats guards, and registration integrity after module reshuffling.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# LSP Dispatch — Empty, Encoding, Semantically Hostile
# ---------------------------------------------------------------------------


class TestLspDispatchAdversarial:
    """Adversarial patterns on the dispatch dict routing."""

    @pytest.mark.asyncio
    async def test_empty_string_operation(self):
        """Empty string is not a valid operation — should return error dict."""
        from chunkhound.mcp_server.tools.lsp_tools import lsp_impl

        pool = MagicMock()
        pool.get = AsyncMock(return_value=AsyncMock())
        config = MagicMock()
        config.target_dir = MagicMock()
        config.target_dir.__bool__ = MagicMock(return_value=True)
        config.target_dir.__str__ = MagicMock(return_value="/workspace")

        result = await lsp_impl(
            lsp_client_pool=pool,
            services=MagicMock(),
            config=config,
            file="/workspace/foo.py",
            line=0,
            character=0,
            operation="",
        )
        assert result["error"] == "invalid_operation"

    @pytest.mark.asyncio
    async def test_case_sensitive_operation(self):
        """'DEFINITION' != 'definition' — dispatch is case-sensitive."""
        from chunkhound.mcp_server.tools.lsp_tools import lsp_impl

        pool = MagicMock()
        pool.get = AsyncMock(return_value=AsyncMock())
        config = MagicMock()
        config.target_dir = MagicMock()
        config.target_dir.__bool__ = MagicMock(return_value=True)
        config.target_dir.__str__ = MagicMock(return_value="/workspace")

        result = await lsp_impl(
            lsp_client_pool=pool,
            services=MagicMock(),
            config=config,
            file="/workspace/foo.py",
            line=0,
            character=0,
            operation="DEFINITION",
        )
        assert result["error"] == "invalid_operation"

    def test_dispatch_dict_has_correct_handler_count(self):
        """LSP_DISPATCH has exactly 7 entries — one per operation."""
        from chunkhound.mcp_server.tools.lsp_tools import LSP_DISPATCH

        assert len(LSP_DISPATCH) == 7

    def test_dispatch_handlers_are_async(self):
        """All dispatch handlers are coroutine functions."""
        import asyncio

        from chunkhound.mcp_server.tools.lsp_tools import LSP_DISPATCH

        for op, handler in LSP_DISPATCH.items():
            assert asyncio.iscoroutinefunction(handler), (
                f"Handler for '{op}' is not async"
            )


# ---------------------------------------------------------------------------
# Stats — Type Boundaries, Empty, Second Run
# ---------------------------------------------------------------------------


class TestStatsAdversarial:
    """Adversarial patterns on stats guard functions."""

    @pytest.mark.asyncio
    async def test_all_tables_missing(self):
        """Every query fails — should return all zeros, not crash."""
        from chunkhound.mcp_server.tools.stats import get_stats_impl

        services = MagicMock()
        services.provider.execute_query.side_effect = Exception("table missing")

        result = await get_stats_impl(services=services)
        assert result["files"] == 0
        assert result["chunks"] == 0
        assert result["symbols"] == 0
        assert result["symbol_edges"] == 0
        assert result["languages"] == []

    @pytest.mark.asyncio
    async def test_second_run_idempotent(self):
        """Running stats twice returns same result — no state mutation."""
        from chunkhound.mcp_server.tools.stats import get_stats_impl

        call_count = 0

        def mock_query(sql, params):
            nonlocal call_count
            call_count += 1
            return [{"count": 42}] if "COUNT" in sql else []

        services = MagicMock()
        services.provider.execute_query.side_effect = mock_query

        r1 = await get_stats_impl(services=services)
        r2 = await get_stats_impl(services=services)
        assert r1["files"] == r2["files"]
        assert r1["symbols"] == r2["symbols"]

    @pytest.mark.asyncio
    async def test_empty_query_result(self):
        """execute_query returns empty list — _safe_count returns 0."""
        from chunkhound.mcp_server.tools.stats import _safe_count

        services = MagicMock()
        services.provider.execute_query.return_value = []

        assert _safe_count(services, "SELECT COUNT(*) as count FROM files") == 0


# ---------------------------------------------------------------------------
# Registration Integrity — Critical After Module Reshuffling
# ---------------------------------------------------------------------------


class TestRegistrationIntegrity:
    """Verify TOOL_REGISTRY state after decomposition."""

    EXPECTED_TOOLS = {
        "search",
        "code_research",
        "lsp",
        "lsp_status",
        "symbol_context",
        "graph",
        "get_stats",
        "impact_cascade",
        "test_targeting",
        "cross_language_check",
    }

    def test_all_expected_tools_registered(self):
        """All 10 tools present in TOOL_REGISTRY."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        assert set(TOOL_REGISTRY.keys()) == self.EXPECTED_TOOLS

    def test_no_duplicate_registrations(self):
        """Each tool has exactly one entry — no shadowing from dual registry."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        # If there were dual registration, the count would differ
        # from unique names
        assert len(TOOL_REGISTRY) == len(self.EXPECTED_TOOLS)

    def test_code_research_requires_llm(self):
        """code_research tool must have requires_llm=True."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        tool = TOOL_REGISTRY["code_research"]
        assert tool.requires_llm is True
        assert tool.requires_embeddings is True
        assert tool.requires_reranker is True

    def test_lsp_tool_does_not_require_embeddings(self):
        """LSP tools should not require embeddings."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        for name in ("lsp", "lsp_status", "symbol_context"):
            tool = TOOL_REGISTRY[name]
            assert tool.requires_embeddings is False, (
                f"{name} should not require embeddings"
            )
            assert tool.requires_llm is False, f"{name} should not require LLM"

    def test_tool_implementations_are_from_domain_modules(self):
        """Tool implementations should come from domain modules, not __init__.py."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        for name, tool in TOOL_REGISTRY.items():
            module = tool.implementation.__module__
            assert module != "chunkhound.mcp_server.tools", (
                f"Tool '{name}' registered from __init__.py — "
                f"should be from a domain module, got {module}"
            )
