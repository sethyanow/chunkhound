"""Tests for LSPPopulationService (ch-5b3).

Verifies that the population service calls documentSymbol via the LSP client
and writes correct rows to the DuckDB symbols table.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.lsp.types import (
    CallHierarchyItem,
    HoverResult,
    Location,
    LSPCapability,
    LSPError,
    SymbolInfo,
)
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


# --- Tests for hover / type_signature population (ch-nvc) ---


class TestCollectTypeSignatures:
    """
    Feature: Hover per symbol collects type_signature data

    As the population service
    I want to call hover for each symbol and collect type signatures
    So that the symbols table contains type information for downstream tools
    """

    @pytest.mark.asyncio
    async def test_hover_called_per_symbol_returns_mapping(self, tmp_path: Path) -> None:
        """
        Scenario: Collect type signatures from hover for all symbols (including nested)
        Given a file with 3 symbols: Greeter (class), greet (method), main (function)
        When _collect_type_signatures is called
        Then hover is called 3 times (once per symbol including nested child)
             and the returned dict maps (line, char) → hover contents
        """
        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})
        # Hover returns different content for each symbol position
        hover_responses = {
            (1, 0): HoverResult(contents="class Greeter"),
            (5, 4): HoverResult(contents="(method) greet() -> None"),
            (10, 0): HoverResult(contents="(function) main() -> None"),
        }
        async def hover_side_effect(uri: str, line: int, char: int) -> HoverResult | None:
            return hover_responses.get((line, char))
        client.hover = AsyncMock(side_effect=hover_side_effect)

        provider = _make_provider(tmp_path)
        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        # Hover called 3 times — Greeter, greet (nested child), main
        assert client.hover.call_count == 3

        # Mapping contains all 3 symbols
        assert result == {
            (1, 0): "class Greeter",
            (5, 4): "(method) greet() -> None",
            (10, 0): "(function) main() -> None",
        }

    @pytest.mark.asyncio
    async def test_hover_failure_on_one_symbol_does_not_block_others(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: One symbol's hover raises, others still collected
        Given 3 symbols where hover raises Exception for the 2nd (greet)
        When _collect_type_signatures is called
        Then the 1st and 3rd symbols still have type_signatures
             and the 2nd symbol is absent from the mapping
        """
        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})

        call_count = 0
        async def hover_side_effect(uri: str, line: int, char: int) -> HoverResult | None:
            nonlocal call_count
            call_count += 1
            if line == 5 and char == 4:  # greet — simulate failure
                raise RuntimeError("LSP server crashed mid-hover")
            if line == 1:
                return HoverResult(contents="class Greeter")
            if line == 10:
                return HoverResult(contents="(function) main() -> None")
            return None
        client.hover = AsyncMock(side_effect=hover_side_effect)

        provider = _make_provider(tmp_path)
        pool = AsyncMock()

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        # All 3 hover calls attempted
        assert call_count == 3

        # Only 2 results — greet's failure was isolated
        assert result == {
            (1, 0): "class Greeter",
            (10, 0): "(function) main() -> None",
        }
        assert (5, 4) not in result

    @pytest.mark.asyncio
    async def test_no_hover_capability_returns_empty_dict(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: Server doesn't advertise hover capability
        Given a client whose capabilities do NOT include HOVER
        When _collect_type_signatures is called
        Then an empty dict is returned and hover is never called
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        # Only DOCUMENT_SYMBOL, no HOVER
        client.capabilities = frozenset({LSPCapability.DOCUMENT_SYMBOL})
        client.hover = AsyncMock()

        provider = _make_provider(tmp_path)
        pool = AsyncMock()

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(
            client, "file:///test.py", symbols,
        )

        assert result == {}
        client.hover.assert_not_called()


class TestPopulateFileTypeSignature:
    """
    Feature: populate_file stores type_signature in the symbols table

    As the indexing pipeline
    I want type_signature populated from hover data during populate_file
    So that downstream graph tools have type information without live LSP queries
    """

    @pytest.mark.asyncio
    async def test_type_signature_stored_for_symbols_with_hover_data(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: populate_file with hover data writes type_signature to DB
        Given a file with 3 symbols where hover returns data for 2 of them
        When populate_file is called
        Then type_signature is populated for the 2 symbols with hover data
             and NULL for the symbol where hover returned None
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        symbols = _sample_symbols()  # Greeter(children=[greet]), main

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()

        # Hover returns data for Greeter and main, None for greet
        async def hover_side_effect(uri: str, line: int, char: int) -> HoverResult | None:
            if line == 1 and char == 0:
                return HoverResult(contents="class Greeter")
            if line == 10 and char == 0:
                return HoverResult(contents="(function) main() -> None")
            return None  # greet at (5, 4) — no hover data
        client.hover = AsyncMock(side_effect=hover_side_effect)

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/greeter.py"), file_id=1, language="python",
        )

        rows = provider.execute_query(
            "SELECT fqn, type_signature FROM symbols ORDER BY range_start"
        )

        assert len(rows) == 3

        # Greeter — has hover data
        assert rows[0]["fqn"] == "Greeter"
        assert rows[0]["type_signature"] == "class Greeter"

        # greet — no hover data
        assert rows[1]["fqn"] == "Greeter::greet"
        assert rows[1]["type_signature"] is None

        # main — has hover data
        assert rows[2]["fqn"] == "main"
        assert rows[2]["type_signature"] == "(function) main() -> None"


class TestAdversarialHover:
    """Adversarial stress tests for hover / type_signature population (ch-nvc)."""

    @pytest.mark.asyncio
    async def test_empty_symbols_no_hover_calls(self, tmp_path: Path) -> None:
        """
        Pattern: Empty
        Hypothesis: _collect_type_signatures with [] symbols → empty dict, no hover calls.
        """
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER})
        client.hover = AsyncMock()

        provider = _make_provider(tmp_path)
        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        result = await service._collect_type_signatures(client, "file:///e.py", [])

        assert result == {}
        client.hover.assert_not_called()

    @pytest.mark.asyncio
    async def test_singular_symbol_gets_type_signature(self, tmp_path: Path) -> None:
        """
        Pattern: Singular
        Hypothesis: Single symbol with hover → one-entry dict, stored in DB correctly.
        """
        single = SymbolInfo(
            name="VERSION", kind=13, range_start_line=1, range_start_char=0,
            range_end_line=1, range_end_char=20, children=[],
        )

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=[single])
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents="(variable) VERSION: str"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/ver.py")

        src = tmp_path / "src" / "ver.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text('VERSION = "1.0"\n')

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/ver.py"), 1, "python")

        rows = provider.execute_query("SELECT name, type_signature FROM symbols")
        assert len(rows) == 1
        assert rows[0]["name"] == "VERSION"
        assert rows[0]["type_signature"] == "(variable) VERSION: str"

    @pytest.mark.asyncio
    async def test_unicode_in_hover_contents_stored_faithfully(self, tmp_path: Path) -> None:
        """
        Pattern: Encoding boundaries
        Hypothesis: Hover contents with unicode (CJK, emoji, math symbols) stored as-is.
        """
        sym = SymbolInfo(
            name="grüße", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )
        # Multi-byte content: CJK + emoji + mathematical notation
        unicode_hover = "(function) grüße() -> Résultat[données, 错误] # 🎯 ∀x∈ℝ"

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=[sym])
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents=unicode_hover))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/u.py")

        src = tmp_path / "src" / "u.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("def grüße(): pass\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )
        await service.populate_file(Path("src/u.py"), 1, "python")

        rows = provider.execute_query("SELECT type_signature FROM symbols")
        assert len(rows) == 1
        assert rows[0]["type_signature"] == unicode_hover

    @pytest.mark.asyncio
    async def test_second_run_preserves_type_signatures(self, tmp_path: Path) -> None:
        """
        Pattern: The "second run"
        Hypothesis: populate_file called twice → same rows with same type_signatures.
        Delete-before-insert must not leave stale type_signature data.
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=HoverResult(contents="type info"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/r.py")

        src = tmp_path / "src" / "r.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/r.py"), 1, "python")
        await service.populate_file(Path("src/r.py"), 1, "python")

        rows = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert rows[0]["cnt"] == 3  # Not 6 — idempotent

        sigs = provider.execute_query(
            "SELECT type_signature FROM symbols WHERE type_signature IS NOT NULL"
        )
        assert len(sigs) == 3  # All 3 have type_signature from hover
        assert all(s["type_signature"] == "type info" for s in sigs)

    @pytest.mark.asyncio
    async def test_all_hover_fails_symbols_still_inserted_with_null_type_sig(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Dependency treachery — server degrades, all hover fails
        Hypothesis: All hover calls raise → symbols still inserted, all type_signature = NULL.
        The file's symbols must not be lost because hover failed.
        """
        symbols = _sample_symbols()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.HOVER, LSPCapability.DOCUMENT_SYMBOL})
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(side_effect=ConnectionError("server died"))

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/dead.py")

        src = tmp_path / "src" / "dead.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/dead.py"), 1, "python")

        rows = provider.execute_query("SELECT name, type_signature FROM symbols ORDER BY range_start")
        assert len(rows) == 3  # All symbols inserted despite hover failure
        assert all(r["type_signature"] is None for r in rows)


