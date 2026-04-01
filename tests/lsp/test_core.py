"""Core LSPPopulationService tests — populate_file, batch insert, FQN, delete, skip."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.lsp.types import LSPError, SymbolInfo
from chunkhound.services.lsp_population import LSPPopulationService, PopulateResult
from tests.lsp.conftest import (
    _insert_file,
    _make_mock_pool,
    _make_provider,
    _sample_symbols,
)

pytestmark = pytest.mark.unit


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


class TestPopulateFileEdgeCollectionFailure:
    """
    Regression: when _collect_edges raises inside populate_file's try block,
    the exception must propagate, notify_did_close must still fire (finally),
    and _batch_insert_edges must NOT be called with stale data.

    Guards removal of dead `edges: list[tuple] = []` initializer (line 78
    diagnostic: variable unused because exception path re-raises past the
    `if edges:` check).
    """

    @pytest.mark.asyncio
    async def test_collect_edges_failure_still_calls_did_close(
        self, tmp_path: Path,
    ) -> None:
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        pool, client = _make_mock_pool(_sample_symbols())

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        # Make _collect_edges blow up after symbols are inserted
        with patch.object(service, "_collect_edges", side_effect=RuntimeError("LSP crashed")):
            with pytest.raises(RuntimeError, match="LSP crashed"):
                await service.populate_file(
                    file_path=Path("src/greeter.py"), file_id=1, language="python",
                )

        # Finally block must have fired
        client.notify_did_close.assert_called_once()

    @pytest.mark.asyncio
    async def test_collect_edges_failure_does_not_insert_stale_edges(
        self, tmp_path: Path,
    ) -> None:
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        pool, client = _make_mock_pool(_sample_symbols())

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n\ndef main(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        with patch.object(service, "_collect_edges", side_effect=RuntimeError("LSP crashed")):
            with patch.object(service, "_batch_insert_edges") as mock_insert_edges:
                with pytest.raises(RuntimeError, match="LSP crashed"):
                    await service.populate_file(
                        file_path=Path("src/greeter.py"), file_id=1, language="python",
                    )

                # Must NOT have been called — exception path skips edge insertion
                mock_insert_edges.assert_not_called()


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
    Feature: Batch INSERT flattens heterogeneous symbol trees with correct column alignment

    A column-shift bug in tuple flattening (e.g., `name` landing in `kind`) is
    invisible to tests that only count INSERT calls. This test asserts all 12
    columns on every row of a varied symbol tree: 3 levels deep, nodes with 0/1/3
    children, different kinds, siblings at different depths.
    """

    @pytest.mark.asyncio
    async def test_heterogeneous_tree_all_columns_correct(self, tmp_path: Path) -> None:
        """
        Given a symbol tree:
          config (Variable, leaf)
          Router (Class)
            get (Method, leaf)
            post (Method)
              _validate (Function, leaf)  ← 3 levels deep
            middleware (Property, leaf)
          standalone (Function, leaf)
        When populate_file is called
        Then all 7 symbols have correct values across all 12 columns.
        """
        _validate = SymbolInfo(
            name="_validate", kind=12, range_start_line=8, range_start_char=8,
            range_end_line=10, range_end_char=0, children=[],
        )
        get = SymbolInfo(
            name="get", kind=6, range_start_line=3, range_start_char=4,
            range_end_line=5, range_end_char=0, children=[],
        )
        post = SymbolInfo(
            name="post", kind=6, range_start_line=6, range_start_char=4,
            range_end_line=11, range_end_char=0, children=[_validate],
        )
        middleware = SymbolInfo(
            name="middleware", kind=7, range_start_line=12, range_start_char=4,
            range_end_line=14, range_end_char=0, children=[],
        )
        router = SymbolInfo(
            name="Router", kind=5, range_start_line=2, range_start_char=0,
            range_end_line=14, range_end_char=0, children=[get, post, middleware],
        )
        config = SymbolInfo(
            name="config", kind=13, range_start_line=1, range_start_char=0,
            range_end_line=1, range_end_char=20, children=[],
        )
        standalone = SymbolInfo(
            name="standalone", kind=12, range_start_line=16, range_start_char=0,
            range_end_line=18, range_end_char=0, children=[],
        )

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/app.py")

        pool, _ = _make_mock_pool([config, router, standalone])

        src_file = tmp_path / "src" / "app.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("config = {}\nclass Router:\n    ...\ndef standalone(): ...\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/app.py"), file_id=1, language="python",
        )

        rows = provider.execute_query(
            "SELECT fqn, name, kind, language, file_id, file_path, "
            "range_start, range_end, parent_fqn, confidence, lsp_server, "
            "type_signature "
            "FROM symbols ORDER BY range_start"
        )

        assert len(rows) == 7

        # All 12 columns verified per row — catches tuple column-shift bugs
        srv = "pyright-langserver"
        expected = [
            # (fqn, name, kind, language, file_id, file_path,
            #  range_start, range_end, parent_fqn, confidence, lsp_server, type_sig)
            ("config", "config", "Variable", "python", 1, "src/app.py",
             1, 1, None, 1.0, srv, None),
            ("Router", "Router", "Class", "python", 1, "src/app.py",
             2, 14, None, 1.0, srv, None),
            ("Router::get", "get", "Method", "python", 1, "src/app.py",
             3, 5, "Router", 1.0, srv, None),
            ("Router::post", "post", "Method", "python", 1, "src/app.py",
             6, 11, "Router", 1.0, srv, None),
            ("Router::post::_validate", "_validate", "Function", "python", 1, "src/app.py",
             8, 10, "Router::post", 1.0, srv, None),
            ("Router::middleware", "middleware", "Property", "python", 1, "src/app.py",
             12, 14, "Router", 1.0, srv, None),
            ("standalone", "standalone", "Function", "python", 1, "src/app.py",
             16, 18, None, 1.0, srv, None),
        ]

        columns = [
            "fqn", "name", "kind", "language", "file_id", "file_path",
            "range_start", "range_end", "parent_fqn", "confidence",
            "lsp_server", "type_signature",
        ]
        for i, (row, exp) in enumerate(zip(rows, expected)):
            for col, val in zip(columns, exp):
                actual = row[col]
                if col == "confidence":
                    assert actual == pytest.approx(val), (
                        f"Row {i} ({exp[0]}): {col} = {actual!r}, expected {val!r}"
                    )
                else:
                    assert actual == val, (
                        f"Row {i} ({exp[0]}): {col} = {actual!r}, expected {val!r}"
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


class TestWorkspaceRootResolution:
    """
    Feature: LSPPopulationService resolves relative workspace_root to absolute

    As the indexing pipeline
    I want workspace_root to always be absolute
    So that Path.as_uri() never raises ValueError for relative paths
    """

    def test_relative_workspace_root_resolved_to_absolute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        Scenario: Constructor receives a relative path
        Given a relative Path(".") as workspace_root
        When LSPPopulationService is constructed
        Then _workspace_root is an absolute path
        """
        monkeypatch.chdir(tmp_path)
        provider = _make_provider(tmp_path)
        pool, _ = _make_mock_pool()

        service = LSPPopulationService(pool, provider, workspace_root=Path("."))
        assert service._workspace_root.is_absolute(), (
            f"workspace_root must be absolute, got: {service._workspace_root}"
        )


class TestPopulateResult:
    """
    Feature: populate_file returns a status enum for accurate counting

    As the population loop
    I want populate_file to signal populated/skipped/failed
    So that the summary can report accurate counts (ch-ko4)
    """

    def test_enum_has_three_members(self) -> None:
        """PopulateResult has POPULATED, SKIPPED, FAILED members."""
        assert hasattr(PopulateResult, "POPULATED")
        assert hasattr(PopulateResult, "SKIPPED")
        assert hasattr(PopulateResult, "FAILED")

    @pytest.mark.asyncio
    async def test_populate_file_returns_populated_on_success(
        self, tmp_path: Path
    ) -> None:
        """populate_file returns POPULATED when symbols are written."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/greeter.py")

        pool, _client = _make_mock_pool(_sample_symbols())

        src_file = tmp_path / "src" / "greeter.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("class Greeter:\n    def greet(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        result = await service.populate_file(
            file_path=Path("src/greeter.py"), file_id=1, language="python",
        )
        assert result is PopulateResult.POPULATED

    @pytest.mark.asyncio
    async def test_populate_file_returns_skipped_when_no_server(
        self, tmp_path: Path
    ) -> None:
        """populate_file returns SKIPPED when LSPError (no server) is raised."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.mk")

        pool = AsyncMock()
        pool.get = AsyncMock(side_effect=LSPError("No server"))

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        result = await service.populate_file(
            file_path=Path("src/main.mk"), file_id=1, language="makefile",
        )
        assert result is PopulateResult.SKIPPED

    @pytest.mark.asyncio
    async def test_populate_file_returns_skipped_when_unreadable(
        self, tmp_path: Path
    ) -> None:
        """populate_file returns SKIPPED when file can't be read."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/missing.py")

        pool, _client = _make_mock_pool()

        # Don't create the file — it won't be readable
        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        result = await service.populate_file(
            file_path=Path("src/missing.py"), file_id=1, language="python",
        )
        assert result is PopulateResult.SKIPPED
