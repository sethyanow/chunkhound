"""Tests for LSP MCP tools — lsp, lsp_status, and infrastructure wiring."""

import asyncio
from dataclasses import asdict
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

from chunkhound.lsp.types import (
    CallHierarchyItem,
    Diagnostic,
    HoverResult,
    Location,
    LSPCapabilityError,
    LSPError,
    LSPTransportError,
    SymbolInfo,
)
from chunkhound.mcp_server.tools import (
    TOOL_REGISTRY,
    _generate_json_schema_from_signature,
    execute_tool,
)


# ---------------------------------------------------------------------------
# Step 1: Infrastructure wiring tests
# ---------------------------------------------------------------------------


class TestLspClientPoolWiring:
    """Verify lsp_client_pool is threaded through the tool infrastructure."""

    def test_schema_gen_skips_lsp_client_pool(self) -> None:
        """_generate_json_schema_from_signature must exclude lsp_client_pool
        from the generated JSON schema (same as services, embedding_manager, etc.)."""

        async def dummy_tool(
            lsp_client_pool: Any,
            services: Any,
            query: str,
            count: int = 5,
        ) -> dict:
            ...

        schema = _generate_json_schema_from_signature(dummy_tool)

        # lsp_client_pool and services should be excluded
        assert "lsp_client_pool" not in schema["properties"]
        assert "services" not in schema["properties"]
        # query and count should be present
        assert "query" in schema["properties"]
        assert "count" in schema["properties"]

    @pytest.mark.asyncio
    async def test_execute_tool_passes_lsp_client_pool(self) -> None:
        """execute_tool must pass lsp_client_pool to tool implementations
        that declare it in their signature."""
        received_pool = None

        async def capture_pool_tool(lsp_client_pool: Any, services: Any) -> dict:
            nonlocal received_pool
            received_pool = lsp_client_pool
            return {"ok": True}

        # Temporarily register the test tool
        from chunkhound.mcp_server.tools import Tool

        TOOL_REGISTRY["_test_capture_pool"] = Tool(
            name="_test_capture_pool",
            description="test",
            parameters={},
            implementation=capture_pool_tool,
        )
        try:
            sentinel = object()
            await execute_tool(
                tool_name="_test_capture_pool",
                services=MagicMock(),
                embedding_manager=None,
                arguments={},
                lsp_client_pool=sentinel,
            )
            assert received_pool is sentinel
        finally:
            TOOL_REGISTRY.pop("_test_capture_pool", None)

    @pytest.mark.asyncio
    async def test_execute_tool_omits_pool_when_not_declared(self) -> None:
        """Tools that don't declare lsp_client_pool should not receive it."""
        called = False

        async def no_pool_tool(services: Any) -> dict:
            nonlocal called
            called = True
            return {"ok": True}

        from chunkhound.mcp_server.tools import Tool

        TOOL_REGISTRY["_test_no_pool"] = Tool(
            name="_test_no_pool",
            description="test",
            parameters={},
            implementation=no_pool_tool,
        )
        try:
            await execute_tool(
                tool_name="_test_no_pool",
                services=MagicMock(),
                embedding_manager=None,
                arguments={},
                lsp_client_pool=object(),
            )
            assert called
        finally:
            TOOL_REGISTRY.pop("_test_no_pool", None)


# ---------------------------------------------------------------------------
# Step 3: lsp tool tests
# ---------------------------------------------------------------------------


def _make_mock_pool(client: AsyncMock | None = None) -> MagicMock:
    """Create a mock LSPClientPool with a controlled client."""
    pool = MagicMock()
    if client is None:
        client = AsyncMock()
    pool.get = AsyncMock(return_value=client)
    pool._clients = {}
    return pool


def _make_mock_config(target_dir: str = "/workspace") -> MagicMock:
    """Create a mock Config with target_dir."""
    config = MagicMock()
    config.target_dir = Path(target_dir)
    return config


