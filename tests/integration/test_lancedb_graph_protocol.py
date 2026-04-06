"""Integration tests for LanceDB provider graph query protocol methods.

Tests graph_walk, graph_reachability, symbol_overlap, chunk_resolution,
and symbol_stats on LanceDB backend.
"""

import pytest

from chunkhound.core.models.symbol import EdgeRow, SymbolRow

pytestmark = pytest.mark.integration


def _insert_test_file(provider) -> int:
    """Insert a test file and return its file_id."""
    from chunkhound.core.models import File
    from chunkhound.core.types.common import Language

    return provider.insert_file(
        File(path="src/example.py", mtime=1234567890.0, language=Language.PYTHON, size_bytes=100)
    )


def _build_graph(provider) -> dict[str, int]:
    """Build a small graph: A calls B, B calls C. Returns fqn→id mapping."""
    file_id = _insert_test_file(provider)

    symbols: list[SymbolRow] = [
        SymbolRow(fqn="mod::A", name="A", kind="Function", language="python",
                  file_id=file_id, file_path="src/example.py",
                  range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                  parent_fqn=None, type_signature="() -> None"),
        SymbolRow(fqn="mod::B", name="B", kind="Function", language="python",
                  file_id=file_id, file_path="src/example.py",
                  range_start=15, range_end=25, confidence=1.0, lsp_server="pyright",
                  parent_fqn=None, type_signature="(x: int) -> str"),
        SymbolRow(fqn="mod::C", name="C", kind="Function", language="python",
                  file_id=file_id, file_path="src/example.py",
                  range_start=30, range_end=40, confidence=1.0, lsp_server="pyright",
                  parent_fqn=None, type_signature=None),
    ]
    provider.insert_symbols_batch(symbols)
    fqn_map = provider.query_symbol_fqns_by_file(file_id)

    edges: list[EdgeRow] = [
        EdgeRow(from_symbol_id=fqn_map["mod::A"], from_fqn="mod::A", from_file="src/example.py",
                to_symbol_id=fqn_map["mod::B"], to_fqn="mod::B", to_file="src/example.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        EdgeRow(from_symbol_id=fqn_map["mod::B"], from_fqn="mod::B", from_file="src/example.py",
                to_symbol_id=fqn_map["mod::C"], to_fqn="mod::C", to_file="src/example.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright"),
    ]
    provider.insert_edges_batch(edges)
    return fqn_map


class TestLanceDBGraphWalk:
    """graph_walk returns connected nodes and edges from seed FQNs."""

    def test_walk_finds_connected_nodes(self, lancedb_provider) -> None:
        _build_graph(lancedb_provider)

        nodes, edges = lancedb_provider.graph_walk(
            seed_fqns=["mod::A"], depth=2, directed=False,
            edge_kind=None, limit=100,
        )

        fqns = {n["fqn"] for n in nodes}
        assert "mod::A" in fqns
        assert "mod::B" in fqns
        assert "mod::C" in fqns  # reachable at depth 2
        assert len(edges) >= 2

    def test_walk_respects_depth(self, lancedb_provider) -> None:
        _build_graph(lancedb_provider)

        nodes, edges = lancedb_provider.graph_walk(
            seed_fqns=["mod::A"], depth=1, directed=False,
            edge_kind=None, limit=100,
        )

        fqns = {n["fqn"] for n in nodes}
        assert "mod::A" in fqns
        assert "mod::B" in fqns
        assert "mod::C" not in fqns  # too deep

    def test_walk_empty_seed_returns_empty(self, lancedb_provider) -> None:
        nodes, edges = lancedb_provider.graph_walk(
            seed_fqns=[], depth=2, directed=False, edge_kind=None, limit=100,
        )
        assert nodes == []
        assert edges == []

    def test_walk_cyclic_edges_terminate(self, lancedb_provider) -> None:
        """Graph walk with A→B→A cycle must terminate."""
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="cyc::X", name="X", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="cyc::Y", name="Y", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        lancedb_provider.insert_symbols_batch(symbols)
        fqn_map = lancedb_provider.query_symbol_fqns_by_file(file_id)

        # Create cycle: X→Y and Y→X
        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["cyc::X"], from_fqn="cyc::X", from_file="src/example.py",
                    to_symbol_id=fqn_map["cyc::Y"], to_fqn="cyc::Y", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["cyc::Y"], from_fqn="cyc::Y", from_file="src/example.py",
                    to_symbol_id=fqn_map["cyc::X"], to_fqn="cyc::X", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        lancedb_provider.insert_edges_batch(edges)

        # Should terminate — not infinite loop
        nodes, edges_result = lancedb_provider.graph_walk(
            seed_fqns=["cyc::X"], depth=10, directed=False,
            edge_kind=None, limit=100,
        )
        fqns = {n["fqn"] for n in nodes}
        assert fqns == {"cyc::X", "cyc::Y"}