# --- Tests for edge population (ch-zlg) ---


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
        result = service._resolve_symbol(uri, 6)

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
        result = service._resolve_symbol(uri, 5)

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

        result = service._resolve_symbol("untitled:Untitled-1", 1)
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
        result = service._resolve_symbol("file:///usr/lib/python3.13/typing.py", 50)
        assert result is None


class TestCollectEdges:
    """
    Feature: Collect edges from LSP operations per symbol

    As the edge population service
    I want to call definition/references/implementation/calls per symbol
    So that the symbol_edges table contains cross-symbol relationships
    """

    @pytest.mark.asyncio
    async def test_definition_and_references_produce_correct_edge_tuples(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: go_to_definition and find_references produce edges with correct edge_kind
        Given two files with symbols: main.py has 'caller' (line 1-3), greeter.py has 'Greeter' (line 1-8)
        When _collect_edges is called for main.py's symbols
             and go_to_definition returns a Location in greeter.py
             and find_references returns a Location in greeter.py
        Then edges are returned with kind 'defines' and 'references' respectively
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/greeter.py")

        # Source file symbols
        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("caller", "caller", "Function", 1, 3),
        ])
        # Target file symbols
        tgt_ids = _insert_symbols(provider, 2, "src/greeter.py", [
            ("Greeter", "Greeter", "Class", 1, 8),
        ])

        fqn_to_id = {"caller": src_ids[0]}

        # Mock LSP client
        greeter_uri = (tmp_path / "src" / "greeter.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DEFINITION,
            LSPCapability.REFERENCES,
        })
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=greeter_uri, range_start_line=1, range_start_char=0,
                     range_end_line=8, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri=greeter_uri, range_start_line=1, range_start_char=0,
                     range_end_line=8, range_end_char=0),
        ])
        # No implementation or call hierarchy capability
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # Should have exactly 1 edge (defines and references resolve to same target,
        # but edge_kind differs so they're both kept)
        edge_kinds = {e[6] for e in edges}  # index 6 = edge_kind
        assert "defines" in edge_kinds
        assert "references" in edge_kinds

        # Verify from/to fields on a 'defines' edge
        defines_edge = [e for e in edges if e[6] == "defines"][0]
        assert defines_edge[0] == src_ids[0]     # from_symbol_id
        assert defines_edge[1] == "caller"        # from_fqn
        assert defines_edge[2] == "src/main.py"   # from_file
        assert defines_edge[3] == tgt_ids[0]      # to_symbol_id
        assert defines_edge[4] == "Greeter"       # to_fqn
        assert defines_edge[5] == "src/greeter.py" # to_file

    @pytest.mark.asyncio
    async def test_skips_operations_for_missing_capabilities(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: Server lacks CALL_HIERARCHY — calls/called_by edges skipped
        Given a client with only DEFINITION capability (no REFERENCES, CALL_HIERARCHY, IMPLEMENTATION)
        When _collect_edges is called
        Then only 'defines' edges are produced (others silently skipped)
             and incoming_calls/outgoing_calls/find_references/go_to_implementation are never called
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        client = AsyncMock()
        # Only DEFINITION — nothing else
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func_a", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # Only 'defines' edges — others skipped due to missing capabilities
        edge_kinds = {e[6] for e in edges}
        assert edge_kinds == {"defines"}

        # Verify skipped operations were never called
        client.find_references.assert_not_called()
        client.go_to_implementation.assert_not_called()
        client.incoming_calls.assert_not_called()
        client.outgoing_calls.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_on_one_symbol_does_not_block_others(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: LSP operation fails for one symbol, other symbols still produce edges
        Given two symbols where go_to_definition raises for the first
        When _collect_edges is called
        Then edges from the second symbol are still collected
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 3),
            ("func_b", "func_b", "Function", 5, 8),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0], "func_b": src_ids[1]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()

        async def definition_side_effect(uri: str, line: int, char: int) -> list[Location]:
            if line == 1:  # func_a — blow up
                raise RuntimeError("LSP server error")
            return [Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                             range_end_line=3, range_end_char=0)]

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock(side_effect=definition_side_effect)
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [
            SymbolInfo(name="func_a", kind=12, range_start_line=1, range_start_char=0,
                       range_end_line=3, range_end_char=0, children=[]),
            SymbolInfo(name="func_b", kind=12, range_start_line=5, range_start_char=0,
                       range_end_line=8, range_end_char=0, children=[]),
        ]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # func_a's edge lost to exception, but func_b's edge is collected
        assert len(edges) >= 1
        from_fqns = {e[1] for e in edges}
        assert "func_b" in from_fqns
        assert "func_a" not in from_fqns


