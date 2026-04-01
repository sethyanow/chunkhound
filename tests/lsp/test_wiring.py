"""Wiring tests — batch, realtime, MCP server, and CLI integration of LSP population."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.core.config.config import Config
from chunkhound.mcp_server.base import MCPServerBase

pytestmark = pytest.mark.unit


# --- Shared helpers (deduplicates ~80 lines across MCP/CLI tests) ---


class _TestServer(MCPServerBase):
    """Minimal concrete MCPServerBase for testing."""

    def _register_tools(self) -> None:
        pass

    async def run(self) -> None:
        pass


def _make_mcp_config(tmp_path: Path) -> tuple[Config, SimpleNamespace]:
    """Shared MCP test config."""
    fake_args = SimpleNamespace(path=tmp_path)
    config = Config(
        args=fake_args,
        database={
            "path": str(tmp_path / ".chunkhound" / "test.db"),
            "provider": "duckdb",
        },
        indexing={"include": ["*.py"], "exclude": []},
    )
    return config, fake_args


def _make_mock_formatter() -> MagicMock:
    """Shared mock RichOutputFormatter for CLI tests."""
    mock_formatter = MagicMock()
    mock_progress_ctx = MagicMock()
    mock_progress_ctx.__enter__ = MagicMock(return_value=mock_progress_ctx)
    mock_progress_ctx.__exit__ = MagicMock(return_value=False)
    mock_progress_ctx.get_progress_instance = MagicMock(return_value=MagicMock())
    mock_formatter.create_progress_display.return_value = mock_progress_ctx
    return mock_formatter


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
        from chunkhound.database_factory import create_services

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

            # Create mock LSP population service with event-based signaling
            populate_called = asyncio.Event()
            mock_pop = AsyncMock()
            mock_pop.populate_file = AsyncMock(
                side_effect=lambda *_a, **_kw: populate_called.set()
            )

            service = RealtimeIndexingService(
                services, config, lsp_population=mock_pop,
            )
            service.watch_path = tmp_path  # Set watch path so relative_to works

            # Queue a file with "lsp" priority
            await service.add_file(test_file, priority="lsp")

            # Run processing loop and wait for populate_file signal (not sleep)
            service.process_task = asyncio.create_task(service._process_loop())
            await asyncio.wait_for(populate_called.wait(), timeout=5.0)
            service.process_task.cancel()
            try:
                await service.process_task
            except asyncio.CancelledError:
                pass

            # populate_file should have been called
            mock_pop.populate_file.assert_called_once()

        finally:
            services.provider.disconnect()


class TestMCPServerWiring:
    """
    Feature: MCP server constructs LSP population service and passes to indexing services

    As the MCP server
    I want to create an LSPClientPool and LSPPopulationService at startup
    So that realtime and batch indexing populate the symbols table
    """

    @pytest.mark.asyncio
    async def test_deferred_connect_passes_lsp_population_to_realtime(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: _deferred_connect_and_start wires lsp_population to RealtimeIndexingService
        Given an MCPServerBase with valid services
        When _deferred_connect_and_start completes
        Then RealtimeIndexingService._lsp_population is not None
        """
        config, fake_args = _make_mcp_config(tmp_path)
        server = _TestServer(config=config, args=fake_args)
        server.services = MagicMock()
        server.services.provider = MagicMock()

        with (
            patch.object(server, "_connect_provider", new_callable=AsyncMock),
            patch.object(server, "_coordinated_initial_scan", new_callable=AsyncMock),
        ):
            await server._deferred_connect_and_start(tmp_path)

        assert server.realtime_indexing is not None, (
            "RealtimeIndexingService should be created"
        )
        assert server.realtime_indexing._lsp_population is not None, (
            "base.py must pass lsp_population= to RealtimeIndexingService"
        )

    @pytest.mark.asyncio
    async def test_background_scan_passes_lsp_population_to_directory_service(
        self, tmp_path: Path
    ) -> None:
        """
        Scenario: _background_initial_scan wires lsp_population to DirectoryIndexingService
        Given an MCPServerBase with an LSPPopulationService stored on self
        When _background_initial_scan runs
        Then DirectoryIndexingService receives lsp_population
        """
        from chunkhound.services.directory_indexing_service import (
            DirectoryIndexingService,
        )

        config, fake_args = _make_mcp_config(tmp_path)
        server = _TestServer(config=config, args=fake_args)
        server.services = MagicMock()
        server.services.indexing_coordinator = MagicMock()

        # Simulate _deferred_connect_and_start having created the population service
        server._lsp_population_service = MagicMock()

        # Capture the DirectoryIndexingService constructor call
        captured_kwargs: dict = {}
        original_init = DirectoryIndexingService.__init__

        def spy_init(self_svc, *args, **kwargs):
            captured_kwargs.update(kwargs)
            # Mock process_directory to avoid real work
            self_svc.process_directory = AsyncMock(
                return_value=MagicMock(
                    files_processed=0,
                    chunks_created=0,
                )
            )
            original_init(self_svc, *args, **kwargs)

        with patch.object(DirectoryIndexingService, "__init__", spy_init):
            await server._background_initial_scan(tmp_path)

        assert "lsp_population" in captured_kwargs, (
            "base.py must pass lsp_population= to DirectoryIndexingService"
        )
        assert captured_kwargs["lsp_population"] is not None, (
            "lsp_population must be a live service, not None"
        )

    @pytest.mark.asyncio
    async def test_double_construction_preserves_original_pool(
        self, tmp_path: Path
    ) -> None:
        """
        Adversarial: State transition — _deferred_connect_and_start called twice.
        The guard (if self._lsp_pool is None) should prevent re-creation.
        """
        config, fake_args = _make_mcp_config(tmp_path)
        server = _TestServer(config=config, args=fake_args)
        server.services = MagicMock()
        server.services.provider = MagicMock()

        with (
            patch.object(server, "_connect_provider", new_callable=AsyncMock),
            patch.object(server, "_coordinated_initial_scan", new_callable=AsyncMock),
        ):
            await server._deferred_connect_and_start(tmp_path)
            first_pool = server._lsp_pool
            assert first_pool is not None

            # Second call — pool should be preserved, not re-created
            await server._deferred_connect_and_start(tmp_path)
            assert server._lsp_pool is first_pool, (
                "Double-construction must preserve original pool"
            )

    @pytest.mark.asyncio
    async def test_cleanup_nullifies_pool_and_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """
        Adversarial: State transition — cleanup sets pool to None.
        Second cleanup call is a no-op (idempotent).
        """
        config, fake_args = _make_mcp_config(tmp_path)
        server = _TestServer(config=config, args=fake_args)
        server.services = MagicMock()
        server.services.provider = MagicMock()
        server.services.provider.is_connected = False

        with (
            patch.object(server, "_connect_provider", new_callable=AsyncMock),
            patch.object(server, "_coordinated_initial_scan", new_callable=AsyncMock),
        ):
            await server._deferred_connect_and_start(tmp_path)
            assert server._lsp_pool is not None

        # Patch stop_all to track calls
        server._lsp_pool.stop_all = AsyncMock()

        await server.cleanup()
        assert server._lsp_pool is None, "cleanup must nullify pool"
        assert server._lsp_population_service is None, "cleanup must nullify service"

        # Second cleanup — no-op, no error
        await server.cleanup()  # Should not raise

    @pytest.mark.asyncio
    async def test_cleanup_survives_pool_stop_all_failure(
        self, tmp_path: Path
    ) -> None:
        """
        Adversarial: Semantically hostile — pool.stop_all() raises during cleanup.
        Cleanup must not propagate the error.
        """
        config, fake_args = _make_mcp_config(tmp_path)
        server = _TestServer(config=config, args=fake_args)
        server.services = MagicMock()
        server.services.provider = MagicMock()
        server.services.provider.is_connected = False

        # Simulate pool that fails on stop_all
        mock_pool = MagicMock()
        mock_pool.stop_all = AsyncMock(side_effect=RuntimeError("LSP process hung"))
        server._lsp_pool = mock_pool
        server._lsp_population_service = MagicMock()

        # Cleanup must not raise
        await server.cleanup()
        assert server._lsp_pool is None, "Pool must be nullified even after error"


