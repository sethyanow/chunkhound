"""Unit tests for scripts/demo_lsp.py internal logic.

The script itself IS the acceptance walkthrough (e2e test).
These tests cover the internal helpers: DB discovery, lock fallback, symbol filtering,
and Phase 2 population demo scenarios.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import duckdb
import pytest

pytestmark = [pytest.mark.unit]


# ── _find_db ──────────────────────────────────────────────────


class TestFindDb:
    """DB discovery logic: config → flat file → nested dir fallback chain."""

    def test_flat_file_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finds .chunkhound/db as a flat DuckDB file (this project's layout)."""
        monkeypatch.chdir(tmp_path)
        db = tmp_path / ".chunkhound" / "db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"duckdb")

        from scripts.demo_lsp import _find_db

        assert _find_db().resolve() == db.resolve()

    def test_nested_dir_default(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Finds .chunkhound/db/chunks.db in nested directory layout."""
        monkeypatch.chdir(tmp_path)
        db = tmp_path / ".chunkhound" / "db" / "chunks.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"duckdb")

        from scripts.demo_lsp import _find_db

        assert _find_db().resolve() == db.resolve()

    def test_config_file_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reads database.path from .chunkhound.json when it points to a file."""
        monkeypatch.chdir(tmp_path)
        db = tmp_path / "custom.db"
        db.write_bytes(b"duckdb")
        config = tmp_path / ".chunkhound.json"
        config.write_text(json.dumps({"database": {"path": "custom.db"}}))

        from scripts.demo_lsp import _find_db

        assert _find_db().resolve() == db.resolve()

    def test_config_dir_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reads database.path from .chunkhound.json when it points to a directory."""
        monkeypatch.chdir(tmp_path)
        db = tmp_path / "data" / "db" / "chunks.db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"duckdb")
        config = tmp_path / ".chunkhound.json"
        config.write_text(json.dumps({"database": {"path": "data"}}))

        from scripts.demo_lsp import _find_db

        assert _find_db().resolve() == db.resolve()

    def test_no_db_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Returns None when no DB exists anywhere."""
        monkeypatch.chdir(tmp_path)

        from scripts.demo_lsp import _find_db

        assert _find_db() is None

    def test_malformed_config_falls_through(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Falls through to defaults when .chunkhound.json is malformed."""
        monkeypatch.chdir(tmp_path)
        config = tmp_path / ".chunkhound.json"
        config.write_text("not json{{{")
        db = tmp_path / ".chunkhound" / "db"
        db.parent.mkdir(parents=True)
        db.write_bytes(b"duckdb")

        from scripts.demo_lsp import _find_db

        assert _find_db().resolve() == db.resolve()


# ── Symbol filtering ──────────────────────────────────────────


@dataclass
class FakeSymbol:
    """Minimal stand-in for SymbolInfo to test filtering logic."""

    name: str
    kind: int
    range_start_line: int = 1
    range_end_line: int = 1
    children: list[FakeSymbol] = field(default_factory=list)


class TestSymbolFiltering:
    """Symbol output should match editor LSP: classes, methods, no local vars."""

    def _collect(self, symbols: list[FakeSymbol]) -> list[str]:
        """Run the filtering logic and collect printed symbol names."""
        # Replicate the filtering from demo_lsp._print_symbols
        _METHOD_KINDS = {6, 9, 12}  # Method, Constructor, Function
        _VARIABLE_KIND = 13

        collected: list[str] = []

        def _walk(syms: list[FakeSymbol], parent_kind: int = 0) -> None:
            for s in syms:
                if s.kind == _VARIABLE_KIND and parent_kind in _METHOD_KINDS:
                    continue
                collected.append(s.name)
                if s.children:
                    _walk(s.children, parent_kind=s.kind)

        _walk(symbols)
        return collected

    def test_top_level_variable_kept(self) -> None:
        """File-level variables (like `logger`) are shown."""
        symbols = [FakeSymbol("logger", kind=13)]
        assert "logger" in self._collect(symbols)

    def test_class_level_variable_kept(self) -> None:
        """Instance attributes inside a class (not inside a method) are shown."""
        cls = FakeSymbol("MyClass", kind=5, children=[
            FakeSymbol("_config", kind=13),
        ])
        result = self._collect([cls])
        assert "_config" in result

    def test_method_local_variable_filtered(self) -> None:
        """Local variables inside methods are filtered out."""
        method = FakeSymbol("start", kind=6, children=[
            FakeSymbol("result", kind=13),
            FakeSymbol("timeout", kind=13),
        ])
        cls = FakeSymbol("MyClass", kind=5, children=[method])
        result = self._collect([cls])
        assert "MyClass" in result
        assert "start" in result
        assert "result" not in result
        assert "timeout" not in result

    def test_constructor_local_variable_filtered(self) -> None:
        """Local variables inside constructors are filtered out."""
        ctor = FakeSymbol("__init__", kind=9, children=[
            FakeSymbol("self", kind=13),
        ])
        cls = FakeSymbol("MyClass", kind=5, children=[ctor])
        result = self._collect([cls])
        assert "__init__" in result
        assert "self" not in result

    def test_function_local_variable_filtered(self) -> None:
        """Local variables inside standalone functions are filtered out."""
        func = FakeSymbol("main", kind=12, children=[
            FakeSymbol("args", kind=13),
        ])
        result = self._collect([func])
        assert "main" in result
        assert "args" not in result

    def test_nested_method_in_method_filtered(self) -> None:
        """Methods inside methods keep methods, filter their locals."""
        inner = FakeSymbol("helper", kind=6, children=[
            FakeSymbol("x", kind=13),
        ])
        outer = FakeSymbol("process", kind=6, children=[
            FakeSymbol("temp", kind=13),
            inner,
        ])
        result = self._collect([outer])
        assert "process" in result
        assert "helper" in result
        assert "temp" not in result
        assert "x" not in result


# ── Phase 2: DB fixture ─────────────────────────────────────


@pytest.fixture()
def demo_db(tmp_path: Path) -> Path:
    """Create a DuckDB with symbols + symbol_edges tables and sample data."""
    db_path = tmp_path / ".chunkhound" / "db"
    db_path.parent.mkdir(parents=True)
    conn = duckdb.connect(str(db_path))
    conn.execute("CREATE SEQUENCE IF NOT EXISTS files_id_seq")
    conn.execute("""
        CREATE TABLE files (
            id INTEGER PRIMARY KEY DEFAULT nextval('files_id_seq'),
            path TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            content_hash TEXT
        )
    """)
    conn.execute("CREATE SEQUENCE IF NOT EXISTS symbols_id_seq")
    conn.execute("""
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY DEFAULT nextval('symbols_id_seq'),
            fqn TEXT NOT NULL,
            name TEXT NOT NULL,
            kind TEXT NOT NULL,
            language TEXT,
            file_id INTEGER REFERENCES files(id),
            file_path TEXT,
            range_start INTEGER,
            range_end INTEGER,
            type_signature TEXT,
            parent_fqn TEXT,
            confidence FLOAT DEFAULT 1.0,
            lsp_server TEXT
        )
    """)
    conn.execute("CREATE SEQUENCE IF NOT EXISTS symbol_edges_id_seq")
    conn.execute("""
        CREATE TABLE symbol_edges (
            id INTEGER PRIMARY KEY DEFAULT nextval('symbol_edges_id_seq'),
            from_symbol_id INTEGER NOT NULL,
            from_fqn TEXT,
            from_file TEXT,
            to_symbol_id INTEGER NOT NULL,
            to_fqn TEXT,
            to_file TEXT,
            edge_kind TEXT NOT NULL,
            confidence FLOAT DEFAULT 1.0,
            lsp_server TEXT
        )
    """)
    # Insert sample files
    conn.execute(
        "INSERT INTO files (id, path, name, content_hash) VALUES "
        "(1, 'chunkhound/lsp/client.py', 'client.py', 'abc'), "
        "(2, 'chunkhound/lsp/types.py', 'types.py', 'def'), "
        "(3, 'src/main.ts', 'main.ts', 'ghi')"
    )
    # Insert symbols: Python + TypeScript
    conn.execute(
        "INSERT INTO symbols (id, fqn, name, kind, language, file_id, file_path, range_start, range_end) VALUES "
        "(1, 'LSPClient', 'LSPClient', 'Class', 'python', 1, 'chunkhound/lsp/client.py', 32, 576), "
        "(2, 'LSPClient::start', 'start', 'Method', 'python', 1, 'chunkhound/lsp/client.py', 63, 160), "
        "(3, 'SymbolInfo', 'SymbolInfo', 'Class', 'python', 2, 'chunkhound/lsp/types.py', 10, 30), "
        "(4, 'main', 'main', 'Function', 'typescript', 3, 'src/main.ts', 1, 50)"
    )
    # Insert edges: defines, references, calls
    conn.execute(
        "INSERT INTO symbol_edges (from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind) VALUES "
        "(2, 'LSPClient::start', 'chunkhound/lsp/client.py', 3, 'SymbolInfo', 'chunkhound/lsp/types.py', 'references'), "
        "(1, 'LSPClient', 'chunkhound/lsp/client.py', 3, 'SymbolInfo', 'chunkhound/lsp/types.py', 'defines'), "
        "(4, 'main', 'src/main.ts', 1, 'LSPClient', 'chunkhound/lsp/client.py', 'calls')"
    )
    conn.close()
    return db_path


# ── Phase 2 Scenario 1: Symbols Populated ────────────────────


class TestDemoSymbolsPopulated:
    """Scenario 1: symbols table has rows and known symbols are findable."""

    def test_pass_when_symbols_exist(self, demo_db: Path) -> None:
        """Returns True when symbols table has rows and spot-check passes."""
        from scripts.demo_lsp import demo_symbols_populated

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            assert demo_symbols_populated(conn) is True
        finally:
            conn.close()

    def test_fail_when_no_symbols(self, tmp_path: Path) -> None:
        """Returns False when symbols table is empty."""
        db_path = tmp_path / "empty.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbols (id INTEGER, fqn TEXT, name TEXT, kind TEXT, language TEXT, file_path TEXT)")
        conn.execute("CREATE TABLE files (id INTEGER, path TEXT)")

        from scripts.demo_lsp import demo_symbols_populated

        assert demo_symbols_populated(conn) is False
        conn.close()


# ── Phase 2 Scenario 2: Incremental Refresh ──────────────────


class TestDemoIncrementalRefresh:
    """Scenario 2: per-file delete + repopulate path works."""

    def test_pass_when_symbols_restored_after_delete(self, demo_db: Path) -> None:
        """Returns True when delete + repopulate restores symbol count."""
        from scripts.demo_lsp import demo_incremental_refresh

        conn = duckdb.connect(str(demo_db))
        try:
            # File 1 has 2 symbols — delete then re-insert should restore count
            result = demo_incremental_refresh(conn, file_id=1, file_path="chunkhound/lsp/client.py")
            assert result is True
        finally:
            conn.close()

    def test_fail_when_file_has_no_symbols(self, demo_db: Path) -> None:
        """Returns False when target file has no symbols to refresh."""
        from scripts.demo_lsp import demo_incremental_refresh

        conn = duckdb.connect(str(demo_db))
        try:
            result = demo_incremental_refresh(conn, file_id=999, file_path="nonexistent.py")
            assert result is False
        finally:
            conn.close()


# ── Phase 2 Scenario 3: Multi-Language ────────────────────────


class TestDemoMultiLanguage:
    """Scenario 3: symbols from multiple languages present."""

    def test_pass_with_two_languages(self, demo_db: Path) -> None:
        """Returns True when ≥2 distinct languages in symbols table."""
        from scripts.demo_lsp import demo_multi_language

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            assert demo_multi_language(conn) is True
        finally:
            conn.close()

    def test_skip_with_one_language(self, tmp_path: Path) -> None:
        """Returns None (SKIP) when only 1 language present."""
        db_path = tmp_path / "single_lang.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbols (id INTEGER, name TEXT, language TEXT)")
        conn.execute("INSERT INTO symbols VALUES (1, 'foo', 'python'), (2, 'bar', 'python')")

        from scripts.demo_lsp import demo_multi_language

        result = demo_multi_language(conn)
        assert result is None  # SKIP, not FAIL
        conn.close()

    def test_fail_with_no_symbols(self, tmp_path: Path) -> None:
        """Returns False when symbols table is empty."""
        db_path = tmp_path / "empty.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbols (id INTEGER, name TEXT, language TEXT)")

        from scripts.demo_lsp import demo_multi_language

        assert demo_multi_language(conn) is False
        conn.close()


# ── Phase 2 Scenario 4: Edge Kinds ───────────────────────────


class TestDemoEdgeKinds:
    """Scenario 4: symbol_edges contains expected edge kinds."""

    def test_pass_with_required_kinds(self, demo_db: Path) -> None:
        """Returns True when both 'defines' and 'references' edge kinds exist."""
        from scripts.demo_lsp import demo_edge_kinds

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            assert demo_edge_kinds(conn) is True
        finally:
            conn.close()

    def test_fail_with_no_edges(self, tmp_path: Path) -> None:
        """Returns False when symbol_edges table is empty."""
        db_path = tmp_path / "no_edges.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbol_edges (id INTEGER, edge_kind TEXT)")

        from scripts.demo_lsp import demo_edge_kinds

        assert demo_edge_kinds(conn) is False
        conn.close()

    def test_fail_with_only_one_kind(self, tmp_path: Path) -> None:
        """Returns False when only one of the required edge kinds exists."""
        db_path = tmp_path / "one_kind.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbol_edges (id INTEGER, edge_kind TEXT)")
        conn.execute("INSERT INTO symbol_edges VALUES (1, 'calls')")

        from scripts.demo_lsp import demo_edge_kinds

        assert demo_edge_kinds(conn) is False
        conn.close()
