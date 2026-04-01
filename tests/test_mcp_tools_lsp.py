"""Tests for LSP MCP tools — lsp, lsp_status, and infrastructure wiring."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from chunkhound.lsp.types import (
    CallHierarchyItem,
    Diagnostic,
    HoverResult,
    Location,
    LSPCapabilityError,
    LSPError,
    LSPTransportError,
    SymbolInfo,
)
from chunkhound.mcp_server.tools import (
    TOOL_REGISTRY,
    _generate_json_schema_from_signature,
    execute_tool,
)


# ---------------------------------------------------------------------------
# Step 1: Infrastructure wiring tests
# ---------------------------------------------------------------------------


class TestLspClientPoolWiring:
    """Verify lsp_client_pool is threaded through the tool infrastructure."""

    def test_schema_gen_skips_lsp_client_pool(self) -> None:
        """_generate_json_schema_from_signature must exclude lsp_client_pool
        from the generated JSON schema (same as services, embedding_manager, etc.)."""

        async def dummy_tool(
            lsp_client_pool: Any,
            services: Any,
            query: str,
            count: int = 5,
        ) -> dict:
            ...

        schema = _generate_json_schema_from_signature(dummy_tool)

        # lsp_client_pool and services should be excluded
        assert "lsp_client_pool" not in schema["properties"]
        assert "services" not in schema["properties"]
        # query and count should be present
        assert "query" in schema["properties"]
        assert "count" in schema["properties"]

    @pytest.mark.asyncio
    async def test_execute_tool_passes_lsp_client_pool(self) -> None:
        """execute_tool must pass lsp_client_pool to tool implementations
        that declare it in their signature."""
        received_pool = None

        async def capture_pool_tool(lsp_client_pool: Any, services: Any) -> dict:
            nonlocal received_pool
            received_pool = lsp_client_pool
            return {"ok": True}

        # Temporarily register the test tool
        from chunkhound.mcp_server.tools import Tool

        TOOL_REGISTRY["_test_capture_pool"] = Tool(
            name="_test_capture_pool",
            description="test",
            parameters={},
            implementation=capture_pool_tool,
        )
        try:
            sentinel = object()
            await execute_tool(
                tool_name="_test_capture_pool",
                services=MagicMock(),
                embedding_manager=None,
                arguments={},
                lsp_client_pool=sentinel,
            )
            assert received_pool is sentinel
        finally:
            TOOL_REGISTRY.pop("_test_capture_pool", None)

    @pytest.mark.asyncio
    async def test_execute_tool_omits_pool_when_not_declared(self) -> None:
        """Tools that don't declare lsp_client_pool should not receive it."""
        called = False

        async def no_pool_tool(services: Any) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        from chunkhound.mcp_server.tools import Tool

        TOOL_REGISTRY["_test_no_pool"] = Tool(
            name="_test_no_pool",
            description="test",
            parameters={},
            implementation=no_pool_tool,
        )
        try:
            await execute_tool(
                tool_name="_test_no_pool",
                services=MagicMock(),
                embedding_manager=None,
                arguments={},
                lsp_client_pool=object(),
            )
            assert called
        finally:
            TOOL_REGISTRY.pop("_test_no_pool", None)


# ---------------------------------------------------------------------------
# Step 3: lsp tool tests
# ---------------------------------------------------------------------------


def _make_mock_pool(client: AsyncMock | None = None) -> MagicMock:
    """Create a mock LSPClientPool with a controlled client."""
    pool = MagicMock()
    if client is None:
        client = AsyncMock()
    pool.get = AsyncMock(return_value=client)
    pool._clients = {}
    return pool


def _make_mock_config(target_dir: str = "/workspace") -> MagicMock:
    """Create a mock Config with target_dir."""
    config = MagicMock()
    config.target_dir = Path(target_dir)
    return config