class TestCLIWiring:
    """
    Feature: CLI index command constructs LSP population and passes to DirectoryIndexingService

    As the chunkhound CLI
    I want to create an LSPClientPool and LSPPopulationService for bulk indexing
    So that `chunkhound index .` populates the symbols table
    """

    @pytest.mark.asyncio
    async def test_run_command_passes_lsp_population(self, tmp_path: Path) -> None:
        """
        Scenario: run_command wires lsp_population to DirectoryIndexingService
        Given a valid config and target path
        When run_command executes the indexing flow
        Then DirectoryIndexingService receives a non-None lsp_population
        """
        from argparse import Namespace
        from typing import Any

        from chunkhound.services.directory_indexing_service import (
            DirectoryIndexingService,
        )

        # Minimal args + config
        db_path = tmp_path / ".chunkhound" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        args = Namespace(
            path=str(tmp_path),
            db=str(db_path),
            verbose=False,
            no_embeddings=True,
            check_ignores=False,
            simulate=False,
            perf_diagnostics=False,
            profile_startup=False,
        )
        config = Config(
            args=args,
            database={"path": str(db_path), "provider": "duckdb"},
            indexing={"include": ["*.py"], "exclude": []},
        )

        # Capture DirectoryIndexingService constructor kwargs
        captured_kwargs: dict = {}
        mock_stats = MagicMock(
            files_processed=0, chunks_created=0, embeddings_generated=0,
            files_skipped=0, files_errors=0, errors_encountered=[],
            skipped_due_to_timeout=[], skipped_unchanged=0, skipped_filtered=0,
            processing_time=0.0, cleanup_deleted_files=0, cleanup_deleted_chunks=0,
        )
        original_init = DirectoryIndexingService.__init__

        def spy_init(self_svc: Any, *a: Any, **kw: Any) -> None:
            captured_kwargs.update(kw)
            original_init(self_svc, *a, **kw)
            # Prevent real indexing
            self_svc.process_directory = AsyncMock(return_value=mock_stats)

        # Mock coordinator
        mock_coord = MagicMock()
        mock_coord.get_stats = AsyncMock(return_value={})

        run_mod = "chunkhound.api.cli.commands.run"
        with (
            patch(f"{run_mod}.configure_registry"),
            patch(f"{run_mod}.create_indexing_coordinator", return_value=mock_coord),
            patch(f"{run_mod}.process_batch_arguments"),
            patch(f"{run_mod}._validate_run_arguments", return_value=True),
            patch(f"{run_mod}.RichOutputFormatter", return_value=_make_mock_formatter()),
            patch.object(DirectoryIndexingService, "__init__", spy_init),
        ):
            from chunkhound.api.cli.commands.run import run_command

            await run_command(args, config)

        assert "lsp_population" in captured_kwargs, (
            "run.py must pass lsp_population= to DirectoryIndexingService"
        )
        assert captured_kwargs["lsp_population"] is not None, (
            "lsp_population must be a live service, not None"
        )

    @pytest.mark.asyncio
    async def test_cli_pool_cleanup_on_indexing_failure(self, tmp_path: Path) -> None:
        """
        Adversarial: State transition — process_directory raises, pool still cleaned up.
        """
        from argparse import Namespace
        from typing import Any

        from chunkhound.lsp.client import LSPClientPool
        from chunkhound.services.directory_indexing_service import (
            DirectoryIndexingService,
        )

        db_path = tmp_path / ".chunkhound" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        args = Namespace(
            path=str(tmp_path),
            db=str(db_path),
            verbose=False,
            no_embeddings=True,
            check_ignores=False,
            simulate=False,
            perf_diagnostics=False,
            profile_startup=False,
        )
        config = Config(
            args=args,
            database={"path": str(db_path), "provider": "duckdb"},
            indexing={"include": ["*.py"], "exclude": []},
        )

        # Track pool.stop_all calls
        pool_stop_called: list[bool] = []
        original_init = DirectoryIndexingService.__init__

        def spy_init(self_svc: Any, *a: Any, **kw: Any) -> None:
            original_init(self_svc, *a, **kw)
            self_svc.process_directory = AsyncMock(
                side_effect=RuntimeError("Index failed")
            )

        mock_coord = MagicMock()
        mock_coord.get_stats = AsyncMock(return_value={})
        mock_coord.database = MagicMock()

        async def spy_stop(_self_pool: Any) -> None:
            pool_stop_called.append(True)

        run_mod = "chunkhound.api.cli.commands.run"
        with (
            patch(f"{run_mod}.configure_registry"),
            patch(f"{run_mod}.create_indexing_coordinator", return_value=mock_coord),
            patch(f"{run_mod}.process_batch_arguments"),
            patch(f"{run_mod}._validate_run_arguments", return_value=True),
            patch(f"{run_mod}.RichOutputFormatter", return_value=_make_mock_formatter()),
            patch.object(DirectoryIndexingService, "__init__", spy_init),
            patch.object(LSPClientPool, "stop_all", spy_stop),
        ):
            from chunkhound.api.cli.commands.run import run_command

            with pytest.raises(SystemExit):
                await run_command(args, config)

        assert len(pool_stop_called) == 1, (
            "Pool must be cleaned up even when indexing fails"
        )
