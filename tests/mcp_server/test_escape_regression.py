"""Regression test: LIKE ESCAPE clause survives sqlglot round-trip and executes in DuckDB.

Bug: scope_filter() and _build_symbol_conditions() produce raw SQL fragments
with ESCAPE '\\' that, after sqlglot.parse_one() round-trip, can produce an
escape string DuckDB rejects ("Invalid escape string. Escape string must be
empty or one character.").

This test exercises the full pipeline: query builder → sqlglot → DuckDB execute.
"""

import duckdb
import pytest

from chunkhound.mcp_server.tools.queries.graph import (
    build_boundary_query,
    build_overview_query,
    build_reachability_all_symbols_query,
    build_reachability_reachable_query,
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
        assert len(result) >= 1

    def test_boundary_query_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_boundary_query output runs against DuckDB."""
        sql, params = build_boundary_query(scope="src/auth/", limit=50)
        result = db.execute(sql, params).fetchall()
        # bar→baz crosses the boundary
        assert len(result) >= 1

    def test_overview_with_scope_executes(self, db: duckdb.DuckDBPyConnection) -> None:
        """build_overview_query with scope runs against DuckDB."""
        sql, params = build_overview_query(scope="src/auth/", limit=20)
        result = db.execute(sql, params).fetchall()
        assert len(result) >= 1


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
