"""Adversarial stress tests for registry module."""

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools.registry import (
    TOOL_REGISTRY,
    Tool,
    _convert_paths_to_native,
    _extract_param_descriptions_from_docstring,
    _generate_json_schema_from_signature,
    _python_type_to_json_schema_type,
    execute_tool,
    register_tool,
)


@pytest.fixture(autouse=True)
def _clean_registry():
    """Isolate registry state per test."""
    saved = dict(TOOL_REGISTRY)
    TOOL_REGISTRY.clear()
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(saved)


# ── register_tool adversarial ────────────────────────────────────────────────


class TestRegisterToolAdversarial:
    """Adversarial patterns for register_tool."""

    def test_empty_description(self):
        """Empty: empty string description should still register."""

        @register_tool(description="")
        async def empty_desc(query: str) -> dict:
            return {}

        assert TOOL_REGISTRY["empty_desc"].description == ""

    def test_no_docstring(self):
        """Empty: function with no docstring — schema should still generate."""

        @register_tool(description="No docs")
        async def no_docs(query: str) -> dict:
            return {}

        tool = TOOL_REGISTRY["no_docs"]
        assert "query" in tool.parameters["properties"]

    def test_no_params(self):
        """Singular: function with zero tool-visible params."""

        @register_tool(description="No params")
        async def bare() -> dict:
            return {}

        tool = TOOL_REGISTRY["bare"]
        assert tool.parameters["properties"] == {}
        assert tool.parameters["required"] == []

    def test_duplicate_registration_overwrites(self):
        """Redundant: registering same name twice — last wins."""

        @register_tool(description="First")
        async def dup_tool(query: str) -> dict:
            return {"v": 1}

        @register_tool(description="Second", name="dup_tool")
        async def dup_tool_v2(query: str) -> dict:
            return {"v": 2}

        assert TOOL_REGISTRY["dup_tool"].description == "Second"

    def test_unicode_tool_name(self):
        """Encoding boundaries: non-ASCII tool name."""

        @register_tool(description="Unicode", name="búsqueda")
        async def search_es(query: str) -> dict:
            return {}

        assert "búsqueda" in TOOL_REGISTRY

    def test_only_infrastructure_params(self):
        """Edge: function whose only params are all infrastructure — empty schema."""

        @register_tool(description="Infra only")
        async def infra_only(services: object, embedding_manager: object) -> dict:
            return {}

        tool = TOOL_REGISTRY["infra_only"]
        assert tool.parameters["properties"] == {}
        assert tool.parameters["required"] == []


# ── execute_tool adversarial ─────────────────────────────────────────────────


class TestExecuteToolAdversarial:
    """Adversarial patterns for execute_tool."""

    @pytest.mark.asyncio
    async def test_empty_arguments(self):
        """Empty: tool expecting no args called with empty arguments dict."""

        @register_tool(description="No args")
        async def no_args() -> dict:
            return {"ok": True}

        result = await execute_tool(
            tool_name="no_args",
            services=None,
            embedding_manager=None,
            arguments={},
        )
        assert result == {"ok": True}

    @pytest.mark.asyncio
    async def test_extra_arguments_ignored(self):
        """Semantically hostile: extra keys in arguments dict — should be ignored."""

        @register_tool(description="One param")
        async def one_param(query: str) -> dict:
            return {"q": query}

        result = await execute_tool(
            tool_name="one_param",
            services=None,
            embedding_manager=None,
            arguments={"query": "test", "bogus_key": "junk", "another": 42},
        )
        assert result == {"q": "test"}

    @pytest.mark.asyncio
    async def test_tool_returning_non_dict(self):
        """Type boundary: tool returns a string, not a dict."""

        @register_tool(description="String return")
        async def str_return(query: str) -> dict:
            return "raw string"  # type: ignore[return-value]

        result = await execute_tool(
            tool_name="str_return",
            services=None,
            embedding_manager=None,
            arguments={"query": "x"},
        )
        assert result == {"result": "raw string"}

    @pytest.mark.asyncio
    async def test_tool_returning_none(self):
        """Type boundary: tool returns None."""

        @register_tool(description="None return")
        async def none_return(query: str) -> dict:
            return None  # type: ignore[return-value]

        result = await execute_tool(
            tool_name="none_return",
            services=None,
            embedding_manager=None,
            arguments={"query": "x"},
        )
        # None has no __dict__, not a dict → {"result": None}
        assert result == {"result": None}

    @pytest.mark.asyncio
    async def test_second_run_idempotent(self):
        """The 'second run' test: executing same tool twice returns same result."""

        @register_tool(description="Idempotent")
        async def idem(query: str) -> dict:
            return {"q": query}

        args = {"query": "hello"}
        r1 = await execute_tool("idem", None, None, args)
        r2 = await execute_tool("idem", None, None, args)
        assert r1 == r2


