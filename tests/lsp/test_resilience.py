"""Resilience tests — error handling, incremental refresh, multi-language, adversarial."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from chunkhound.lsp.types import (
    Location,
    LSPCapability,
    LSPError,
    LSPTransportError,
    SymbolInfo,
)
from chunkhound.services.lsp_population import LSPPopulationService
from tests.lsp.conftest import (
    _insert_file,
    _make_mock_pool,
    _make_provider,
    _sample_symbols,
)

pytestmark = pytest.mark.unit


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


class TestPopulateFilesResilience:
    """
    Feature: populate_files continues on per-file failure (ch-ko4)

    As the batch indexing path
    I want a single file failure to not crash the entire loop
    So that remaining files are populated and workspace symbols still runs
    """

    @pytest.mark.asyncio
    async def test_loop_continues_after_provider_error(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: ProviderError on one file must not abort the loop.
        ProviderError is the protocol-level wrapper for backend DB errors
        (e.g. FK violation, constraint error), so catching it here gives
        callers one type regardless of backend (DuckDB, LanceDB, etc.).
        """
        from chunkhound.interfaces.database_provider import ProviderError

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        for name in ("src/a.py", "src/b.py"):
            f = tmp_path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        # Monkeypatch populate_file to raise ProviderError on first file only
        original_populate = service.populate_file
        call_count = 0

        async def patched_populate(file_path, file_id, language):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderError(
                    "Constraint Error: Violates foreign key constraint"
                )
            return await original_populate(file_path, file_id, language)

        service.populate_file = patched_populate  # type: ignore[assignment]

        # Should NOT raise — loop must catch the provider error and continue
        await service.populate_files()

        # Second file should still be populated
        rows = provider.execute_query("SELECT file_id FROM symbols")
        file_ids = {r["file_id"] for r in rows}
        assert 2 in file_ids, "Second file should be populated despite first file's provider error"

    @pytest.mark.asyncio
    async def test_loop_continues_after_mid_loop_transport_error(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: Second of three files raises LSPTransportError
        Given three files in the DB, middle one triggers transport error
        When populate_files runs
        Then first and third files are populated, workspace symbols runs
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")
        _insert_file(provider, 3, "src/c.py")

        symbols = _sample_symbols()
        pool, client = _make_mock_pool(symbols)

        # Create source files
        for name in ("a.py", "b.py", "c.py"):
            f = tmp_path / "src" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class Foo:\n    def bar(self): ...\n")

        call_count = 0

        async def doc_symbols_with_failure(_uri: str) -> list:
            nonlocal call_count
            call_count += 1
            if call_count == 2:  # Second file fails
                raise LSPTransportError("Client degraded")
            return symbols

        client.document_symbols = AsyncMock(side_effect=doc_symbols_with_failure)
        # workspace_symbols returns empty (just needs to be called)
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        await service.populate_files()

        # Files 1 and 3 should have symbols, file 2 should not
        rows = provider.execute_query(
            "SELECT DISTINCT file_id FROM symbols ORDER BY file_id"
        )
        file_ids = [r["file_id"] for r in rows]
        assert 1 in file_ids, "First file should be populated"
        assert 2 not in file_ids, "Failed file should have no symbols"
        assert 3 in file_ids, "Third file should be populated despite earlier failure"

    @pytest.mark.asyncio
    async def test_workspace_symbols_runs_after_all_files_fail(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: Every file fails but workspace symbols still runs
        Given one file that raises on populate
        When populate_files runs
        Then _populate_workspace_symbols is still called
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/fail.py")

        pool, client = _make_mock_pool()
        client.document_symbols = AsyncMock(
            side_effect=LSPTransportError("Dead")
        )
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        (tmp_path / "src" / "fail.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "fail.py").write_text("x = 1\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        await service.populate_files()

        # workspace_symbols should have been called despite file failure
        client.workspace_symbols.assert_called_once()

    @pytest.mark.asyncio
    async def test_summary_logged_with_correct_counts(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Scenario: Summary shows accurate populated/failed/skipped counts
        Given three files — one succeeds, one fails, one has no server
        When populate_files completes
        Then summary log contains correct counts
        """
        import logging as _logging

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/good.py")
        _insert_file(provider, 2, "src/bad.py")
        _insert_file(provider, 3, "src/skip.mk")

        symbols = _sample_symbols()

        call_count = 0

        async def get_client(language: str, _workspace: str) -> AsyncMock:
            nonlocal call_count
            if language == "makefile":
                raise LSPError("No server for makefile")
            client = AsyncMock()
            client.notify_did_open = AsyncMock()
            client.notify_did_close = AsyncMock()
            client.workspace_symbols = AsyncMock(return_value=[])
            client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

            async def doc_sym(_uri: str) -> list:
                nonlocal call_count
                call_count += 1
                if "bad.py" in _uri:
                    raise LSPTransportError("Degraded")
                return symbols

            client.document_symbols = AsyncMock(side_effect=doc_sym)
            return client

        pool = AsyncMock()
        pool.get = AsyncMock(side_effect=get_client)

        for name in ("good.py", "bad.py"):
            f = tmp_path / "src" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class Foo:\n    def bar(self): ...\n")
        # skip.mk doesn't need to exist — pool.get raises before file read

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        with caplog.at_level(_logging.INFO, logger="chunkhound.services.lsp_population"):
            await service.populate_files()

        # Find the summary log line
        summary_lines = [r.message for r in caplog.records if "populated" in r.message.lower() and "failed" in r.message.lower()]
        assert len(summary_lines) >= 1, f"Expected summary log, got: {[r.message for r in caplog.records]}"
        summary = summary_lines[0]
        assert "1" in summary, f"Expected 1 populated in: {summary}"

    @pytest.mark.asyncio
    async def test_failed_file_logged_with_path_and_error(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Scenario: Each failed file gets a log entry with path and error detail
        """
        import logging as _logging

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/broken.py")

        pool, client = _make_mock_pool()
        client.document_symbols = AsyncMock(
            side_effect=LSPTransportError("Pipe broken")
        )
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        (tmp_path / "src" / "broken.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "broken.py").write_text("x = 1\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        with caplog.at_level(_logging.WARNING, logger="chunkhound.services.lsp_population"):
            await service.populate_files()

        # Should log the failed file path and error
        failure_logs = [r.message for r in caplog.records if "broken.py" in r.message]
        assert len(failure_logs) >= 1, f"Expected failure log for broken.py, got: {[r.message for r in caplog.records]}"
        assert "Pipe broken" in failure_logs[0] or "LSPTransportError" in failure_logs[0]


class TestPopulateFilesTypedCatch:
    """Tests that populate_files catches only expected exception types."""

    @pytest.mark.asyncio
    async def test_runtime_error_escapes_the_loop(self, tmp_path: Path) -> None:
        """RuntimeError is unexpected — must NOT be caught, should propagate."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "a.py").write_text("x = 1\n")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        async def exploding_populate(**_kwargs):  # noqa: ARG001
            raise RuntimeError("unexpected internal bug")

        service.populate_file = exploding_populate  # type: ignore[assignment]

        with pytest.raises(RuntimeError, match="unexpected internal bug"):
            await service.populate_files()

    @pytest.mark.asyncio
    async def test_provider_error_is_caught(self, tmp_path: Path) -> None:
        """ProviderError (the backend-agnostic wrapper) is caught — loop continues."""
        from chunkhound.interfaces.database_provider import ProviderError

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")
        for name in ("a.py", "b.py"):
            f = tmp_path / "src" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class Foo:\n    def bar(self): ...\n")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        call_count = 0

        async def patched(file_path, file_id, language):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ProviderError("FK violation")
            return await service._original_populate(file_path, file_id, language)

        service._original_populate = service.populate_file  # type: ignore[attr-defined]
        service.populate_file = patched  # type: ignore[assignment]

        # Should NOT raise — ProviderError is caught
        await service.populate_files()


class TestPopulateFilesAdversarial:
    """Adversarial stress tests for populate_files error handling (ch-ko4)."""

    @pytest.mark.asyncio
    async def test_empty_files_table_no_crash(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Empty: zero files in DB — loop is a no-op, summary still logged."""
        import logging as _logging

        provider = _make_provider(tmp_path)
        pool, client = _make_mock_pool()
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.WORKSPACE_SYMBOL}

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        with caplog.at_level(_logging.INFO, logger="chunkhound.services.lsp_population"):
            await service.populate_files()

        summary = [r.message for r in caplog.records if "complete" in r.message.lower()]
        assert len(summary) >= 1, f"Expected summary log, got: {[r.message for r in caplog.records]}"

    @pytest.mark.asyncio
    async def test_file_with_no_extension_caught(self, tmp_path: Path) -> None:
        """Type boundary: file with no extension — Language.from_file_extension may raise."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "Makefile")
        _insert_file(provider, 2, "src/good.py")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        (tmp_path / "Makefile").write_text("all: build\n")
        src = tmp_path / "src" / "good.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        # Should not crash — extensionless file should be handled
        await service.populate_files()

        # good.py should still be populated despite Makefile issue
        rows = provider.execute_query("SELECT file_id FROM symbols")
        file_ids = {r["file_id"] for r in rows}
        assert 2 in file_ids, "good.py should be populated even if Makefile fails"

    @pytest.mark.asyncio
    async def test_second_run_resets_counters(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Second run: counters must reflect the second run, not accumulate."""
        import logging as _logging

        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        src = tmp_path / "src" / "a.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        # First run
        await service.populate_files()

        # Second run — capture logs
        caplog.clear()
        with caplog.at_level(_logging.INFO, logger="chunkhound.services.lsp_population"):
            await service.populate_files()

        summary = [r.message for r in caplog.records if "complete" in r.message.lower()]
        assert len(summary) >= 1
        # Should show 1 populated for second run, not 2 accumulated
        assert "1 populated" in summary[0]

    @pytest.mark.asyncio
    async def test_file_deleted_from_disk_between_query_and_populate(
        self, tmp_path: Path
    ) -> None:
        """Semantically hostile: file in DB but deleted from disk before populate_file reads it."""
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/ghost.py")
        _insert_file(provider, 2, "src/real.py")

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        # Only create real.py — ghost.py doesn't exist on disk
        src = tmp_path / "src" / "real.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)
        await service.populate_files()

        # real.py should be populated, ghost.py skipped
        rows = provider.execute_query("SELECT file_id FROM symbols")
        file_ids = {r["file_id"] for r in rows}
        assert 2 in file_ids, "real.py should be populated"
        assert 1 not in file_ids, "ghost.py should be skipped (not on disk)"
