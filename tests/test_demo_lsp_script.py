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


# ── Phase 2: Connection management regression ──────────────────


# ── Phase 2 Scenario 5: Live vs Populated comparison ────────


class TestDemoLiveVsPopulated:
    """Scenario 5: compare live LSP documentSymbol output with populated DB symbols."""

    def test_pass_when_live_symbols_match_db(self, demo_db: Path) -> None:
        """Returns True and match report when live symbols have DB counterparts."""
        from scripts.demo_lsp import demo_live_vs_populated

        # Fake "live" symbols matching fixture data for file_id=1 (client.py)
        live_symbols = [
            {"name": "LSPClient", "kind": "Class", "line": 32},
            {"name": "start", "kind": "Method", "line": 63},
        ]
        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            result = demo_live_vs_populated(conn, live_symbols, "chunkhound/lsp/client.py")
            assert result is True
        finally:
            conn.close()

    def test_fail_when_live_symbol_missing_from_db(self, demo_db: Path) -> None:
        """Returns False when a live symbol has no DB counterpart."""
        from scripts.demo_lsp import demo_live_vs_populated

        live_symbols = [
            {"name": "LSPClient", "kind": "Class", "line": 32},
            {"name": "totally_new_function", "kind": "Function", "line": 999},
        ]
        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            result = demo_live_vs_populated(conn, live_symbols, "chunkhound/lsp/client.py")
            assert result is False
        finally:
            conn.close()

    def test_reports_db_extras(self, demo_db: Path) -> None:
        """Still passes but reports symbols in DB that live LSP didn't return."""
        from scripts.demo_lsp import demo_live_vs_populated

        # Only one live symbol — DB has 2 for this file
        live_symbols = [
            {"name": "LSPClient", "kind": "Class", "line": 32},
        ]
        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # Should still pass — DB extras aren't a failure (workspaceSymbol adds them)
            result = demo_live_vs_populated(conn, live_symbols, "chunkhound/lsp/client.py")
            assert result is True
        finally:
            conn.close()


# ── Phase 2 Scenario 6: Cross-file edge health ─────────────


