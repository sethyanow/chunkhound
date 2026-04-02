"""Tests for the `lsp_status` MCP tool — pool health aggregation.

Verifies:
- Enum→string conversion (ServerState, LSPCapability)
- Per-server field contracts
- Aggregate counters (total, ready, degraded)
- Edge cases: empty pool, pool not initialized
"""

from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from chunkhound.lsp.types import LSPCapability, ServerConfig, ServerState
from chunkhound.mcp_server.tools import execute_tool


async def _call_lsp_status(pool: MagicMock | None) -> dict | str:
    return await execute_tool(
        tool_name="lsp_status",
        services=MagicMock(),
        embedding_manager=None,
        arguments={},
        lsp_client_pool=pool,
    )


def _make_client(
    state: ServerState,
    capabilities: frozenset[LSPCapability],
    language_id: str,
    command: str = "pyright-langserver",
    server_info: dict | None = None,
    degraded_reason: dict | None = None,
) -> MagicMock:
    client = MagicMock()
    client.state = state
    client.capabilities = capabilities
    client.server_info = server_info
    client.degraded_reason = degraded_reason
    client._config = ServerConfig(language_id=language_id, command=command)
    return client


class TestLspStatusTool:
    """Behavioral tests for lsp_status pool aggregation."""

    @pytest.mark.asyncio
    async def test_enum_values_serialized_as_strings(self) -> None:
        """ServerState and LSPCapability enums must appear as their .value strings,
        not as Python enum repr."""
        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): _make_client(
                state=ServerState.READY,
                capabilities=frozenset({LSPCapability.DEFINITION, LSPCapability.REFERENCES}),
                language_id="python",
                server_info={"name": "pyright", "version": "1.1.0"},
            ),
        }

        result = await _call_lsp_status(pool)

        server = result["servers"][0]
        assert server["state"] == "ready"  # string, not ServerState.READY
        assert isinstance(server["state"], str)
        for cap in server["capabilities"]:
            assert isinstance(cap, str)
        assert "definitionProvider" in server["capabilities"]
        assert "referencesProvider" in server["capabilities"]

    @pytest.mark.asyncio
    async def test_mixed_pool_aggregates_correctly(self) -> None:
        """Ready + degraded servers produce correct per-server entries and counters."""
        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): _make_client(
                state=ServerState.READY,
                capabilities=frozenset({LSPCapability.DEFINITION}),
                language_id="python",
                server_info={"name": "pyright", "version": "1.1.0"},
            ),
            ("rust", "/workspace"): _make_client(
                state=ServerState.DEGRADED,
                capabilities=frozenset(),
                language_id="rust",
                command="rust-analyzer",
                degraded_reason={"code": "spawn_failed", "detail": "Command not found"},
            ),
        }

        result = await _call_lsp_status(pool)

        # Aggregate counters
        assert result["total"] == 2
        assert result["ready"] == 1
        assert result["degraded"] == 1

        # Per-server field contracts
        ready = [s for s in result["servers"] if s["language_id"] == "python"][0]
        assert ready["state"] == "ready"
        assert ready["server_info"] == {"name": "pyright", "version": "1.1.0"}
        assert ready["degraded_reason"] is None

        degraded = [s for s in result["servers"] if s["language_id"] == "rust"][0]
        assert degraded["state"] == "degraded"
        assert degraded["degraded_reason"]["code"] == "spawn_failed"
        assert degraded["capabilities"] == []

    @pytest.mark.asyncio
    async def test_empty_pool_returns_zero_counters(self) -> None:
        """No active servers → all counters zero, empty server list."""
        pool = MagicMock()
        pool._clients = {}

        result = await _call_lsp_status(pool)

        assert result == {"total": 0, "ready": 0, "degraded": 0, "servers": []}

    @pytest.mark.asyncio
    async def test_pool_not_ready_returns_error(self) -> None:
        """None pool → structured error, not crash."""
        result = await _call_lsp_status(None)

        assert result["error"] == "lsp_not_ready"
        assert "message" in result
