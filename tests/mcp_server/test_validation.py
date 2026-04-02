"""Tests for chunkhound.mcp_server.tools.validation — param checking, range clamping."""

import pytest

from chunkhound.mcp_server.tools.validation import clamp, require_param

pytestmark = pytest.mark.unit


class TestRequireParam:
    """require_param returns error dict if missing/empty, None if present."""

    def test_missing_value_returns_error(self) -> None:
        result = require_param("symbol", None)
        assert result is not None
        assert result["error"] == "missing_parameter"
        assert "symbol" in result["message"]

    def test_empty_string_treated_as_missing(self) -> None:
        result = require_param("scope", "")
        assert result is not None
        assert result["error"] == "missing_parameter"
        assert "scope" in result["message"]

    def test_present_value_returns_none(self) -> None:
        result = require_param("symbol", "some::fqn")
        assert result is None

    def test_zero_is_not_missing(self) -> None:
        result = require_param("depth", 0)
        assert result is None

    def test_false_is_not_missing(self) -> None:
        result = require_param("flag", False)
        assert result is None


class TestClamp:
    """clamp constrains a value to [min_val, max_val]."""

    def test_below_min_clamps_up(self) -> None:
        assert clamp(0, 1, 20) == 1

    def test_above_max_clamps_down(self) -> None:
        assert clamp(50, 1, 20) == 20

    def test_in_range_unchanged(self) -> None:
        assert clamp(10, 1, 20) == 10

    def test_at_min_boundary(self) -> None:
        assert clamp(1, 1, 20) == 1

    def test_at_max_boundary(self) -> None:
        assert clamp(20, 1, 20) == 20

    def test_negative_value_clamps_up(self) -> None:
        assert clamp(-5, 1, 100) == 1
