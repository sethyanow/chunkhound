"""Tests for the `get_stats` MCP tool — database and LSP status summary.

Covers R5 of parent epic ch-8e7: get_stats gains graph + LSP data.

ch-nxu Step 18: stats.py is provider-agnostic — uses ``provider.get_stats()``
for files/chunks and ``provider.symbol_stats()`` for symbols/edges/languages.
No raw execute_query in the MCP tool.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from tests.lsp.mcp_tool_helpers import call_get_stats_tool


def _make_stats_services(
    *,
    provider_stats: dict | None = None,
    symbol_stats: dict | None = None,
) -> MagicMock:
    services = MagicMock()
    services.provider.get_stats.return_value = provider_stats or {
        "files": 0,
        "chunks": 0,
        "embeddings": 0,
        "providers": 0,
    }
    services.provider.symbol_stats.return_value = symbol_stats or {
        "symbol_count": 0,
        "edge_count": 0,
        "languages": [],
    }
    # Guard: the tool must not call execute_query anymore.
    services.provider.execute_query.side_effect = AssertionError(
        "stats.py must not call execute_query — use provider.get_stats/symbol_stats"
    )
    return services


class TestGetStatsTool:
    """get_stats returns file, chunk, symbol, edge counts + language breakdown."""

    @pytest.mark.asyncio
    async def test_get_stats_returns_counts(self) -> None:
        """Basic stats include file, chunk, symbol, edge counts and languages."""
        services = _make_stats_services(
            provider_stats={"files": 42, "chunks": 350, "embeddings": 0, "providers": 0},
            symbol_stats={
                "symbol_count": 1200,
                "edge_count": 3500,
                "languages": [
                    {"language": "python", "count": 800},
                    {"language": "typescript", "count": 300},
                    {"language": "rust", "count": 100},
                ],
            },
        )

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
        services = _make_stats_services(
            provider_stats={"files": 10, "chunks": 50, "embeddings": 0, "providers": 0},
            symbol_stats={"symbol_count": 100, "edge_count": 200, "languages": []},
        )

        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): MagicMock(),
            ("typescript", "/workspace"): MagicMock(),
        }

        result = await call_get_stats_tool(services=services, lsp_client_pool=pool)

        assert "lsp_servers" in result
        assert result["lsp_servers"] is not None

    @pytest.mark.asyncio
    async def test_get_stats_no_lsp(self) -> None:
        """Without lsp_client_pool, lsp_servers is null/None."""
        services = _make_stats_services(
            provider_stats={"files": 5, "chunks": 20, "embeddings": 0, "providers": 0},
            symbol_stats={"symbol_count": 0, "edge_count": 0, "languages": []},
        )

        result = await call_get_stats_tool(services=services)

        assert result["lsp_servers"] is None
