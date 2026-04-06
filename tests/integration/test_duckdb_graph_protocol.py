"""Integration tests for DuckDB provider graph query protocol methods.

Tests graph_walk, graph_reachability, graph_boundary, graph_overview,
symbol_overlap, chunk_resolution, and symbol_stats.
"""

from pathlib import Path

import pytest

from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.integration


def _connect_fresh(tmp_path: Path) -> DuckDBProvider:
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _insert_file(provider: DuckDBProvider, path: str = "src/example.py") -> int:
    provider.execute_query(
        "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
        [path, path.split("/")[-1], ".py", "python", 100],
    )
    rows = provider.execute_query("SELECT id FROM files WHERE path = ?", [path])
    return rows[0]["id"]


def _build_graph(provider: DuckDBProvider, tmp_path: Path) -> dict[str, int]:
    """Build a small graph: A calls B, B calls C. Returns fqn→id mapping."""
    file_id = _insert_file(provider)

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


class TestDuckDBGraphWalk:
    """graph_walk returns connected nodes and edges from seed FQNs."""

    def test_walk_finds_connected_nodes(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)

        nodes, edges = provider.graph_walk(
            seed_fqns=["mod::A"], depth=2, directed=False,
            edge_kind=None, limit=100,
        )

        fqns = {n["fqn"] for n in nodes}
        assert "mod::A" in fqns
        assert "mod::B" in fqns
        assert "mod::C" in fqns  # reachable at depth 2
        assert len(edges) >= 2
        provider.disconnect()

    def test_walk_respects_depth(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)

        nodes, edges = provider.graph_walk(
            seed_fqns=["mod::A"], depth=1, directed=False,
            edge_kind=None, limit=100,
        )

        fqns = {n["fqn"] for n in nodes}
        assert "mod::A" in fqns
        assert "mod::B" in fqns
        assert "mod::C" not in fqns  # too deep
        provider.disconnect()

    def test_walk_empty_seed_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        nodes, edges = provider.graph_walk(
            seed_fqns=[], depth=2, directed=False, edge_kind=None, limit=100,
        )
        assert nodes == []
        assert edges == []
        provider.disconnect()

    def test_walk_cyclic_edges_terminate(self, tmp_path: Path) -> None:
        """Graph walk with A→B→A cycle must terminate."""
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

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
        provider.insert_symbols_batch(symbols)
        fqn_map = provider.query_symbol_fqns_by_file(file_id)

        # Create cycle: X→Y and Y→X
        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["cyc::X"], from_fqn="cyc::X", from_file="src/example.py",
                    to_symbol_id=fqn_map["cyc::Y"], to_fqn="cyc::Y", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["cyc::Y"], from_fqn="cyc::Y", from_file="src/example.py",
                    to_symbol_id=fqn_map["cyc::X"], to_fqn="cyc::X", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        provider.insert_edges_batch(edges)

        # Should terminate — not infinite loop
        nodes, edges_result = provider.graph_walk(
            seed_fqns=["cyc::X"], depth=10, directed=False,
            edge_kind=None, limit=100,
        )
        fqns = {n["fqn"] for n in nodes}
        assert fqns == {"cyc::X", "cyc::Y"}
        provider.disconnect()


