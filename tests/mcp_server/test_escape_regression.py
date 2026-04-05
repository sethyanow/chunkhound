"""Regression tests: LIKE ESCAPE clauses execute correctly in DuckDB.

Verifies query builders produce valid SQL with proper ESCAPE handling
and that LIKE-special characters in scope/query values are escaped.
"""

import duckdb
import pytest

from chunkhound.mcp_server.tools.queries.graph import (
    build_boundary_query,
    build_overview_query,
    build_reachability_all_symbols_query,
    build_reachability_reachable_query,
    build_walk_edges_query,
    build_walk_query,
)
from chunkhound.mcp_server.tools.queries.search import (
    build_symbol_count_query,
    build_symbol_search_query,
    build_type_filter_query,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def db() -> duckdb.DuckDBPyConnection:
    """In-memory DuckDB with minimal schema for query execution."""
    conn = duckdb.connect()
    conn.execute("""
        CREATE TABLE symbols (
            fqn TEXT, name TEXT, kind TEXT, language TEXT,
            file_path TEXT, file_id INTEGER,
            range_start INTEGER, range_end INTEGER,
            type_signature TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE symbol_edges (
            from_fqn TEXT, to_fqn TEXT, edge_kind TEXT,
            from_file TEXT, to_file TEXT
        )
    """)
    conn.execute("""
        INSERT INTO symbols VALUES
            ('mod::foo', 'foo', 'function', 'python', 'src/auth/login.py', 1, 10, 20, '() -> bool'),
            ('mod::bar', 'bar', 'function', 'python', 'src/auth/utils.py', 2, 5, 15, '(str) -> int'),
            ('mod::baz', 'baz', 'class', 'python', 'lib/external.py', 3, 1, 50, NULL)
    """)
    conn.execute("""
        INSERT INTO symbol_edges VALUES
            ('mod::foo', 'mod::bar', 'calls', 'src/auth/login.py', 'src/auth/utils.py'),
            ('mod::bar', 'mod::baz', 'references', 'src/auth/utils.py', 'lib/external.py')
    """)
    return conn


class TestScopeFilterEscapeExecution:
    """Verify ESCAPE clause in scope-filtered queries executes in DuckDB."""

    def test_reachability_all_symbols_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_reachability_all_symbols_query output runs against DuckDB."""
        sql, params = build_reachability_all_symbols_query(scope="src/auth/")
        result = db.execute(sql, params).fetchall()
        assert len(result) == 2  # foo and bar

    def test_reachability_reachable_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_reachability_reachable_query output runs against DuckDB."""
        sql, params = build_reachability_reachable_query(scope="src/auth/")
        result = db.execute(sql, params).fetchall()
        fqns = {row[0] for row in result}
        # foo and bar are in scope; baz excluded (lib/external.py not in src/auth/)
        assert fqns == {"mod::foo", "mod::bar"}

    def test_boundary_query_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_boundary_query output runs against DuckDB."""
        sql, params = build_boundary_query(scope="src/auth/", limit=50)
        result = db.execute(sql, params).fetchall()
        # bar→baz crosses the boundary (src/auth/ → lib/external/)
        assert len(result) == 1

    def test_overview_with_scope_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_overview_query with scope runs against DuckDB."""
        sql, params = build_overview_query(scope="src/auth/", limit=20)
        result = db.execute(sql, params).fetchall()
        # foo has 1 edge (calls bar), bar has 2 edges (called by foo, references baz)
        assert len(result) == 2


class TestSymbolSearchEscapeExecution:
    """Verify ESCAPE clause in symbol search queries executes in DuckDB."""

    def test_symbol_search_with_path_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_symbol_search_query with path filter runs against DuckDB."""
        sql, params = build_symbol_search_query(
            query="foo", path="src/auth/", type_filter=None, limit=10, offset=0
        )
        result = db.execute(sql, params).fetchall()
        assert len(result) == 1

    def test_symbol_search_with_type_filter_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_symbol_search_query with type_filter runs against DuckDB."""
        sql, params = build_symbol_search_query(
            query="bar", path=None, type_filter="int", limit=10, offset=0
        )
        result = db.execute(sql, params).fetchall()
        assert len(result) == 1

    def test_symbol_search_all_filters_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """All filters active: query + path + type_filter."""
        sql, params = build_symbol_search_query(
            query="bar", path="src/", type_filter="int", limit=10, offset=0
        )
        result = db.execute(sql, params).fetchall()
        assert len(result) == 1

    def test_symbol_count_with_path_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_symbol_count_query with path filter runs against DuckDB."""
        sql, params = build_symbol_count_query(
            query="", path="src/auth/", type_filter=None
        )
        result = db.execute(sql, params).fetchall()
        assert result[0][0] == 2

    def test_type_filter_query_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_type_filter_query output runs against DuckDB."""
        results = [{"file_path": "src/auth/login.py", "start_line": 10, "end_line": 20}]
        sql, params = build_type_filter_query(results=results, type_filter="bool")
        result = db.execute(sql, params).fetchall()
        assert len(result) == 1


class TestEscapeLikeSpecialCharsExecution:
    """Verify LIKE-special characters in scope/query are properly escaped."""

    def test_scope_with_underscore(self, db: duckdb.DuckDBPyConnection) -> None:
        """Underscore in scope path is escaped (not treated as single-char wildcard)."""
        # "src_auth" should NOT match "src/auth" — underscore must be literal
        sql, params = build_reachability_all_symbols_query(scope="src_auth/")
        result = db.execute(sql, params).fetchall()
        assert len(result) == 0

    def test_query_with_percent(self, db: duckdb.DuckDBPyConnection) -> None:
        """Percent in query is escaped (not treated as multi-char wildcard)."""
        sql, params = build_symbol_search_query(
            query="%", path=None, type_filter=None, limit=10, offset=0
        )
        result = db.execute(sql, params).fetchall()
        # No symbol names contain literal "%"
        assert len(result) == 0

    def test_scope_with_exclamation_mark(self, db: duckdb.DuckDBPyConnection) -> None:
        """Exclamation mark (escape char itself) in scope is double-escaped."""
        sql, params = build_reachability_all_symbols_query(scope="src!/")
        result = db.execute(sql, params).fetchall()
        assert len(result) == 0


class TestQueryBuildersNoSqlglotMutation:
    """Query builders must return SQL without sqlglot normalization artifacts.

    sqlglot.parse_one() round-trip mutates valid SQL in ways that are cosmetic
    but prove an unnecessary processing step that has caused real bugs (e.g.
    backslash ESCAPE mangling). These tests assert the SQL output preserves
    the hand-written form.
    """

    def test_boundary_query_preserves_not_like(self) -> None:
        """NOT LIKE must remain as NOT LIKE, not be rewritten to NOT x LIKE."""
        sql, _params = build_boundary_query(scope="src/", limit=10)
        assert "NOT LIKE" in sql, (
            f"Expected 'NOT LIKE' in SQL but got sqlglot-rewritten form. SQL: {sql}"
        )

    def test_reachability_query_preserves_escape_clause(self) -> None:
        """ESCAPE '!' must appear exactly as written, not normalized."""
        sql, _params = build_reachability_reachable_query(scope="src/auth/")
        assert "ESCAPE '!'" in sql

    def test_reachability_all_preserves_escape_clause(self) -> None:
        """ESCAPE '!' in simple scope query preserved."""
        sql, _params = build_reachability_all_symbols_query(scope="src/")
        assert "ESCAPE '!'" in sql

    def test_overview_query_preserves_escape_clause(self) -> None:
        """ESCAPE '!' in overview scope clause preserved."""
        sql, _params = build_overview_query(scope="src/", limit=10)
        assert "ESCAPE '!'" in sql

    def test_walk_query_no_sqlglot_import_needed(self) -> None:
        """Walk query executes without sqlglot dependency at call time."""
        sql, params = build_walk_query(
            symbol="test::fqn", depth=2, edge_kind=None, limit=10
        )
        assert "?" in sql
        assert len(params) == 3  # symbol, depth, limit

    def test_walk_query_with_edge_kind_filter(self) -> None:
        """Walk query with edge_kind has correct param count."""
        sql, params = build_walk_query(
            symbol="test::fqn", depth=2, edge_kind="calls", limit=10
        )
        assert len(params) == 4  # symbol, depth, edge_kind, limit

    def test_boundary_query_has_four_escape_clauses(self) -> None:
        """Boundary query uses ESCAPE '!' in all four LIKE conditions."""
        sql, _params = build_boundary_query(scope="src/", limit=10)
        escape_count = sql.count("ESCAPE '!'")
        assert escape_count == 4, f"Expected 4 ESCAPE clauses, got {escape_count}"

    def test_walk_edges_query_structure(self) -> None:
        """Walk edges query has correct placeholder count for FQN list."""
        fqns = ["a::b", "c::d", "e::f"]
        sql, params = build_walk_edges_query(fqns=fqns, edge_kind=None)
        # 3 fqns × 2 (from_fqn IN + to_fqn IN)
        assert len(params) == 6
        assert sql.count("?") == 6