class TestLanceDBGraphReachability:
    """graph_reachability finds unreachable symbols by seeding from entry points."""

    def test_dead_code_cycle_detected(self, lancedb_provider) -> None:
        """X↔Y mutual cycle, no entry point reaches them → both unreachable."""
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::A", name="A", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::B", name="B", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::X", name="X", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=20, range_end=25, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::Y", name="Y", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=30, range_end=35, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        lancedb_provider.insert_symbols_batch(symbols)
        fqn_map = lancedb_provider.query_symbol_fqns_by_file(file_id)

        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["mod::A"], from_fqn="mod::A", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::B"], to_fqn="mod::B", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["mod::X"], from_fqn="mod::X", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::Y"], to_fqn="mod::Y", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["mod::Y"], from_fqn="mod::Y", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::X"], to_fqn="mod::X", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        lancedb_provider.insert_edges_batch(edges)

        result = lancedb_provider.graph_reachability("src/example")
        unreachable_fqns = {s["fqn"] for s in result}
        assert unreachable_fqns == {"mod::X", "mod::Y"}

    def test_no_entry_points_all_unreachable(self, lancedb_provider) -> None:
        """P→Q→R→P full cycle, every symbol has inbound → all unreachable."""
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::P", name="P", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::Q", name="Q", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::R", name="R", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=20, range_end=25, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        lancedb_provider.insert_symbols_batch(symbols)
        fqn_map = lancedb_provider.query_symbol_fqns_by_file(file_id)

        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["mod::P"], from_fqn="mod::P", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::Q"], to_fqn="mod::Q", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["mod::Q"], from_fqn="mod::Q", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::R"], to_fqn="mod::R", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["mod::R"], from_fqn="mod::R", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::P"], to_fqn="mod::P", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        lancedb_provider.insert_edges_batch(edges)

        result = lancedb_provider.graph_reachability("src/example")
        unreachable_fqns = {s["fqn"] for s in result}
        assert unreachable_fqns == {"mod::P", "mod::Q", "mod::R"}

    def test_all_connected_none_unreachable(self, lancedb_provider) -> None:
        """A→B→C, A is entry point, all reachable."""
        _build_graph(lancedb_provider)
        result = lancedb_provider.graph_reachability("src/example")
        assert result == []

    def test_empty_scope_returns_empty(self, lancedb_provider) -> None:
        result = lancedb_provider.graph_reachability("nonexistent/scope")
        assert result == []


class TestLanceDBSymbolOverlap:
    """symbol_overlap resolves chunks to symbol FQNs."""

    def test_resolves_chunks_to_fqns(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::func_a", name="func_a", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        chunks = [{"file_path": "src/example.py", "start_line": 2, "end_line": 8}]
        fqns = lancedb_provider.symbol_overlap(chunks)
        assert "mod::func_a" in fqns

    def test_empty_chunks_returns_empty(self, lancedb_provider) -> None:
        assert lancedb_provider.symbol_overlap([]) == []


class TestLanceDBChunkResolution:
    """chunk_resolution resolves FQNs to chunks."""

    def test_resolves_fqns_to_chunks(self, lancedb_provider) -> None:
        from chunkhound.core.models import Chunk
        from chunkhound.core.types.common import ChunkType, Language

        file_id = _insert_test_file(lancedb_provider)

        # Insert a chunk that overlaps the symbol range
        lancedb_provider.insert_chunks_batch([
            Chunk(
                file_id=file_id,
                code="def func_a(): pass",
                start_line=1,
                end_line=10,
                chunk_type=ChunkType.FUNCTION,
                language=Language.PYTHON,
                symbol="func_a",
            ),
        ])

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::func_a", name="func_a", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        result = lancedb_provider.chunk_resolution(["mod::func_a"])
        assert len(result) >= 1
        assert result[0]["file_path"] == "src/example.py"


class TestLanceDBSymbolStats:
    """symbol_stats returns counts."""

    def test_returns_counts(self, lancedb_provider) -> None:
        _build_graph(lancedb_provider)

        stats = lancedb_provider.symbol_stats()
        assert stats["symbol_count"] == 3
        assert stats["edge_count"] == 2

    def test_empty_tables(self, lancedb_provider) -> None:
        stats = lancedb_provider.symbol_stats()
        assert stats["symbol_count"] == 0
        assert stats["edge_count"] == 0
