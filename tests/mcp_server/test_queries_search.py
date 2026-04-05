"""Tests for chunkhound.mcp_server.tools.queries.search — sqlglot search query builders."""

import pytest
import sqlglot
from sqlglot import exp

from chunkhound.mcp_server.tools.queries.search import (
    build_chunk_resolution_query,
    build_structural_walk_query,
    build_symbol_count_query,
    build_symbol_overlap_query,
    build_symbol_search_query,
    build_type_filter_query,
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
# build_symbol_search_query
# ---------------------------------------------------------------------------


class TestBuildSymbolSearchQuery:
    """Symbol search: LIKE on name/fqn, optional path + type_filter."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_symbol_search_query(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        _roundtrip(sql)

    def test_like_on_name_and_fqn(self) -> None:
        sql, _ = build_symbol_search_query(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        lower = sql.lower()
        assert lower.count("like") >= 2, "Should LIKE on both name and fqn"

    def test_empty_query_matches_all(self) -> None:
        """Empty query string means 'list all symbols' — no name/fqn LIKE."""
        sql, params = build_symbol_search_query(
            query="", path=None, type_filter=None, limit=10, offset=0
        )
        _roundtrip(sql)
        # Only LIMIT and OFFSET params when no filters
        assert _count_placeholders(sql) == 2
        assert params == [10, 0]

    def test_path_filter_with_escape(self) -> None:
        sql, params = build_symbol_search_query(
            query="parse", path="src/auth", type_filter=None, limit=10, offset=0
        )
        lower = sql.lower()
        assert "escape" in lower, "Path filter should use LIKE ESCAPE"

    def test_type_filter_adds_like(self) -> None:
        sql, params = build_symbol_search_query(
            query="parse", path=None, type_filter="Result", limit=10, offset=0
        )
        lower = sql.lower()
        # name/fqn LIKE + type_signature LIKE
        assert lower.count("like") >= 3

    def test_placeholder_count_query_only(self) -> None:
        sql, params = build_symbol_search_query(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        # 2 for name/fqn LIKE + 2 for LIMIT/OFFSET
        assert _count_placeholders(sql) == len(params)

    def test_placeholder_count_all_filters(self) -> None:
        sql, params = build_symbol_search_query(
            query="parse", path="src/", type_filter="int", limit=10, offset=0
        )
        # 2 (name/fqn) + 1 (path) + 1 (type_filter) + 2 (LIMIT/OFFSET)
        assert _count_placeholders(sql) == len(params)
        assert len(params) == 6

    def test_has_limit_and_offset(self) -> None:
        sql, _ = build_symbol_search_query(
            query="parse", path=None, type_filter=None, limit=10, offset=5
        )
        upper = sql.upper()
        assert "LIMIT" in upper
        assert "OFFSET" in upper

    def test_has_order_by(self) -> None:
        sql, _ = build_symbol_search_query(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        upper = sql.upper()
        assert "ORDER BY" in upper


# ---------------------------------------------------------------------------
# build_symbol_count_query
# ---------------------------------------------------------------------------


class TestBuildSymbolCountQuery:
    """Count query: same conditions as search, but SELECT COUNT(*)."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_symbol_count_query(query="parse", path=None, type_filter=None)
        _roundtrip(sql)

    def test_has_count(self) -> None:
        sql, _ = build_symbol_count_query(query="parse", path=None, type_filter=None)
        upper = sql.upper()
        assert "COUNT" in upper

    def test_no_limit_offset(self) -> None:
        """Count query should not have LIMIT or OFFSET."""
        sql, params = build_symbol_count_query(
            query="parse", path=None, type_filter=None
        )
        upper = sql.upper()
        assert "LIMIT" not in upper
        assert "OFFSET" not in upper
        # Only 2 params for name/fqn LIKE (no limit/offset)
        assert len(params) == 2

    def test_same_conditions_as_search(self) -> None:
        """Count and search queries should use the same filter conditions."""
        _, search_params = build_symbol_search_query(
            query="parse", path="src/", type_filter="int", limit=10, offset=0
        )
        _, count_params = build_symbol_count_query(
            query="parse", path="src/", type_filter="int"
        )
        # Count params = search params minus last 2 (limit, offset)
        assert count_params == search_params[:-2]


# ---------------------------------------------------------------------------
# build_symbol_overlap_query
# ---------------------------------------------------------------------------


class TestBuildSymbolOverlapQuery:
    """Symbol overlap: find FQNs overlapping semantic result ranges."""

    def test_roundtrip_parses(self) -> None:
        chunks = [
            {"file_path": "src/main.py", "start_line": 10, "end_line": 20},
        ]
        sql, _ = build_symbol_overlap_query(chunks)
        _roundtrip(sql)

    def test_placeholder_count_per_chunk(self) -> None:
        chunks = [
            {"file_path": "src/a.py", "start_line": 1, "end_line": 10},
            {"file_path": "src/b.py", "start_line": 5, "end_line": 15},
        ]
        sql, params = build_symbol_overlap_query(chunks)
        # 3 placeholders per chunk (file_path, end_line, start_line)
        assert _count_placeholders(sql) == 6
        assert len(params) == 6

    def test_selects_fqn_and_file_id(self) -> None:
        """Column alias contract: orchestration code expects 'fqn' and 'file_id'."""
        chunks = [{"file_path": "a.py", "start_line": 1, "end_line": 5}]
        sql, _ = build_symbol_overlap_query(chunks)
        lower = sql.lower()
        assert "fqn" in lower
        assert "file_id" in lower

    def test_empty_chunks_raises(self) -> None:
        """Empty input must not generate invalid SQL."""
        with pytest.raises(ValueError):
            build_symbol_overlap_query([])


# ---------------------------------------------------------------------------
# build_structural_walk_query
# ---------------------------------------------------------------------------


class TestBuildStructuralWalkQuery:
    """Structural walk: recursive CTE with bidirectional edges from multiple seeds."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_structural_walk_query(
            seed_fqns=["mod::A", "mod::B"], depth=2, limit=20
        )
        _roundtrip(sql)

    def test_has_recursive_cte(self) -> None:
        sql, _ = build_structural_walk_query(seed_fqns=["mod::A"], depth=2, limit=20)
        upper = sql.upper()
        assert "WITH RECURSIVE" in upper

    def test_bidirectional_edge_pattern(self) -> None:
        """MUST have both edge directions — not just UNION ALL presence.

        The bidirectional_edges() fragment produces:
        - SELECT from_fqn AS src, to_fqn AS dst ... (forward)
        - SELECT to_fqn AS src, from_fqn AS dst ... (reverse)
        Both must be present.
        """
        sql, _ = build_structural_walk_query(seed_fqns=["mod::A"], depth=2, limit=20)
        lower = sql.lower()
        # Both directions must appear in the edge subquery
        assert "from_fqn" in lower and "to_fqn" in lower
        # The bidirectional subquery maps both directions to src/dst
        assert "as src" in lower or "AS src" in sql
        assert "as dst" in lower or "AS dst" in sql
        # Must have UNION ALL for the bidirectional combination
        assert "union all" in lower

    def test_cycle_tracking(self) -> None:
        sql, _ = build_structural_walk_query(seed_fqns=["mod::A"], depth=2, limit=20)
        lower = sql.lower()
        # sqlglot normalizes list_contains → array_contains for DuckDB
        assert "array_contains" in lower or "list_contains" in lower
        assert "list_concat" in lower

    def test_multi_seed_in_clause(self) -> None:
        sql, params = build_structural_walk_query(
            seed_fqns=["a::X", "b::Y", "c::Z"], depth=2, limit=20
        )
        # 3 seed placeholders + 1 depth + 1 limit
        assert _count_placeholders(sql) == 5
        assert params[:3] == ["a::X", "b::Y", "c::Z"]
        assert params[3] == 2  # depth
        assert params[4] == 20  # limit

    def test_single_seed(self) -> None:
        sql, params = build_structural_walk_query(
            seed_fqns=["mod::A"], depth=2, limit=20
        )
        assert _count_placeholders(sql) == 3
        assert params == ["mod::A", 2, 20]

    def test_edge_kind_adds_filter_clause(self) -> None:
        """edge_kind parameter adds edge_kind filter and increases placeholder count."""
        sql_without, params_without = build_structural_walk_query(
            seed_fqns=["mod::A"], depth=2, limit=20
        )
        sql_with, params_with = build_structural_walk_query(
            seed_fqns=["mod::A"], depth=2, limit=20, edge_kind="calls"
        )
        # Filter clause present in SQL
        assert "edge_kind" in sql_with
        # "calls" value in params
        assert "calls" in params_with
        # One additional placeholder vs without edge_kind
        assert _count_placeholders(sql_with) == _count_placeholders(sql_without) + 1
        # Params: seed_fqn, depth, edge_kind, limit
        assert params_with == ["mod::A", 2, "calls", 20]

    def test_empty_seeds_raises(self) -> None:
        """Empty seed list must not generate invalid SQL."""
        with pytest.raises(ValueError):
            build_structural_walk_query(seed_fqns=[], depth=2, limit=20)


# ---------------------------------------------------------------------------
# build_chunk_resolution_query
# ---------------------------------------------------------------------------


class TestBuildChunkResolutionQuery:
    """Chunk resolution: FQNs → chunks via range overlap."""

    def test_roundtrip_parses(self) -> None:
        sql, _ = build_chunk_resolution_query(fqns=["a::B", "c::D"])
        _roundtrip(sql)

    def test_placeholder_count_matches_fqns(self) -> None:
        fqns = ["a::B", "c::D", "e::F"]
        sql, params = build_chunk_resolution_query(fqns=fqns)
        assert _count_placeholders(sql) == 3
        assert params == list(fqns)

    def test_column_aliases(self) -> None:
        """Column alias contract: orchestration expects file_path, content, start_line, end_line."""
        sql, _ = build_chunk_resolution_query(fqns=["a::B"])
        lower = sql.lower()
        assert "file_path" in lower
        assert "content" in lower
        assert "start_line" in lower
        assert "end_line" in lower

    def test_joins_chunks_files_symbols(self) -> None:
        sql, _ = build_chunk_resolution_query(fqns=["a::B"])
        lower = sql.lower()
        assert "chunks" in lower
        assert "files" in lower
        assert "symbols" in lower

    def test_empty_fqns_raises(self) -> None:
        with pytest.raises(ValueError):
            build_chunk_resolution_query(fqns=[])


# ---------------------------------------------------------------------------
# build_type_filter_query
# ---------------------------------------------------------------------------


class TestBuildTypeFilterQuery:
    """Type filter: batch symbol lookup for type_signature overlap."""

    def test_roundtrip_parses(self) -> None:
        results = [{"file_path": "a.py", "start_line": 1, "end_line": 10}]
        sql, _ = build_type_filter_query(results=results, type_filter="Result")
        _roundtrip(sql)

    def test_placeholder_count(self) -> None:
        results = [
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
            {"file_path": "b.py", "start_line": 5, "end_line": 15},
        ]
        sql, params = build_type_filter_query(results=results, type_filter="int")
        # 3 per result (file_path, end_line, start_line) + 1 for type_filter
        assert _count_placeholders(sql) == 7
        assert len(params) == 7

    def test_type_signature_like(self) -> None:
        results = [{"file_path": "a.py", "start_line": 1, "end_line": 10}]
        sql, _ = build_type_filter_query(results=results, type_filter="Result")
        lower = sql.lower()
        assert "type_signature" in lower
        assert "like" in lower

    def test_column_aliases(self) -> None:
        """Column alias contract: needs file_path, range_start, range_end."""
        results = [{"file_path": "a.py", "start_line": 1, "end_line": 10}]
        sql, _ = build_type_filter_query(results=results, type_filter="int")
        lower = sql.lower()
        assert "file_path" in lower
        assert "range_start" in lower
        assert "range_end" in lower

    def test_empty_results_raises(self) -> None:
        with pytest.raises(ValueError):
            build_type_filter_query(results=[], type_filter="int")


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestAdversarialSymbolSearch:
    """Adversarial battery for build_symbol_search_query."""

    def test_unicode_query(self) -> None:
        """Multi-byte characters in query produce valid SQL."""
        sql, params = build_symbol_search_query(
            query="函数", path=None, type_filter=None, limit=10, offset=0
        )
        _roundtrip(sql)
        assert any("函数" in str(p) for p in params)

    def test_sql_injection_in_query(self) -> None:
        """SQL injection attempt is safely parameterized, not interpolated."""
        sql, params = build_symbol_search_query(
            query="'; DROP TABLE symbols; --",
            path=None,
            type_filter=None,
            limit=10,
            offset=0,
        )
        _roundtrip(sql)
        # Injection string is in params, not in SQL text
        assert "DROP" not in sql
        assert any("DROP" in str(p) for p in params)

    def test_limit_zero(self) -> None:
        """limit=0 produces valid SQL (caller is responsible for clamping)."""
        sql, params = build_symbol_search_query(
            query="x", path=None, type_filter=None, limit=0, offset=0
        )
        _roundtrip(sql)
        assert 0 in params

    def test_very_large_offset(self) -> None:
        """Offset beyond any realistic total produces valid SQL."""
        sql, params = build_symbol_search_query(
            query="x", path=None, type_filter=None, limit=10, offset=999999
        )
        _roundtrip(sql)
        assert 999999 in params

    def test_like_wildcard_in_query_escaped(self) -> None:
        """LIKE wildcards in query are escaped — % doesn't match everything."""
        sql, params = build_symbol_search_query(
            query="%", path=None, type_filter=None, limit=10, offset=0
        )
        _roundtrip(sql)
        name_param = params[0]
        assert "!%" in name_param


class TestAdversarialSymbolOverlap:
    """Adversarial battery for build_symbol_overlap_query."""

    def test_duplicate_chunks(self) -> None:
        """Same chunk twice produces valid SQL with duplicate conditions."""
        chunk = {"file_path": "a.py", "start_line": 1, "end_line": 10}
        sql, params = build_symbol_overlap_query([chunk, chunk])
        _roundtrip(sql)
        assert _count_placeholders(sql) == 6

    def test_inverted_range(self) -> None:
        """start_line > end_line (semantically hostile) still produces valid SQL."""
        chunk = {"file_path": "a.py", "start_line": 100, "end_line": 1}
        sql, params = build_symbol_overlap_query([chunk])
        _roundtrip(sql)
        # Params are in order: file_path, end_line, start_line
        assert params == ["a.py", 1, 100]


class TestAdversarialStructuralWalk:
    """Adversarial battery for build_structural_walk_query."""

    def test_duplicate_seed_fqns(self) -> None:
        """Duplicate FQNs in seed list produce valid SQL (IN allows duplicates)."""
        sql, params = build_structural_walk_query(
            seed_fqns=["a::X", "a::X", "a::X"], depth=2, limit=20
        )
        _roundtrip(sql)
        assert _count_placeholders(sql) == 5  # 3 seeds + depth + limit
        assert params[:3] == ["a::X", "a::X", "a::X"]

    def test_depth_zero(self) -> None:
        """depth=0 produces valid SQL — base case only, no recursion."""
        sql, params = build_structural_walk_query(seed_fqns=["a::X"], depth=0, limit=20)
        _roundtrip(sql)
        assert params == ["a::X", 0, 20]

    def test_fqn_with_special_chars(self) -> None:
        """FQNs with dots, colons, underscores as params (not interpolated)."""
        fqn = "my.module::MyClass.__init__"
        sql, params = build_structural_walk_query(seed_fqns=[fqn], depth=2, limit=10)
        _roundtrip(sql)
        assert params[0] == fqn

    def test_large_seed_list(self) -> None:
        """100 seeds produce valid SQL with large IN clause."""
        fqns = [f"mod::sym_{i}" for i in range(100)]
        sql, params = build_structural_walk_query(seed_fqns=fqns, depth=2, limit=50)
        _roundtrip(sql)
        assert _count_placeholders(sql) == 102  # 100 seeds + depth + limit


class TestAdversarialTypeFilter:
    """Adversarial battery for build_type_filter_query."""

    def test_like_wildcards_in_filter_escaped(self) -> None:
        """LIKE wildcards in type_filter are escaped."""
        results = [{"file_path": "a.py", "start_line": 1, "end_line": 10}]
        sql, params = build_type_filter_query(results=results, type_filter="Result[%]")
        _roundtrip(sql)
        # The type_filter param should have escaped %
        filter_param = params[-1]
        assert "!%" in filter_param

    def test_identical_result_entries(self) -> None:
        """Duplicate results produce valid SQL with duplicate OR conditions."""
        result = {"file_path": "a.py", "start_line": 1, "end_line": 10}
        sql, params = build_type_filter_query(
            results=[result, result], type_filter="int"
        )
        _roundtrip(sql)
        assert _count_placeholders(sql) == 7  # 3*2 + 1