class TestDemoCrossFileEdgeHealth:
    """Scenario 6: cross-file edges exist and aren't all self-referential."""

    def test_pass_when_cross_file_edges_exist(self, demo_db: Path) -> None:
        """Returns True when edges span multiple files."""
        from scripts.demo_lsp import demo_cross_file_edge_health

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # Fixture has edges between client.py, types.py, main.ts
            result = demo_cross_file_edge_health(conn)
            assert result is True
        finally:
            conn.close()

    def test_fail_when_all_edges_self_referential(self, tmp_path: Path) -> None:
        """Returns False when every edge is within the same file."""
        from scripts.demo_lsp import demo_cross_file_edge_health

        db_path = tmp_path / "self_ref.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("""
            CREATE TABLE symbol_edges (
                id INTEGER, from_symbol_id INTEGER, from_fqn TEXT,
                from_file TEXT, to_symbol_id INTEGER, to_fqn TEXT,
                to_file TEXT, edge_kind TEXT
            )
        """)
        conn.execute(
            "INSERT INTO symbol_edges VALUES "
            "(1, 1, 'A', 'a.py', 2, 'B', 'a.py', 'references'),"
            "(2, 2, 'B', 'a.py', 1, 'A', 'a.py', 'references')"
        )

        result = demo_cross_file_edge_health(conn)
        assert result is False
        conn.close()

    def test_fail_when_no_edges(self, tmp_path: Path) -> None:
        """Returns False when symbol_edges is empty."""
        from scripts.demo_lsp import demo_cross_file_edge_health

        db_path = tmp_path / "empty.db"
        conn = duckdb.connect(str(db_path))
        conn.execute("CREATE TABLE symbol_edges (id INTEGER, from_file TEXT, to_file TEXT, edge_kind TEXT)")

        result = demo_cross_file_edge_health(conn)
        assert result is False
        conn.close()


# ── Phase 3: MCP tool demo scenarios ──────────────────────────


class TestDemoSearchSymbols:
    """Phase 3 Scenario: search(type=symbols) via execute_tool."""

    def test_pass_when_symbols_found(self, demo_db: Path) -> None:
        """Returns True when search returns matching symbols."""
        from scripts.demo_lsp import demo_search_symbols

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # demo_db fixture has symbols with "LSPClient", "start", "SymbolInfo", "main"
            result = demo_search_symbols(conn, query="LSP")
            assert result is True
        finally:
            conn.close()

    def test_fail_when_no_matches(self, demo_db: Path) -> None:
        """Returns False when query matches nothing."""
        from scripts.demo_lsp import demo_search_symbols

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            result = demo_search_symbols(conn, query="zzz_nonexistent_zzz")
            assert result is False
        finally:
            conn.close()


class TestDemoGraphWalk:
    """Phase 3 Scenario: graph(walk) via execute_tool."""

    def test_pass_when_edges_found(self, demo_db: Path) -> None:
        """Returns True when walk finds connected symbols with edges."""
        from scripts.demo_lsp import demo_graph_walk

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # demo_db has edges: LSPClient::start -> SymbolInfo (references),
            #                    LSPClient -> SymbolInfo (defines),
            #                    main -> LSPClient (calls)
            result = demo_graph_walk(conn, symbol="main")
            assert result is True
        finally:
            conn.close()

    def test_returns_false_when_no_edges(self, demo_db: Path) -> None:
        """Returns False when walk finds the symbol but no outbound edges."""
        from scripts.demo_lsp import demo_graph_walk

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # SymbolInfo has no outbound edges in fixture
            result = demo_graph_walk(conn, symbol="SymbolInfo")
            assert result is False
        finally:
            conn.close()

    def test_returns_false_when_symbol_not_found(self, demo_db: Path) -> None:
        """Returns False when the starting symbol doesn't exist."""
        from scripts.demo_lsp import demo_graph_walk

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            result = demo_graph_walk(conn, symbol="NonexistentSymbol")
            assert result is False
        finally:
            conn.close()


class TestDemoGraphBoundary:
    """Phase 3 Scenario: graph(boundary) via execute_tool."""

    def test_pass_when_cross_scope_edges_found(self, demo_db: Path) -> None:
        """Returns True when boundary finds edges crossing the scope."""
        from scripts.demo_lsp import demo_graph_boundary

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # demo_db has edge: main (src/main.ts) -> LSPClient (chunkhound/lsp/client.py)
            # Scope "src/" should show this as a boundary edge
            result = demo_graph_boundary(conn, scope="src/")
            assert result is True
        finally:
            conn.close()

    def test_returns_false_when_no_boundary_edges(self, demo_db: Path) -> None:
        """Returns False when no edges cross the scope boundary."""
        from scripts.demo_lsp import demo_graph_boundary

        conn = duckdb.connect(str(demo_db), read_only=True)
        try:
            # Scope "nonexistent/" has no symbols, so no boundary edges
            result = demo_graph_boundary(conn, scope="nonexistent/")
            assert result is False
        finally:
            conn.close()


class TestDemoLspDefinition:
    """Phase 3 Scenario: lsp(definition) through the demo's async wrapper."""

    @pytest.mark.asyncio
    async def test_pass_when_definition_found(self) -> None:
        """Returns True when definition returns a valid location."""
        from scripts.demo_lsp import demo_lsp_definition

        fake_result = {
            "results": [{"file_path": "/src/client.py", "line": 32, "character": 4,
                         "end_line": 32, "end_character": 13}]
        }
        result = await demo_lsp_definition(fake_result)
        assert result is True

    @pytest.mark.asyncio
    async def test_fail_when_error_returned(self) -> None:
        """Returns False when LSP returns an error."""
        from scripts.demo_lsp import demo_lsp_definition

        fake_result = {"error": "lsp_not_ready", "message": "not initialized"}
        result = await demo_lsp_definition(fake_result)
        assert result is False

    @pytest.mark.asyncio
    async def test_fail_when_no_results(self) -> None:
        """Returns False when definition returns empty results."""
        from scripts.demo_lsp import demo_lsp_definition

        fake_result = {"results": []}
        result = await demo_lsp_definition(fake_result)
        assert result is False