# ── schema generation adversarial ────────────────────────────────────────────


class TestSchemaGenerationAdversarial:
    """Adversarial patterns for schema generation helpers."""

    def test_no_type_annotation(self):
        """Empty: param with no type hint → defaults to 'object'."""

        def no_hints(x) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(no_hints)
        assert schema["properties"]["x"]["type"] == "object"

    def test_bool_type(self):
        """Type boundary: bool is a subtype of int in Python — schema should say boolean."""

        def with_bool(flag: bool) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(with_bool)
        assert schema["properties"]["flag"]["type"] == "boolean"

    def test_literal_type(self):
        """Type boundary: Literal enum generates correct schema."""
        from typing import Literal

        def with_literal(mode: Literal["fast", "slow"]) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(with_literal)
        prop = schema["properties"]["mode"]
        assert prop["type"] == "string"
        assert set(prop["enum"]) == {"fast", "slow"}

    def test_none_type(self):
        """Type boundary: None type annotation."""
        schema = _python_type_to_json_schema_type(None)
        assert schema["type"] == "null"

    def test_complex_nested_type(self):
        """Dense: nested generic type falls to object default."""

        def nested(data: dict[str, list[int]]) -> dict:
            return {}

        schema = _generate_json_schema_from_signature(nested)
        # dict origin → "object" (no deeper inspection)
        assert schema["properties"]["data"]["type"] == "object"


# ── docstring parsing adversarial ────────────────────────────────────────────


class TestDocstringParsingAdversarial:
    """Adversarial patterns for docstring extraction."""

    def test_no_docstring(self):
        def no_doc(x: str) -> dict:
            return {}

        no_doc.__doc__ = None
        result = _extract_param_descriptions_from_docstring(no_doc)
        assert result == {}

    def test_empty_docstring(self):
        def empty_doc(x: str) -> dict:
            """"""
            return {}

        result = _extract_param_descriptions_from_docstring(empty_doc)
        assert result == {}

    def test_docstring_without_args_section(self):
        def no_args_section(x: str) -> dict:
            """Just a summary line."""
            return {}

        result = _extract_param_descriptions_from_docstring(no_args_section)
        assert result == {}

    def test_colon_in_description(self):
        """Encoding boundary: param description containing colons."""

        def colon_desc(query: str) -> dict:
            """Search.

            Args:
                query: The query string: supports regex: and globs
            """
            return {}

        result = _extract_param_descriptions_from_docstring(colon_desc)
        assert result["query"] == "The query string: supports regex: and globs"


# ── _convert_paths_to_native adversarial ─────────────────────────────────────


class TestConvertPathsAdversarial:
    """Adversarial patterns for path conversion."""

    def test_empty_list(self):
        assert _convert_paths_to_native([]) == []

    def test_missing_file_path_key(self):
        """Sparse: result dict without file_path key — should not crash."""
        results = [{"content": "hello", "start_line": 1}]
        converted = _convert_paths_to_native(results)
        assert "file_path" not in converted[0]

    def test_none_file_path(self):
        """Type boundary: file_path is None — falsy, should skip."""
        results = [{"file_path": None, "content": "x"}]
        converted = _convert_paths_to_native(results)
        assert converted[0]["file_path"] is None

    def test_empty_string_file_path(self):
        """Empty: file_path is empty string — falsy, should skip."""
        results = [{"file_path": "", "content": "x"}]
        converted = _convert_paths_to_native(results)
        assert converted[0]["file_path"] == ""
