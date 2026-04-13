"""Tests for the `graph` MCP tool — provider-agnostic symbol dependency queries.

Organized by operation (walk, reachability, boundary, overview) then by
adversarial stress tests that probe boundary conditions, cycle handling,
parameter clamping, and SQL injection safety.

Post-ch-nxu Step 14: the tool calls DatabaseProvider graph methods directly.
Tests mock provider return values, not execute_query sequences.
"""

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.unit

# escape_like import removed by ch-nxu Step 15 — helper absorbed into providers.
from tests.lsp.mcp_tool_helpers import call_graph_tool, make_mock_services


def _set_walk(services: Any, nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    services.provider.graph_walk.return_value = (nodes, edges)


def _set_reachability(services: Any, unreachable: list[dict[str, Any]]) -> None:
    services.provider.graph_reachability.return_value = unreachable


def _set_boundary(services: Any, edges: list[dict[str, Any]]) -> None:
    services.provider.graph_boundary.return_value = edges


def _set_overview(
    services: Any,
    top_symbols: list[dict[str, Any]],
    breakdown: dict[str, dict[str, int]] | None = None,
) -> None:
    services.provider.graph_overview.return_value = top_symbols
    services.provider.graph_overview_breakdown.return_value = breakdown or {}


# ---------------------------------------------------------------------------
# Walk operation
# ---------------------------------------------------------------------------


class TestGraphWalk:
    """Walk traverses connected symbols from a starting FQN via provider.graph_walk."""

    @pytest.mark.asyncio
    async def test_walk_returns_nodes_and_edges(self) -> None:
        """Walk produces {results: [node], edges: [edge], count: N} with clean field names."""
        services = make_mock_services()
        _set_walk(
            services,
            nodes=[
                {"fqn": "mod::A", "name": "A", "kind": "Class", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 1},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 2},
            ],
            edges=[
                {"from_fqn": "mod::A", "to_fqn": "mod::A::foo", "edge_kind": "defines", "from_file": "mod.py", "to_file": "mod.py"},
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        )

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", depth=2,
        )

        assert len(result["results"]) == 3
        assert result["count"] == 3

        # Node field contract
        node = result["results"][0]
        assert node["fqn"] == "mod::A"
        assert node["kind"] == "Class"

        # Edge field contract — no raw DB column names
        assert len(result["edges"]) == 2
        edge = result["edges"][0]
        assert "from_symbol" in edge
        assert "to_symbol" in edge
        assert "edge_kind" in edge
        assert "from_symbol_id" not in edge

        # Tool passed the symbol through unchanged
        services.provider.graph_walk.assert_called_once()
        kwargs = services.provider.graph_walk.call_args.kwargs
        assert kwargs["seed_fqns"] == ["mod::A"]
        assert kwargs["depth"] == 2

    @pytest.mark.asyncio
    async def test_walk_edge_kind_filter(self) -> None:
        """edge_kind parameter forwards to provider.graph_walk."""
        services = make_mock_services()
        _set_walk(
            services,
            nodes=[
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 1},
            ],
            edges=[
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        )

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A::foo", depth=2, edge_kind="calls",
        )

        assert len(result["results"]) == 2
        assert all(e["edge_kind"] == "calls" for e in result["edges"])
        assert services.provider.graph_walk.call_args.kwargs["edge_kind"] == "calls"

    @pytest.mark.asyncio
    async def test_walk_nonexistent_fqn_returns_empty(self) -> None:
        """Nonexistent FQN → empty results, not error."""
        services = make_mock_services()
        _set_walk(services, nodes=[], edges=[])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="nonexistent::Symbol",
        )

        assert result["results"] == []
        assert result["edges"] == []
        assert result["count"] == 0
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_walk_missing_symbol_returns_error(self) -> None:
        """Walk without symbol parameter → {error: 'missing_parameter'}."""
        services = make_mock_services()

        result = await call_graph_tool(services=services, operation="walk")

        assert result["error"] == "missing_parameter"
        services.provider.graph_walk.assert_not_called()

    @pytest.mark.asyncio
    async def test_walk_empty_string_symbol_returns_error(self) -> None:
        """Empty string symbol treated same as missing."""
        services = make_mock_services()

        result = await call_graph_tool(
            services=services, operation="walk", symbol="",
        )

        assert result["error"] == "missing_parameter"
        services.provider.graph_walk.assert_not_called()

    @pytest.mark.asyncio
    async def test_walk_cycle_terminates(self) -> None:
        """Cyclic graph (A→B→A) terminates without hanging."""
        services = make_mock_services()
        _set_walk(
            services,
            nodes=[
                {"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::B", "name": "B", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            edges=[
                {"from_fqn": "mod::A", "to_fqn": "mod::B", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"},
                {"from_fqn": "mod::B", "to_fqn": "mod::A", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"},
            ],
        )

        result = await asyncio.wait_for(
            call_graph_tool(
                services=services, operation="walk", symbol="mod::A", depth=5,
            ),
            timeout=5.0,
        )

        assert isinstance(result, dict)
        assert "results" in result
        assert len(result["results"]) <= 10


class TestGraphReachability:
    """Reachability finds symbols unreachable from scope entry points."""

    @pytest.mark.asyncio
    async def test_reachability_identifies_orphans(self) -> None:
        """Provider returns unreachable symbols directly; tool forwards them."""
        services = make_mock_services()
        _set_reachability(
            services,
            unreachable=[
                {"fqn": "pkg::orphan", "name": "orphan", "kind": "Function", "file_path": "pkg/dead.py"},
            ],
        )

        result = await call_graph_tool(
            services=services, operation="reachability", scope="pkg/",
        )

        fqns = [s["fqn"] for s in result["unreachable"]]
        assert "pkg::orphan" in fqns
        assert result["count"] == 1
        services.provider.graph_reachability.assert_called_once_with("pkg/")

    @pytest.mark.asyncio
    async def test_reachability_missing_scope_returns_error(self) -> None:
        services = make_mock_services()

        result = await call_graph_tool(services=services, operation="reachability")

        assert result["error"] == "missing_parameter"
        services.provider.graph_reachability.assert_not_called()


class TestGraphBoundary:
    """Boundary finds edges crossing a scope boundary."""

    @pytest.mark.asyncio
    async def test_boundary_returns_cross_scope_edges(self) -> None:
        """Edge from inside scope to outside appears in result."""
        services = make_mock_services()
        _set_boundary(
            services,
            edges=[
                {
                    "from_fqn": "pkg::client", "from_name": "client", "from_kind": "Function",
                    "from_file": "pkg/client.py",
                    "to_fqn": "external::api", "to_name": "api", "to_kind": "Function",
                    "to_file": "external/api.py",
                    "edge_kind": "calls",
                },
            ],
        )

        result = await call_graph_tool(
            services=services, operation="boundary", scope="pkg/",
        )

        assert len(result["edges"]) == 1
        edge = result["edges"][0]
        assert "from_symbol" in edge
        assert "to_symbol" in edge
        assert edge["edge_kind"] == "calls"
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_boundary_missing_scope_returns_error(self) -> None:
        services = make_mock_services()

        result = await call_graph_tool(services=services, operation="boundary")

        assert result["error"] == "missing_parameter"
        services.provider.graph_boundary.assert_not_called()


class TestGraphOverview:
    """Overview returns most-connected symbols with per-edge_kind breakdown."""

    @pytest.mark.asyncio
    async def test_overview_returns_breakdown(self) -> None:
        """Each symbol gets a {edge_kind: count} breakdown dict."""
        services = make_mock_services()
        _set_overview(
            services,
            top_symbols=[
                {"fqn": "mod::Hub", "name": "Hub", "kind": "Class", "file_path": "mod.py", "total_edges": 15},
                {"fqn": "mod::Helper", "name": "Helper", "kind": "Function", "file_path": "mod.py", "total_edges": 8},
            ],
            breakdown={
                "mod::Hub": {"calls": 10, "defines": 5},
                "mod::Helper": {"called_by": 6, "references": 2},
            },
        )

        result = await call_graph_tool(
            services=services, operation="overview", limit=10,
        )

        assert len(result["symbols"]) == 2
        assert result["count"] == 2

        hub = result["symbols"][0]
        assert hub["fqn"] == "mod::Hub"
        assert hub["total_edges"] == 15
        assert hub["breakdown"] == {"calls": 10, "defines": 5}

        helper = result["symbols"][1]
        assert helper["breakdown"]["called_by"] == 6


class TestGraphValidation:
    """Parameter validation and clamping."""

    @pytest.mark.asyncio
    async def test_invalid_operation_returns_error(self) -> None:
        services = make_mock_services()

        result = await call_graph_tool(services=services, operation="bogus")

        assert result["error"] == "invalid_operation"
        services.provider.graph_walk.assert_not_called()
        services.provider.graph_reachability.assert_not_called()
        services.provider.graph_boundary.assert_not_called()
        services.provider.graph_overview.assert_not_called()


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


# TestEscapeLike deleted by ch-nxu Step 15 — the escape_like helper was
# absorbed into DuckDBProvider internals. LIKE-escape behavior is tested
# via provider integration tests (test_{duckdb,lancedb}_symbol_protocol.py).


class TestGraphAdversarial:
    """Boundary conditions: self-loops, dense graphs, parameter clamping, SQL injection."""

    @pytest.mark.asyncio
    async def test_walk_self_loop_terminates(self) -> None:
        """Symbol with edge to itself → single result, no infinite expansion."""
        services = make_mock_services()
        _set_walk(
            services,
            nodes=[{"fqn": "mod::Self", "name": "Self", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            edges=[{"from_fqn": "mod::Self", "to_fqn": "mod::Self", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"}],
        )

        result = await asyncio.wait_for(
            call_graph_tool(services=services, operation="walk", symbol="mod::Self", depth=10),
            timeout=5.0,
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["fqn"] == "mod::Self"

    @pytest.mark.asyncio
    async def test_walk_dense_graph_respects_limit(self) -> None:
        """limit parameter forwards to provider.graph_walk."""
        services = make_mock_services()
        # Provider honours limit internally; tool passes it through
        _set_walk(
            services,
            nodes=[{"fqn": f"mod::N{i}", "name": f"N{i}", "kind": "Function", "file_path": "mod.py", "depth": 1} for i in range(5)],
            edges=[{"from_fqn": "mod::Root", "to_fqn": f"mod::N{i}", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"} for i in range(5)],
        )

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::Root", depth=2, limit=5,
        )

        assert len(result["results"]) <= 5
        assert services.provider.graph_walk.call_args.kwargs["limit"] == 5

    @pytest.mark.asyncio
    async def test_walk_depth_zero_clamped_to_minimum(self) -> None:
        """depth=0 silently clamped to 1 — verified via call kwargs."""
        services = make_mock_services()
        _set_walk(services, nodes=[{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}], edges=[])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", depth=0,
        )

        assert "error" not in result
        assert services.provider.graph_walk.call_args.kwargs["depth"] == 1

    @pytest.mark.asyncio
    async def test_walk_depth_huge_clamped_to_maximum(self) -> None:
        """depth=9999 silently clamped to 20."""
        services = make_mock_services()
        _set_walk(services, nodes=[{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}], edges=[])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", depth=9999,
        )

        assert "error" not in result
        assert services.provider.graph_walk.call_args.kwargs["depth"] == 20

    @pytest.mark.asyncio
    async def test_walk_negative_limit_clamped(self) -> None:
        """limit=-5 clamped to 1."""
        services = make_mock_services()
        _set_walk(services, nodes=[{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}], edges=[])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", limit=-5,
        )

        assert "error" not in result
        assert services.provider.graph_walk.call_args.kwargs["limit"] == 1

    @pytest.mark.asyncio
    async def test_reachability_empty_scope(self) -> None:
        """Scope with no unreachable symbols → empty list."""
        services = make_mock_services()
        _set_reachability(services, unreachable=[])

        result = await call_graph_tool(
            services=services, operation="reachability", scope="nonexistent/",
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_reachability_all_connected(self) -> None:
        """Provider returns [] when everything is reachable."""
        services = make_mock_services()
        _set_reachability(services, unreachable=[])

        result = await call_graph_tool(
            services=services, operation="reachability", scope="pkg/",
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_boundary_passes_scope_verbatim(self) -> None:
        """Tool forwards scope to provider unchanged — LIKE-escaping is a provider concern."""
        services = make_mock_services()
        _set_boundary(services, edges=[])

        await call_graph_tool(
            services=services, operation="boundary", scope="chunk_ound/",
        )

        services.provider.graph_boundary.assert_called_once_with("chunk_ound/", 20)

    @pytest.mark.asyncio
    async def test_overview_single_symbol(self) -> None:
        services = make_mock_services()
        _set_overview(
            services,
            top_symbols=[{"fqn": "mod::Only", "name": "Only", "kind": "Function", "file_path": "mod.py", "total_edges": 1}],
            breakdown={"mod::Only": {"calls": 1}},
        )

        result = await call_graph_tool(services=services, operation="overview")

        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["breakdown"] == {"calls": 1}

    @pytest.mark.asyncio
    async def test_overview_empty_graph(self) -> None:
        services = make_mock_services()
        _set_overview(services, top_symbols=[], breakdown={})

        result = await call_graph_tool(services=services, operation="overview")

        assert result["symbols"] == []
        assert result["count"] == 0
        # Short-circuit: breakdown lookup skipped when top_symbols empty
        services.provider.graph_overview_breakdown.assert_not_called()

    @pytest.mark.asyncio
    async def test_walk_idempotent(self) -> None:
        """Two identical walk calls return identical results (read-only queries)."""
        services = make_mock_services()
        _set_walk(
            services,
            nodes=[{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            edges=[],
        )

        r1 = await call_graph_tool(services=services, operation="walk", symbol="mod::A")
        r2 = await call_graph_tool(services=services, operation="walk", symbol="mod::A")

        assert r1 == r2

    @pytest.mark.asyncio
    async def test_walk_symbol_passed_verbatim(self) -> None:
        """FQN containing SQL keywords forwards to provider unchanged.

        Injection safety is now the provider's responsibility (parameterized
        queries in DuckDB, native filters in LanceDB). The tool just passes
        the raw FQN through.
        """
        services = make_mock_services()
        _set_walk(services, nodes=[], edges=[])

        payload = "'; DROP TABLE symbols; --"
        result = await call_graph_tool(
            services=services, operation="walk", symbol=payload,
        )

        assert result["results"] == []
        assert "error" not in result
        assert services.provider.graph_walk.call_args.kwargs["seed_fqns"] == [payload]
