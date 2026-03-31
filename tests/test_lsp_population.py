"""Tests for LSPPopulationService (ch-5b3).

Verifies that the population service calls documentSymbol via the LSP client
and writes correct rows to the DuckDB symbols table.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.lsp.types import LSPError, SymbolInfo
from chunkhound.providers.database.duckdb_provider import DuckDBProvider
from chunkhound.services.lsp_population import LSPPopulationService

pytestmark = pytest.mark.unit


# --- Helpers ---


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


# --- Test 1: Core populate_file flow ---


class TestPopulateFileCore:
    """
    Feature: LSP population service writes symbols to DuckDB

    As the ChunkHound indexing pipeline
    I want to call documentSymbol per file and store results
    So that the symbols table contains structured code intelligence data
    """

    @pytest.mark.asyncio
    async def test_populate_file_calls_didopen_symbols_didclose(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: Core protocol flow for a single file
        Given a file tracked in the DB and a mock LSP server
        When populate_file is called
        Then didOpen is called with file content,
             documentSymbol is called with the URI,
             didClose is called to free server memory,
             and rows are inserted into the symbols table
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        pool, client = _make_mock_pool(_sample_symbols())

        # Write a fake source file so populate_file can read it for didOpen
        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool,
            provider=provider,
            workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/greeter.py"),
            file_id=1,
            language="python",
        )

        # Verify LSP protocol calls
        client.notify_did_open.assert_called_once()
        client.document_symbols.assert_called_once()
        client.notify_did_close.assert_called_once()

        # Verify symbols written to DB
        rows = provider.execute_query(
            "SELECT fqn, name, kind, language, file_id, file_path, "
            "range_start, range_end, parent_fqn, confidence, lsp_server "
            "FROM symbols ORDER BY range_start"
        )

        assert len(rows) == 3  # Greeter + greet + main

        # Class: Greeter
        greeter = rows[0]
        assert greeter["fqn"] == "Greeter"
        assert greeter["name"] == "Greeter"
        assert greeter["kind"] == "Class"
        assert greeter["language"] == "python"
        assert greeter["file_id"] == 1
        assert greeter["file_path"] == "src/greeter.py"
        assert greeter["range_start"] == 1
        assert greeter["range_end"] == 8
        assert greeter["parent_fqn"] is None
        assert greeter["confidence"] == 1.0

        # Method: Greeter::greet
        greet = rows[1]
        assert greet["fqn"] == "Greeter::greet"
        assert greet["name"] == "greet"
        assert greet["kind"] == "Method"
        assert greet["parent_fqn"] == "Greeter"

        # Function: main
        main = rows[2]
        assert main["fqn"] == "main"
        assert main["name"] == "main"
        assert main["kind"] == "Function"
        assert main["parent_fqn"] is None


class TestGracefulSkip:
    """
    Feature: Population service handles missing LSP servers gracefully

    As the indexing pipeline
    I want files without LSP support to be silently skipped
    So that indexing doesn't fail for unsupported languages
    """

    @pytest.mark.asyncio
    async def test_lsp_error_skips_file_no_crash(self, tmp_path: Path) -> None:
        """
        Scenario: No LSP server configured for language
        Given a file whose language has no LSP server
        When populate_file is called
        Then it returns without error and writes no rows
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.mk")

        pool = AsyncMock()
        pool.get = AsyncMock(side_effect=LSPError("No server config for language: makefile"))

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        # Should not raise
        await service.populate_file(
            file_path=Path("src/main.mk"), file_id=1, language="makefile",
        )

        # No symbols should be written
        rows = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert rows[0]["cnt"] == 0


class TestBatchInsert:
    """
    Feature: Symbols are batch-inserted in a single statement

    As the population service
    I want to insert all symbols for a file in one DB call
    So that performance is acceptable for large codebases
    """

    @pytest.mark.asyncio
    async def test_single_execute_query_for_all_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: Multiple symbols inserted in one batch
        Given a file with 3 symbols (class, method, function)
        When populate_file is called
        Then execute_query is called exactly once for the INSERT
             (not once per symbol)
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        pool, client = _make_mock_pool(_sample_symbols())

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        with patch.object(provider, "execute_query", wraps=provider.execute_query) as spy:
            await service.populate_file(
                file_path=Path("src/greeter.py"), file_id=1, language="python",
            )

            # Filter to only INSERT calls (ignore SELECT, etc.)
            insert_calls = [
                call for call in spy.call_args_list
                if "INSERT INTO symbols" in str(call)
            ]
            assert len(insert_calls) == 1, (
                f"Expected 1 batch INSERT, got {len(insert_calls)}"
            )


