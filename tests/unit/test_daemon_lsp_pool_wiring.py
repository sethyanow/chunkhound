"""Regression test: daemon tool dispatch must pass lsp_client_pool to handle_tool_call.

The stdio path (stdio.py) passes lsp_client_pool=self._lsp_pool to handle_tool_call().
The daemon path (daemon/server.py) was missing this parameter, causing all LSP tools
to return "lsp_not_ready" through the daemon proxy even when the pool was constructed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_daemon_handle_tools_call_passes_lsp_client_pool() -> None:
    """_handle_tools_call must pass lsp_client_pool=self._lsp_pool to handle_tool_call."""
    from chunkhound.daemon.server import ChunkHoundDaemon

    # Build a minimal mock daemon with the attributes _handle_tools_call reads
    daemon = MagicMock(spec=ChunkHoundDaemon)
    daemon._lsp_pool = MagicMock(name="lsp_pool_sentinel")
    daemon.embedding_manager = None
    daemon.llm_manager = None
    daemon.debug_mode = False
    daemon.config = MagicMock()
    daemon._scan_progress = {}
    daemon._initialization_complete = MagicMock()
    daemon.ensure_services = AsyncMock(return_value=MagicMock())

    fake_msg = {
        "id": 1,
        "params": {"name": "lsp_status", "arguments": {}},
    }

    mock_result = [MagicMock(type="text", text="ok")]

    with patch(
        "chunkhound.daemon.server.handle_tool_call",
        new_callable=AsyncMock,
        return_value=mock_result,
    ) as mock_handle:
        # Call the real method on our mock self
        await ChunkHoundDaemon._handle_tools_call(daemon, fake_msg)

        # Verify lsp_client_pool was passed
        mock_handle.assert_called_once()
        call_kwargs = mock_handle.call_args.kwargs
        assert "lsp_client_pool" in call_kwargs, (
            f"handle_tool_call called without lsp_client_pool. "
            f"Got kwargs: {sorted(call_kwargs.keys())}"
        )
        assert call_kwargs["lsp_client_pool"] is daemon._lsp_pool, (
            "lsp_client_pool should be self._lsp_pool"
        )
