"""Integration tests for DuckDB provider symbol/edge CRUD protocol methods.

Tests that DuckDBProvider correctly implements the symbol/edge protocol
methods declared on DatabaseProvider.
"""

from pathlib import Path

import pytest

from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.integration


def _connect_fresh(tmp_path: Path) -> DuckDBProvider:
    """Create and connect a DuckDBProvider to a fresh temp DB."""
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _insert_test_file(provider: DuckDBProvider) -> int:
    """Insert a test file and return its file_id."""
    provider.execute_query(
        "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
        ["src/example.py", "example.py", ".py", "python", 200],
    )
    rows = provider.execute_query("SELECT id FROM files WHERE path = ?", ["src/example.py"])
    return rows[0]["id"]


class TestDuckDBInsertSymbolsBatch:
    """insert_symbols_batch must batch-insert symbol rows."""

    def test_insert_and_query_roundtrip(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::MyClass",
                name="MyClass",
                kind="Class",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=50,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::MyClass::method",
                name="method",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::MyClass",
                type_signature="(self) -> None",
            ),
        ]

        provider.insert_symbols_batch(symbols)
        result = provider.query_symbols_by_file(file_id)

        assert len(result) == 2
        fqns = {r["fqn"] for r in result}
        assert fqns == {"example::MyClass", "example::MyClass::method"}
        provider.disconnect()

    def test_insert_empty_batch_is_noop(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        provider.insert_symbols_batch([])  # Should not raise
        provider.disconnect()

    def test_large_batch_chunking(self, tmp_path: Path) -> None:
        """Batch insert >500 symbols must succeed (internal chunking)."""
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn=f"example::func_{i}",
                name=f"func_{i}",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=i * 10,
                range_end=i * 10 + 9,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            )
            for i in range(600)
        ]

        provider.insert_symbols_batch(symbols)
        result = provider.query_symbols_by_file(file_id)
        assert len(result) == 600
        provider.disconnect()


class TestDuckDBDeleteSymbolsByFile:
    """delete_symbols_by_file must remove all symbols for a file."""

    def test_delete_clears_file_symbols(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::func",
                name="func",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)
        assert len(provider.query_symbols_by_file(file_id)) == 1

        provider.delete_symbols_by_file(file_id)
        assert len(provider.query_symbols_by_file(file_id)) == 0
        provider.disconnect()


class TestDuckDBDeleteEdgesByFile:
    """delete_edges_by_file must remove edges referencing a file's symbols."""

    def test_delete_clears_file_edges(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        # Insert two symbols
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::caller",
                name="caller",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::callee",
                name="callee",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=15,
                range_end=25,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)

        # Get symbol IDs
        fqn_map = provider.query_symbol_fqns_by_file(file_id)

        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=fqn_map["example::caller"],
                from_fqn="example::caller",
                from_file="src/example.py",
                to_symbol_id=fqn_map["example::callee"],
                to_fqn="example::callee",
                to_file="src/example.py",
                edge_kind="calls",
                confidence=1.0,
                lsp_server="pyright",
            ),
        ]
        provider.insert_edges_batch(edges)

        # Verify edge exists
        edge_rows = provider.execute_query("SELECT * FROM symbol_edges", [])
        assert len(edge_rows) == 1

        provider.delete_edges_by_file(file_id)

        edge_rows = provider.execute_query("SELECT * FROM symbol_edges", [])
        assert len(edge_rows) == 0
        provider.disconnect()


class TestDuckDBQuerySymbolsByRange:
    """query_symbols_by_range returns the innermost symbol at a line."""

    def test_returns_innermost_symbol(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::MyClass",
                name="MyClass",
                kind="Class",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=50,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::MyClass::method",
                name="method",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::MyClass",
                type_signature="(self) -> None",
            ),
        ]
        provider.insert_symbols_batch(symbols)

        result = provider.query_symbols_by_range("src/example.py", 10)
        assert result is not None
        assert result["fqn"] == "example::MyClass::method"  # innermost

        provider.disconnect()

    def test_returns_none_for_no_match(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _insert_test_file(provider)

        result = provider.query_symbols_by_range("src/example.py", 999)
        assert result is None
        provider.disconnect()


class TestDuckDBQuerySymbolsByRangeOverlap:
    """query_symbols_by_range_overlap returns all overlapping symbols."""

    def test_returns_overlapping_symbols(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::func_a",
                name="func_a",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::func_b",
                name="func_b",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=8,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::func_c",
                name="func_c",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=30,
                range_end=40,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)

        result = provider.query_symbols_by_range_overlap("src/example.py", 5, 15)
        fqns = {r["fqn"] for r in result}
        # func_a (0-10) and func_b (8-20) overlap [5,15]; func_c (30-40) does not
        assert fqns == {"example::func_a", "example::func_b"}
        provider.disconnect()


class TestDuckDBQuerySymbolFqnsByFile:
    """query_symbol_fqns_by_file returns fqn→id mapping."""

    def test_returns_fqn_to_id_mapping(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::alpha",
                name="alpha",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)

        fqn_map = provider.query_symbol_fqns_by_file(file_id)
        assert "example::alpha" in fqn_map
        assert isinstance(fqn_map["example::alpha"], int)
        provider.disconnect()


class TestDuckDBQuerySymbolsByFqnExists:
    """query_symbols_by_fqn_exists checks for symbol existence."""

    def test_returns_true_when_exists(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::exists",
                name="exists",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=5,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)

        assert provider.query_symbols_by_fqn_exists("example::exists", "src/example.py") is True
        assert provider.query_symbols_by_fqn_exists("example::nope", "src/example.py") is False
        provider.disconnect()
