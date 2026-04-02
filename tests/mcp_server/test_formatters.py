"""Tests for chunkhound.mcp_server.tools.formatters — result formatting helpers."""

import pytest

from chunkhound.lsp.types import CallHierarchyItem, Diagnostic, Location
from chunkhound.mcp_server.tools.formatters import (
    call_item_to_dict,
    diagnostic_to_dict,
    format_edge,
    format_node,
    location_to_dict,
)

pytestmark = pytest.mark.unit


class TestFormatNode:
    """format_node maps a raw DB dict to a clean response dict."""

    def test_maps_all_fields(self) -> None:
        row = {
            "fqn": "module::MyClass",
            "name": "MyClass",
            "kind": "class",
            "file_path": "src/module.py",
            "depth": 2,
        }
        result = format_node(row)
        assert result == {
            "fqn": "module::MyClass",
            "name": "MyClass",
            "kind": "class",
            "file_path": "src/module.py",
            "depth": 2,
        }

    def test_preserves_zero_depth(self) -> None:
        row = {
            "fqn": "root::func",
            "name": "func",
            "kind": "function",
            "file_path": "root.py",
            "depth": 0,
        }
        assert format_node(row)["depth"] == 0


class TestFormatEdge:
    """format_edge renames from_fqn/to_fqn to from_symbol/to_symbol."""

    def test_renames_fqn_fields(self) -> None:
        row = {
            "from_fqn": "a::caller",
            "to_fqn": "b::callee",
            "edge_kind": "calls",
            "from_file": "a.py",
            "to_file": "b.py",
        }
        result = format_edge(row)
        assert result == {
            "from_symbol": "a::caller",
            "to_symbol": "b::callee",
            "edge_kind": "calls",
            "from_file": "a.py",
            "to_file": "b.py",
        }

    def test_no_fqn_keys_in_output(self) -> None:
        row = {
            "from_fqn": "x",
            "to_fqn": "y",
            "edge_kind": "references",
            "from_file": "x.py",
            "to_file": "y.py",
        }
        result = format_edge(row)
        assert "from_fqn" not in result
        assert "to_fqn" not in result


class TestLocationToDict:
    """location_to_dict converts a Location dataclass to a clean dict."""

    def test_converts_file_uri(self) -> None:
        loc = Location(
            uri="file:///src/module.py",
            range_start_line=10,
            range_start_char=4,
            range_end_line=10,
            range_end_char=20,
        )
        result = location_to_dict(loc)
        assert result == {
            "file_path": "/src/module.py",
            "line": 10,
            "character": 4,
            "end_line": 10,
            "end_character": 20,
        }

    def test_passthrough_non_file_uri(self) -> None:
        loc = Location(
            uri="untitled:buffer",
            range_start_line=0,
            range_start_char=0,
            range_end_line=0,
            range_end_char=0,
        )
        result = location_to_dict(loc)
        assert result["file_path"] == "untitled:buffer"


class TestCallItemToDict:
    """call_item_to_dict converts a CallHierarchyItem to a clean dict."""

    def test_maps_all_fields(self) -> None:
        item = CallHierarchyItem(
            name="my_function",
            kind=12,
            uri="file:///src/mod.py",
            range_start_line=5,
            range_start_char=0,
            range_end_line=15,
            range_end_char=0,
            selection_range_start_line=5,
            selection_range_start_char=4,
            selection_range_end_line=5,
            selection_range_end_char=15,
            detail="mod.py",
        )
        result = call_item_to_dict(item)
        assert result["name"] == "my_function"
        assert result["kind"] == 12
        assert result["file_path"] == "/src/mod.py"
        assert result["line"] == 5
        assert result["detail"] == "mod.py"

    def test_none_detail(self) -> None:
        item = CallHierarchyItem(
            name="f",
            kind=6,
            uri="file:///a.py",
            range_start_line=0,
            range_start_char=0,
            range_end_line=0,
            range_end_char=0,
            selection_range_start_line=0,
            selection_range_start_char=0,
            selection_range_end_line=0,
            selection_range_end_char=0,
        )
        result = call_item_to_dict(item)
        assert result["detail"] is None


class TestDiagnosticToDict:
    """diagnostic_to_dict converts a Diagnostic to a clean dict."""

    def test_maps_all_fields(self) -> None:
        diag = Diagnostic(
            range_start_line=42,
            range_start_char=0,
            range_end_line=42,
            range_end_char=10,
            severity=1,
            message="Undefined variable",
            source="pyright",
            code="reportUndefinedVariable",
        )
        result = diagnostic_to_dict(diag)
        assert result == {
            "line": 42,
            "character": 0,
            "end_line": 42,
            "end_character": 10,
            "severity": 1,
            "message": "Undefined variable",
            "source": "pyright",
            "code": "reportUndefinedVariable",
        }

    def test_optional_fields_none(self) -> None:
        diag = Diagnostic(
            range_start_line=1,
            range_start_char=0,
            range_end_line=1,
            range_end_char=5,
            severity=2,
            message="Warning",
        )
        result = diagnostic_to_dict(diag)
        assert result["source"] is None
        assert result["code"] is None
