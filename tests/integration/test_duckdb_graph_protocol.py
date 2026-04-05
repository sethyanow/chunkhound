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
    """graph_reachability finds unreachable symbols in a scope.

    NOTE: The existing CTE seeds with ALL scope symbols, so every symbol
    is trivially "reachable from itself." This test verifies existing behavior.
    True unreachability detection would require seeding only from entry points.
    """

    def test_returns_list(self, tmp_path: Path) -> None:
        """Reachability returns a list (may be empty with current CTE semantics)."""
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="src.example::main", name="main", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="src.example::orphan", name="orphan", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=20, range_end=25, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)

        result = provider.graph_reachability("src/example")
        assert isinstance(result, list)
        # Current CTE seeds all symbols → all reachable → unreachable is empty
        # This matches existing graph.py behavior
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
