"""Tests for the tool registry module."""

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools.registry import (
    Tool,
    TOOL_REGISTRY,
    _generate_json_schema_from_signature,
    execute_tool,
    register_tool,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Clear the registry before and after each test."""
    saved = dict(TOOL_REGISTRY)
    TOOL_REGISTRY.clear()
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(saved)


class TestRegisterTool:
    """Tests for the register_tool decorator."""

    def test_registers_function_in_registry(self):
        @register_tool(description="A test tool")
        async def my_tool(query: str) -> dict:
            """Search for something.

            Args:
                query: The search query
            """
            return {"result": query}

        assert "my_tool" in TOOL_REGISTRY
        tool = TOOL_REGISTRY["my_tool"]
        assert isinstance(tool, Tool)
        assert tool.name == "my_tool"
        assert tool.description == "A test tool"
        assert tool.implementation is my_tool

    def test_generates_json_schema_parameters(self):
        @register_tool(description="Tool with params")
        async def typed_tool(name: str, count: int, label: str | None = None) -> dict:
            """A typed tool.

            Args:
                name: The name
                count: How many
                label: Optional label
            """
            return {}

        tool = TOOL_REGISTRY["typed_tool"]
        params = tool.parameters
        assert params["type"] == "object"
        assert "name" in params["properties"]
        assert params["properties"]["name"]["type"] == "string"
        assert "count" in params["properties"]
        assert params["properties"]["count"]["type"] == "integer"
        assert "label" in params["properties"]
        assert "name" in params["required"]
        assert "count" in params["required"]
        # label has a default, so not required
        assert "label" not in params["required"]

    def test_custom_name_overrides_function_name(self):
        @register_tool(description="Custom named", name="custom_name")
        async def internal_name(query: str) -> dict:
            return {}

        assert "custom_name" in TOOL_REGISTRY
        assert "internal_name" not in TOOL_REGISTRY

    def test_skips_infrastructure_params_in_schema(self):
        @register_tool(description="Tool with infra params")
        async def infra_tool(
            services: object,
            embedding_manager: object,
            query: str,
            page_size: int = 10,
        ) -> dict:
            """Tool.

            Args:
                query: The query
                page_size: Results per page
            """
            return {}

        params = TOOL_REGISTRY["infra_tool"].parameters
        # Infrastructure params excluded from schema
        assert "services" not in params["properties"]
        assert "embedding_manager" not in params["properties"]
        # Tool params included
        assert "query" in params["properties"]
        assert "page_size" in params["properties"]


class TestExecuteTool:
    """Tests for execute_tool dispatch."""

    @pytest.mark.asyncio
    async def test_dispatches_to_registered_function(self):
        @register_tool(description="Echo tool")
        async def echo(query: str) -> dict:
            return {"echo": query}

        result = await execute_tool(
            tool_name="echo",
            services=None,
            embedding_manager=None,
            arguments={"query": "hello"},
        )
        assert result == {"echo": "hello"}

    @pytest.mark.asyncio
    async def test_passes_infrastructure_params(self):
        captured = {}

        @register_tool(description="Capture tool")
        async def capture(services: object, query: str) -> dict:
            captured["services"] = services
            captured["query"] = query
            return {"ok": True}

        sentinel = object()
        result = await execute_tool(
            tool_name="capture",
            services=sentinel,
            embedding_manager=None,
            arguments={"query": "test"},
        )
        assert result == {"ok": True}
        assert captured["services"] is sentinel
        assert captured["query"] == "test"

    @pytest.mark.asyncio
    async def test_raises_valueerror_for_unknown_tool(self):
        with pytest.raises(ValueError, match="Unknown tool: nonexistent"):
            await execute_tool(
                tool_name="nonexistent",
                services=None,
                embedding_manager=None,
                arguments={},
            )


class TestGenerateJsonSchema:
    """Tests for _generate_json_schema_from_signature."""

    def test_basic_typed_function(self):
        def func(name: str, count: int, ratio: float) -> dict:
            """A function.

            Args:
                name: The name
                count: The count
                ratio: The ratio
            """
            return {}

        schema = _generate_json_schema_from_signature(func)
        assert schema["type"] == "object"
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["count"]["type"] == "integer"
        assert schema["properties"]["ratio"]["type"] == "number"
        assert set(schema["required"]) == {"name", "count", "ratio"}

    def test_optional_params_not_required(self):
        def func(name: str, label: str | None = None) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(func)
        assert "name" in schema["required"]
        assert "label" not in schema["required"]

    def test_includes_docstring_descriptions(self):
        def func(query: str) -> dict:
            """Search.

            Args:
                query: The search query to execute
            """
            return {}

        schema = _generate_json_schema_from_signature(func)
        assert schema["properties"]["query"]["description"] == "The search query to execute"

    def test_includes_default_values(self):
        def func(page_size: int = 10) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(func)
        assert schema["properties"]["page_size"]["default"] == 10
