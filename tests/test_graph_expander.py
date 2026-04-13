"""Tests for GraphWalkExpander — chunk-to-symbol-to-chunk graph expansion.

GraphWalkExpander takes seed chunks from semantic search, resolves them to
symbols via range overlap, walks the symbol_edges graph, resolves discovered
symbols back to chunks, and deduplicates against the seed set.

ch-nxu Step 17: Tests mock the DatabaseProvider protocol methods
(symbol_overlap, graph_walk, chunk_resolution) directly — the expander no
longer calls execute_query. Every test asserts its three protocol methods
were called to prevent regressions where the expander silently skips a
pipeline stage (see skeleton Step 17 failure catalog, "Test mock rewrite
masks sequence bugs").
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

pytestmark = pytest.mark.unit


def _make_mock_provider(
    *,
    overlap_fqns: list[str],
    walk_nodes: list[dict[str, Any]],
    chunk_rows: list[dict[str, Any]],
    walk_edges: list[dict[str, Any]] | None = None,
) -> MagicMock:
    """Create a DatabaseProvider mock wired for a full expander pipeline run.

    Args:
        overlap_fqns: Return value of provider.symbol_overlap(chunks).
        walk_nodes: "nodes" member of provider.graph_walk(...) return tuple.
        chunk_rows: Return value of provider.chunk_resolution(fqns).
        walk_edges: "edges" member of the graph_walk return tuple (usually
            unused by the expander; defaults to empty).
    """
    provider = MagicMock()
    provider.symbol_overlap.return_value = overlap_fqns
    provider.graph_walk.return_value = (walk_nodes, walk_edges or [])
    provider.chunk_resolution.return_value = chunk_rows
    return provider


def _assert_called_once_each(provider: MagicMock) -> None:
    """Every stage of the expander pipeline must fire exactly once per run."""
    assert provider.symbol_overlap.call_count == 1
    assert provider.graph_walk.call_count == 1
    assert provider.chunk_resolution.call_count == 1


class TestChunkToSymbolResolution:
    """expand() resolves seed chunks to symbols via file_path + range overlap."""

    @pytest.mark.asyncio
    async def test_seeds_resolve_to_overlapping_symbols(self) -> None:
        """Seed chunk overlapping a symbol produces that symbol's graph neighbors as chunks."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "other.py"}],
            chunk_rows=[
                {
                    "file_path": "other.py",
                    "content": "def func_b(): pass",
                    "start_line": 5,
                    "end_line": 10,
                }
            ],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 20},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "other.py"
        assert result[0]["start_line"] == 5
        _assert_called_once_each(provider)

    @pytest.mark.asyncio
    async def test_symbol_overlap_receives_seed_chunks_verbatim(self) -> None:
        """Seed chunk dicts are forwarded to provider.symbol_overlap unchanged."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "main.py"}],
            chunk_rows=[],
        )
        seeds = [
            {"file_path": "main.py", "start_line": 1, "end_line": 20},
            {"file_path": "other.py", "start_line": 5, "end_line": 25},
        ]

        await GraphWalkExpander(provider).expand(seeds)

        provider.symbol_overlap.assert_called_once_with(seeds)


class TestSymbolGraphWalk:
    """expand() walks symbol_edges to discover additional symbols."""

    @pytest.mark.asyncio
    async def test_walk_discovers_multi_hop_neighbors(self) -> None:
        """Walk at depth=2 discovers both 1-hop and 2-hop neighbors."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[
                {"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "b.py"},
                {"fqn": "mod::func_c", "name": "func_c", "kind": "Function", "file_path": "c.py"},
            ],
            chunk_rows=[
                {"file_path": "b.py", "content": "def func_b(): ...", "start_line": 1, "end_line": 5},
                {"file_path": "c.py", "content": "def func_c(): ...", "start_line": 1, "end_line": 5},
            ],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 2
        assert {r["file_path"] for r in result} == {"b.py", "c.py"}
        _assert_called_once_each(provider)

    @pytest.mark.asyncio
    async def test_walk_called_with_bidirectional_directed_false(self) -> None:
        """expand() must pass directed=False so graph_walk traverses both edge directions.

        Guards the failure-catalog "directed=False contract drift" on LanceDB.
        """
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "a.py"}],
            chunk_rows=[],
        )

        await GraphWalkExpander(provider).expand([
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
        ])

        kwargs = provider.graph_walk.call_args.kwargs
        assert kwargs.get("directed") is False, (
            f"graph_walk must be called with directed=False, got kwargs={kwargs}"
        )


