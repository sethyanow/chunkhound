"""Tests for stats.py — get_stats_impl moved to its own module.

ch-nxu Step 18: stats.py is provider-agnostic — graceful degradation when
the provider raises on symbol_stats/get_stats (pre-population state).
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
    """get_stats_impl returns 0 for symbol/edge counts when symbol_stats raises."""
    from chunkhound.mcp_server.tools.stats import get_stats_impl

    services = MagicMock()
    services.provider.get_stats.return_value = {
        "files": 10,
        "chunks": 50,
        "embeddings": 0,
        "providers": 0,
    }
    # Symbol tables missing — symbol_stats raises
    services.provider.symbol_stats.side_effect = RuntimeError("symbols table missing")

    result = await get_stats_impl(services=services)

    assert result["files"] == 10
    assert result["chunks"] == 50
    assert result["symbols"] == 0
    assert result["symbol_edges"] == 0
    assert result["languages"] == []


@pytest.mark.asyncio
async def test_stats_handles_get_stats_failure():
    """get_stats_impl returns 0 for files/chunks when provider.get_stats raises."""
    from chunkhound.mcp_server.tools.stats import get_stats_impl

    services = MagicMock()
    services.provider.get_stats.side_effect = RuntimeError("db locked")
    services.provider.symbol_stats.return_value = {
        "symbol_count": 5,
        "edge_count": 7,
        "languages": [{"language": "python", "count": 5}],
    }

    result = await get_stats_impl(services=services)

    assert result["files"] == 0
    assert result["chunks"] == 0
    assert result["symbols"] == 5
    assert result["symbol_edges"] == 7
    assert result["languages"] == [{"language": "python", "count": 5}]
