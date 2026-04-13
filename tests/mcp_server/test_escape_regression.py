"""Regression tests: LIKE ESCAPE clauses execute correctly in DuckDB.

Verifies query builders produce valid SQL with proper ESCAPE handling
and that LIKE-special characters in scope/query values are escaped.

Note: graph query builders were removed by ch-nxu Step 14 (absorbed into
DuckDBProvider). Behavioral coverage for graph LIKE-escape semantics now
lives in tests/integration/test_duckdb_graph_protocol.py. Search query
builders will be removed by Step 16.
"""

import duckdb
import pytest

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


class TestSearchQueryPercentEscape:
    """Verify LIKE-special characters in search query are properly escaped."""

    def test_query_with_percent(self, db: duckdb.DuckDBPyConnection) -> None:
        """Percent in query is escaped (not treated as multi-char wildcard)."""
        sql, params = build_symbol_search_query(
            query="%", path=None, type_filter=None, limit=10, offset=0
        )
        result = db.execute(sql, params).fetchall()
        # No symbol names contain literal "%"
        assert len(result) == 0
