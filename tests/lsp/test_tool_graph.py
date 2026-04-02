"""Tests for the `graph` MCP tool — DuckDB-backed symbol dependency queries.

Organized by operation (walk, reachability, boundary, overview) then by
adversarial stress tests that probe boundary conditions, cycle handling,
parameter clamping, and SQL injection safety.
"""

import asyncio
from typing import Any

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools import _escape_like
from tests.lsp.mcp_tool_helpers import call_graph_tool, make_mock_services


# ---------------------------------------------------------------------------
# Walk operation
# ---------------------------------------------------------------------------


class TestGraphWalk:
    """Walk traverses connected symbols from a starting FQN via recursive CTE."""

    @pytest.mark.asyncio
    async def test_walk_returns_nodes_and_edges(self) -> None:
        """Walk produces {results: [node], edges: [edge], count: N} with clean field names."""
        services = make_mock_services([
            [
                {"fqn": "mod::A", "name": "A", "kind": "Class", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 1},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 2},
            ],
            [
                {"from_fqn": "mod::A", "to_fqn": "mod::A::foo", "edge_kind": "defines", "from_file": "mod.py", "to_file": "mod.py"},
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        ])

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

    @pytest.mark.asyncio
    async def test_walk_edge_kind_filter(self) -> None:
        """edge_kind parameter filters traversal to only matching edges."""
        services = make_mock_services([
            [
                {"fqn": "mod::A::foo", "name": "foo", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "util::helper", "name": "helper", "kind": "Function", "file_path": "util.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::A::foo", "to_fqn": "util::helper", "edge_kind": "calls", "from_file": "mod.py", "to_file": "util.py"},
            ],
        ])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A::foo", depth=2, edge_kind="calls",
        )

        assert len(result["results"]) == 2
        assert all(e["edge_kind"] == "calls" for e in result["edges"])

    @pytest.mark.asyncio
    async def test_walk_nonexistent_fqn_returns_empty(self) -> None:
        """Nonexistent FQN → empty results, not error."""
        services = make_mock_services([[], []])

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
        services.provider.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_walk_empty_string_symbol_returns_error(self) -> None:
        """Empty string symbol treated same as missing."""
        services = make_mock_services()

        result = await call_graph_tool(
            services=services, operation="walk", symbol="",
        )

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()

    @pytest.mark.asyncio
    async def test_walk_cycle_terminates(self) -> None:
        """Cyclic graph (A→B→A) terminates without hanging."""
        services = make_mock_services([
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
        """Symbols not in the reachable set appear in 'unreachable'."""
        services = make_mock_services([
            [
                {"fqn": "pkg::main", "name": "main", "kind": "Function", "file_path": "pkg/main.py"},
                {"fqn": "pkg::helper", "name": "helper", "kind": "Function", "file_path": "pkg/util.py"},
                {"fqn": "pkg::orphan", "name": "orphan", "kind": "Function", "file_path": "pkg/dead.py"},
            ],
            [
                {"fqn": "pkg::main"},
                {"fqn": "pkg::helper"},
            ],
        ])

        result = await call_graph_tool(
            services=services, operation="reachability", scope="pkg/",
        )

        fqns = [s["fqn"] for s in result["unreachable"]]
        assert "pkg::orphan" in fqns
        assert "pkg::main" not in fqns
        assert result["count"] == 1

    @pytest.mark.asyncio
    async def test_reachability_missing_scope_returns_error(self) -> None:
        services = make_mock_services()

        result = await call_graph_tool(services=services, operation="reachability")

        assert result["error"] == "missing_parameter"
        services.provider.execute_query.assert_not_called()


class TestGraphBoundary:
    """Boundary finds edges crossing a scope boundary."""

    @pytest.mark.asyncio
    async def test_boundary_returns_cross_scope_edges(self) -> None:
        """Edge from inside scope to outside appears in result."""
        services = make_mock_services([
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
        services.provider.execute_query.assert_not_called()


class TestGraphOverview:
    """Overview returns most-connected symbols with per-edge_kind breakdown."""

    @pytest.mark.asyncio
    async def test_overview_returns_breakdown(self) -> None:
        """Each symbol gets a {edge_kind: count} breakdown dict."""
        services = make_mock_services([
            [
                {"fqn": "mod::Hub", "name": "Hub", "kind": "Class", "file_path": "mod.py", "total_edges": 15},
                {"fqn": "mod::Helper", "name": "Helper", "kind": "Function", "file_path": "mod.py", "total_edges": 8},
            ],
            [
                {"fqn": "mod::Hub", "edge_kind": "calls", "edge_count": 10},
                {"fqn": "mod::Hub", "edge_kind": "defines", "edge_count": 5},
                {"fqn": "mod::Helper", "edge_kind": "called_by", "edge_count": 6},
                {"fqn": "mod::Helper", "edge_kind": "references", "edge_count": 2},
            ],
        ])

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
        services.provider.execute_query.assert_not_called()


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestEscapeLike:
    """_escape_like: pure function for LIKE-safe string encoding."""

    def test_percent(self) -> None:
        assert _escape_like("chunk%ound") == "chunk\\%ound"

    def test_underscore(self) -> None:
        assert _escape_like("chunk_ound") == "chunk\\_ound"

    def test_backslash_escaped_first(self) -> None:
        """Backslash must be escaped before % and _ to avoid double-escaping."""
        assert _escape_like("path\\to") == "path\\\\to"

    def test_all_special_chars(self) -> None:
        assert _escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"

    def test_empty_string(self) -> None:
        assert _escape_like("") == ""

    def test_unicode_passthrough(self) -> None:
        assert _escape_like("パス/ファイル") == "パス/ファイル"


class TestGraphAdversarial:
    """Boundary conditions: self-loops, dense graphs, parameter clamping, SQL injection."""

    @pytest.mark.asyncio
    async def test_walk_self_loop_terminates(self) -> None:
        """Symbol with edge to itself → single result, no infinite expansion."""
        services = make_mock_services([
            [{"fqn": "mod::Self", "name": "Self", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [{"from_fqn": "mod::Self", "to_fqn": "mod::Self", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"}],
        ])

        result = await asyncio.wait_for(
            call_graph_tool(services=services, operation="walk", symbol="mod::Self", depth=10),
            timeout=5.0,
        )

        assert len(result["results"]) == 1
        assert result["results"][0]["fqn"] == "mod::Self"

    @pytest.mark.asyncio
    async def test_walk_dense_graph_respects_limit(self) -> None:
        """50-node fan-out capped by limit parameter."""
        nodes = [{"fqn": f"mod::N{i}", "name": f"N{i}", "kind": "Function", "file_path": "mod.py", "depth": 1} for i in range(50)]
        nodes.insert(0, {"fqn": "mod::Root", "name": "Root", "kind": "Function", "file_path": "mod.py", "depth": 0})
        edges = [{"from_fqn": "mod::Root", "to_fqn": f"mod::N{i}", "edge_kind": "calls", "from_file": "mod.py", "to_file": "mod.py"} for i in range(50)]
        services = make_mock_services([nodes[:5], edges[:5]])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::Root", depth=2, limit=5,
        )

        assert len(result["results"]) <= 5

    @pytest.mark.asyncio
    async def test_walk_depth_zero_clamped_to_minimum(self) -> None:
        """depth=0 silently clamped to 1 — no error returned."""
        services = make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", depth=0,
        )

        assert "error" not in result
        assert "results" in result

    @pytest.mark.asyncio
    async def test_walk_depth_huge_clamped_to_maximum(self) -> None:
        """depth=9999 silently clamped to 20 — verified via query params."""
        services = make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", depth=9999,
        )

        assert "error" not in result
        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]
        assert params[1] == 20  # clamped

    @pytest.mark.asyncio
    async def test_walk_negative_limit_clamped(self) -> None:
        """limit=-5 clamped to 1."""
        services = make_mock_services([
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="mod::A", limit=-5,
        )

        assert "error" not in result

    @pytest.mark.asyncio
    async def test_reachability_empty_scope(self) -> None:
        """Scope with no symbols → empty unreachable list."""
        services = make_mock_services([[], []])

        result = await call_graph_tool(
            services=services, operation="reachability", scope="nonexistent/",
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_reachability_all_connected(self) -> None:
        """All symbols reachable → unreachable list empty."""
        services = make_mock_services([
            [
                {"fqn": "pkg::a", "name": "a", "kind": "Function", "file_path": "pkg/a.py"},
                {"fqn": "pkg::b", "name": "b", "kind": "Function", "file_path": "pkg/b.py"},
            ],
            [{"fqn": "pkg::a"}, {"fqn": "pkg::b"}],
        ])

        result = await call_graph_tool(
            services=services, operation="reachability", scope="pkg/",
        )

        assert result["unreachable"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_boundary_scope_with_underscore_escaped(self) -> None:
        """Scope with _ must be LIKE-escaped so 'a_b/' doesn't match 'aXb/'."""
        services = make_mock_services([[]])

        await call_graph_tool(
            services=services, operation="boundary", scope="chunk_ound/",
        )

        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]
        assert "chunk\\_ound/" in params[0]

    @pytest.mark.asyncio
    async def test_overview_single_symbol(self) -> None:
        services = make_mock_services([
            [{"fqn": "mod::Only", "name": "Only", "kind": "Function", "file_path": "mod.py", "total_edges": 1}],
            [{"fqn": "mod::Only", "edge_kind": "calls", "edge_count": 1}],
        ])

        result = await call_graph_tool(services=services, operation="overview")

        assert len(result["symbols"]) == 1
        assert result["symbols"][0]["breakdown"] == {"calls": 1}

    @pytest.mark.asyncio
    async def test_overview_empty_graph(self) -> None:
        services = make_mock_services([[]])

        result = await call_graph_tool(services=services, operation="overview")

        assert result["symbols"] == []
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_walk_idempotent(self) -> None:
        """Two identical walk calls return identical results (read-only queries)."""
        mock_data: list[list[dict[str, Any]]] = [
            [{"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        ]
        services = make_mock_services(mock_data + mock_data)

        r1 = await call_graph_tool(services=services, operation="walk", symbol="mod::A")
        r2 = await call_graph_tool(services=services, operation="walk", symbol="mod::A")

        assert r1 == r2

    @pytest.mark.asyncio
    async def test_walk_sql_injection_safe(self) -> None:
        """FQN containing SQL keywords passed as parameter, not interpolated."""
        services = make_mock_services([[], []])

        result = await call_graph_tool(
            services=services, operation="walk", symbol="'; DROP TABLE symbols; --",
        )

        assert result["results"] == []
        assert "error" not in result
        call_args = services.provider.execute_query.call_args_list[0]
        params = call_args[0][1]
        assert "'; DROP TABLE symbols; --" in params
