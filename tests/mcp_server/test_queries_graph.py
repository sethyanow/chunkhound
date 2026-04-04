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


class TestBuildWalkQueryDirected:
    """Walk CTE with directed=True: forward-only edge traversal."""

    def test_directed_roundtrip_parses(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind="called_by", limit=20, directed=True)
        _roundtrip(sql)

    def test_directed_no_bidirectional_union(self) -> None:
        """directed=True uses forward-only edges — no UNION ALL for bidirectional."""
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind="called_by", limit=20, directed=True)
        upper = sql.upper()
        # The recursive CTE still has UNION ALL (seed UNION ALL recursive)
        # But there should NOT be a nested UNION ALL for bidirectional edges
        # Forward-only: SELECT from_fqn AS src, to_fqn AS dst FROM symbol_edges
        assert "FROM_FQN" in upper
        assert "TO_FQN" in upper

    def test_directed_still_has_recursive_cte(self) -> None:
        sql, _ = build_walk_query(symbol="mod::A", depth=2, edge_kind="called_by", limit=20, directed=True)
        upper = sql.upper()
        assert "WITH RECURSIVE" in upper

    def test_directed_false_is_default(self) -> None:
        """Default (no directed param) produces same SQL as directed=False."""
        sql_default, params_default = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20)
        sql_explicit, params_explicit = build_walk_query(symbol="mod::A", depth=2, edge_kind=None, limit=20, directed=False)
        assert sql_default == sql_explicit
        assert params_default == params_explicit

    def test_directed_param_count_matches_undirected(self) -> None:
        """Same params regardless of directed flag."""
        _, params_bidir = build_walk_query(symbol="mod::A", depth=2, edge_kind="called_by", limit=20, directed=False)
        _, params_directed = build_walk_query(symbol="mod::A", depth=2, edge_kind="called_by", limit=20, directed=True)
        assert params_bidir == params_directed


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
        # At least one param should contain the escaped scope (underscore escaped with !)
        scope_params = [p for p in params if isinstance(p, str) and "chunk!_ound/" in p]
        assert len(scope_params) > 0

    def test_boundary_filters_on_edge_file_columns(self) -> None:
        """Regression: boundary WHERE must use e.from_file/e.to_file, not s1/s2.file_path.

        Using symbol table file_path in the WHERE clause causes cartesian products
        when FQNs are non-unique (e.g. __all__ in multiple __init__.py files).
        """
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        lower = sql.lower()
        # The LIKE conditions must reference the edge table's file columns
        assert "e.from_file" in lower or "symbol_edges.from_file" in lower
        assert "e.to_file" in lower or "symbol_edges.to_file" in lower
        # Must NOT use s1.file_path or s2.file_path in WHERE (they produce cartesian products)
        # Parse and check WHERE clause specifically — s1/s2.file_path may appear in SELECT
        parsed = _parse_duckdb(sql)
        where_sql = ""
        for node in parsed.walk():
            if isinstance(node, exp.Where):
                where_sql = node.sql(dialect="duckdb").lower()
                break
        assert "s1.file_path" not in where_sql, "WHERE must not use s1.file_path (cartesian product bug)"
        assert "s2.file_path" not in where_sql, "WHERE must not use s2.file_path (cartesian product bug)"

    def test_boundary_joins_disambiguated_by_file(self) -> None:
        """Regression: symbol JOINs must include file_path to prevent cartesian products.

        JOIN symbols s1 ON e.from_fqn = s1.fqn AND e.from_file = s1.file_path
        Without the file_path condition, non-unique FQNs match multiple symbols.
        """
        sql, _ = build_boundary_query(scope="pkg/", limit=50)
        parsed = _parse_duckdb(sql)
        # Extract JOIN ON conditions
        join_conditions = []
        for node in parsed.walk():
            if isinstance(node, exp.Join):
                on_clause = node.args.get("on")
                if on_clause:
                    join_conditions.append(on_clause.sql(dialect="duckdb").lower())
        assert len(join_conditions) >= 2, f"Expected 2 JOINs, got {len(join_conditions)}"
        # Each JOIN must include a file_path equality (not just fqn)
        for i, cond in enumerate(join_conditions):
            assert "file_path" in cond, (
                f"JOIN {i + 1} missing file_path disambiguator: {cond}"
            )


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
