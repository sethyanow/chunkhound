"""Tests for stats.py — get_stats_impl moved to its own module.

Existing tests in tests/lsp/test_tool_get_stats.py cover the tool through
execute_tool. These tests verify the module-level import and the new
missing-table graceful degradation.
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit


def test_stats_module_exports_get_stats_impl():
    """stats.py is importable and exports get_stats_impl."""
    from chunkhound.mcp_server.tools.stats import get_stats_impl

    assert callable(get_stats_impl)


def test_stats_module_exports_description():
    """stats.py exports GET_STATS_DESCRIPTION."""
    from chunkhound.mcp_server.tools.stats import GET_STATS_DESCRIPTION

    assert isinstance(GET_STATS_DESCRIPTION, str)
    assert len(GET_STATS_DESCRIPTION) > 0


@pytest.mark.asyncio
async def test_stats_handles_missing_symbol_tables():
    """get_stats_impl returns 0 for symbol/edge counts when tables don't exist."""
    from duckdb import CatalogException

    from chunkhound.mcp_server.tools.stats import get_stats_impl

    services = MagicMock()

    # files and chunks succeed, symbols and edges raise CatalogException
    def mock_execute_query(sql, params):
        if "FROM files" in sql:
            return [{"count": 10}]
        elif "FROM chunks" in sql:
            return [{"count": 50}]
        elif "FROM symbols" in sql and "GROUP BY" not in sql:
            raise CatalogException("Table 'symbols' does not exist")
        elif "FROM symbol_edges" in sql:
            raise CatalogException("Table 'symbol_edges' does not exist")
        elif "GROUP BY language" in sql:
            raise CatalogException("Table 'symbols' does not exist")
        return []

    services.provider.execute_query.side_effect = mock_execute_query

    result = await get_stats_impl(services=services)

    assert result["files"] == 10
    assert result["chunks"] == 50
    assert result["symbols"] == 0
    assert result["symbol_edges"] == 0
    assert result["languages"] == []