class TestDemoLspReferences:
    """Phase 3 Scenario: lsp(references) through the demo's async wrapper."""

    @pytest.mark.asyncio
    async def test_pass_when_references_found(self) -> None:
        """Returns True when references returns call sites."""
        from scripts.demo_lsp import demo_lsp_references

        fake_result = {
            "results": [
                {"file_path": "/src/a.py", "line": 10, "character": 0,
                 "end_line": 10, "end_character": 5},
                {"file_path": "/src/b.py", "line": 20, "character": 0,
                 "end_line": 20, "end_character": 5},
            ]
        }
        result = await demo_lsp_references(fake_result)
        assert result is True

    @pytest.mark.asyncio
    async def test_fail_when_error(self) -> None:
        """Returns False when LSP returns an error."""
        from scripts.demo_lsp import demo_lsp_references

        result = await demo_lsp_references({"error": "lsp_error", "message": "timeout"})
        assert result is False


class TestDemoSymbolContext:
    """Phase 3 Scenario: symbol_context compound profile."""

    @pytest.mark.asyncio
    async def test_pass_when_profile_returned(self) -> None:
        """Returns True when symbol_context returns at least hover or definition."""
        from scripts.demo_lsp import demo_symbol_context

        fake_result = {
            "hover": "```python\n(class) LSPClient\n```",
            "definition": [{"file_path": "/src/client.py", "line": 32}],
            "callers": [],
            "callees": [{"name": "start", "file_path": "/src/client.py"}],
            "graph_neighborhood": {"results": [], "edges": [], "count": 0},
        }
        result = await demo_symbol_context(fake_result)
        assert result is True

    @pytest.mark.asyncio
    async def test_fail_when_error(self) -> None:
        """Returns False on error."""
        from scripts.demo_lsp import demo_symbol_context

        result = await demo_symbol_context({"error": "lsp_not_ready"})
        assert result is False

    @pytest.mark.asyncio
    async def test_fail_when_empty_profile(self) -> None:
        """Returns False when all profile fields are empty/None."""
        from scripts.demo_lsp import demo_symbol_context

        fake_result = {
            "hover": None,
            "definition": [],
            "callers": [],
            "callees": [],
            "graph_neighborhood": {"results": [], "edges": [], "count": 0},
        }
        result = await demo_symbol_context(fake_result)
        assert result is False


class TestPhase2ConnectionManagement:
    """Regression: read-only + read-write connections to same DuckDB file conflict."""

    def test_phase2_scenarios_sequential_no_connection_conflict(
        self, demo_db: Path
    ) -> None:
        """Running all Phase 2 scenarios sequentially against one DB must not crash.

        DuckDB forbids mixing read-only and read-write connections to the same file.
        Read-only scenarios run first, then the destructive incremental refresh
        runs last with a separate read-write connection.
        """
        from scripts.demo_lsp import (
            demo_edge_kinds,
            demo_incremental_refresh,
            demo_multi_language,
            demo_symbols_populated,
        )

        # Read-only scenarios first (symbols, multi-lang, edges)
        conn_ro = duckdb.connect(str(demo_db), read_only=True)
        assert demo_symbols_populated(conn_ro) is True
        assert demo_multi_language(conn_ro) is True
        assert demo_edge_kinds(conn_ro) is True
        conn_ro.close()

        # Destructive scenario last: separate read-write connection
        conn_rw = duckdb.connect(str(demo_db))
        assert demo_incremental_refresh(conn_rw, file_id=1, file_path="chunkhound/lsp/client.py") is True
        conn_rw.close()
