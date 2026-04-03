"""Tests for the `lsp` MCP tool — behavioral verification of data transformations.

Each test verifies a concrete behavior of the lsp tool:
- How LSP dataclasses (Location, CallHierarchyItem, HoverResult, Diagnostic)
  are transformed into response dicts
- URI → filesystem path stripping
- Error classification and response shape
- Response shape contracts (all expected keys present)
"""

from unittest.mock import AsyncMock, MagicMock

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
)
from tests.lsp.mcp_tool_helpers import call_lsp_tool, make_mock_config, make_mock_pool


# ---------------------------------------------------------------------------
# Response contract: Location-returning operations
# ---------------------------------------------------------------------------


LOCATION_RESPONSE_KEYS = {"file_path", "line", "character", "end_line", "end_character"}


def _make_location(
    uri: str = "file:///workspace/foo.py",
    start_line: int = 10,
    start_char: int = 4,
    end_line: int = 10,
    end_char: int = 20,
) -> Location:
    return Location(
        uri=uri,
        range_start_line=start_line,
        range_start_char=start_char,
        range_end_line=end_line,
        range_end_char=end_char,
    )


def _make_call_item(
    name: str = "caller",
    kind: int = 12,
    uri: str = "file:///workspace/caller.py",
) -> CallHierarchyItem:
    return CallHierarchyItem(
        name=name,
        kind=kind,
        uri=uri,
        range_start_line=20,
        range_start_char=0,
        range_end_line=30,
        range_end_char=0,
        selection_range_start_line=20,
        selection_range_start_char=4,
        selection_range_end_line=20,
        selection_range_end_char=15,
    )


CALL_ITEM_RESPONSE_KEYS = {
    "name", "kind", "file_path", "line", "character",
    "end_line", "end_character", "detail",
}


class TestLocationOperations:
    """definition, references, implementations all return {results: [location_dict]}.
    Verify the data transformation is correct for each."""

    @pytest.mark.asyncio
    async def test_definition_transforms_location_fields(self) -> None:
        """Location.uri → file_path (stripped), range fields renamed to flat keys."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(return_value=[
            _make_location("file:///workspace/foo.py", 10, 4, 10, 20),
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(pool=pool, config=config, operation="definition")

        loc = result["results"][0]
        assert set(loc.keys()) == LOCATION_RESPONSE_KEYS
        assert loc["file_path"] == "/workspace/foo.py"  # URI stripped
        assert loc["line"] == 11  # 0-based 10 → 1-based 11
        assert loc["character"] == 5  # 0-based 4 → 1-based 5
        assert loc["end_line"] == 11
        assert loc["end_character"] == 21

    @pytest.mark.asyncio
    async def test_references_transforms_all_locations(self) -> None:
        """Multiple locations each get the same field transformation."""
        client = AsyncMock()
        client.find_references = AsyncMock(return_value=[
            _make_location("file:///workspace/a.py", 5, 0, 5, 10),
            _make_location("file:///workspace/b.py", 20, 2, 20, 12),
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config,
            file="/workspace/a.py", line=5, character=0, operation="references",
        )

        assert len(result["results"]) == 2
        for loc in result["results"]:
            assert set(loc.keys()) == LOCATION_RESPONSE_KEYS
        assert result["results"][0]["file_path"] == "/workspace/a.py"
        assert result["results"][1]["file_path"] == "/workspace/b.py"

    @pytest.mark.asyncio
    async def test_implementations_transforms_location(self) -> None:
        """Same field mapping as definition/references."""
        client = AsyncMock()
        client.go_to_implementation = AsyncMock(return_value=[
            _make_location("file:///workspace/impl.py", 30, 0, 45, 0),
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config,
            file="/workspace/impl.py", line=1, character=0, operation="implementations",
        )

        assert len(result["results"]) == 1
        loc = result["results"][0]
        assert set(loc.keys()) == LOCATION_RESPONSE_KEYS
        assert loc["file_path"] == "/workspace/impl.py"
        assert loc["line"] == 31  # 0-based 30 → 1-based 31
        assert loc["end_line"] == 46  # 0-based 45 → 1-based 46


class TestCallHierarchyOperations:
    """callers and callees return {results: [call_item_dict]}.
    Verify the CallHierarchyItem → dict transformation."""

    @pytest.mark.asyncio
    async def test_callers_transforms_call_hierarchy_item(self) -> None:
        """CallHierarchyItem fields mapped: name, kind, uri→file_path, ranges."""
        client = AsyncMock()
        client.incoming_calls = AsyncMock(return_value=[
            _make_call_item("main", 12, "file:///workspace/main.py"),
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, line=5, character=4, operation="callers",
        )

        assert len(result["results"]) == 1
        caller = result["results"][0]
        assert set(caller.keys()) == CALL_ITEM_RESPONSE_KEYS
        assert caller["name"] == "main"
        assert caller["kind"] == 12
        assert caller["file_path"] == "/workspace/main.py"  # URI stripped

    @pytest.mark.asyncio
    async def test_callees_transforms_call_hierarchy_item(self) -> None:
        """Callees use the same transformation as callers — verify field contract."""
        client = AsyncMock()
        client.outgoing_calls = AsyncMock(return_value=[
            _make_call_item("helper", 12, "file:///workspace/util.py"),
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, line=5, character=4, operation="callees",
        )

        assert len(result["results"]) == 1
        callee = result["results"][0]
        assert set(callee.keys()) == CALL_ITEM_RESPONSE_KEYS
        assert callee["name"] == "helper"
        assert callee["file_path"] == "/workspace/util.py"

    @pytest.mark.asyncio
    async def test_callees_empty_list(self) -> None:
        """Empty callee list returns {results: []} — no error."""
        client = AsyncMock()
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, operation="callees",
        )

        assert result["results"] == []
        assert "error" not in result


class TestHoverOperation:
    """hover has a different response shape: {contents, range} instead of {results}."""

    @pytest.mark.asyncio
    async def test_hover_returns_contents_and_range(self) -> None:
        """HoverResult → {contents: str, range: {start_line, start_character, ...}}."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(
            contents="```python\ndef foo() -> int\n```",
            range_start_line=5,
            range_start_char=0,
            range_end_line=5,
            range_end_char=3,
        ))
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, line=5, character=0, operation="hover",
        )

        assert set(result.keys()) == {"contents", "range"}
        assert "def foo" in result["contents"]
        assert result["range"] == {
            "start_line": 6,  # 0-based 5 → 1-based 6
            "start_character": 1,  # 0-based 0 → 1-based 1
            "end_line": 6,
            "end_character": 4,
        }

    @pytest.mark.asyncio
    async def test_hover_null_returns_null_fields(self) -> None:
        """When hover returns None, both contents and range are null."""
        client = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, operation="hover",
        )

        assert result == {"contents": None, "range": None}