class TestPopulateFileEdges:
    """
    Feature: populate_file writes edges to the symbol_edges table

    As the indexing pipeline
    I want populate_file to collect and store edges alongside symbols
    So that the graph is built during indexing
    """

    @pytest.mark.asyncio
    async def test_populate_file_stores_edges_in_symbol_edges_table(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: End-to-end edge population via populate_file
        Given two files where main.py's 'caller' function has a definition pointing to lib.py's 'helper'
        When populate_file is called for main.py
        Then symbol_edges table contains an edge with correct from/to IDs and edge_kind='defines'
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        # Pre-insert target symbols (lib.py already populated by prior pass)
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        # Source file symbols (returned by documentSymbol)
        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()

        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DOCUMENT_SYMBOL, LSPCapability.HOVER,
            LSPCapability.DEFINITION,
        })
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        # Hover returns nothing (simplify test)
        client.hover = AsyncMock(return_value=None)
        # Definition returns location in lib.py
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "main.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def caller():\n    helper()\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/main.py"), file_id=1, language="python",
        )

        # Verify symbols were inserted
        sym_rows = provider.execute_query(
            "SELECT id, fqn FROM symbols WHERE file_id = 1"
        )
        assert len(sym_rows) == 1
        assert sym_rows[0]["fqn"] == "caller"

        # Verify edges were inserted
        edge_rows = provider.execute_query(
            "SELECT from_fqn, to_fqn, edge_kind, from_file, to_file "
            "FROM symbol_edges"
        )
        assert len(edge_rows) == 1
        edge = edge_rows[0]
        assert edge["from_fqn"] == "caller"
        assert edge["to_fqn"] == "helper"
        assert edge["edge_kind"] == "defines"
        assert edge["from_file"] == "src/main.py"
        assert edge["to_file"] == "src/lib.py"


