"""Adversarial stress tests for shared foundation modules.

Structural patterns: empty, type boundaries, encoding boundaries,
semantically hostile, dense, redundant.

Note: ch-nxu Step 15 removed the escape_like / scope_filter /
visited_tracking_columns adversarial classes — the underlying helpers
were absorbed into DuckDBProvider and their behavioral coverage now lives
in tests/integration/test_duckdb_graph_protocol.py and
tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
"""

import pytest

from chunkhound.mcp_server.tools.formatters import (
    _uri_to_path,
    format_edge,
    format_node,
)
from chunkhound.mcp_server.tools.validation import clamp, require_param

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# require_param — Type boundaries
# ---------------------------------------------------------------------------


class TestRequireParamAdversarial:
    """Adversarial: type boundary and encoding edge cases."""

    def test_whitespace_only_string_is_present(self) -> None:
        """Whitespace-only is truthy — require_param passes it.

        Caller validates semantics (is this a valid FQN?), not require_param.
        """
        assert require_param("symbol", "   ") is None

    def test_empty_list_is_present(self) -> None:
        """Empty list is falsy but not None or empty string."""
        assert require_param("items", []) is None

    def test_unicode_value_is_present(self) -> None:
        assert require_param("path", "パス/ファイル") is None

    def test_numeric_string_zero_is_present(self) -> None:
        assert require_param("val", "0") is None


# ---------------------------------------------------------------------------
# clamp — Type boundaries
# ---------------------------------------------------------------------------


class TestClampAdversarial:
    """Adversarial: degenerate and inverted ranges."""

    def test_equal_bounds(self) -> None:
        """When min == max, value is always clamped to that single point."""
        assert clamp(5, 3, 3) == 3
        assert clamp(3, 3, 3) == 3
        assert clamp(1, 3, 3) == 3

    def test_inverted_range_clamps_to_min(self) -> None:
        """When min_val > max_val, max(min_val, min(value, max_val)) = min_val."""
        assert clamp(5, 10, 1) == 10

    def test_large_values(self) -> None:
        assert clamp(2**31, 1, 100) == 100

    def test_large_negative(self) -> None:
        assert clamp(-(2**31), 1, 100) == 1


# ---------------------------------------------------------------------------
# format_node / format_edge — Empty, Encoding, Redundant
# ---------------------------------------------------------------------------


class TestFormatNodeAdversarial:
    """Adversarial: missing keys, extra keys, unicode content."""

    def test_missing_key_raises_key_error(self) -> None:
        """Missing keys are programming errors — KeyError is correct."""
        with pytest.raises(KeyError):
            format_node({"fqn": "x", "name": "x"})

    def test_extra_keys_ignored(self) -> None:
        row = {
            "fqn": "x",
            "name": "x",
            "kind": "fn",
            "file_path": "a.py",
            "depth": 0,
            "extra_col": "ignored",
        }
        result = format_node(row)
        assert "extra_col" not in result

    def test_unicode_in_fqn(self) -> None:
        row = {
            "fqn": "モジュール::関数",
            "name": "関数",
            "kind": "function",
            "file_path": "日本語.py",
            "depth": 1,
        }
        result = format_node(row)
        assert result["fqn"] == "モジュール::関数"


class TestFormatEdgeAdversarial:
    """Adversarial: missing keys, empty strings."""

    def test_missing_key_raises_key_error(self) -> None:
        with pytest.raises(KeyError):
            format_edge({"from_fqn": "a"})

    def test_empty_string_values(self) -> None:
        row = {
            "from_fqn": "",
            "to_fqn": "",
            "edge_kind": "",
            "from_file": "",
            "to_file": "",
        }
        result = format_edge(row)
        assert result["from_symbol"] == ""


# ---------------------------------------------------------------------------
# _uri_to_path — Encoding boundaries, Semantically hostile
# ---------------------------------------------------------------------------


class TestUriToPathAdversarial:
    """Adversarial: percent-encoded, unicode, edge-case URIs."""

    def test_percent_encoded_spaces(self) -> None:
        assert _uri_to_path("file:///path%20with%20spaces/f.py") == (
            "/path with spaces/f.py"
        )

    def test_percent_encoded_unicode(self) -> None:
        result = _uri_to_path("file:///tmp/%E3%83%91%E3%82%B9")
        assert result == "/tmp/パス"

    def test_empty_string(self) -> None:
        assert _uri_to_path("") == ""

    def test_file_uri_root_only(self) -> None:
        assert _uri_to_path("file:///") == "/"

    def test_double_encoded_preserved(self) -> None:
        """Double-encoded %2520 decodes once to %20, not to space."""
        result = _uri_to_path("file:///path%2520name")
        assert result == "/path%20name"

    def test_windows_style_uri(self) -> None:
        result = _uri_to_path("file:///C:/Users/test/file.py")
        # urlparse preserves the path as-is on non-Windows
        assert "C:" in result


# escape_like / scope_filter / visited_tracking_columns classes deleted by
# ch-nxu Step 15 — helpers absorbed into DuckDBProvider; behavioral coverage
# now lives in tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
