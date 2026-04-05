"""Symbol resolution tests — _resolve_symbol and execute_query_async."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chunkhound.services.lsp_population import LSPPopulationService
from tests.lsp.conftest import (
    _insert_file,
    _insert_symbols,
    _make_provider,
)

pytestmark = pytest.mark.unit


class TestResolveSymbol:
    """
    Feature: Resolve a URI + line to the innermost symbol in the DB

    As the edge collection logic
    I want to map LSP result locations (URI + line) to symbol records
    So that edges can reference valid symbol IDs in both endpoints
    """

    @pytest.mark.asyncio
    async def test_finds_innermost_symbol_at_line(self, tmp_path: Path) -> None:
        """
        Scenario: URI + line resolves to the most specific (innermost) symbol
        Given a file with a class (lines 1-8) containing a method (lines 5-8)
        When _resolve_symbol is called with line 6 (inside both)
        Then it returns the method (narrower range), not the class
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        # Class at lines 1-8, method at lines 5-8 — line 6 is inside both
        sym_ids = _insert_symbols(provider, 1, "src/greeter.py", [
            ("Greeter", "Greeter", "Class", 1, 8),
            ("Greeter::greet", "greet", "Method", 5, 8),
        ])

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        uri = (tmp_path / "src" / "greeter.py").as_uri()
        result = await service._resolve_symbol(uri, 6)

        assert result is not None
        symbol_id, fqn, file_path = result
        assert symbol_id == sym_ids[1]  # greet, not Greeter
        assert fqn == "Greeter::greet"
        assert file_path == "src/greeter.py"

    @pytest.mark.asyncio
    async def test_returns_none_for_unindexed_file(self, tmp_path: Path) -> None:
        """
        Scenario: URI points to a file not in the symbols table
        Given an empty symbols table
        When _resolve_symbol is called with a valid file URI
        Then it returns None (no match)
        """
        provider = _make_provider(tmp_path)

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        uri = (tmp_path / "src" / "unknown.py").as_uri()
        result = await service._resolve_symbol(uri, 5)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_non_file_uri(self, tmp_path: Path) -> None:
        """
        Scenario: URI uses non-file scheme (stdlib, virtual file)
        Given symbols in the DB
        When _resolve_symbol is called with an 'untitled:' URI
        Then it returns None without querying the DB
        """
        provider = _make_provider(tmp_path)

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        result = await service._resolve_symbol("untitled:Untitled-1", 1)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_for_outside_workspace_uri(self, tmp_path: Path) -> None:
        """
        Scenario: URI points to stdlib/third-party (outside workspace)
        Given symbols in the DB for workspace files
        When _resolve_symbol is called with a URI outside the workspace
        Then it returns None
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")
        _insert_symbols(provider, 1, "src/greeter.py", [
            ("Greeter", "Greeter", "Class", 1, 8),
        ])

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        # URI pointing to stdlib — outside workspace_root
        result = await service._resolve_symbol("file:///usr/lib/python3.13/typing.py", 50)
        assert result is None


class TestExecuteQueryAsync:
    """Tests for execute_query_async behavioral equivalence with sync version."""

    @pytest.mark.asyncio
    async def test_async_returns_same_as_sync(self, tmp_path: Path) -> None:
        provider = _make_provider(tmp_path)
        try:
            _insert_file(provider, 1, "src/hello.py")
            sync_result = provider.execute_query("SELECT id, path FROM files WHERE id = ?", [1])
            async_result = await provider.execute_query_async("SELECT id, path FROM files WHERE id = ?", [1])
            assert async_result == sync_result
        finally:
            provider.close()