class TestDeleteFileEdges:
    """
    Feature: delete_file_edges removes edges referencing a file's symbols

    As the incremental repopulation pipeline
    I want to delete a file's edges before deleting its symbols
    So that FK integrity is maintained and stale edges don't persist
    """

    @pytest.mark.asyncio
    async def test_deletes_edges_for_file_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: Edges from and to a file's symbols are removed
        Given file 1 has a symbol with an edge to file 2's symbol
             and file 2 has a symbol with an edge to file 1's symbol
        When delete_file_edges is called for file 1
        Then both edges are removed (from file1 and to file1)
             but file 2's symbols remain intact
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        ids_a = _insert_symbols(provider, 1, "src/a.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        ids_b = _insert_symbols(provider, 2, "src/b.py", [
            ("func_b", "func_b", "Function", 1, 5),
        ])

        # Insert edges in both directions
        provider.execute_query(
            "INSERT INTO symbol_edges "
            "(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
            "VALUES (?, 'func_a', 'src/a.py', ?, 'func_b', 'src/b.py', 'calls', 1.0, 'pyright'), "
            "(?, 'func_b', 'src/b.py', ?, 'func_a', 'src/a.py', 'references', 1.0, 'pyright')",
            [ids_a[0], ids_b[0], ids_b[0], ids_a[0]],
        )

        # Verify both edges exist
        assert len(provider.execute_query("SELECT * FROM symbol_edges")) == 2

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        await service.delete_file_edges(1)  # Delete edges for file 1

        # Both edges removed — one had from_symbol in file 1, other had to_symbol in file 1
        remaining = provider.execute_query("SELECT * FROM symbol_edges")
        assert len(remaining) == 0

        # Symbols from both files still exist
        sym_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert sym_count[0]["cnt"] == 2


