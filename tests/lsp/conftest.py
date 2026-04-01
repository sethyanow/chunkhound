"""Shared fixtures and helpers for LSP population tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from chunkhound.lsp.types import SymbolInfo
from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.unit


# --- Helper functions (take arguments, not fixtures) ---


def _make_provider(tmp_path: Path) -> DuckDBProvider:
    """Create a connected DuckDBProvider with schema initialized."""
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _insert_file(provider: DuckDBProvider, file_id: int, path: str) -> None:
    """Insert a row into the files table so FK constraints are satisfied."""
    name = Path(path).name
    provider.execute_query(
        "INSERT INTO files (id, path, name, content_hash) VALUES (?, ?, ?, ?)",
        [file_id, path, name, "abc123"],
    )


def _make_mock_pool(symbols: list[SymbolInfo] | None = None) -> tuple[MagicMock, AsyncMock]:
    """Create a mock LSPClientPool returning a mock client with canned symbols."""
    client = AsyncMock()
    client.document_symbols = AsyncMock(return_value=symbols or [])
    client.notify_did_open = AsyncMock()
    client.notify_did_close = AsyncMock()

    pool = AsyncMock()
    pool.get = AsyncMock(return_value=client)
    return pool, client


def _insert_symbols(
    provider: DuckDBProvider,
    file_id: int,
    file_path: str,
    symbols: list[tuple[str, str, str, int, int]],
) -> list[int]:
    """Insert symbols directly and return their IDs.

    Each tuple: (fqn, name, kind, range_start, range_end).
    """
    ids = []
    for fqn, name, kind, start, end in symbols:
        provider.execute_query(
            "INSERT INTO symbols (fqn, name, kind, language, file_id, file_path, "
            "range_start, range_end, confidence, lsp_server) "
            "VALUES (?, ?, ?, 'python', ?, ?, ?, ?, 1.0, 'pyright')",
            [fqn, name, kind, file_id, file_path, start, end],
        )
        rows = provider.execute_query(
            "SELECT id FROM symbols WHERE fqn = ? AND file_id = ?", [fqn, file_id]
        )
        ids.append(rows[0]["id"])
    return ids


def _sample_symbols() -> list[SymbolInfo]:
    """A class with one method — minimal nested hierarchy."""
    method = SymbolInfo(
        name="greet",
        kind=6,  # Method
        range_start_line=5,
        range_start_char=4,
        range_end_line=8,
        range_end_char=0,
        children=[],
    )
    cls = SymbolInfo(
        name="Greeter",
        kind=5,  # Class
        range_start_line=1,
        range_start_char=0,
        range_end_line=8,
        range_end_char=0,
        children=[method],
    )
    func = SymbolInfo(
        name="main",
        kind=12,  # Function
        range_start_line=10,
        range_start_char=0,
        range_end_line=15,
        range_end_char=0,
        children=[],
    )
    return [cls, func]
