"""Tests for MCP tool infrastructure wiring — lsp_client_pool injection, schema generation."""

from typing import Any
from unittest.mock import MagicMock

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools import (
    TOOL_REGISTRY,
    Tool,
    _generate_json_schema_from_signature,
    execute_tool,
)


class TestLspClientPoolWiring:
    """Verify lsp_client_pool is threaded through the tool infrastructure."""

    def test_schema_gen_excludes_infrastructure_params(self) -> None:
        """Infrastructure params (lsp_client_pool, services, config, etc.) must not
        appear in the generated JSON schema — they're injected, not user-provided."""

        async def dummy_tool(
            lsp_client_pool: Any,
            services: Any,
            query: str,
            count: int = 5,
        ) -> dict:
            ...

        schema = _generate_json_schema_from_signature(dummy_tool)

        assert "lsp_client_pool" not in schema["properties"]
        assert "services" not in schema["properties"]
        assert "query" in schema["properties"]
        assert "count" in schema["properties"]

    @pytest.mark.asyncio
    async def test_pool_injected_when_declared(self) -> None:
        """Tools declaring lsp_client_pool receive the pool instance via
        signature-driven injection."""
        received_pool = None

        async def capture_pool_tool(lsp_client_pool: Any, services: Any) -> dict:
            nonlocal received_pool
            received_pool = lsp_client_pool
            return {"ok": True}

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
    async def test_pool_omitted_when_not_declared(self) -> None:
        """Tools that don't declare lsp_client_pool must not receive it —
        signature-driven injection only injects what the tool asks for."""
        called = False

        async def no_pool_tool(services: Any) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

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