class TestEdgeDeduplication:
    """
    Feature: Edge deduplication on (from_fqn, to_fqn, edge_kind)

    As the edge collection pipeline
    I want duplicate edges deduplicated before batch insert
    So that the same relationship isn't stored multiple times
    """

    @pytest.mark.asyncio
    async def test_same_edge_from_definition_and_references_deduplicates(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: go_to_definition and find_references both discover the same (from, to) pair
        Given func_a → helper discovered via go_to_definition (edge_kind='defines')
             AND func_a → helper discovered via find_references (edge_kind='references')
        When _collect_edges returns
        Then both edges exist (different edge_kind = different dedup keys)
             But if the SAME (from_fqn, to_fqn, edge_kind) appeared twice, only one is kept
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        # Both definition and references point to the same target
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DEFINITION, LSPCapability.REFERENCES,
        })
        # go_to_definition returns SAME location TWICE (e.g., overloaded resolve)
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func_a", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # 2 unique edges: (func_a, helper, defines) + (func_a, helper, references)
        # The duplicate 'defines' from the two go_to_definition results is deduped
        assert len(edges) == 2
        edge_kinds = {e[6] for e in edges}
        assert edge_kinds == {"defines", "references"}


class TestAdversarialEdges:
    """Adversarial stress tests for edge population (ch-zlg)."""

    @pytest.mark.asyncio
    async def test_empty_symbols_no_edges_no_lsp_calls(self, tmp_path: Path) -> None:
        """
        Pattern: Empty
        Hypothesis: _collect_edges with [] symbols → empty list, no LSP calls.
        """
        provider = _make_provider(tmp_path)
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock()

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        edges = await service._collect_edges(
            client, "file:///test.py", [], "test.py", {},
        )

        assert edges == []
        client.go_to_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_edge_filtered_out(self, tmp_path: Path) -> None:
        """
        Pattern: Self-referential
        Hypothesis: go_to_definition returns the symbol's own location → 'defines' self-edge filtered.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/self.py")

        ids = _insert_symbols(provider, 1, "src/self.py", [
            ("MyClass", "MyClass", "Class", 1, 10),
        ])

        fqn_to_id = {"MyClass": ids[0]}
        self_uri = (tmp_path / "src" / "self.py").as_uri()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        # Definition points to itself
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=self_uri, range_start_line=1, range_start_char=0,
                     range_end_line=10, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="MyClass", kind=5, range_start_line=1, range_start_char=0,
            range_end_line=10, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        edges = await service._collect_edges(
            client, self_uri, symbols, "src/self.py", fqn_to_id,
        )

        # Self-edge (MyClass → MyClass, defines) should be filtered
        assert len(edges) == 0

    @pytest.mark.asyncio
    async def test_percent_encoded_uri_resolved_correctly(self, tmp_path: Path) -> None:
        """
        Pattern: Encoding boundaries
        Hypothesis: URI with %20 (space) in path is decoded and resolved correctly.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/my module/helper.py")
        _insert_symbols(provider, 1, "src/my module/helper.py", [
            ("helper", "helper", "Function", 1, 5),
        ])

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        # URI with percent-encoded space
        encoded_uri = (tmp_path / "src" / "my module" / "helper.py").as_uri()
        # Path.as_uri() encodes spaces as %20
        assert "%20" in encoded_uri or "my module" in encoded_uri

        result = service._resolve_symbol(encoded_uri, 3)
        assert result is not None
        assert result[1] == "helper"

    @pytest.mark.asyncio
    async def test_second_run_edges_idempotent(self, tmp_path: Path) -> None:
        """
        Pattern: The "second run"
        Hypothesis: populate_file called twice → same number of edges (delete-before-insert).
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DOCUMENT_SYMBOL, LSPCapability.HOVER,
            LSPCapability.DEFINITION,
        })
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "main.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def caller(): pass\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/main.py"), 1, "python")
        await service.populate_file(Path("src/main.py"), 1, "python")

        edge_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbol_edges")
        assert edge_count[0]["cnt"] == 1  # Idempotent — not doubled

        sym_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols WHERE file_id = 1")
        assert sym_count[0]["cnt"] == 1  # Also idempotent

    @pytest.mark.asyncio
    async def test_fqn_missing_from_map_skips_edge_collection(self, tmp_path: Path) -> None:
        """
        Pattern: Semantically hostile
        Hypothesis: fqn_to_id doesn't contain a symbol's FQN → edges skipped for that symbol.
        """
        provider = _make_provider(tmp_path)

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock()

        symbols = [SymbolInfo(
            name="ghost", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        # fqn_to_id is empty — "ghost" has no ID mapping
        edges = await service._collect_edges(
            client, "file:///test.py", symbols, "test.py", {},
        )

        assert edges == []
        # go_to_definition should never have been called (skipped due to missing ID)
        client.go_to_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_lsp_results_outside_workspace_produces_zero_edges(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Disconnected
        Hypothesis: All LSP results point to files outside workspace → zero edges.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func", "func", "Function", 1, 3),
        ])

        fqn_to_id = {"func": src_ids[0]}

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION, LSPCapability.REFERENCES})
        # All results point to stdlib
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri="file:///usr/lib/python3.13/builtins.py",
                     range_start_line=100, range_start_char=0,
                     range_end_line=105, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri="file:///usr/lib/python3.13/typing.py",
                     range_start_line=50, range_start_char=0,
                     range_end_line=55, range_end_char=0),
        ])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # All targets outside workspace → _resolve_symbol returns None → zero edges
        assert edges == []

    @pytest.mark.asyncio
    async def test_nested_symbols_collect_edges_for_children(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Singular (nested variant)
        Hypothesis: Nested symbol (method inside class) collects edges with correct parent FQN.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/cls.py")
        _insert_file(provider, 2, "src/dep.py")

        src_ids = _insert_symbols(provider, 1, "src/cls.py", [
            ("MyClass", "MyClass", "Class", 1, 10),
            ("MyClass::method", "method", "Method", 3, 8),
        ])
        _insert_symbols(provider, 2, "src/dep.py", [
            ("dep_func", "dep_func", "Function", 1, 5),
        ])

        fqn_to_id = {"MyClass": src_ids[0], "MyClass::method": src_ids[1]}

        dep_uri = (tmp_path / "src" / "dep.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        # Class definition → no results. Method definition → dep_func.
        async def defn_side_effect(uri: str, line: int, char: int) -> list[Location]:
            if line == 3:  # method
                return [Location(uri=dep_uri, range_start_line=1, range_start_char=0,
                                 range_end_line=5, range_end_char=0)]
            return []
        client.go_to_definition = AsyncMock(side_effect=defn_side_effect)
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        method = SymbolInfo(
            name="method", kind=6, range_start_line=3, range_start_char=4,
            range_end_line=8, range_end_char=0, children=[],
        )
        cls = SymbolInfo(
            name="MyClass", kind=5, range_start_line=1, range_start_char=0,
            range_end_line=10, range_end_char=0, children=[method],
        )

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        cls_uri = (tmp_path / "src" / "cls.py").as_uri()
        edges = await service._collect_edges(
            client, cls_uri, [cls], "src/cls.py", fqn_to_id,
        )

        # Only method→dep_func edge (class definition returned empty)
        assert len(edges) == 1
        edge = edges[0]
        assert edge[1] == "MyClass::method"  # from_fqn preserves parent::child
        assert edge[4] == "dep_func"          # to_fqn
        assert edge[6] == "defines"           # edge_kind


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


class TestIncrementalRefresh:
    """
    Feature: Incremental refresh replaces old symbols and edges

    As the population service
    I want a second populate_file call with different symbols to replace old data
    So that the DB reflects the current file state, not stale data
    """

    @pytest.mark.asyncio
    async def test_second_populate_replaces_symbols_and_edges(self, tmp_path: Path) -> None:
        """
        Scenario: File changes, second populate_file replaces old symbols + edges
        Given populate_file inserts symbols [A, B] with edges for file_id=1
        When populate_file is called again with symbols [C]
        Then A and B are gone from symbols, their edges are gone, only C remains
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/mod.py")
        _insert_file(provider, 2, "src/dep.py")

        # Create file on disk
        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "mod.py").write_text("class A: pass\nclass B: pass\n")

        # First run: symbols A and B
        first_symbols = [
            SymbolInfo(name="A", kind=5, range_start_line=0, range_start_char=0,
                       range_end_line=5, range_end_char=0, children=[]),
            SymbolInfo(name="B", kind=5, range_start_line=6, range_start_char=0,
                       range_end_line=10, range_end_char=0, children=[]),
        ]

        pool, client = _make_mock_pool(first_symbols)
        client.hover = AsyncMock(return_value=None)
        client.capabilities = {
            LSPCapability.DOCUMENT_SYMBOL, LSPCapability.DEFINITION,
        }

        # Mock definition to produce an edge: A defines something in dep.py
        # Insert a symbol in dep.py so _resolve_symbol can find it
        provider.execute_query(
            "INSERT INTO symbols (fqn, name, kind, language, file_id, file_path, "
            "range_start, range_end, parent_fqn, confidence, lsp_server, type_signature) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ["dep_func", "dep_func", "Function", "python", 2, "src/dep.py",
             0, 5, None, 1.0, "pyright-langserver", None],
        )

        dep_location = Location(
            uri=f"file://{tmp_path}/src/dep.py",
            range_start_line=0, range_start_char=0,
            range_end_line=5, range_end_char=0,
        )
        client.go_to_definition = AsyncMock(return_value=[dep_location])

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        await service.populate_file(
            file_path=Path("src/mod.py"), file_id=1, language="python",
        )

        # Verify first run: A, B in symbols, at least 1 edge
        sym_rows = provider.execute_query(
            "SELECT fqn FROM symbols WHERE file_id = 1"
        )
        first_fqns = {row["fqn"] for row in sym_rows}
        assert "A" in first_fqns
        assert "B" in first_fqns

        edge_rows = provider.execute_query(
            "SELECT from_fqn FROM symbol_edges WHERE from_fqn IN ('A', 'B')"
        )
        assert len(edge_rows) > 0, "Expected at least one edge from first run"
        first_edge_count = len(edge_rows)

        # Second run: only symbol C (file content changed)
        second_symbols = [
            SymbolInfo(name="C", kind=12, range_start_line=0, range_start_char=0,
                       range_end_line=3, range_end_char=0, children=[]),
        ]
        client.document_symbols = AsyncMock(return_value=second_symbols)
        client.go_to_definition = AsyncMock(return_value=[])  # no edges

        await service.populate_file(
            file_path=Path("src/mod.py"), file_id=1, language="python",
        )

        # Verify second run: only C remains, A and B gone
        sym_rows = provider.execute_query(
            "SELECT fqn FROM symbols WHERE file_id = 1"
        )
        second_fqns = {row["fqn"] for row in sym_rows}
        assert second_fqns == {"C"}, f"Expected only C, got {second_fqns}"

        # Edges from A/B should be gone
        edge_rows = provider.execute_query(
            "SELECT from_fqn FROM symbol_edges WHERE from_fqn IN ('A', 'B')"
        )
        assert len(edge_rows) == 0, f"Expected 0 edges from A/B, got {len(edge_rows)}"