class TestLspTool:
    """Tests for the `lsp` MCP tool."""

    @pytest.mark.asyncio
    async def test_lsp_definition(self) -> None:
        """lsp(operation='definition') returns clean location dicts."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(
            return_value=[
                Location(
                    uri="file:///workspace/foo.py",
                    range_start_line=10,
                    range_start_char=4,
                    range_end_line=10,
                    range_end_char=20,
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 10,
                "character": 4,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert isinstance(result, dict)
        assert "results" in result
        assert len(result["results"]) == 1
        loc = result["results"][0]
        assert loc["file_path"] == "/workspace/foo.py"
        assert loc["line"] == 10
        assert loc["character"] == 4

    @pytest.mark.asyncio
    async def test_lsp_references(self) -> None:
        """lsp(operation='references') returns location list."""
        client = AsyncMock()
        client.find_references = AsyncMock(
            return_value=[
                Location("file:///workspace/a.py", 5, 0, 5, 10),
                Location("file:///workspace/b.py", 20, 2, 20, 12),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/a.py",
                "line": 5,
                "character": 0,
                "operation": "references",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 2

    @pytest.mark.asyncio
    async def test_lsp_implementations(self) -> None:
        """lsp(operation='implementations') returns location list."""
        client = AsyncMock()
        client.go_to_implementation = AsyncMock(
            return_value=[
                Location("file:///workspace/impl.py", 30, 0, 45, 0),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/impl.py",
                "line": 1,
                "character": 0,
                "operation": "implementations",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_lsp_callers(self) -> None:
        """lsp(operation='callers') returns call hierarchy items."""
        client = AsyncMock()
        client.incoming_calls = AsyncMock(
            return_value=[
                CallHierarchyItem(
                    name="main",
                    kind=12,
                    uri="file:///workspace/main.py",
                    range_start_line=1,
                    range_start_char=0,
                    range_end_line=10,
                    range_end_char=0,
                    selection_range_start_line=1,
                    selection_range_start_char=4,
                    selection_range_end_line=1,
                    selection_range_end_char=8,
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 4,
                "operation": "callers",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1
        caller = result["results"][0]
        assert caller["name"] == "main"
        assert caller["kind"] == 12

    @pytest.mark.asyncio
    async def test_lsp_callees(self) -> None:
        """lsp(operation='callees') returns call hierarchy items."""
        client = AsyncMock()
        client.outgoing_calls = AsyncMock(return_value=[])
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 4,
                "operation": "callees",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_lsp_hover(self) -> None:
        """lsp(operation='hover') returns hover content."""
        client = AsyncMock()
        client.hover = AsyncMock(
            return_value=HoverResult(
                contents="```python\ndef foo() -> int\n```",
                range_start_line=5,
                range_start_char=0,
                range_end_line=5,
                range_end_char=3,
            )
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 5,
                "character": 0,
                "operation": "hover",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert "contents" in result
        assert "def foo" in result["contents"]

    @pytest.mark.asyncio
    async def test_lsp_diagnostics(self) -> None:
        """lsp(operation='diagnostics') returns diagnostic list."""
        client = AsyncMock()
        client.get_diagnostics = AsyncMock(
            return_value=[
                Diagnostic(
                    range_start_line=10,
                    range_start_char=0,
                    range_end_line=10,
                    range_end_char=5,
                    severity=1,
                    message="Undefined variable 'x'",
                    source="pyright",
                    code="reportUndefinedVariable",
                ),
            ]
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 10,
                "character": 0,
                "operation": "diagnostics",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert len(result["results"]) == 1
        diag = result["results"][0]
        assert diag["message"] == "Undefined variable 'x'"
        assert diag["severity"] == 1
        assert diag["source"] == "pyright"

    @pytest.mark.asyncio
    async def test_lsp_capability_error(self) -> None:
        """Unsupported capability returns structured error, not crash."""
        client = AsyncMock()
        client.go_to_definition = AsyncMock(
            side_effect=LSPCapabilityError("textDocument/definition", "pyright")
        )
        pool = _make_mock_pool(client)
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "capability_not_supported"
        assert "operation" in result

    @pytest.mark.asyncio
    async def test_lsp_transport_error(self) -> None:
        """Transport failure returns structured error."""
        pool = _make_mock_pool()
        pool.get = AsyncMock(
            side_effect=LSPTransportError("Server binary not found: pyright-langserver")
        )
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "server_not_available"

    @pytest.mark.asyncio
    async def test_lsp_unknown_extension(self) -> None:
        """File with unknown extension returns structured error."""
        pool = _make_mock_pool()
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/data.xyz",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=pool,
            config=config,
        )

        assert result["error"] == "unsupported_language"

    @pytest.mark.asyncio
    async def test_lsp_pool_not_ready(self) -> None:
        """lsp tool with None pool returns structured error."""
        config = _make_mock_config()

        result = await execute_tool(
            tool_name="lsp",
            services=MagicMock(),
            embedding_manager=None,
            arguments={
                "file": "/workspace/foo.py",
                "line": 1,
                "character": 0,
                "operation": "definition",
            },
            lsp_client_pool=None,
            config=config,
        )

        assert result["error"] == "lsp_not_ready"


# ---------------------------------------------------------------------------
# Step 5: lsp_status tool tests
# ---------------------------------------------------------------------------


class TestLspStatusTool:
    """Tests for the `lsp_status` MCP tool."""

    @pytest.mark.asyncio
    async def test_lsp_status_mixed_pool(self) -> None:
        """lsp_status with READY and DEGRADED servers returns per-server info."""
        from chunkhound.lsp.types import LSPCapability, ServerConfig, ServerState

        # Build mock clients
        ready_client = MagicMock()
        ready_client.state = ServerState.READY
        ready_client.capabilities = frozenset(
            {LSPCapability.DEFINITION, LSPCapability.REFERENCES}
        )
        ready_client.server_info = {"name": "pyright", "version": "1.1.0"}
        ready_client.degraded_reason = None
        ready_client._config = ServerConfig(language_id="python", command="pyright-langserver")

        degraded_client = MagicMock()
        degraded_client.state = ServerState.DEGRADED
        degraded_client.capabilities = frozenset()
        degraded_client.server_info = None
        degraded_client.degraded_reason = {
            "code": "spawn_failed",
            "detail": "Command not found: rust-analyzer",
        }
        degraded_client._config = ServerConfig(language_id="rust", command="rust-analyzer")

        pool = MagicMock()
        pool._clients = {
            ("python", "/workspace"): ready_client,
            ("rust", "/workspace"): degraded_client,
        }

        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=pool,
        )

        assert result["total"] == 2
        assert result["ready"] == 1
        assert result["degraded"] == 1
        assert len(result["servers"]) == 2

        # Find the ready server entry
        ready_entry = [s for s in result["servers"] if s["language_id"] == "python"][0]
        assert ready_entry["state"] == "ready"
        assert "definitionProvider" in ready_entry["capabilities"]
        assert ready_entry["server_info"] == {"name": "pyright", "version": "1.1.0"}

        # Find the degraded server entry
        degraded_entry = [s for s in result["servers"] if s["language_id"] == "rust"][0]
        assert degraded_entry["state"] == "degraded"
        assert degraded_entry["degraded_reason"]["code"] == "spawn_failed"

    @pytest.mark.asyncio
    async def test_lsp_status_empty_pool(self) -> None:
        """lsp_status with no active servers returns empty list."""
        pool = MagicMock()
        pool._clients = {}

        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=pool,
        )

        assert result["total"] == 0
        assert result["ready"] == 0
        assert result["degraded"] == 0
        assert result["servers"] == []

    @pytest.mark.asyncio
    async def test_lsp_status_pool_not_ready(self) -> None:
        """lsp_status with None pool returns structured error."""
        result = await execute_tool(
            tool_name="lsp_status",
            services=MagicMock(),
            embedding_manager=None,
            arguments={},
            lsp_client_pool=None,
        )

        assert result["error"] == "lsp_not_ready"


# ---------------------------------------------------------------------------
# Graph tool tests
# ---------------------------------------------------------------------------


def _make_mock_services(
    query_results: list[list[dict[str, Any]]] | None = None,
) -> MagicMock:
    """Create mock services with execute_query returning sequential results.

    Each element in query_results is returned for successive execute_query calls.
    """
    services = MagicMock()
    if query_results:
        services.provider.execute_query.side_effect = query_results
    else:
        services.provider.execute_query.return_value = []
    return services


class TestGraphTool:
    """Tests for the ``graph`` MCP tool — pure DuckDB queries against symbols/symbol_edges."""

    @pytest.mark.asyncio
    async def test_graph_walk(self) -> None:
        """graph(walk) returns connected symbols + edges from a starting FQN."""
        services = _make_mock_services([
            # Call 1: recursive CTE returns reachable nodes
            [
                {"fqn": "mod::A", "name": "A", "kind": "Class", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 1},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 2},
            ],
            # Call 2: edges between discovered nodes
            [
                {"from_fqn": "mod::A", "to_fqn": "mod::A::foo", "edge_kind": "defines", "from_file": "mod.py", "to_file": "mod.py"},
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A", "depth": 2},
        )

        assert isinstance(result, dict)
        assert "results" in result
        assert "edges" in result
        assert "count" in result
        assert len(result["results"]) == 3
        assert result["results"][0]["fqn"] == "mod::A"
        assert result["results"][0]["kind"] == "Class"
        assert len(result["edges"]) == 2
        edge = result["edges"][0]
        assert "from_symbol" in edge
        assert "to_symbol" in edge
        assert "edge_kind" in edge
        assert "from_symbol_id" not in edge  # no raw DB column names
        assert result["count"] == 3

    @pytest.mark.asyncio
    async def test_graph_walk_edge_filter(self) -> None:
        """graph(walk, edge_kind='calls') returns only call edges."""
        services = _make_mock_services([
            [
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A::foo", "depth": 2, "edge_kind": "calls"},
        )

        assert len(result["results"]) == 2
        assert all(e["edge_kind"] == "calls" for e in result["edges"])

    @pytest.mark.asyncio
    async def test_graph_walk_empty(self) -> None:
        """graph(walk) from nonexistent FQN returns empty results, not error."""
        services = _make_mock_services([[], []])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "nonexistent::Symbol"},
        )

        assert result["results"] == []
        assert result["edges"] == []
        assert result["count"] == 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_graph_walk_missing_symbol(self) -> None:
        """graph(walk) without symbol returns structured error."""
        services = _make_mock_services()

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk"},
        )

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_walk_empty_string_symbol(self) -> None:
        """graph(walk) with empty string symbol returns same error as None."""
        services = _make_mock_services()

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": ""},
        )

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_graph_walk_cycle(self) -> None:
        """graph(walk) on cyclic graph terminates without hanging."""
        import asyncio

        services = _make_mock_services([
            [
                {"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::B", "name": "B", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::A", "to_fqn": "mod::B", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"},
                {"from_fqn": "mod::B", "to_fqn": "mod::A", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"},
            ],
        ])

        result = await asyncio.wait_for(
            execute_tool(
                tool_name="graph",
                services=services,
                embedding_manager=None,
                arguments={"operation": "walk", "symbol": "mod::A", "depth": 5},
            ),
            timeout=5.0,
        )

        assert isinstance(result, dict)
        assert "results" in result
        assert len(result["results"]) <= 10

    @pytest.mark.asyncio
    async def test_graph_invalid_operation(self) -> None:
        """graph with invalid operation returns structured error."""
        services = _make_mock_services()

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "bogus"},
        )

        assert result["error"] == "invalid_operation"
        services.provider.execute_query.assert_not_called()

    # --- reachability tests ---

    @pytest.mark.asyncio
    async def test_graph_reachability(self) -> None:
        """graph(reachability) returns symbols unreachable from scope entry points."""
        services = _make_mock_services([
            # Call 1: all symbols in scope
            [
                {"fqn": "pkg::main", "name": "main", "kind": "Function", "file_path": "pkg/main.py"},
                {"fqn": "pkg::helper", "name": "helper", "kind": "Function", "file_path": "pkg/util.py"},
                {"fqn": "pkg::orphan", "name": "orphan", "kind": "Function", "file_path": "pkg/dead.py"},
            ],
            # Call 2: reachable FQNs (from edge traversal)
            [
                {"fqn": "pkg::main"},
                {"fqn": "pkg::helper"},
            ],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "reachability", "scope": "pkg/"},
        )

        assert isinstance(result, dict)
        assert "unreachable" in result
        assert "count" in result
        # orphan is not reachable
        fqns = [s["fqn"] for s in result["unreachable"]]
        assert "pkg::orphan" in fqns
        assert "pkg::main" not in fqns
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_graph_reachability_missing_scope(self) -> None:
        """graph(reachability) without scope returns structured error."""
        services = _make_mock_services()

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "reachability"},
        )

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()

    # --- boundary tests ---

    @pytest.mark.asyncio
    async def test_graph_boundary(self) -> None:
        """graph(boundary) returns edges crossing scope boundary."""
        services = _make_mock_services([
            # Edges where from is inside scope, to is outside (or vice versa)
            [
                {
                    "from_fqn": "pkg::client", "from_name": "client", "from_kind": "Function",
                    "from_file": "pkg/client.py",
                    "to_fqn": "external::api", "to_name": "api", "to_kind": "Function",
                    "to_file": "external/api.py",
                    "edge_kind": "calls",
                },
            ],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "boundary", "scope": "pkg/"},
        )

        assert isinstance(result, dict)
        assert "edges" in result
        assert "count" in result
        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert "from_symbol" in edge
        assert "to_symbol" in edge
        assert edge["edge_kind"] == "calls"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_graph_boundary_missing_scope(self) -> None:
        """graph(boundary) without scope returns structured error."""
        services = _make_mock_services()

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "boundary"},
        )

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()

    # --- overview tests ---

    @pytest.mark.asyncio
    async def test_graph_overview(self) -> None:
        """graph(overview) returns most-connected symbols with edge_kind breakdown."""
        services = _make_mock_services([
            # Call 1: top symbols by total edge count
            [
                {"fqn": "mod::Hub", "name": "Hub", "kind": "Class", "file_path": "mod.py", "total_edges": 15},
                {"fqn": "mod::Helper", "name": "Helper", "kind": "Function", "file_path": "mod.py", "total_edges": 8},
            ],
            # Call 2: per-symbol edge_kind breakdown
            [
                {"fqn": "mod::Hub", "edge_kind": "calls", "edge_count": 10},
                {"fqn": "mod::Hub", "edge_kind": "defines", "edge_count": 5},
                {"fqn": "mod::Helper", "edge_kind": "called_by", "edge_count": 6},
                {"fqn": "mod::Helper", "edge_kind": "references", "edge_count": 2},
            ],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "overview", "limit": 10},
        )

        assert isinstance(result, dict)
        assert "symbols" in result
        assert "count" in result
        assert len(result["symbols"]) == 2

        hub = result["symbols"][0]
        assert hub["fqn"] == "mod::Hub"
        assert hub["total_edges"] == 15
        assert "breakdown" in hub
        assert hub["breakdown"]["calls"] == 10
        assert hub["breakdown"]["defines"] == 5

        helper = result["symbols"][1]
        assert helper["breakdown"]["called_by"] == 6
        assert result["count"] == 2


# ---------------------------------------------------------------------------
# Adversarial stress tests for graph tool
# ---------------------------------------------------------------------------


class TestGraphToolAdversarial:
    """Adversarial battery: structural patterns to expose assumptions."""

    # --- _escape_like: pure function, encoding boundaries ---

    def test_escape_like_percent(self) -> None:
        """LIKE wildcard % in scope must be escaped."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("chunk%ound") == "chunk\\%ound"

    def test_escape_like_underscore(self) -> None:
        """LIKE wildcard _ in scope must be escaped."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("chunk_ound") == "chunk\\_ound"

    def test_escape_like_backslash(self) -> None:
        """Backslash itself must be escaped first to avoid double-escaping."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("path\\to") == "path\\\\to"

    def test_escape_like_all_special(self) -> None:
        """All three LIKE-special chars in one string."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"

    def test_escape_like_empty(self) -> None:
        """Empty string stays empty."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("") == ""

    def test_escape_like_unicode(self) -> None:
        """Unicode chars pass through unchanged."""
        from chunkhound.mcp_server.tools import _escape_like
        assert _escape_like("パス/ファイル") == "パス/ファイル"

    # --- walk: self-referential (self-loop) ---

    @pytest.mark.asyncio
    async def test_walk_self_loop(self) -> None:
        """Walk from a symbol that has an edge to itself terminates."""
        import asyncio

        services = _make_mock_services([
            [{"fqn": "mod::Self", "name": "Self", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [{"from_fqn": "mod::Self", "to_fqn": "mod::Self", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"}],
        ])

        result = await asyncio.wait_for(
            execute_tool(
                tool_name="graph",
                services=services,
                embedding_manager=None,
                arguments={"operation": "walk", "symbol": "mod::Self", "depth": 10},
            ),
            timeout=5.0,
        )

        assert len(result["results"]) == 1  # only Self, no infinite expansion
        assert result["results"][0]["fqn"] == "mod::Self"

    # --- walk: dense graph (fully connected) ---

    @pytest.mark.asyncio
    async def test_walk_dense_graph_respects_limit(self) -> None:
        """Walk on a dense graph returns at most `limit` nodes."""
        # 50 nodes all interconnected — limit should cap results
        nodes = [
            {"fqn": f"mod::N{i}", "name": f"N{i}", "kind": "Function", "file_path": "mod.py", "depth": 1}
            for i in range(50)
        ]
        nodes.insert(0, {"fqn": "mod::Root", "name": "Root", "kind": "Function", "file_path": "mod.py", "depth": 0})
        edges = [
            {"from_fqn": "mod::Root", "to_fqn": f"mod::N{i}", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"}
            for i in range(50)
        ]
        services = _make_mock_services([nodes[:5], edges[:5]])  # CTE returns capped results

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::Root", "depth": 2, "limit": 5},
        )

        assert len(result["results"]) <= 5

    # --- walk: type boundaries (depth/limit edge values) ---

    @pytest.mark.asyncio
    async def test_walk_depth_zero_clamped(self) -> None:
        """depth=0 should be clamped to 1 (minimum)."""
        services = _make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A", "depth": 0},
        )

        # Should not error — depth clamped to 1
        assert "error" not in result
        assert "results" in result

    @pytest.mark.asyncio
    async def test_walk_depth_huge_clamped(self) -> None:
        """depth=9999 should be clamped to 20 (maximum)."""
        services = _make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A", "depth": 9999},
        )

        assert "error" not in result
        # Verify the clamped depth was passed to the query
        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]  # second positional arg is params list
        # params[1] is depth — should be clamped to 20
        assert params[1] == 20

    @pytest.mark.asyncio
    async def test_walk_negative_limit_clamped(self) -> None:
        """limit=-5 should be clamped to 1."""
        services = _make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A", "limit": -5},
        )

        assert "error" not in result

    # --- reachability: empty (no symbols in scope) ---

    @pytest.mark.asyncio
    async def test_reachability_empty_scope(self) -> None:
        """Reachability on a scope with no symbols returns empty."""
        services = _make_mock_services([[], []])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "reachability", "scope": "nonexistent/"},
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    # --- reachability: all reachable (nothing unreachable) ---

    @pytest.mark.asyncio
    async def test_reachability_all_connected(self) -> None:
        """When all symbols are reachable, unreachable list is empty."""
        services = _make_mock_services([
            [
                {"fqn": "pkg::a", "name": "a", "kind": "Function", "file_path": "pkg/a.py"},
                {"fqn": "pkg::b", "name": "b", "kind": "Function", "file_path": "pkg/b.py"},
            ],
            [{"fqn": "pkg::a"}, {"fqn": "pkg::b"}],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "reachability", "scope": "pkg/"},
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    # --- boundary: scope with LIKE-special chars ---

    @pytest.mark.asyncio
    async def test_boundary_scope_with_underscore(self) -> None:
        """Scope containing _ must be escaped so 'a_b/' doesn't match 'aXb/'."""
        services = _make_mock_services([[]])  # boundary returns whatever DB gives

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "boundary", "scope": "chunk_ound/"},
        )

        # Verify the escaped pattern was passed to execute_query
        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]
        # First param should be the escaped scope pattern
        assert "chunk\\_ound/" in params[0]

    # --- overview: singular (one symbol, one edge) ---

    @pytest.mark.asyncio
    async def test_overview_single_symbol(self) -> None:
        """Overview with a single connected symbol returns it."""
        services = _make_mock_services([
            [{"fqn": "mod::Only", "name": "Only", "kind": "Function", "file_path": "mod.py", "total_edges": 1}],
            [{"fqn": "mod::Only", "edge_kind": "calls", "edge_count": 1}],
        ])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "overview"},
        )

        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["breakdown"] == {"calls": 1}

    # --- overview: empty graph ---

    @pytest.mark.asyncio
    async def test_overview_empty_graph(self) -> None:
        """Overview on empty graph returns no symbols."""
        services = _make_mock_services([[]])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "overview"},
        )

        assert result["symbols"] == []
        assert result["count"] == 0

    # --- the "second run": idempotency ---

    @pytest.mark.asyncio
    async def test_walk_idempotent(self) -> None:
        """Calling walk twice with same input returns same result (read-only)."""
        mock_data = [
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ]
        services = _make_mock_services(mock_data + mock_data)  # 4 calls total

        r1 = await execute_tool(
            tool_name="graph", services=services, embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A"},
        )
        r2 = await execute_tool(
            tool_name="graph", services=services, embedding_manager=None,
            arguments={"operation": "walk", "symbol": "mod::A"},
        )

        assert r1 == r2

    # --- semantically hostile: FQN with SQL-like content ---

    @pytest.mark.asyncio
    async def test_walk_sql_injection_in_symbol(self) -> None:
        """FQN containing SQL keywords is safe (parameterized)."""
        services = _make_mock_services([[], []])

        result = await execute_tool(
            tool_name="graph",
            services=services,
            embedding_manager=None,
            arguments={"operation": "walk", "symbol": "'; DROP TABLE symbols; --"},
        )

        # Should return empty results, not error — SQL injection is parameterized
        assert result["results"] == []
        assert "error" not in result
        # Verify the hostile string was passed as a param, not interpolated
        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]
        assert "'; DROP TABLE symbols; --" in params
