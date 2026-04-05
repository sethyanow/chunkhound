"""Tests for GraphWalkExpander — chunk-to-symbol-to-chunk graph expansion.

GraphWalkExpander takes seed chunks from semantic search, resolves them to
symbols via range overlap, walks the symbol_edges graph, resolves discovered
symbols back to chunks, and deduplicates against the seed set.

All tests mock DatabaseProvider.execute_query with sequential return values
matching the three internal queries: overlap → walk → chunk resolution.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

pytestmark = pytest.mark.unit


def _make_mock_provider(
    query_results: list[list[dict[str, Any]]],
) -> MagicMock:
    """Create mock DatabaseProvider with sequential execute_query results."""
    provider = MagicMock()
    provider.execute_query.side_effect = query_results
    return provider


class TestChunkToSymbolResolution:
    """expand() resolves seed chunks to symbols via file_path + range overlap."""

    @pytest.mark.asyncio
    async def test_seeds_resolve_to_overlapping_symbols(self) -> None:
        """Seed chunk overlapping a symbol produces that symbol's graph neighbors as chunks."""
        provider = _make_mock_provider([
            # Call 1 - overlap: seed chunk in main.py:1-20 maps to mod::func_a
            [{"fqn": "mod::func_a", "file_id": 1}],
            # Call 2 - walk: func_a's neighbor is func_b
            [{"fqn": "mod::func_b"}],
            # Call 3 - chunk resolution: func_b maps to chunk in other.py
            [
                {
                    "file_path": "other.py",
                    "content": "def func_b(): pass",
                    "start_line": 5,
                    "end_line": 10,
                }
            ],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 20},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "other.py"
        assert result[0]["start_line"] == 5


class TestSymbolGraphWalk:
    """expand() walks symbol_edges to discover additional symbols."""

    @pytest.mark.asyncio
    async def test_walk_discovers_multi_hop_neighbors(self) -> None:
        """Walk at depth=2 discovers both 1-hop and 2-hop neighbors."""
        provider = _make_mock_provider([
            # overlap: seed maps to func_a
            [{"fqn": "mod::func_a", "file_id": 1}],
            # walk: func_a → func_b (1-hop) → func_c (2-hop)
            [{"fqn": "mod::func_b"}, {"fqn": "mod::func_c"}],
            # resolution: both neighbors map to chunks
            [
                {"file_path": "b.py", "content": "def func_b(): ...", "start_line": 1, "end_line": 5},
                {"file_path": "c.py", "content": "def func_c(): ...", "start_line": 1, "end_line": 5},
            ],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "a.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 2
        result_paths = {r["file_path"] for r in result}
        assert result_paths == {"b.py", "c.py"}


class TestSymbolToChunkResolution:
    """expand() resolves discovered symbols back to chunks via range overlap."""

    @pytest.mark.asyncio
    async def test_discovered_symbols_resolve_to_chunks(self) -> None:
        """Walked symbols produce chunks with file_path, content, start_line, end_line."""
        provider = _make_mock_provider([
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::helper"}],
            [
                {
                    "file_path": "helpers.py",
                    "content": "def helper(x):\n    return x + 1",
                    "start_line": 10,
                    "end_line": 12,
                }
            ],
        ])

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


    def test_resolution_query_selects_chunk_id(self) -> None:
        """_build_resolution_query SELECT includes c.id AS chunk_id."""
        from chunkhound.services.search.graph_walk_expander import (
            _build_resolution_query,
        )

        sql, params = _build_resolution_query(["mod::func_a"])
        assert "c.id AS chunk_id" in sql
        assert params == ["mod::func_a"]


class TestDeduplication:
    """expand() deduplicates discovered chunks against seed set."""

    @pytest.mark.asyncio
    async def test_seed_chunk_excluded_from_results(self) -> None:
        """Chunk matching a seed (same file_path + lines) is not in output."""
        provider = _make_mock_provider([
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::func_b"}],
            # resolution returns the seed chunk itself AND a new chunk
            [
                {"file_path": "main.py", "content": "...", "start_line": 1, "end_line": 10},
                {"file_path": "other.py", "content": "...", "start_line": 5, "end_line": 15},
            ],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 1
        assert result[0]["file_path"] == "other.py"


class TestGracefulEmptyGraph:
    """expand() returns [] when symbols or symbol_edges are empty."""

    @pytest.mark.asyncio
    async def test_no_symbols_returns_empty(self) -> None:
        """Empty symbols table → overlap returns nothing → []."""
        provider = _make_mock_provider([
            [],  # overlap returns nothing
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert result == []

    @pytest.mark.asyncio
    async def test_no_edges_returns_empty(self) -> None:
        """symbol_edges empty → walk returns only seed FQNs → all deduped → []."""
        provider = _make_mock_provider([
            [{"fqn": "mod::func_a", "file_id": 1}],
            # walk returns only the seed FQN itself (no neighbors)
            [{"fqn": "mod::func_a"}],
            # resolution returns the seed chunk back
            [{"file_path": "main.py", "content": "...", "start_line": 1, "end_line": 10}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert result == []

    @pytest.mark.asyncio
    async def test_empty_seed_chunks_returns_empty(self) -> None:
        """Empty seed_chunks input → [] immediately, no queries."""
        provider = _make_mock_provider([])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([])

        assert result == []
        provider.execute_query.assert_not_called()


class TestEdgeKindFiltering:
    """expand(edge_kind=...) restricts walk to specific edge types."""

    @pytest.mark.asyncio
    async def test_edge_kind_passed_to_walk_query(self) -> None:
        """edge_kind="calls" filters the walk to calls edges only."""
        provider = _make_mock_provider([
            [{"fqn": "mod::func_a", "file_id": 1}],
            # walk: only calls-reachable neighbor
            [{"fqn": "mod::callee"}],
            [{"file_path": "callee.py", "content": "...", "start_line": 1, "end_line": 5}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            edge_kind="calls",
        )

        assert len(result) == 1
        assert result[0]["file_path"] == "callee.py"

        # Verify the walk query SQL contains edge_kind filter
        walk_call = provider.execute_query.call_args_list[1]
        walk_sql = walk_call[0][0]
        walk_params = walk_call[0][1]
        assert "edge_kind" in walk_sql
        assert "calls" in walk_params


class TestDepthConfiguration:
    """expand(depth=N) limits walk to N hops."""

    @pytest.mark.asyncio
    async def test_depth_passed_to_walk_query(self) -> None:
        """depth=1 limits walk to direct neighbors only."""
        provider = _make_mock_provider([
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::direct_neighbor"}],
            [{"file_path": "neighbor.py", "content": "...", "start_line": 1, "end_line": 5}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            depth=1,
        )

        assert len(result) == 1

        # Verify the walk query received depth=1
        walk_call = provider.execute_query.call_args_list[1]
        walk_params = walk_call[0][1]
        assert 1 in walk_params


# =============================================================================
# Adversarial Stress Tests
# =============================================================================


class TestAdversarialSelfReferential:
    """Walk CTE always includes seed FQNs in base case — realistic scenario."""

    @pytest.mark.asyncio
    async def test_walk_returning_seed_fqns_dedupes_correctly(self) -> None:
        """Walk returns seed FQN + neighbor. Seed's chunk is deduped; neighbor's stays."""
        provider = _make_mock_provider([
            # overlap: seed maps to func_a
            [{"fqn": "mod::func_a", "file_id": 1}],
            # walk: returns seed FQN (base case) AND neighbor (realistic CTE output)
            [{"fqn": "mod::func_a"}, {"fqn": "mod::func_b"}],
            # resolution: chunks for BOTH func_a (= seed) and func_b (= new)
            [
                {"file_path": "main.py", "content": "def func_a(): ...", "start_line": 1, "end_line": 10},
                {"file_path": "other.py", "content": "def func_b(): ...", "start_line": 5, "end_line": 15},
            ],
        ])

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
        provider = _make_mock_provider([
            # overlap: both seeds map to same FQN (DISTINCT handles it in real DB)
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::func_b"}],
            [{"file_path": "other.py", "content": "...", "start_line": 1, "end_line": 5}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
            {"file_path": "main.py", "start_line": 1, "end_line": 10},
        ])

        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_multiple_seeds_overlapping_same_symbol(self) -> None:
        """Two different seed chunks overlapping the same symbol."""
        provider = _make_mock_provider([
            # overlap: both seeds map to same FQN
            [{"fqn": "mod::big_func", "file_id": 1}],
            [{"fqn": "mod::helper"}],
            [{"file_path": "util.py", "content": "...", "start_line": 1, "end_line": 5}],
        ])

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
        provider = _make_mock_provider([
            # overlap: only one of two seeds maps to a symbol
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::func_b"}],
            [{"file_path": "found.py", "content": "...", "start_line": 1, "end_line": 5}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand([
            {"file_path": "has_symbols.py", "start_line": 1, "end_line": 10},
            {"file_path": "no_symbols.py", "start_line": 1, "end_line": 10},
        ])

        # The overlap query runs on both seeds but only finds symbols for one.
        # The walk still proceeds with the found FQN.
        assert len(result) == 1
        assert result[0]["file_path"] == "found.py"


class TestAdversarialTypeBoundaries:
    """Boundary values for numeric parameters."""

    @pytest.mark.asyncio
    async def test_depth_zero_returns_seed_symbol_chunks_only(self) -> None:
        """depth=0 means no walk — resolve seed FQNs but don't traverse edges.

        If seed FQN's chunk differs from the seed chunk, it appears in results.
        If it matches, it's deduped away.
        """
        provider = _make_mock_provider([
            # overlap: seed maps to func_a
            [{"fqn": "mod::func_a", "file_id": 1}],
            # walk at depth=0: CTE base case returns seed FQN only (no recursion)
            [{"fqn": "mod::func_a"}],
            # resolution: func_a maps to a chunk that IS the seed → deduped
            [{"file_path": "main.py", "content": "...", "start_line": 1, "end_line": 10}],
        ])

        expander = GraphWalkExpander(provider)
        result = await expander.expand(
            [{"file_path": "main.py", "start_line": 1, "end_line": 10}],
            depth=0,
        )

        assert result == []


class TestAdversarialStateTransitions:
    """Verify no side effects between calls."""

    @pytest.mark.asyncio
    async def test_second_run_produces_identical_results(self) -> None:
        """Calling expand() twice on same instance produces same results."""
        results_batch = [
            [{"fqn": "mod::func_a", "file_id": 1}],
            [{"fqn": "mod::func_b"}],
            [{"file_path": "other.py", "content": "...", "start_line": 1, "end_line": 5}],
        ]

        provider = _make_mock_provider(results_batch + results_batch)

        expander = GraphWalkExpander(provider)
        seeds = [{"file_path": "main.py", "start_line": 1, "end_line": 10}]

        result1 = await expander.expand(seeds)
        result2 = await expander.expand(seeds)

        assert result1 == result2
        assert provider.execute_query.call_count == 6  # 3 calls × 2 runs
