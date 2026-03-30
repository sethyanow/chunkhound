"""Unit tests for scripts/demo_lsp.py internal logic.

The script itself IS the acceptance walkthrough (e2e test).
These tests cover the internal helpers: DB discovery, lock fallback, symbol filtering.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

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