class TestDiagnosticsOperation:
    """diagnostics returns {results: [diagnostic_dict]} with full field contract."""

    @pytest.mark.asyncio
    async def test_diagnostics_transforms_all_fields(self) -> None:
        """Diagnostic → dict with severity, message, source, code, range fields."""
        client = AsyncMock()
        client.get_diagnostics = AsyncMock(return_value=[
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
        ])
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, line=10, character=0, operation="diagnostics",
        )

        diag = result["results"][0]
        expected_keys = {
            "line", "character", "end_line", "end_character",
            "severity", "message", "source", "code",
        }
        assert set(diag.keys()) == expected_keys
        assert diag["message"] == "Undefined variable 'x'"
        assert diag["severity"] == 1
        assert diag["source"] == "pyright"
        assert diag["code"] == "reportUndefinedVariable"


class TestLspErrorHandling:
    """Error classification: each LSP exception type → specific error key."""

    @pytest.mark.asyncio
    async def test_capability_error_classified(self) -> None:
        """LSPCapabilityError → {error: 'capability_not_supported', operation: ...}."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(
            side_effect=LSPCapabilityError("textDocument/definition", "pyright"),
        )
        pool = make_mock_pool(client)
        config = make_mock_config()

        result = await call_lsp_tool(pool=pool, config=config, operation="definition")

        assert result["error"] == "capability_not_supported"
        assert "operation" in result
        assert "message" in result

    @pytest.mark.asyncio
    async def test_transport_error_classified(self) -> None:
        """LSPTransportError → {error: 'server_not_available'}."""
        pool = make_mock_pool()
        pool.get = AsyncMock(
            side_effect=LSPTransportError("Server binary not found: pyright-langserver"),
        )
        config = make_mock_config()

        result = await call_lsp_tool(pool=pool, config=config, operation="definition")

        assert result["error"] == "server_not_available"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_unknown_extension_classified(self) -> None:
        """File with unrecognized extension → {error: 'unsupported_language'}."""
        pool = make_mock_pool()
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=pool, config=config, file="/workspace/data.xyz", operation="definition",
        )

        assert result["error"] == "unsupported_language"
        assert "message" in result

    @pytest.mark.asyncio
    async def test_pool_not_ready_classified(self) -> None:
        """None pool → {error: 'lsp_not_ready'}."""
        config = make_mock_config()

        result = await call_lsp_tool(
            pool=None, config=config, operation="definition",
        )

        assert result["error"] == "lsp_not_ready"
        assert "message" in result