class TestFQNConstruction:
    """
    Feature: FQN constructed from nested symbol hierarchy

    As a downstream graph tool
    I want symbols to have correct fully-qualified names
    So that symbol identity is unambiguous across the codebase
    """

    @pytest.mark.asyncio
    async def test_three_level_nesting_produces_correct_fqn(self, tmp_path: Path) -> None:
        """
        Scenario: Class → Method → nested function (3 levels)
        Given a file with Outer::inner_method.helper nesting
        When populate_file is called
        Then FQNs are Outer, Outer::inner_method, Outer::inner_method.helper
        """
        helper = SymbolInfo(
            name="helper", kind=12, range_start_line=6, range_start_char=8,
            range_end_line=8, range_end_char=0, children=[],
        )
        method = SymbolInfo(
            name="inner_method", kind=6, range_start_line=3, range_start_char=4,
            range_end_line=9, range_end_char=0, children=[helper],
        )
        cls = SymbolInfo(
            name="Outer", kind=5, range_start_line=1, range_start_char=0,
            range_end_line=10, range_end_char=0, children=[method],
        )

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/nested.py")

        pool, _ = _make_mock_pool([cls])

        src_file = tmp_path / "src" / "nested.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Outer:\n    def inner_method(self):\n        def helper(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/nested.py"), file_id=1, language="python",
        )

        rows = provider.execute_query(
            "SELECT fqn, parent_fqn FROM symbols ORDER BY range_start"
        )

        assert len(rows) == 3
        assert rows[0]["fqn"] == "Outer"
        assert rows[0]["parent_fqn"] is None
        assert rows[1]["fqn"] == "Outer::inner_method"
        assert rows[1]["parent_fqn"] == "Outer"
        assert rows[2]["fqn"] == "Outer::inner_method::helper"
        assert rows[2]["parent_fqn"] == "Outer::inner_method"


class TestDeleteFileSymbols:
    """
    Feature: Remove all symbols for a file

    As the incremental update pipeline
    I want to delete all symbols for a changed file
    So that repopulation starts from a clean state
    """

    @pytest.mark.asyncio
    async def test_deletes_all_symbols_for_file_id(self, tmp_path: Path) -> None:
        """
        Scenario: Delete symbols for one file, keep others
        Given two files with symbols in the DB
        When delete_file_symbols is called for file 1
        Then file 1 symbols are removed but file 2 symbols remain
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        pool, _ = _make_mock_pool(_sample_symbols())

        for name, fid, path in [("a.py", 1, "src/a.py"), ("b.py", 2, "src/b.py")]:
            src_file = tmp_path / path
            src_file.parent.mkdir(parents=True, exist_ok=True)
            src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        # Populate both files
        await service.populate_file(Path("src/a.py"), 1, "python")
        await service.populate_file(Path("src/b.py"), 2, "python")

        before = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert before[0]["cnt"] == 6  # 3 symbols × 2 files

        # Delete file 1's symbols
        await service.delete_file_symbols(1)

        after = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert after[0]["cnt"] == 3  # Only file 2 remains

        remaining = provider.execute_query("SELECT DISTINCT file_id FROM symbols")
        assert len(remaining) == 1
        assert remaining[0]["file_id"] == 2


class TestUnknownSymbolKind:
    """
    Feature: Unknown SymbolKind values handled gracefully

    As the population service
    I want to handle non-standard SymbolKind values
    So that custom LSP servers don't crash the indexer
    """

    @pytest.mark.asyncio
    async def test_unknown_kind_maps_to_unknown_N(self, tmp_path: Path) -> None:
        """
        Scenario: LSP server returns a non-standard kind value
        Given a symbol with kind=99 (not in LSP spec)
        When populate_file processes it
        Then the symbol is stored with kind="unknown_99"
        """
        unknown_sym = SymbolInfo(
            name="mystery", kind=99, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/weird.py")

        pool, _ = _make_mock_pool([unknown_sym])

        src_file = tmp_path / "src" / "weird.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("mystery = 42\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/weird.py"), 1, "python")

        rows = provider.execute_query("SELECT kind FROM symbols")
        assert len(rows) == 1
        assert rows[0]["kind"] == "unknown_99"


class TestAdversarial:
    """Adversarial stress tests for LSPPopulationService (ch-5b3)."""

    @pytest.mark.asyncio
    async def test_second_run_is_idempotent(self, tmp_path: Path) -> None:
        """
        Pattern: The "second run"
        Verify: populate_file called twice → same 3 rows (delete-before-insert).
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/mod.py")
        pool, _ = _make_mock_pool(_sample_symbols())

        src = tmp_path / "src" / "mod.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/mod.py"), 1, "python")
        await service.populate_file(Path("src/mod.py"), 1, "python")

        rows = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        # Idempotent — delete-before-insert produces exactly 3 rows
        assert rows[0]["cnt"] == 3

    @pytest.mark.asyncio
    async def test_symbol_name_with_dot_in_fqn(self, tmp_path: Path) -> None:
        """
        Pattern: Encoding boundary
        Hypothesis: Symbol named "my.module" → FQN "Parent.my.module" is ambiguous
        with a real child named "module" under "Parent.my".
        Verify: we store it faithfully even if ambiguous.
        """
        dotted = SymbolInfo(
            name="my.decorated", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/d.py")
        pool, _ = _make_mock_pool([dotted])

        src = tmp_path / "src" / "d.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("pass\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )
        await service.populate_file(Path("src/d.py"), 1, "python")

        rows = provider.execute_query("SELECT fqn FROM symbols")
        assert len(rows) == 1
        # FQN stores the name as-is — dots in names are passed through
        assert rows[0]["fqn"] == "my.decorated"

    @pytest.mark.asyncio
    async def test_empty_document_symbols_no_insert(self, tmp_path: Path) -> None:
        """
        Pattern: Empty
        Hypothesis: documentSymbol returns [] → no rows, no crash.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/empty.py")
        pool, _ = _make_mock_pool([])  # Empty symbols

        src = tmp_path / "src" / "empty.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )
        await service.populate_file(Path("src/empty.py"), 1, "python")

        rows = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert rows[0]["cnt"] == 0

    @pytest.mark.asyncio
    async def test_single_symbol_batch_insert(self, tmp_path: Path) -> None:
        """
        Pattern: Singular
        Hypothesis: Single symbol → batch INSERT with one row works.
        """
        single = SymbolInfo(
            name="alone", kind=13, range_start_line=1, range_start_char=0,
            range_end_line=1, range_end_char=10, children=[],
        )
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/one.py")
        pool, _ = _make_mock_pool([single])

        src = tmp_path / "src" / "one.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("alone = 42\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )
        await service.populate_file(Path("src/one.py"), 1, "python")

        rows = provider.execute_query("SELECT name, kind FROM symbols")
        assert len(rows) == 1
        assert rows[0]["name"] == "alone"
        assert rows[0]["kind"] == "Variable"


class TestBatchWiring:
    """
    Feature: Batch indexing runs LSP population after embeddings

    As the CLI indexer
    I want symbols populated during batch indexing
    So that a full index includes both chunks and symbols
    """

    @pytest.mark.asyncio
    async def test_process_directory_calls_populate_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: DirectoryIndexingService with lsp_population
        Given a DirectoryIndexingService with an LSPPopulationService
        When process_directory completes
        Then _populate_symbols is called (via the lsp_population service)
        """
        from chunkhound.services.directory_indexing_service import (
            DirectoryIndexingService,
        )

        mock_coordinator = AsyncMock()
        mock_coordinator.process_directory = AsyncMock(return_value={
            "status": "complete", "processed": 1, "skipped": 0,
            "errors": 0, "total_chunks": 5,
        })
        mock_coordinator.generate_missing_embeddings = AsyncMock(return_value={
            "status": "success", "generated": 0,
        })

        mock_pop = AsyncMock()
        mock_pop.populate_files = AsyncMock()

        # Config with indexing settings
        from types import SimpleNamespace
        config = SimpleNamespace(
            indexing=SimpleNamespace(
                include=["*.py"], exclude=[],
                config_file_size_threshold_kb=500,
            ),
        )

        service = DirectoryIndexingService(
            indexing_coordinator=mock_coordinator,
            config=config,
            lsp_population=mock_pop,
        )

        await service.process_directory(tmp_path)

        # LSP population should have been called
        mock_pop.populate_files.assert_called_once()