class TestDuckDBGraphReachability:
    """graph_reachability finds unreachable symbols by seeding from entry points.

    Entry points = scope symbols with no inbound edges from other scope symbols.
    Unreachable = scope symbols not reachable from any entry point via forward edges.
    """

    def test_dead_code_cycle_detected(self, tmp_path: Path) -> None:
        """X↔Y mutual cycle, no entry point reaches them → both unreachable.

        Graph: A→B (A is entry point), X↔Y (both have inbound → neither is entry point).
        Reachable from entry points: {A, B}. Unreachable: {X, Y}.
        """
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

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
        provider.insert_symbols_batch(symbols)
        fqn_map = provider.query_symbol_fqns_by_file(file_id)

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
        provider.insert_edges_batch(edges)

        result = provider.graph_reachability("src/example")
        unreachable_fqns = {s["fqn"] for s in result}
        assert unreachable_fqns == {"mod::X", "mod::Y"}
        provider.disconnect()

    def test_transitive_reachability(self, tmp_path: Path) -> None:
        """Entry point A reaches D transitively through A→B→C→D.

        All symbols have inbound edges except A. A is the sole entry point.
        D is reachable via A→B→C→D. No unreachable symbols.
        """
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::A", name="A", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::B", name="B", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::C", name="C", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=20, range_end=25, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::D", name="D", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=30, range_end=35, confidence=1.0, lsp_server="pyright",
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
            EdgeRow(from_symbol_id=fqn_map["mod::C"], from_fqn="mod::C", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::D"], to_fqn="mod::D", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        provider.insert_edges_batch(edges)

        result = provider.graph_reachability("src/example")
        assert result == []
        provider.disconnect()

    def test_single_unreachable_with_inbound_only_from_outside_scope(self, tmp_path: Path) -> None:
        """Symbol with inbound edge only from outside scope has no scope-internal inbound.

        Graph in scope: A→B. Symbol Z has no edges at all in scope.
        Z has no inbound from scope → Z is an entry point → Z is reachable.
        If Z had inbound ONLY from outside scope, it still has no scope-internal
        inbound, so it's an entry point. Unreachable requires inbound from scope
        but no path from any entry point.
        """
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)
        file_id2 = _insert_file(provider, path="other/module.py")

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::A", name="A", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::B", name="B", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mod::Z", name="Z", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=20, range_end=25, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="ext::Caller", name="Caller", kind="Function", language="python",
                      file_id=file_id2, file_path="other/module.py",
                      range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)
        fqn_map = provider.query_symbol_fqns_by_file(file_id)
        fqn_map2 = provider.query_symbol_fqns_by_file(file_id2)

        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["mod::A"], from_fqn="mod::A", from_file="src/example.py",
                    to_symbol_id=fqn_map["mod::B"], to_fqn="mod::B", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            # Outside-scope caller → Z (does NOT make Z have scope-internal inbound)
            EdgeRow(from_symbol_id=fqn_map2["ext::Caller"], from_fqn="ext::Caller", from_file="other/module.py",
                    to_symbol_id=fqn_map["mod::Z"], to_fqn="mod::Z", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        provider.insert_edges_batch(edges)

        result = provider.graph_reachability("src/example")
        # Z has no scope-internal inbound → entry point → reachable from itself
        assert result == []
        provider.disconnect()

    def test_no_entry_points_all_unreachable(self, tmp_path: Path) -> None:
        """Every symbol has inbound from scope → no entry points → all unreachable."""
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

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
        provider.insert_symbols_batch(symbols)
        fqn_map = provider.query_symbol_fqns_by_file(file_id)

        # P→Q→R→P: full cycle, every symbol has inbound
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
        provider.insert_edges_batch(edges)

        result = provider.graph_reachability("src/example")
        unreachable_fqns = {s["fqn"] for s in result}
        assert unreachable_fqns == {"mod::P", "mod::Q", "mod::R"}
        provider.disconnect()

    def test_empty_scope_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        result = provider.graph_reachability("nonexistent/scope")
        assert result == []
        provider.disconnect()


class TestDuckDBSymbolOverlap:
    """symbol_overlap resolves chunks to symbol FQNs."""

    def test_resolves_chunks_to_fqns(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::func_a", name="func_a", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)

        chunks = [{"file_path": "src/example.py", "start_line": 2, "end_line": 8}]
        fqns = provider.symbol_overlap(chunks)
        assert "mod::func_a" in fqns
        provider.disconnect()

    def test_empty_chunks_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        assert provider.symbol_overlap([]) == []
        provider.disconnect()


class TestDuckDBChunkResolution:
    """chunk_resolution resolves FQNs to chunks."""

    def test_resolves_fqns_to_chunks(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

        # Insert a chunk that overlaps the symbol range
        provider.execute_query(
            "INSERT INTO chunks (file_id, chunk_type, symbol, code, start_line, end_line, "
            "start_byte, end_byte, language) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [file_id, "function", "func_a", "def func_a(): pass", 0, 10, 0, 20, "python"],
        )

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mod::func_a", name="func_a", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=10, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)

        result = provider.chunk_resolution(["mod::func_a"])
        assert len(result) >= 1
        assert result[0]["file_path"] == "src/example.py"
        provider.disconnect()


class TestDuckDBSymbolStats:
    """symbol_stats returns counts."""

    def test_returns_counts(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        fqn_map = _build_graph(provider, tmp_path)

        stats = provider.symbol_stats()
        assert stats["symbol_count"] == 3
        assert stats["edge_count"] == 2
        provider.disconnect()

    def test_empty_tables(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        stats = provider.symbol_stats()
        assert stats["symbol_count"] == 0
        assert stats["edge_count"] == 0
        provider.disconnect()
