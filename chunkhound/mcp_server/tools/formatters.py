"""Result formatting helpers for MCP tool responses."""

from typing import Any
from urllib.parse import unquote, urlparse


def _uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    if uri.startswith("file://"):
        parsed = urlparse(uri)
        return unquote(parsed.path)
    return uri


def format_node(row: dict[str, Any]) -> dict[str, Any]:
    """Format a raw DB row into a clean node response dict."""
    return {
        "fqn": row["fqn"],
        "name": row["name"],
        "kind": row["kind"],
        "file_path": row["file_path"],
        "depth": row["depth"],
    }


def format_edge(row: dict[str, Any]) -> dict[str, Any]:
    """Format a raw DB edge row, renaming fqn fields to symbol."""
    return {
        "from_symbol": row["from_fqn"],
        "to_symbol": row["to_fqn"],
        "edge_kind": row["edge_kind"],
        "from_file": row["from_file"],
        "to_file": row["to_file"],
    }


def location_to_dict(loc: Any) -> dict[str, Any]:
    """Convert a Location dataclass to a clean dict.

    LSP uses 0-based line/character; output is 1-based for user consumption.
    """
    return {
        "file_path": _uri_to_path(loc.uri),
        "line": loc.range_start_line + 1,
        "character": loc.range_start_char + 1,
        "end_line": loc.range_end_line + 1,
        "end_character": loc.range_end_char + 1,
    }


def call_item_to_dict(item: Any) -> dict[str, Any]:
    """Convert a CallHierarchyItem to a clean dict.

    LSP uses 0-based line/character; output is 1-based for user consumption.
    """
    return {
        "name": item.name,
        "kind": item.kind,
        "file_path": _uri_to_path(item.uri),
        "line": item.range_start_line + 1,
        "character": item.range_start_char + 1,
        "end_line": item.range_end_line + 1,
        "end_character": item.range_end_char + 1,
        "detail": item.detail,
    }


def diagnostic_to_dict(diag: Any) -> dict[str, Any]:
    """Convert a Diagnostic to a clean dict.

    LSP uses 0-based line/character; output is 1-based for user consumption.
    """
    return {
        "line": diag.range_start_line + 1,
        "character": diag.range_start_char + 1,
        "end_line": diag.range_end_line + 1,
        "end_character": diag.range_end_char + 1,
        "severity": diag.severity,
        "message": diag.message,
        "source": diag.source,
        "code": diag.code,
    }