class TestRealtimeWiring:
    """
    Feature: Realtime indexing queues LSP population after file processing

    As the file watcher pipeline
    I want LSP population to run as a background pass after tree-sitter
    So that symbols are populated without blocking the indexing loop
    """

    @pytest.mark.asyncio
    async def test_process_loop_handles_lsp_priority(self, tmp_path: Path) -> None:
        """
        Scenario: "lsp" priority event triggers populate_file
        Given a RealtimeIndexingService with an LSPPopulationService
        When a file is queued with priority="lsp"
        Then populate_file is called for that file
        """
        import asyncio
        from chunkhound.core.config.config import Config
        from chunkhound.database_factory import create_services
        from types import SimpleNamespace

        db_path = tmp_path / ".chunkhound" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        fake_args = SimpleNamespace(path=tmp_path)
        config = Config(
            args=fake_args,
            database={"path": str(db_path), "provider": "duckdb"},
            indexing={"include": ["*.py"], "exclude": []},
        )

        services = create_services(db_path, config)
        services.provider.connect()

        # Create a test file so process_file has something to index
        test_file = tmp_path / "test_mod.py"
        test_file.write_text("def hello(): pass\n")

        try:
            from chunkhound.services.realtime_indexing_service import (
                RealtimeIndexingService,
            )

            # Pre-insert the file into the DB (simulates process_file completing first)
            services.provider.execute_query(
                "INSERT INTO files (id, path, name, content_hash) VALUES (?, ?, ?, ?)",
                [1, "test_mod.py", "test_mod.py", "abc"],
            )

            # Create mock LSP population service
            mock_pop = AsyncMock()
            mock_pop.populate_file = AsyncMock()

            service = RealtimeIndexingService(
                services, config, lsp_population=mock_pop,
            )
            service.watch_path = tmp_path  # Set watch path so relative_to works

            # Queue a file with "lsp" priority
            await service.add_file(test_file, priority="lsp")

            # Run one processing iteration
            service.process_task = asyncio.create_task(service._process_loop())
            await asyncio.sleep(0.5)  # Let one loop iteration complete
            service.process_task.cancel()
            try:
                await service.process_task
            except asyncio.CancelledError:
                pass

            # populate_file should have been called
            mock_pop.populate_file.assert_called_once()

        finally:
            services.provider.disconnect()
