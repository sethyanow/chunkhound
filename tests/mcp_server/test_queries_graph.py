"""Tests for chunkhound.mcp_server.tools.queries.graph — sqlglot graph query builders."""

import pytest
import sqlglot
from sqlglot import exp

from chunkhound.mcp_server.tools.queries.graph import (
    build_boundary_query,
    build_overview_breakdown_query,
    build_overview_query,
    build_reachability_all_symbols_query,
    build_reachability_reachable_query,
    build_walk_edges_query,
    build_walk_query,
)

pytestmark = pytest.mark.unit


def _parse_duckdb(sql: str) -> exp.Expression:
    """Parse SQL string as DuckDB dialect, raising on invalid SQL."""
    return sqlglot.parse_one(sql, dialect="duckdb")


def _roundtrip(sql: str) -> str:
    """Parse then regenerate — validates SQL is structurally sound."""
    return _parse_duckdb(sql).sql(dialect="duckdb")


def _count_placeholders(sql: str) -> int:
    """Count ? placeholders in generated SQL."""
    return sql.count("?")


# ---------------------------------------------------------------------------
# build_walk_query
# ---------------------------------------------------------------------------


class TestBuildWalkQuery:
    """Walk CTE: recursive, bidirectional, cycle-safe."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        _roundtrip(sql)

    def test_has_recursive_cte(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        upper = sql.upper()
        assert "WITH RECURSIVE" in upper

    def test_bidirectional_union_all(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        upper = sql.upper()
        assert "UNION ALL" in upper

    def test_cycle_tracking(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        lower = sql.lower()
        # sqlglot normalizes list_contains → array_contains for DuckDB
        assert "array_contains" in lower or "list_contains" in lower
        assert "list_concat" in lower

    def test_placeholder_count_no_edge_kind(self) -> None:
        sql, params = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        assert _count_placeholders(sql) == 3
        assert params == ["mod::A", 2, 20]

    def test_placeholder_count_with_edge_kind(self) -> None:
        sql, params = build_walk_query(symbol="mod::A", depth=2, edge_kind="calls", limit=20)
        assert _count_placeholders(sql) == 4
        assert params == ["mod::A", 2, "calls", 20]

    def test_param_ordering(self) -> None:
        """symbol, depth, [edge_kind], limit — in that order."""
        _, params = build_walk_query(symbol="x::Y", depth=5, edge_kind=None, limit=10)
        assert params == ["x::Y", 5, 10]

        _, params_ek = build_walk_query(symbol="x::Y", depth=5, edge_kind="defines", limit=10)
        assert params_ek == ["x::Y", 5, "defines", 10]


# ---------------------------------------------------------------------------
# build_walk_edges_query
# ---------------------------------------------------------------------------


class TestBuildWalkEdgesQuery:
    """Second walk query: edges between discovered FQNs."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_walk_edges_query(fqns=["a::B", "c::D"], edge_kind=None)
        _roundtrip(sql)

    def test_placeholder_count_matches_fqns_doubled(self) -> None:
        fqns = ["a::B", "c::D", "e::F"]
        sql, params = build_walk_edges_query(fqns=fqns, edge_kind=None)
        # from_fqn IN (?, ?, ?) AND to_fqn IN (?, ?, ?)
        assert _count_placeholders(sql) == 6
        assert params == ["a::B", "c::D", "e::F", "a::B", "c::D", "e::F"]

    def test_with_edge_kind_adds_placeholder(self) -> None:
        fqns = ["a::B"]
        sql, params = build_walk_edges_query(fqns=fqns, edge_kind="calls")
        assert _count_placeholders(sql) == 3  # 1 + 1 + 1
        assert params == ["a::B", "a::B", "calls"]

    def test_selects_edge_columns(self) -> None:
        sql, _ = build_walk_edges_query(fqns=["a::B"], edge_kind=None)
        lower = sql.lower()
        assert "from_fqn" in lower
        assert "to_fqn" in lower
        assert "edge_kind" in lower


# ---------------------------------------------------------------------------
# build_reachability_query
# ---------------------------------------------------------------------------


