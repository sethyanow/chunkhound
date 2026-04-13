"""Tests for chunkhound.mcp_server.tools.queries.search — sqlglot search query builders.

ch-nxu Step 16 removed tests for build_symbol_search_query, build_symbol_count_query,
and build_type_filter_query — the builders were absorbed into
DuckDBProvider.search_symbols / filter_chunks_by_symbol_type_signature, with
behavioral coverage in tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
The remaining 3 builders (overlap, walk, resolution) will go in Step 17.
"""

import pytest
import sqlglot
from sqlglot import exp

from chunkhound.mcp_server.tools.queries.search import (
    build_chunk_resolution_query,
    build_structural_walk_query,
    build_symbol_overlap_query,
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


# build_symbol_search_query / build_symbol_count_query test classes removed by
# ch-nxu Step 16 — see tests/integration/test_{duckdb,lancedb}_symbol_protocol.py
# (TestDuckDBSearchSymbolsContract, TestLanceDBSearchSymbolsContract) for
# real-database behavioral coverage.


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


# build_type_filter_query test class removed by ch-nxu Step 16 — see
# TestDuckDBFilterChunksByTypeSignature / TestLanceDBFilterChunksByTypeSignature.


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


# TestAdversarialSymbolSearch removed by ch-nxu Step 16 — LIKE-escape,
# unicode, injection, pagination edge cases now tested via real-database
# contract tests on both DuckDB and LanceDB backends.


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


# TestAdversarialTypeFilter removed by ch-nxu Step 16 — LIKE-wildcard escape
# and duplicate-result handling now tested in
# TestDuckDBFilterChunksByTypeSignature / TestLanceDBFilterChunksByTypeSignature.
