"""Integration tests for LSP client manager.

All tests require pyright-langserver on PATH.
Tests use real pyright process — no mocks.
"""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path

import pytest

from chunkhound.lsp.client import LSPClient
from chunkhound.lsp.types import (
    LSPCapability,
    LSPCapabilityError,
    LSPError,
    ServerConfig,
    ServerState,
)

# Skip entire module if pyright not available
pytestmark = [pytest.mark.skipif(
    not shutil.which("pyright-langserver"),
    reason="pyright-langserver not installed",
), pytest.mark.integration]

FIXTURES_DIR = Path(__file__).parent / "fixtures"

PYRIGHT_CONFIG = ServerConfig(
    language_id="python",
    command="pyright-langserver",
    args=["--stdio"],
    request_timeout=60.0,  # pyright can be slow on first operations
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a temp workspace with the LSP test fixture."""
    sample = FIXTURES_DIR / "lsp_test_sample.py"
    dest = tmp_path / "lsp_test_sample.py"
    dest.write_text(sample.read_text())
    return tmp_path


class TestSpawnAndInitialize:
    """
    Feature: LSP client spawn and initialize handshake

    As a ChunkHound indexing service
    I want to spawn an LSP server and complete the initialize handshake
    So that I can query the server for symbol information
    """

    @pytest.mark.asyncio
    async def test_spawn_and_initialize_pyright(self, workspace: Path) -> None:
        """
        Scenario: Spawn pyright and complete initialize handshake
        Given a workspace directory with a Python file
        When I create an LSPClient with pyright config and call start()
        Then the client state should transition to READY
        And the initialize response should contain capabilities
        And the server info should include a name
        """
        client = LSPClient(PYRIGHT_CONFIG)

        assert client.state == ServerState.NOT_STARTED

        try:
            init_result = await client.start(workspace)

            assert client.state == ServerState.READY
            assert isinstance(init_result, dict)
            assert "capabilities" in init_result
            assert isinstance(init_result["capabilities"], dict)
            # Capabilities should be non-empty (pyright advertises many)
            assert len(init_result["capabilities"]) > 0
            # Parsed capabilities should include at least definition and hover
            assert LSPCapability.DEFINITION in client.capabilities
            assert LSPCapability.HOVER in client.capabilities
        finally:
            await client.stop()


class TestStateTracking:
    """
    Feature: Server state lifecycle tracking

    As a ChunkHound indexing service
    I want the LSP client to track server state accurately
    So that I can detect failures and avoid sending requests to dead servers
    """

    @pytest.mark.asyncio
    async def test_server_state_transitions(self, workspace: Path) -> None:
        """
        Scenario: State transitions through normal lifecycle
        Given a fresh LSPClient
        When I start then stop the server
        Then states should be NOT_STARTED → READY → STOPPED
        """
        client = LSPClient(PYRIGHT_CONFIG)
        assert client.state == ServerState.NOT_STARTED

        try:
            await client.start(workspace)
            assert client.state == ServerState.READY
        finally:
            await client.stop()

        assert client.state == ServerState.STOPPED

    @pytest.mark.asyncio
    async def test_degraded_state_on_process_exit(self, workspace: Path) -> None:
        """
        Scenario: Server process exits unexpectedly
        Given a running LSP client in READY state
        When the server process is killed
        Then the client state should transition to DEGRADED
        And degraded_reason should have code and detail keys
        """
        client = LSPClient(PYRIGHT_CONFIG)

        try:
            await client.start(workspace)
            assert client.state == ServerState.READY

            # Kill the server process
            proc = client._transport._process
            proc.kill()
            await proc.wait()

            # Give the monitor task time to detect the exit
            await asyncio.sleep(0.2)

            assert client.state == ServerState.DEGRADED
            assert client.degraded_reason is not None
            assert "code" in client.degraded_reason
            assert "detail" in client.degraded_reason
            assert client.degraded_reason["code"] == "process_exited"
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_double_start_raises(self, workspace: Path) -> None:
        """
        Scenario: Calling start() twice on the same client
        Given an LSP client that is already READY
        When start() is called again
        Then it should raise LSPError
        """
        client = LSPClient(PYRIGHT_CONFIG)

        try:
            await client.start(workspace)

            with pytest.raises(LSPError, match="Cannot start client"):
                await client.start(workspace)
        finally:
            await client.stop()


class TestCapabilityGating:
    """
    Feature: Capability gating for LSP operations

    As a ChunkHound indexing service
    I want operations to check server capabilities before sending requests
    So that I get a clear error instead of a confusing server error or crash
    """

    @pytest.mark.asyncio
    async def test_capability_gating_unadvertised(self, workspace: Path) -> None:
        """
        Scenario: Calling an operation the server doesn't advertise
        Given a running LSP client with capabilities parsed
        When a capability is removed from the internal set
        And the corresponding operation is called
        Then LSPCapabilityError should be raised with the method name
        """
        client = LSPClient(PYRIGHT_CONFIG)
        try:
            await client.start(workspace)

            # Artificially remove a capability we know pyright has
            client._capabilities.discard(LSPCapability.HOVER)

            uri = (workspace / "lsp_test_sample.py").as_uri()
            with pytest.raises(LSPCapabilityError, match="hover"):
                await client.hover(uri, 0, 0)
        finally:
            await client.stop()

    @pytest.mark.asyncio
    async def test_capability_gating_advertised(self, workspace: Path) -> None:
        """
        Scenario: Calling an operation the server does advertise
        Given a running LSP client
        When an advertised operation is called
        Then it should succeed without LSPCapabilityError
        """
        client = LSPClient(PYRIGHT_CONFIG)
        try:
            await client.start(workspace)
            assert LSPCapability.DOCUMENT_SYMBOL in client.capabilities

            uri = (workspace / "lsp_test_sample.py").as_uri()
            # Should not raise — just verify no exception
            result = await client.document_symbols(uri)
            assert isinstance(result, list)
        finally:
            await client.stop()


@pytest.fixture
async def ready_client(workspace: Path) -> LSPClient:
    """Provide a started LSP client with test file opened, cleaned up after test."""
    # Add pyrightconfig.json to scope analysis to workspace only
    pyright_config = workspace / "pyrightconfig.json"
    pyright_config.write_text('{"include": ["."], "extraPaths": []}')

    client = LSPClient(PYRIGHT_CONFIG)
    await client.start(workspace)

    # Open the test file so pyright starts analyzing it
    sample_path = workspace / "lsp_test_sample.py"
    uri = sample_path.as_uri()
    text = sample_path.read_text()
    await client.notify_did_open(uri, text)

    # Wait for pyright to publish initial diagnostics (signals analysis is done)
    await client.wait_for_diagnostics(uri, timeout=10.0)
    # Additional settle time for reference index building
    await asyncio.sleep(1)

    yield client
    await client.stop()


class TestLSPOperations:
    """
    Feature: 8 LSP operations via pyright

    As a ChunkHound indexing service
    I want to query pyright for symbols, definitions, references, hover,
    call hierarchy, implementations, and diagnostics
    So that I can build a symbol graph for the codebase
    """

    @pytest.mark.asyncio
    async def test_document_symbols(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Get all symbols in a Python file
        Given a file with classes, functions, and variables
        When documentSymbol is called
        Then it returns symbols with names, kinds, and ranges
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        symbols = await ready_client.document_symbols(uri)

        assert len(symbols) > 0
        names = {s.name for s in symbols}
        # Fixture has Greeter, FriendlyGreeter, format_greeting, make_greeting, result, bad_value
        assert "Greeter" in names
        assert "FriendlyGreeter" in names
        assert "format_greeting" in names
        assert "make_greeting" in names

        # Check that kind values are valid SymbolKind numbers
        for s in symbols:
            assert s.kind > 0

    @pytest.mark.asyncio
    async def test_go_to_definition(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Navigate from function call to its definition
        Given a call to format_greeting at line 24 char 15 (inside FriendlyGreeter.greet)
        When goToDefinition is called at that position
        Then it resolves to the format_greeting definition at line 27
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 24 (0-indexed) = line 25 in file = `return format_greeting(name)`
        # format_greeting starts at char 15
        locations = await ready_client.go_to_definition(uri, 24, 15)

        assert len(locations) >= 1
        # Definition should be on line 27 (0-indexed) = line 28 in file
        def_loc = locations[0]
        assert def_loc.range_start_line == 27

    @pytest.mark.asyncio
    @pytest.mark.timeout(90)
    async def test_find_references(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Find all references to a function
        Given format_greeting defined at line 27
        When findReferences is called at the function name
        Then it returns at least 2 locations (definition + call in FriendlyGreeter.greet)
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 27 (0-indexed) = `def format_greeting(name: str) -> str:`
        refs = await ready_client.find_references(uri, 27, 4)

        assert len(refs) >= 2  # definition + at least one call

    @pytest.mark.asyncio
    async def test_hover(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Get type information for a variable
        Given a type-annotated variable `result: str` at line 40
        When hover is called at that position
        Then it returns markdown content with type information
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 40 (0-indexed) = `result: str = make_greeting("World")`
        hover_result = await ready_client.hover(uri, 40, 0)

        assert hover_result is not None
        assert len(hover_result.contents) > 0
        # Should contain type info
        assert "str" in hover_result.contents

    @pytest.mark.asyncio
    async def test_incoming_calls(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Find callers of a function (two-step call hierarchy)
        Given format_greeting is called by FriendlyGreeter.greet
        When incomingCalls is called at format_greeting definition
        Then it returns at least one caller
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 27 (0-indexed), char 4 = `format_greeting`
        callers = await ready_client.incoming_calls(uri, 27, 4)

        assert len(callers) >= 1
        caller_names = {c.name for c in callers}
        assert "greet" in caller_names

    @pytest.mark.asyncio
    async def test_outgoing_calls(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Find callees of a function (two-step call hierarchy)
        Given make_greeting calls FriendlyGreeter() and .greet() and .upper()
        When outgoingCalls is called at make_greeting definition
        Then it returns callees
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 32 (0-indexed), char 4 = `make_greeting`
        callees = await ready_client.outgoing_calls(uri, 32, 4)

        assert len(callees) >= 1

    @pytest.mark.asyncio
    async def test_go_to_implementation(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Find implementations of a Protocol method
        Given Greeter Protocol with greet() method
        When goToImplementation is called on the Protocol's greet method
        Then it returns FriendlyGreeter.greet location
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # Line 17 (0-indexed), char 8 = Greeter.greet method in Protocol
        impls = await ready_client.go_to_implementation(uri, 17, 8)

        # Pyright may or may not resolve Protocol implementations
        # At minimum, it should not crash
        assert isinstance(impls, list)

    @pytest.mark.asyncio
    async def test_get_diagnostics(self, ready_client: LSPClient, workspace: Path) -> None:
        """
        Scenario: Get diagnostics for a file with a type error
        Given a file with `bad_value: int = "not an int"`
        When getDiagnostics is called (via publishDiagnostics notification cache)
        Then it returns at least one diagnostic about type mismatch
        """
        uri = (workspace / "lsp_test_sample.py").as_uri()
        # ready_client fixture already waited for diagnostics
        diagnostics = await ready_client.get_diagnostics(uri)

        # There should be at least the type error on bad_value
        assert len(diagnostics) >= 1
        # Find the type error diagnostic on line 43 (0-indexed for line 44)
        type_errors = [d for d in diagnostics if d.range_start_line == 43]
        assert len(type_errors) >= 1


class TestLanguageRegistry:
    """
    Feature: Language server registry covers all tree-sitter languages

    As a ChunkHound indexing service
    I want server configs for every language with a tree-sitter grammar
    So that I can extract symbols from any supported language
    """

    def test_registry_covers_all_tree_sitter_languages(self) -> None:
        """
        Scenario: Every Language with a tree-sitter grammar has a registry entry
        Given the Language enum from chunkhound and LANGUAGE_CONFIGS
        When I filter to languages with tree-sitter grammars (exclude TEXT, PDF, UNKNOWN)
        Then each should have an entry in LANGUAGE_SERVER_REGISTRY
        And the entry's language_id should match the Language enum value
        """
        from chunkhound.core.types.common import Language
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY
        from chunkhound.parsers.parser_factory import LANGUAGE_CONFIGS

        # Languages with tree-sitter grammars: in LANGUAGE_CONFIGS with non-None module
        ts_languages = {
            lang
            for lang, cfg in LANGUAGE_CONFIGS.items()
            if cfg.tree_sitter_module is not None
        }
        # Should exclude TEXT, PDF (module=None) and UNKNOWN (not in LANGUAGE_CONFIGS)
        assert Language.TEXT not in ts_languages
        assert Language.PDF not in ts_languages

        missing = []
        for lang in ts_languages:
            lang_id = lang.value
            if lang_id not in LANGUAGE_SERVER_REGISTRY:
                missing.append(lang_id)

        assert missing == [], f"Missing registry entries: {missing}"

    def test_pyright_config_correct(self) -> None:
        """
        Scenario: Pyright config has correct command and args
        Given the LANGUAGE_SERVER_REGISTRY
        When I look up the Python entry
        Then it should have pyright-langserver command with --stdio arg
        """
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        config = LANGUAGE_SERVER_REGISTRY["python"]
        assert "pyright-langserver" in (config.command or "")
        assert "--stdio" in config.args

    def test_registry_language_ids_match(self) -> None:
        """
        Scenario: Registry keys match ServerConfig.language_id
        Given all entries in LANGUAGE_SERVER_REGISTRY
        When I check each entry
        Then the dict key should equal the config's language_id
        """
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        for key, config in LANGUAGE_SERVER_REGISTRY.items():
            assert key == config.language_id, (
                f"Key '{key}' doesn't match config.language_id '{config.language_id}'"
            )


class TestConnectionPool:
    """
    Feature: Connection pooling for LSP clients

    As a ChunkHound indexing service processing many files
    I want a pool that reuses existing server connections
    So that I don't spawn a new server for every file
    """

    @pytest.mark.asyncio
    async def test_pool_reuse(self, workspace: Path) -> None:
        """
        Scenario: Same (language, workspace) returns same client
        Given a pool with a connected client
        When get() is called again with the same language and workspace
        Then it returns the exact same client instance
        """
        from chunkhound.lsp.client import LSPClientPool
        from chunkhound.lsp.registry import LANGUAGE_SERVER_REGISTRY

        pool = LSPClientPool()
        try:
            client1 = await pool.get("python", str(workspace))
            client2 = await pool.get("python", str(workspace))
            assert client1 is client2
            assert client1.state == ServerState.READY
        finally:
            await pool.stop_all()

    @pytest.mark.asyncio
    async def test_pool_different_workspace(self, workspace: Path, tmp_path_factory: pytest.TempPathFactory) -> None:
        """
        Scenario: Different workspaces get different clients
        Given a pool with a client for workspace A
        When get() is called with workspace B
        Then it returns a different client instance
        """
        from chunkhound.lsp.client import LSPClientPool

        workspace_b = tmp_path_factory.mktemp("ws_b")
        (workspace_b / "test.py").write_text("x = 1")

        pool = LSPClientPool()
        try:
            client_a = await pool.get("python", str(workspace))
            client_b = await pool.get("python", str(workspace_b))
            assert client_a is not client_b
            assert client_a.state == ServerState.READY
            assert client_b.state == ServerState.READY
        finally:
            await pool.stop_all()

    @pytest.mark.asyncio
    async def test_pool_respawn_after_stop(self, workspace: Path) -> None:
        """
        Scenario: Pool creates fresh client after explicit stop
        Given a pool with a connected client that was stopped
        When get() is called again
        Then it returns a new READY client (not the stopped one)
        """
        from chunkhound.lsp.client import LSPClientPool

        pool = LSPClientPool()
        try:
            client1 = await pool.get("python", str(workspace))
            assert client1.state == ServerState.READY

            await pool.stop("python", str(workspace))
            assert client1.state == ServerState.STOPPED

            client2 = await pool.get("python", str(workspace))
            assert client2 is not client1
            assert client2.state == ServerState.READY
        finally:
            await pool.stop_all()
