"""Workspace symbol tests — parsing, populate_files integration, edge cases, adversarial."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from chunkhound.lsp.types import LSPCapability, SymbolInfo
from chunkhound.services.lsp_population import LSPPopulationService
from tests.lsp.conftest import (
    _insert_file,
    _make_mock_pool,
    _make_provider,
)

pytestmark = pytest.mark.unit


class TestWorkspaceSymbolsParsing:
    """
    Feature: workspace_symbols returns SymbolInfo with location_uri

    As the population service
    I want workspace symbol results to include the file URI
    So that I can resolve which file each symbol belongs to
    """

    @pytest.mark.asyncio
    async def test_workspace_symbols_returns_symbolinfo_with_location_uri(self) -> None:
        """
        Scenario: workspace_symbols parses SymbolInformation format
        Given a mock LSP server returning SymbolInformation[] with location.uri
        When workspace_symbols is called
        Then results are SymbolInfo instances with location_uri populated
        """
        from chunkhound.lsp.client import LSPClient

        # SymbolInformation format (flat, with location.uri — what workspace/symbol returns)
        raw_response = [
            {
                "name": "MyClass",
                "kind": 5,  # Class
                "location": {
                    "uri": "file:///workspace/src/foo.py",
                    "range": {
                        "start": {"line": 10, "character": 0},
                        "end": {"line": 20, "character": 0},
                    },
                },
                "containerName": "foo",
            },
            {
                "name": "helper",
                "kind": 12,  # Function
                "location": {
                    "uri": "file:///workspace/src/bar.py",
                    "range": {
                        "start": {"line": 5, "character": 0},
                        "end": {"line": 8, "character": 0},
                    },
                },
            },
        ]

        symbols = LSPClient._parse_symbols(raw_response)

        assert len(symbols) == 2

        assert symbols[0].name == "MyClass"
        assert symbols[0].kind == 5
        assert symbols[0].range_start_line == 10
        assert symbols[0].location_uri == "file:///workspace/src/foo.py"

        assert symbols[1].name == "helper"
        assert symbols[1].location_uri == "file:///workspace/src/bar.py"

    @pytest.mark.asyncio
    async def test_document_symbols_have_no_location_uri(self) -> None:
        """
        Scenario: documentSymbol results have no location.uri
        Given a DocumentSymbol[] response (has range, no location)
        When _parse_symbols is called
        Then location_uri is None
        """
        from chunkhound.lsp.client import LSPClient

        raw_response = [
            {
                "name": "MyFunc",
                "kind": 12,
                "range": {
                    "start": {"line": 1, "character": 0},
                    "end": {"line": 5, "character": 0},
                },
                "children": [],
            },
        ]

        symbols = LSPClient._parse_symbols(raw_response)
        assert len(symbols) == 1
        assert symbols[0].location_uri is None


class TestPopulateFilesWorkspaceSymbols:
    """
    Feature: populate_files calls workspaceSymbol after per-file loop

    As the population service
    I want workspace symbols to supplement per-file documentSymbol results
    So that cross-file symbols not visible per-file are captured
    """

    @pytest.mark.asyncio
    async def test_workspace_symbols_inserts_new_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: workspaceSymbol adds symbols not found by documentSymbol
        Given documentSymbol returns [Greeter] for a file
        And workspaceSymbol returns [Greeter, hidden_helper] (hidden_helper is in same file)
        When populate_files runs
        Then both Greeter (from documentSymbol) and hidden_helper (from workspaceSymbol) are in DB
        And hidden_helper has confidence=0.9, Greeter has confidence=1.0
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")

        # Create actual file on disk (populate_file reads content for didOpen)
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("class Greeter: pass\ndef hidden_helper(): pass\n")

        # documentSymbol returns one symbol
        doc_symbols = [
            SymbolInfo(
                name="Greeter", kind=5,
                range_start_line=1, range_start_char=0,
                range_end_line=10, range_end_char=0,
                children=[],
            ),
        ]

        # workspaceSymbol returns two symbols (one overlaps, one is new)
        workspace_symbols_response = [
            SymbolInfo(
                name="Greeter", kind=5,
                range_start_line=1, range_start_char=0,
                range_end_line=10, range_end_char=0,
                location_uri=f"file://{tmp_path}/src/foo.py",
            ),
            SymbolInfo(
                name="hidden_helper", kind=12,
                range_start_line=12, range_start_char=0,
                range_end_line=15, range_end_char=0,
                location_uri=f"file://{tmp_path}/src/foo.py",
            ),
        ]

        pool, client = _make_mock_pool(doc_symbols)
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=workspace_symbols_response)

        # Mock edge collection to avoid LSP calls
        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        # Both symbols should be in DB
        rows = provider.execute_query("SELECT fqn, confidence FROM symbols ORDER BY fqn")
        fqns = {row["fqn"] for row in rows}
        assert "Greeter" in fqns
        assert "hidden_helper" in fqns
        assert len(rows) == 2

        # Verify confidence differentiation
        for row in rows:
            if row["fqn"] == "Greeter":
                assert row["confidence"] == pytest.approx(1.0)  # documentSymbol source
            elif row["fqn"] == "hidden_helper":
                assert row["confidence"] == pytest.approx(0.9, abs=1e-6)  # workspaceSymbol source


class TestWorkspaceSymbolEdgeCases:
    """
    Feature: workspaceSymbol edge case handling

    As the population service
    I want workspaceSymbol to handle edge cases gracefully
    So that bad data from LSP servers doesn't crash or corrupt the DB
    """

    @pytest.mark.asyncio
    async def test_workspace_symbol_for_unknown_file_is_skipped(self, tmp_path: Path) -> None:
        """
        Scenario: workspaceSymbol returns symbol for file not in files table
        Given file "src/foo.py" is in files table but "src/unknown.py" is not
        And workspaceSymbol returns a symbol in "src/unknown.py"
        When populate_files runs
        Then the unknown file's symbol is skipped (no crash, no orphan row)
        And the known file's workspace symbol IS inserted
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        # Note: src/unknown.py is NOT in the files table

        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("def known(): pass\n")

        # documentSymbol returns nothing (so we only test workspace path)
        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        # workspaceSymbol returns two symbols: one in known file, one in unknown file
        workspace_results = [
            SymbolInfo(
                name="known_func", kind=12,
                range_start_line=0, range_start_char=0,
                range_end_line=1, range_end_char=0,
                location_uri=f"file://{tmp_path}/src/foo.py",
            ),
            SymbolInfo(
                name="unknown_func", kind=12,
                range_start_line=0, range_start_char=0,
                range_end_line=1, range_end_char=0,
                location_uri=f"file://{tmp_path}/src/unknown.py",
            ),
        ]
        client.workspace_symbols = AsyncMock(return_value=workspace_results)

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        fqns = {row["fqn"] for row in rows}
        assert "known_func" in fqns, "Known file's workspace symbol should be inserted"
        assert "unknown_func" not in fqns, "Unknown file's symbol should be skipped"

    @pytest.mark.asyncio
    async def test_workspace_symbol_no_capability_skips_gracefully(self, tmp_path: Path) -> None:
        """
        Scenario: Server lacks WORKSPACE_SYMBOL capability
        Given a server that only supports DOCUMENT_SYMBOL (not WORKSPACE_SYMBOL)
        When populate_files runs
        Then workspace symbol pass is skipped (no crash, no workspace_symbols call)
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")

        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("def hello(): pass\n")

        doc_symbols = [SymbolInfo(name="hello", kind=12, range_start_line=0,
                                   range_start_char=0, range_end_line=1, range_end_char=0)]

        pool, client = _make_mock_pool(doc_symbols)
        client.hover = AsyncMock(return_value=None)
        # Only DOCUMENT_SYMBOL — no WORKSPACE_SYMBOL
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL}
        client.workspace_symbols = AsyncMock()  # should NOT be called

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        # workspace_symbols should never have been called
        client.workspace_symbols.assert_not_called()

        # documentSymbol symbols should still be present
        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert {row["fqn"] for row in rows} == {"hello"}


class TestAdversarialWorkspaceSymbols:
    """Adversarial stress tests for _populate_workspace_symbols."""

    @pytest.mark.asyncio
    async def test_empty_workspace_symbols_no_crash(self, tmp_path: Path) -> None:
        """Empty: workspace_symbols returns [] — no inserts, no crash."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=[])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert len(rows) == 0  # no doc symbols, no workspace symbols

    @pytest.mark.asyncio
    async def test_workspace_symbol_with_non_file_uri_skipped(self, tmp_path: Path) -> None:
        """Encoding boundaries: non-file URI schemes (git:, untitled:) must be filtered."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=[
            SymbolInfo(name="ghost", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=1, range_end_char=0,
                       location_uri="git:///some/virtual/file.py"),
            SymbolInfo(name="phantom", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=1, range_end_char=0,
                       location_uri="untitled:Untitled-1"),
        ])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert len(rows) == 0, f"Non-file URIs should be skipped, got {[r['fqn'] for r in rows]}"

    @pytest.mark.asyncio
    async def test_workspace_symbol_with_percent_encoded_path(self, tmp_path: Path) -> None:
        """Encoding boundaries: percent-encoded URI must match unencoded DB path."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/my file.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "my file.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        # URI has %20 for space — must match "my file.py" in DB after unquote
        client.workspace_symbols = AsyncMock(return_value=[
            SymbolInfo(name="spaced_func", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=1, range_end_char=0,
                       location_uri=f"file://{tmp_path}/src/my%20file.py"),
        ])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        fqns = {row["fqn"] for row in rows}
        assert "spaced_func" in fqns, "Percent-encoded URI should resolve to DB path after unquote"

    @pytest.mark.asyncio
    async def test_workspace_symbol_outside_workspace_skipped(self, tmp_path: Path) -> None:
        """Disconnected: symbol URI points outside workspace root."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=[
            SymbolInfo(name="external_func", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=1, range_end_char=0,
                       location_uri="file:///completely/different/path/ext.py"),
        ])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert len(rows) == 0, "Symbols outside workspace should be skipped"

    @pytest.mark.asyncio
    async def test_workspace_symbol_duplicate_in_batch_deduped(self, tmp_path: Path) -> None:
        """Redundant: same symbol appears twice in workspace results (re-exports)."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        # Same symbol returned twice (simulating re-export)
        dup_sym = SymbolInfo(name="ReExported", kind=5, range_start_line=0, range_start_char=0,
                             range_end_line=5, range_end_char=0,
                             location_uri=f"file://{tmp_path}/src/foo.py")
        client.workspace_symbols = AsyncMock(return_value=[dup_sym, dup_sym])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols WHERE fqn = 'ReExported'")
        assert len(rows) == 1, f"Duplicate workspace symbols should be deduped, got {len(rows)}"

    @pytest.mark.asyncio
    async def test_workspace_symbol_none_location_uri_skipped(self, tmp_path: Path) -> None:
        """Semantically hostile: SymbolInfo with location_uri=None from workspace results."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=[
            SymbolInfo(name="no_uri_func", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=1, range_end_char=0,
                       location_uri=None),  # Missing URI
        ])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()

        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert len(rows) == 0, "Symbols with no location_uri should be skipped"

    @pytest.mark.asyncio
    async def test_second_run_workspace_symbols_idempotent(self, tmp_path: Path) -> None:
        """The 'second run': populate_files twice with same workspace data → no duplicates."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/foo.py")
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "foo.py").write_text("x = 1\n")

        ws_sym = SymbolInfo(name="ws_only", kind=12, range_start_line=0, range_start_char=0,
                            range_end_line=1, range_end_char=0,
                            location_uri=f"file://{tmp_path}/src/foo.py")

        pool, client = _make_mock_pool(symbols=[])
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}
        client.workspace_symbols = AsyncMock(return_value=[ws_sym])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
            await service.populate_files()
            await service.populate_files()  # second run

        rows = provider.execute_query("SELECT fqn FROM symbols WHERE fqn = 'ws_only'")
        assert len(rows) == 1, f"Second run should not duplicate workspace symbols, got {len(rows)}"
