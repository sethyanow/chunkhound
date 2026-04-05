"""Regression tests for delete_file_completely cascade (ch-zn5).

Bug: delete_file_completely deletes embeddings → chunks → files but leaves
orphaned symbols and symbol_edges rows. When LSP population later tries to
repopulate the same file, it hits FK violations on symbol_edges deletion
because the edges reference symbol IDs whose parent file row is gone.

This caused a FATAL DuckDB HNSW index corruption on daemon restart (2026-04-05).
"""

from pathlib import Path

import pytest

from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.integration


def _connect_fresh(tmp_path: Path) -> DuckDBProvider:
    """Create and connect a DuckDBProvider to a fresh temp DB."""
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _insert_file_with_symbols(provider: DuckDBProvider) -> int:
    """Insert a file with chunks, symbols, and symbol_edges. Returns file_id."""
    # Insert file
    provider.execute_query(
        "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
        ["test/example.py", "example.py", ".py", "python", 100],
    )
    rows = provider.execute_query("SELECT id FROM files WHERE path = ?", ["test/example.py"])
    file_id = rows[0]["id"]

    # Insert chunks
    provider.execute_query(
        "INSERT INTO chunks (file_id, chunk_type, symbol, code, start_line, end_line, "
        "start_byte, end_byte, language) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [file_id, "function", "my_func", "def my_func(): pass", 1, 1, 0, 20, "python"],
    )

    # Insert symbols
    provider.execute_query(
        "INSERT INTO symbols (fqn, name, kind, language, file_id, file_path, "
        "range_start, range_end, confidence, lsp_server) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["test.example::my_func", "my_func", "Function", "python", file_id,
         "test/example.py", 1, 1, 1.0, "pyright"],
    )
    provider.execute_query(
        "INSERT INTO symbols (fqn, name, kind, language, file_id, file_path, "
        "range_start, range_end, confidence, lsp_server) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ["test.example::helper", "helper", "Function", "python", file_id,
         "test/example.py", 2, 5, 1.0, "pyright"],
    )

    # Get symbol IDs for edges
    sym_rows = provider.execute_query(
        "SELECT id, fqn FROM symbols WHERE file_id = ?", [file_id]
    )
    sym_a = sym_rows[0]
    sym_b = sym_rows[1]

    # Insert symbol_edges (sym_a calls sym_b)
    provider.execute_query(
        "INSERT INTO symbol_edges (from_symbol_id, from_fqn, from_file, "
        "to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [sym_a["id"], sym_a["fqn"], "test/example.py",
         sym_b["id"], sym_b["fqn"], "test/example.py",
         "calls", 1.0, "pyright"],
    )

    return file_id


def test_delete_file_completely_leaves_orphaned_symbols(tmp_path: Path) -> None:
    """Regression: delete_file_completely must not leave orphaned symbols/edges.

    Before the fix, deleting a file left symbols and symbol_edges rows.
    Subsequent LSP population would hit FK violations trying to clean up
    the orphaned data, triggering a FATAL DuckDB HNSW index corruption.
    """
    provider = _connect_fresh(tmp_path)
    file_id = _insert_file_with_symbols(provider)

    # Verify data exists
    symbols = provider.execute_query("SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id])
    edges = provider.execute_query(
        "SELECT COUNT(*) AS cnt FROM symbol_edges WHERE "
        "from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR "
        "to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)",
        [file_id, file_id],
    )
    assert symbols[0]["cnt"] == 2
    assert edges[0]["cnt"] == 1

    # Delete the file
    provider.delete_file_completely("test/example.py")

    # After deletion, symbols and edges must be gone
    symbols_after = provider.execute_query("SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id])
    edges_after = provider.execute_query(
        "SELECT COUNT(*) AS cnt FROM symbol_edges WHERE "
        "from_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?) OR "
        "to_symbol_id IN (SELECT id FROM symbols WHERE file_id = ?)",
        [file_id, file_id],
    )

    assert symbols_after[0]["cnt"] == 0, (
        f"Orphaned symbols remain after delete_file_completely: {symbols_after[0]['cnt']} rows"
    )
    assert edges_after[0]["cnt"] == 0, (
        f"Orphaned symbol_edges remain after delete_file_completely: {edges_after[0]['cnt']} rows"
    )


def test_delete_file_completely_full_cascade(tmp_path: Path) -> None:
    """All five tables must be clean after delete_file_completely."""
    provider = _connect_fresh(tmp_path)
    file_id = _insert_file_with_symbols(provider)

    provider.delete_file_completely("test/example.py")

    # Verify every table is clean for this file
    assert provider.execute_query("SELECT COUNT(*) AS cnt FROM files WHERE id = ?", [file_id])[0]["cnt"] == 0
    assert provider.execute_query("SELECT COUNT(*) AS cnt FROM chunks WHERE file_id = ?", [file_id])[0]["cnt"] == 0
    assert provider.execute_query("SELECT COUNT(*) AS cnt FROM symbols WHERE file_id = ?", [file_id])[0]["cnt"] == 0
    # Edges query uses subselect — if symbols are gone, this returns 0 regardless.
    # Also check by direct edge IDs to catch truly orphaned edges.
    all_edges = provider.execute_query("SELECT COUNT(*) AS cnt FROM symbol_edges")
    assert all_edges[0]["cnt"] == 0, "Orphaned symbol_edges remain in table"