class TestSymbolToChunkResolution:
    """expand() resolves discovered symbols back to chunks via range overlap."""

    @pytest.mark.asyncio
    async def test_discovered_symbols_resolve_to_chunks(self) -> None:
        """Walked symbols produce chunks with file_path, content, start_line, end_line."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::helper", "name": "helper", "kind": "Function", "file_path": "helpers.py"}],
            chunk_rows=[
                {
                    "file_path": "helpers.py",
                    "content": "def helper(x):\n    return x + 1",
                    "start_line": 10,
                    "end_line": 12,
                }
            ],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 5},
        ])

        assert len(result) == 1
        chunk = result[0]
        assert chunk["file_path"] == "helpers.py"
        assert chunk["content"] == "def helper(x):\n    return x + 1"
        assert chunk["start_line"] == 10
        assert chunk["end_line"] == 12
        _assert_called_once_each(provider)

    @pytest.mark.asyncio
    async def test_chunk_resolution_receives_walked_fqns(self) -> None:
        """provider.chunk_resolution is called with the FQNs extracted from walk nodes.

        Guards "graph_walk tuple shape miscompile" — if the expander forgets
        to extract .fqn from node dicts, this test fails loudly.
        """
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[
                {"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "b.py"},
                {"fqn": "mod::func_c", "name": "func_c", "kind": "Function", "file_path": "c.py"},
            ],
            chunk_rows=[],
        )

        await GraphWalkExpander(provider).expand([
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
        ])

        call_args = provider.chunk_resolution.call_args
        # Positional or keyword — accept either.
        fqns_arg = call_args.args[0] if call_args.args else call_args.kwargs["fqns"]
        assert fqns_arg == ["mod::func_b", "mod::func_c"]


class TestDeduplication:
    """expand() deduplicates discovered chunks against seed set."""

    @pytest.mark.asyncio
    async def test_seed_chunk_excluded_from_results(self) -> None:
        """Chunk matching a seed (same file_path + lines) is not in output."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "other.py"}],
            chunk_rows=[
                {"file_path": "main.py", "content": "...", "start_line": 1, "end_line": 10},
                {"file_path": "other.py", "content": "...", "start_line": 5, "end_line": 15},
            ],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "other.py"
        _assert_called_once_each(provider)


