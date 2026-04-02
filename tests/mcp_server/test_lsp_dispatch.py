"""Tests for LSP dispatch dict pattern in lsp_tools.py.

Verifies the dispatch mechanism routes correctly — existing tests in
tests/lsp/test_tool_lsp.py cover individual operation behavior.
"""

import pytest

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Dispatch coverage
# ---------------------------------------------------------------------------

EXPECTED_OPERATIONS = frozenset(
    {
        "definition",
        "references",
        "implementations",
        "callers",
        "callees",
        "hover",
        "diagnostics",
    }
)


def test_dispatch_dict_covers_all_operations():
    """Dispatch dict has entries for exactly the 7 LSP operations."""
    from chunkhound.mcp_server.tools.lsp_tools import LSP_DISPATCH

    assert set(LSP_DISPATCH.keys()) == EXPECTED_OPERATIONS


def test_dispatch_dict_values_are_callable():
    """Every dispatch handler is callable."""
    from chunkhound.mcp_server.tools.lsp_tools import LSP_DISPATCH

    for op, handler in LSP_DISPATCH.items():
        assert callable(handler), f"Handler for '{op}' is not callable"


# ---------------------------------------------------------------------------
# Unknown operation handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_operation_returns_error_dict():
    """lsp_impl returns error dict for unknown operation, not KeyError."""
    from unittest.mock import AsyncMock, MagicMock

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
        operation="nonexistent_operation",
    )

    assert isinstance(result, dict)
    assert result["error"] == "invalid_operation"
    assert "nonexistent_operation" in result["message"]
