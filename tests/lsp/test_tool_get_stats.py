"""Tests for the `get_stats` MCP tool — database and LSP status summary.

Covers R5 of parent epic ch-8e7: get_stats gains graph + LSP data.
"""

import pytest

pytestmark = pytest.mark.unit

from tests.lsp.mcp_tool_helpers import call_get_stats_tool, make_mock_services


class TestGetStatsTool:
    """get_stats returns file, chunk, symbol, edge counts + language breakdown."""

    @pytest.mark.asyncio
    async def test_get_stats_returns_counts(self) -> None:
        """Basic stats include file, chunk, symbol, edge counts and languages."""
        services = make_mock_services([
            # files count
            [{"count": 42}],
            # chunks count
            [{"count": 350}],
            # symbols count
            [{"count": 1200}],
            # symbol_edges count
            [{"count": 3500}],
            # language breakdown
            [
                {"language": "python", "count": 800},
                {"language": "typescript", "count": 300},
                {"language": "rust", "count": 100},
            ],
        ])

        result = await call_get_stats_tool(services=services)

        assert result["files"] == 42
        assert result["chunks"] == 350
        assert result["symbols"] == 1200
        assert result["symbol_edges"] == 3500
        assert len(result["languages"]) == 3
        assert result["languages"][0]["language"] == "python"
        assert result["languages"][0]["count"] == 800

    @pytest.mark.asyncio
    async def test_get_stats_with_lsp(self) -> None:
        """When lsp_client_pool provided, result includes lsp_servers summary."""
        from unittest.mock import MagicMock

        services = make_mock_services([
            [{"count": 10}],
            [{"count": 50}],
            [{"count": 100}],
            [{"count": 200}],
            [],  # no languages
        ])

        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): MagicMock(),
            ("typescript", "/workspace"): MagicMock(),
        }

        result = await call_get_stats_tool(
            services=services, lsp_client_pool=pool,
        )

        assert "lsp_servers" in result
        assert result["lsp_servers"] is not None

    @pytest.mark.asyncio
    async def test_get_stats_no_lsp(self) -> None:
        """Without lsp_client_pool, lsp_servers is null/None."""
        services = make_mock_services([
            [{"count": 5}],
            [{"count": 20}],
            [{"count": 0}],
            [{"count": 0}],
            [],
        ])

        result = await call_get_stats_tool(services=services)

        assert result["lsp_servers"] is None
