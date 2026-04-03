"""Tests for LSP dispatch dict pattern in lsp_tools.py.

Verifies the dispatch mechanism routes correctly — existing tests in
tests/lsp/test_tool_lsp.py cover individual operation behavior.
"""

from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# 1-based input conversion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lsp_impl_converts_one_based_input_to_zero_based():
    """Regression: lsp_impl accepts 1-based line/character and subtracts 1 before dispatch.

    Users pass line=32, character=7 (1-based). The LSP protocol expects 0-based,
    so the handler must receive line=31, character=6.
    """
    from chunkhound.mcp_server.tools.lsp_tools import lsp_impl

    captured_args: list[tuple] = []

    async def fake_definition(uri: str, line: int, char: int):
        captured_args.append((line, char))
        return []

    mock_client = AsyncMock()
    mock_client.go_to_definition = fake_definition

    pool = MagicMock()
    pool.get = AsyncMock(return_value=mock_client)

    config = MagicMock()
    config.target_dir = MagicMock()
    config.target_dir.__bool__ = MagicMock(return_value=True)
    config.target_dir.__str__ = MagicMock(return_value="/workspace")

    await lsp_impl(
        lsp_client_pool=pool,
        services=MagicMock(),
        config=config,
        file="/workspace/foo.py",
        line=32,
        character=7,
        operation="definition",
    )

    assert len(captured_args) == 1, "Handler should have been called once"
    actual_line, actual_char = captured_args[0]
    assert actual_line == 31, f"Expected 0-based line 31, got {actual_line}"
    assert actual_char == 6, f"Expected 0-based character 6, got {actual_char}"
