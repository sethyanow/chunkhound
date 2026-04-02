"""Adversarial stress tests for shared foundation modules.

Structural patterns: empty, type boundaries, encoding boundaries,
semantically hostile, dense, redundant.
"""

import pytest
import sqlglot

from chunkhound.mcp_server.tools.formatters import (
    _uri_to_path,
    format_edge,
    format_node,
)
from chunkhound.mcp_server.tools.queries.common import (
    escape_like,
    scope_filter,
    visited_tracking_columns,
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


# ---------------------------------------------------------------------------
# escape_like — Dense
# ---------------------------------------------------------------------------


class TestEscapeLikeDense:
    """Adversarial: strings made entirely of special characters."""

    def test_all_percents(self) -> None:
        assert escape_like("%%%") == "\\%\\%\\%"

    def test_all_underscores(self) -> None:
        assert escape_like("___") == "\\_\\_\\_"

    def test_all_backslashes(self) -> None:
        # 3 literal backslashes → each doubled = 6 literal backslashes
        assert escape_like("\\\\\\") == "\\\\\\\\\\\\"

    def test_alternating_specials(self) -> None:
        assert escape_like("%_%\\") == "\\%\\_\\%\\\\"


# ---------------------------------------------------------------------------
# scope_filter — Empty, Type boundaries
# ---------------------------------------------------------------------------


class TestScopeFilterAdversarial:
    """Adversarial: empty scope, scope that is a wildcard."""

    def test_empty_scope_matches_everything(self) -> None:
        """Empty scope produces pattern '%' — matches all rows."""
        expr, params = scope_filter("")
        assert params[0] == "%"

    def test_scope_is_percent(self) -> None:
        """Scope '%' must be escaped so it doesn't match everything."""
        expr, params = scope_filter("%")
        assert params[0] == "\\%%"

    def test_scope_is_underscore(self) -> None:
        _, params = scope_filter("_")
        assert params[0] == "\\_%"

    def test_custom_column_name(self) -> None:
        expr, _ = scope_filter("src", column="s.file_path")
        sql = expr.sql(dialect="duckdb")
        assert "s.file_path" in sql


# ---------------------------------------------------------------------------
# visited_tracking_columns — Encoding boundaries
# ---------------------------------------------------------------------------


class TestVisitedTrackingAdversarial:
    """Adversarial: unusual table/column names in expressions."""

    def test_generates_valid_sql_with_alias(self) -> None:
        append, contains = visited_tracking_columns("tbl", "col")
        # Both should produce parseable SQL
        sqlglot.parse_one(append.sql(dialect="duckdb"), dialect="duckdb")
        sqlglot.parse_one(contains.sql(dialect="duckdb"), dialect="duckdb")