class TestMultiLanguagePopulation:
    """
    Feature: Symbols populated for multiple languages

    As the population service
    I want to populate symbols from Python, TypeScript, and Go files
    So that multi-language codebases have complete symbol coverage
    """

    @pytest.mark.asyncio
    async def test_three_languages_produce_correct_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: Three languages each produce symbols with correct language and lsp_server
        Given files in Python, TypeScript, and Go
        When populate_file is called for each
        Then symbols table has entries for all 3 languages with correct lsp_server per language
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/app.ts")
        _insert_file(provider, 3, "src/main.go")

        (tmp_path / "src").mkdir(parents=True)
        (tmp_path / "src" / "main.py").write_text("class PyClass: pass\n")
        (tmp_path / "src" / "app.ts").write_text("class TsClass {}\n")
        (tmp_path / "src" / "main.go").write_text("type GoStruct struct{}\n")

        py_symbols = [SymbolInfo(name="PyClass", kind=5, range_start_line=0,
                                  range_start_char=0, range_end_line=1, range_end_char=0)]
        ts_symbols = [SymbolInfo(name="TsClass", kind=5, range_start_line=0,
                                  range_start_char=0, range_end_line=1, range_end_char=0)]
        go_symbols = [SymbolInfo(name="GoStruct", kind=23, range_start_line=0,
                                  range_start_char=0, range_end_line=1, range_end_char=0)]

        # Create separate mock clients per language
        py_client = AsyncMock()
        py_client.document_symbols = AsyncMock(return_value=py_symbols)
        py_client.notify_did_open = AsyncMock()
        py_client.notify_did_close = AsyncMock()
        py_client.hover = AsyncMock(return_value=None)
        py_client.capabilities = {LSPCapability.DOCUMENT_SYMBOL}

        ts_client = AsyncMock()
        ts_client.document_symbols = AsyncMock(return_value=ts_symbols)
        ts_client.notify_did_open = AsyncMock()
        ts_client.notify_did_close = AsyncMock()
        ts_client.hover = AsyncMock(return_value=None)
        ts_client.capabilities = {LSPCapability.DOCUMENT_SYMBOL}

        go_client = AsyncMock()
        go_client.document_symbols = AsyncMock(return_value=go_symbols)
        go_client.notify_did_open = AsyncMock()
        go_client.notify_did_close = AsyncMock()
        go_client.hover = AsyncMock(return_value=None)
        go_client.capabilities = {LSPCapability.DOCUMENT_SYMBOL}

        client_map = {"python": py_client, "typescript": ts_client, "go": go_client}

        pool = AsyncMock()
        pool.get = AsyncMock(side_effect=lambda lang, _ws: client_map[lang])

        with patch.object(LSPPopulationService, "_collect_edges", new_callable=AsyncMock, return_value=[]):
            service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

            await service.populate_file(Path("src/main.py"), file_id=1, language="python")
            await service.populate_file(Path("src/app.ts"), file_id=2, language="typescript")
            await service.populate_file(Path("src/main.go"), file_id=3, language="go")

        rows = provider.execute_query(
            "SELECT fqn, language, lsp_server FROM symbols ORDER BY fqn"
        )
        assert len(rows) == 3

        by_fqn = {row["fqn"]: row for row in rows}
        assert by_fqn["PyClass"]["language"] == "python"
        assert by_fqn["TsClass"]["language"] == "typescript"
        assert by_fqn["GoStruct"]["language"] == "go"

        # lsp_server should differ per language (looked up from registry)
        servers = {row["lsp_server"] for row in rows}
        assert len(servers) >= 2, f"Expected different servers per language, got {servers}"


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