class TestBuildReachabilityAllSymbolsQuery:
    """Reachability query 1: all symbols in scope."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_reachability_all_symbols_query(scope="pkg/")
        _roundtrip(sql)

    def test_has_scope_like_escape(self) -> None:
        sql, params = build_reachability_all_symbols_query(scope="pkg/")
        upper = sql.upper()
        assert "LIKE" in upper
        assert "ESCAPE" in upper
        assert params[0] == "pkg/%"

    def test_placeholder_count(self) -> None:
        sql, params = build_reachability_all_symbols_query(scope="src/")
        assert _count_placeholders(sql) == 1
        assert len(params) == 1


class TestBuildReachabilityReachableQuery:
    """Reachability query 2: outbound-only CTE — NOT bidirectional."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_reachability_reachable_query(scope="pkg/")
        _roundtrip(sql)

    def test_not_bidirectional(self) -> None:
        """Must NOT have UNION ALL — outbound only, uses UNION for dedup."""
        sql, _ = build_reachability_reachable_query(scope="pkg/")
        upper = sql.upper()
        assert upper.count("UNION ALL") == 0

    def test_has_scope_like_escape(self) -> None:
        sql, params = build_reachability_reachable_query(scope="pkg/")
        upper = sql.upper()
        assert "LIKE" in upper
        assert "ESCAPE" in upper
        assert params[0] == "pkg/%"

    def test_placeholder_count(self) -> None:
        sql, params = build_reachability_reachable_query(scope="src/")
        # Two scope patterns (seed + recursive)
        assert _count_placeholders(sql) == 2
        assert len(params) == 2


# ---------------------------------------------------------------------------
# build_boundary_query
# ---------------------------------------------------------------------------


class TestBuildBoundaryQuery:
    """Boundary: edges crossing scope — LIKE and NOT LIKE."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        _roundtrip(sql)

    def test_has_like_and_negated_like(self) -> None:
        """Both LIKE and NOT ... LIKE present (sqlglot normalizes NOT LIKE to NOT x LIKE)."""
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        upper = sql.upper()
        assert "LIKE" in upper
        # sqlglot normalizes "x NOT LIKE y" to "NOT x LIKE y"
        assert "NOT" in upper

    def test_has_escape(self) -> None:
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        upper = sql.upper()
        assert "ESCAPE" in upper

    def test_edge_output_columns(self) -> None:
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        lower = sql.lower()
        assert "from_fqn" in lower
        assert "to_fqn" in lower
        assert "edge_kind" in lower

    def test_scope_escaping(self) -> None:
        """Special chars in scope get LIKE-escaped in params."""
        _, params = build_boundary_query(scope="chunk_ound/", limit=50)
        # At least one param should contain the escaped scope
        scope_params = [p for p in params if isinstance(p, str) and "chunk\\_ound/" in p]
        assert len(scope_params) > 0


# ---------------------------------------------------------------------------
# build_overview_query
# ---------------------------------------------------------------------------


class TestBuildOverviewQuery:
    """Overview: bidirectional UNION ALL for edge counting."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_overview_query(scope=None, limit=20)
        _roundtrip(sql)

    def test_bidirectional_union_all(self) -> None:
        sql, _ = build_overview_query(scope=None, limit=20)
        upper = sql.upper()
        assert "UNION ALL" in upper

    def test_has_group_by(self) -> None:
        sql, _ = build_overview_query(scope=None, limit=20)
        upper = sql.upper()
        assert "GROUP BY" in upper

    def test_has_order_by_desc(self) -> None:
        sql, _ = build_overview_query(scope=None, limit=20)
        upper = sql.upper()
        assert "ORDER BY" in upper
        assert "DESC" in upper

    def test_with_scope_adds_like_filter(self) -> None:
        sql, params = build_overview_query(scope="src/", limit=20)
        upper = sql.upper()
        assert "LIKE" in upper
        assert any(p == "src/%" for p in params if isinstance(p, str))

    def test_without_scope_no_like(self) -> None:
        sql, params = build_overview_query(scope=None, limit=20)
        # Only limit param when no scope
        str_params = [p for p in params if isinstance(p, str)]
        assert len(str_params) == 0


# ---------------------------------------------------------------------------
# build_overview_breakdown_query
# ---------------------------------------------------------------------------


class TestBuildOverviewBreakdownQuery:
    """Breakdown: per-symbol edge_kind counts."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_overview_breakdown_query(fqns=["a::B", "c::D"])
        _roundtrip(sql)

    def test_placeholder_count_matches_fqns(self) -> None:
        fqns = ["a::B", "c::D", "e::F"]
        sql, params = build_overview_breakdown_query(fqns=fqns)
        assert _count_placeholders(sql) == 3
        assert params == fqns

    def test_has_group_by(self) -> None:
        sql, _ = build_overview_breakdown_query(fqns=["a::B"])
        upper = sql.upper()
        assert "GROUP BY" in upper

    def test_counts_edges(self) -> None:
        sql, _ = build_overview_breakdown_query(fqns=["a::B"])
        upper = sql.upper()
        assert "COUNT" in upper
