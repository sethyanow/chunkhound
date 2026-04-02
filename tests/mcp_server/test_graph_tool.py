"""Tests for chunkhound.mcp_server.tools.graph — thin tool functions.

These verify that the graph tool module properly composes:
  validation (require_param, clamp) → query builders → execute_query → formatters

The existing tests in tests/lsp/test_tool_graph.py cover end-to-end behavior
via the registry. These tests verify the internal composition of the new module.
"""

from unittest.mock import patch

import pytest

from tests.lsp.mcp_tool_helpers import call_graph_tool, make_mock_services

pytestmark = pytest.mark.unit


class TestWalkUsesBuilders:
    """Walk tool uses query builders rather than inline SQL."""

    @pytest.mark.asyncio
    async def test_walk_calls_build_walk_query(self) -> None:
        """Verify walk dispatches to the query builder, not inline SQL."""
        services = make_mock_services([
            [{"fqn": "a::B", "name": "B", "kind": "Function", "file_path": "a.py", "depth": 0}],
            [],  # edges query
        ])

        with patch(
            "chunkhound.mcp_server.tools.graph.build_walk_query"
        ) as mock_build:
            mock_build.return_value = ("SELECT 1", ["a::B", 2, 20])

            await call_graph_tool(
                services=services, operation="walk", symbol="a::B", depth=2,
            )

            mock_build.assert_called_once()
            args = mock_build.call_args
            assert args.kwargs["symbol"] == "a::B" or args.args[0] == "a::B"

    @pytest.mark.asyncio
    async def test_walk_calls_build_walk_edges_query(self) -> None:
        """After finding nodes, walk uses the edges builder."""
        services = make_mock_services([
            [{"fqn": "a::B", "name": "B", "kind": "Function", "file_path": "a.py", "depth": 0}],
            [],
        ])

        with patch(
            "chunkhound.mcp_server.tools.graph.build_walk_edges_query"
        ) as mock_edges:
            mock_edges.return_value = ("SELECT 1", ["a::B", "a::B"])

            await call_graph_tool(
                services=services, operation="walk", symbol="a::B",
            )

            mock_edges.assert_called_once()


class TestReachabilityUsesBuilders:
    """Reachability tool uses both query builders."""

    @pytest.mark.asyncio
    async def test_reachability_calls_both_builders(self) -> None:
        services = make_mock_services([
            [{"fqn": "p::a", "name": "a", "kind": "Function", "file_path": "p/a.py"}],
            [{"fqn": "p::a"}],
        ])

        with (
            patch(
                "chunkhound.mcp_server.tools.graph.build_reachability_all_symbols_query"
            ) as mock_all,
            patch(
                "chunkhound.mcp_server.tools.graph.build_reachability_reachable_query"
            ) as mock_reach,
        ):
            mock_all.return_value = ("SELECT 1", ["p/%"])
            mock_reach.return_value = ("SELECT 2", ["p/%", "p/%"])

            await call_graph_tool(
                services=services, operation="reachability", scope="p/",
            )

            mock_all.assert_called_once()
            mock_reach.assert_called_once()


class TestBoundaryUsesBuilders:
    """Boundary tool uses query builders."""

    @pytest.mark.asyncio
    async def test_boundary_calls_builder(self) -> None:
        services = make_mock_services([[]])

        with patch(
            "chunkhound.mcp_server.tools.graph.build_boundary_query"
        ) as mock_build:
            mock_build.return_value = ("SELECT 1", ["p/%", "p/%", "p/%", "p/%", 50])

            await call_graph_tool(
                services=services, operation="boundary", scope="p/",
            )

            mock_build.assert_called_once()


class TestOverviewUsesBuilders:
    """Overview tool uses query builders."""

    @pytest.mark.asyncio
    async def test_overview_calls_builder(self) -> None:
        services = make_mock_services([[]])

        with patch(
            "chunkhound.mcp_server.tools.graph.build_overview_query"
        ) as mock_build:
            mock_build.return_value = ("SELECT 1", [20])

            await call_graph_tool(
                services=services, operation="overview",
            )

            mock_build.assert_called_once()
