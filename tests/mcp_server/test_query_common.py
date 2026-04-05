"""Tests for chunkhound.mcp_server.tools.queries.common — query fragments."""

import pytest
import sqlglot
from sqlglot import exp

from chunkhound.mcp_server.tools.queries.common import (
    bidirectional_edges,
    escape_like,
    scope_filter,
    visited_tracking_columns,
)

pytestmark = pytest.mark.unit


def _roundtrip_sql(expression: exp.Expression) -> str:
    """Generate SQL from expression, parse it back, return normalized SQL.

    This validates the expression produces valid DuckDB SQL.
    """
    sql = expression.sql(dialect="duckdb")
    parsed = sqlglot.parse_one(sql, dialect="duckdb")
    return parsed.sql(dialect="duckdb")


class TestBidirectionalEdges:
    """bidirectional_edges() generates a UNION ALL for both directions."""

    def test_generates_union_all(self) -> None:
        expr = bidirectional_edges()
        sql = expr.sql(dialect="duckdb")
        assert "UNION ALL" in sql

    def test_has_src_dst_aliases(self) -> None:
        expr = bidirectional_edges()
        sql = expr.sql(dialect="duckdb")
        assert "src" in sql.lower()
        assert "dst" in sql.lower()

    def test_roundtrip_parses_as_valid_duckdb(self) -> None:
        expr = bidirectional_edges()
        sql = _roundtrip_sql(expr)
        # Should contain both directions
        assert "symbol_edges" in sql.lower()

    def test_includes_edge_kind(self) -> None:
        expr = bidirectional_edges()
        sql = expr.sql(dialect="duckdb")
        assert "edge_kind" in sql.lower()


class TestScopeFilter:
    """scope_filter() generates a LIKE clause with ESCAPE and placeholder."""

    def test_generates_like_with_escape(self) -> None:
        sql, params = scope_filter("src/auth")
        assert "LIKE" in sql
        assert "ESCAPE" in sql

    def test_returns_escaped_param(self) -> None:
        _, params = scope_filter("src/auth")
        assert len(params) == 1
        assert params[0] == "src/auth%"

    def test_escapes_special_chars_in_scope(self) -> None:
        _, params = scope_filter("path_with%special")
        assert params[0] == "path!_with!%special%"

    def test_column_defaults_to_file_path(self) -> None:
        sql, _ = scope_filter("src")
        assert "file_path" in sql.lower()

    def test_escape_clause_uses_exclamation(self) -> None:
        """ESCAPE clause must use '!' — a single character DuckDB accepts."""
        sql, _ = scope_filter("pkg/")
        import re
        match = re.search(r"ESCAPE\s+'(.*?)'", sql)
        assert match is not None, f"No ESCAPE clause found in: {sql}"
        escape_val = match.group(1)
        assert escape_val == "!", (
            f"ESCAPE value should be '!', got {repr(escape_val)} "
            f"({len(escape_val)} chars). Full SQL: {sql}"
        )


class TestVisitedTrackingColumns:
    """visited_tracking_columns() generates cycle-tracking expressions."""

    def test_generates_list_concat(self) -> None:
        append_expr, contains_expr = visited_tracking_columns("s2", "fqn")
        sql = append_expr.sql(dialect="duckdb")
        assert "list_concat" in sql.lower() or "LIST_CONCAT" in sql

    def test_generates_list_contains(self) -> None:
        _, contains_expr = visited_tracking_columns("s2", "fqn")
        sql = contains_expr.sql(dialect="duckdb")
        # sqlglot normalizes list_contains to array_contains for DuckDB
        assert "array_contains" in sql.lower() or "list_contains" in sql.lower()

    def test_roundtrip_list_concat(self) -> None:
        append_expr, _ = visited_tracking_columns("s2", "fqn")
        # Should parse without error
        sql = append_expr.sql(dialect="duckdb")
        sqlglot.parse_one(sql, dialect="duckdb")

    def test_roundtrip_list_contains(self) -> None:
        _, contains_expr = visited_tracking_columns("s2", "fqn")
        sql = contains_expr.sql(dialect="duckdb")
        sqlglot.parse_one(sql, dialect="duckdb")


class TestEscapeLike:
    """escape_like escapes LIKE-special characters."""

    def test_percent(self) -> None:
        assert escape_like("chunk%ound") == "chunk!%ound"

    def test_underscore(self) -> None:
        assert escape_like("chunk_ound") == "chunk!_ound"

    def test_escape_char_doubled(self) -> None:
        """The escape char itself (!) is doubled."""
        assert escape_like("path!to") == "path!!to"

    def test_backslash_not_special(self) -> None:
        """Backslash is not a LIKE metacharacter — passes through unchanged."""
        assert escape_like("path\\to") == "path\\to"

    def test_all_special_chars(self) -> None:
        assert escape_like("a%b_c!d") == "a!%b!_c!!d"

    def test_empty_string(self) -> None:
        assert escape_like("") == ""

    def test_unicode_passthrough(self) -> None:
        assert escape_like("パス/ファイル") == "パス/ファイル"