class TestLspTool:
    """Tests for the `lsp` MCP tool."""

    @pytest.mark.asyncio
    async def test_lsp_definition(self) -> None:
        """lsp(operation='definition') returns clean location dicts."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(
            return_value=[
                Location(
                    uri="file:///workspace/foo.py",
                    range_start_line=10,
                    range_start_char=4,
                    range_end_line=10,
                    range_end_char=20,
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 10,
                "character": 4,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert isinstance(result, dict)
        assert "results" in result
        assert len(result["results"]) == 1
        loc = result["results"][0]
        assert loc["file_path"] == "/workspace/foo.py"
        assert loc["line"] == 10
        assert loc["character"] == 4

    @pytest.mark.asyncio
    async def test_lsp_references(self) -> None:
        """lsp(operation='references') returns location list."""
        client = AsyncMock()
        client.find_references = AsyncMock(
            return_value=[
                Location("file:///workspace/a.py", 5, 0, 5, 10),
                Location("file:///workspace/b.py", 20, 2, 20, 12),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/a.py",
                "line": 5,
                "character": 0,
                "operation": "references",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_lsp_implementations(self) -> None:
        """lsp(operation='implementations') returns location list."""
        client = AsyncMock()
        client.go_to_implementation = AsyncMock(
            return_value=[
                Location("file:///workspace/impl.py", 30, 0, 45, 0),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/impl.py",
                "line": 1,
                "character": 0,
                "operation": "implementations",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_lsp_callers(self) -> None:
        """lsp(operation='callers') returns call hierarchy items."""
        client = AsyncMock()
        client.incoming_calls = AsyncMock(
            return_value=[
                CallHierarchyItem(
                    name="main",
                    kind=12,
                    uri="file:///workspace/main.py",
                    range_start_line=1,
                    range_start_char=0,
                    range_end_line=10,
                    range_end_char=0,
                    selection_range_start_line=1,
                    selection_range_start_char=4,
                    selection_range_end_line=1,
                    selection_range_end_char=8,
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 4,
                "operation": "callers",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1
        caller = result["results"][0]
        assert caller["name"] == "main"
        assert caller["kind"] == 12

    @pytest.mark.asyncio
    async def test_lsp_callees(self) -> None:
        """lsp(operation='callees') returns call hierarchy items."""
        client = AsyncMock()
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 4,
                "operation": "callees",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_lsp_hover(self) -> None:
        """lsp(operation='hover') returns hover content."""
        client = AsyncMock()
        client.hover = AsyncMock(
            return_value=HoverResult(
                contents="```python\ndef foo() -> int\n```",
                range_start_line=5,
                range_start_char=0,
                range_end_line=5,
                range_end_char=3,
            )
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 0,
                "operation": "hover",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert "contents" in result
        assert "def foo" in result["contents"]

    @pytest.mark.asyncio
    async def test_lsp_diagnostics(self) -> None:
        """lsp(operation='diagnostics') returns diagnostic list."""
        client = AsyncMock()
        client.get_diagnostics = AsyncMock(
            return_value=[
                Diagnostic(
                    range_start_line=10,
                    range_start_char=0,
                    range_end_line=10,
                    range_end_char=5,
                    severity=1,
                    message="Undefined variable 'x'",
                    source="pyright",
                    code="reportUndefinedVariable",
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 10,
                "character": 0,
                "operation": "diagnostics",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1
        diag = result["results"][0]
        assert diag["message"] == "Undefined variable 'x'"
        assert diag["severity"] == 1
        assert diag["source"] == "pyright"

    @pytest.mark.asyncio
    async def test_lsp_capability_error(self) -> None:
        """Unsupported capability returns structured error, not crash."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(
            side_effect=LSPCapabilityError("textDocument/definition", "pyright")
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "capability_not_supported"
        assert "operation" in result

    @pytest.mark.asyncio
    async def test_lsp_transport_error(self) -> None:
        """Transport failure returns structured error."""
        pool = _make_mock_pool()
        pool.get = AsyncMock(
            side_effect=LSPTransportError("Server binary not found: pyright-langserver")
        )
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "server_not_available"

    @pytest.mark.asyncio
    async def test_lsp_unknown_extension(self) -> None:
        """File with unknown extension returns structured error."""
        pool = _make_mock_pool()
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/data.xyz",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "unsupported_language"

    @pytest.mark.asyncio
    async def test_lsp_pool_not_ready(self) -> None:
        """lsp tool with None pool returns structured error."""
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=None,
            config=config,
        )

        assert result["error"] == "lsp_not_ready"


# ---------------------------------------------------------------------------
# Step 5: lsp_status tool tests
# ---------------------------------------------------------------------------


class TestLspStatusTool:
    """Tests for the `lsp_status` MCP tool."""

    @pytest.mark.asyncio
    async def test_lsp_status_mixed_pool(self) -> None:
        """lsp_status with READY and DEGRADED servers returns per-server info."""
        from chunkhound.lsp.types import LSPCapability, ServerConfig, ServerState

        # Build mock clients
        ready_client = MagicMock()
        ready_client.state = ServerState.READY
        ready_client.capabilities = frozenset(
            {LSPCapability.DEFINITION, LSPCapability.REFERENCES}
        )
        ready_client.server_info = {"name": "pyright", "version": "1.1.0"}
        ready_client.degraded_reason = None
        ready_client._config = ServerConfig(language_id="python", command="pyright-langserver")

        degraded_client = MagicMock()
        degraded_client.state = ServerState.DEGRADED
        degraded_client.capabilities = frozenset()
        degraded_client.server_info = None
        degraded_client.degraded_reason = {
            "code": "spawn_failed",
            "detail": "Command not found: rust-analyzer",
        }
        degraded_client._config = ServerConfig(language_id="rust", command="rust-analyzer")

        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): ready_client,
            ("rust", "/workspace"): degraded_client,
        }

        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=pool,
        )

        assert result["total"] == 2
        assert result["ready"] == 1
        assert result["degraded"] == 1
        assert len(result["servers"]) == 2

        # Find the ready server entry
        ready_entry = [s for s in result["servers"] if s["language_id"] == "python"][0]
        assert ready_entry["state"] == "ready"
        assert "definitionProvider" in ready_entry["capabilities"]
        assert ready_entry["server_info"] == {"name": "pyright", "version": "1.1.0"}

        # Find the degraded server entry
        degraded_entry = [s for s in result["servers"] if s["language_id"] == "rust"][0]
        assert degraded_entry["state"] == "degraded"
        assert degraded_entry["degraded_reason"]["code"] == "spawn_failed"

    @pytest.mark.asyncio
    async def test_lsp_status_empty_pool(self) -> None:
        """lsp_status with no active servers returns empty list."""
        pool = MagicMock()
        pool._clients = {}

        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=pool,
        )

        assert result["total"] == 0
        assert result["ready"] == 0
        assert result["degraded"] == 0
        assert result["servers"] == []

    @pytest.mark.asyncio
    async def test_lsp_status_pool_not_ready(self) -> None:
        """lsp_status with None pool returns structured error."""
        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=None,
        )

        assert result["error"] == "lsp_not_ready"