class TestGracefulEmptyGraph:
    """expand() returns [] when symbols or symbol_edges are empty."""

    @pytest.mark.asyncio
    async def test_no_symbols_returns_empty(self) -> None:
        """Empty symbols table → overlap returns nothing → []."""
        provider = _make_mock_provider(
            overlap_fqns=[],
            walk_nodes=[],
            chunk_rows=[],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert result == []
        provider.symbol_overlap.assert_called_once()
        provider.graph_walk.assert_not_called()
        provider.chunk_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_walk_nodes_returns_empty(self) -> None:
        """Walk returns no nodes → [] without calling chunk_resolution."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[],
            chunk_rows=[],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert result == []
        provider.symbol_overlap.assert_called_once()
        provider.graph_walk.assert_called_once()
        provider.chunk_resolution.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_seed_chunks_returns_empty(self) -> None:
        """Empty seed_chunks input → [] immediately, no queries."""
        provider = MagicMock()

        expander = GraphWalkExpander(provider)
        result = await expander.expand([])

        assert result == []
        provider.symbol_overlap.assert_not_called()
        provider.graph_walk.assert_not_called()
        provider.chunk_resolution.assert_not_called()


class TestEdgeKindFiltering:
    """expand(edge_kind=...) restricts walk to specific edge types."""

    @pytest.mark.asyncio
    async def test_edge_kind_passed_to_graph_walk(self) -> None:
        """edge_kind="calls" is forwarded to provider.graph_walk."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::callee", "name": "callee", "kind": "Function", "file_path": "callee.py"}],
            chunk_rows=[{"file_path": "callee.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            edge_kind="calls",
        )

        assert len(result) == 1
        assert result[0]["file_path"] == "callee.py"
        kwargs = provider.graph_walk.call_args.kwargs
        assert kwargs.get("edge_kind") == "calls"


class TestDepthConfiguration:
    """expand(depth=N) limits walk to N hops."""

    @pytest.mark.asyncio
    async def test_depth_passed_to_graph_walk(self) -> None:
        """depth=1 forwards to provider.graph_walk."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::direct_neighbor", "name": "direct_neighbor", "kind": "Function", "file_path": "neighbor.py"}],
            chunk_rows=[{"file_path": "neighbor.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            depth=1,
        )

        assert len(result) == 1
        kwargs = provider.graph_walk.call_args.kwargs
        assert kwargs.get("depth") == 1


# =============================================================================
# Adversarial Stress Tests
# =============================================================================


class TestAdversarialSelfReferential:
    """Walk always includes seed FQNs in base case — realistic scenario."""

    @pytest.mark.asyncio
    async def test_walk_returning_seed_fqns_dedupes_correctly(self) -> None:
        """Walk returns seed FQN + neighbor. Seed's chunk is deduped; neighbor's stays."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[
                {"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "main.py"},
                {"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "other.py"},
            ],
            chunk_rows=[
                {"file_path": "main.py", "content": "def func_a(): ...", "start_line": 1, "end_line": 10},
                {"file_path": "other.py", "content": "def func_b(): ...", "start_line": 5, "end_line": 15},
            ],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        # func_a's chunk matches seed → deduped. func_b's chunk survives.
        assert len(result) == 1
        assert result[0]["file_path"] == "other.py"


class TestAdversarialRedundant:
    """Duplicate and overlapping seed chunks."""

    @pytest.mark.asyncio
    async def test_duplicate_seed_chunks_produce_single_result(self) -> None:
        """Same seed chunk twice doesn't duplicate output."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "other.py"}],
            chunk_rows=[{"file_path": "other.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_multiple_seeds_overlapping_same_symbol(self) -> None:
        """Two different seed chunks overlapping the same symbol."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::big_func"],
            walk_nodes=[{"fqn": "mod::helper", "name": "helper", "kind": "Function", "file_path": "util.py"}],
            chunk_rows=[{"file_path": "util.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 20},
            {"file_path": "main.py", "start_line": 15, "end_line": 30},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "util.py"


class TestAdversarialSparseWithGaps:
    """Some seeds have symbol coverage, some don't."""

    @pytest.mark.asyncio
    async def test_seeds_with_no_symbol_coverage_skipped(self) -> None:
        """Seeds in files without symbols don't prevent other seeds from expanding."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "found.py"}],
            chunk_rows=[{"file_path": "found.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "has_symbols.py", "start_line": 1, "end_line": 10},
            {"file_path": "no_symbols.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "found.py"


class TestAdversarialTypeBoundaries:
    """Boundary values for numeric parameters."""

    @pytest.mark.asyncio
    async def test_depth_zero_returns_seed_symbol_chunks_only(self) -> None:
        """depth=0 means base case only — walk returns seed FQN, resolution maps to seed chunk, dedup empties."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "main.py"}],
            chunk_rows=[{"file_path": "main.py", "content": "...", "start_line": 1, "end_line": 10}],
        )

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            depth=0,
        )

        assert result == []
        kwargs = provider.graph_walk.call_args.kwargs
        assert kwargs.get("depth") == 0


class TestAdversarialStateTransitions:
    """Verify no side effects between calls."""

    @pytest.mark.asyncio
    async def test_second_run_produces_identical_results(self) -> None:
        """Calling expand() twice on same instance produces same results."""
        provider = _make_mock_provider(
            overlap_fqns=["mod::func_a"],
            walk_nodes=[{"fqn": "mod::func_b", "name": "func_b", "kind": "Function", "file_path": "other.py"}],
            chunk_rows=[{"file_path": "other.py", "content": "...", "start_line": 1, "end_line": 5}],
        )

        expander = GraphWalkExpander(provider)
        seeds = [{"file_path": "main.py", "start_line": 1, "end_line": 10}]

        result1 = await expander.expand(seeds)
        result2 = await expander.expand(seeds)

        assert result1 == result2
        assert provider.symbol_overlap.call_count == 2
        assert provider.graph_walk.call_count == 2
        assert provider.chunk_resolution.call_count == 2
