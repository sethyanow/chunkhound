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


class TestDuckDBGraphBoundary:
    """graph_boundary returns edges crossing a scope prefix boundary."""

    def _build_cross_scope_graph(self, provider: DuckDBProvider) -> None:
        """Two files in different scopes with a cross-boundary edge."""
        provider.execute_query(
            "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
            ["src/inside/a.py", "a.py", ".py", "python", 100],
        )
        provider.execute_query(
            "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
            ["src/outside/b.py", "b.py", ".py", "python", 100],
        )
        inside_id = provider.execute_query(
            "SELECT id FROM files WHERE path = ?", ["src/inside/a.py"]
        )[0]["id"]
        outside_id = provider.execute_query(
            "SELECT id FROM files WHERE path = ?", ["src/outside/b.py"]
        )[0]["id"]

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="in::A", name="A", kind="Function", language="python",
                      file_id=inside_id, file_path="src/inside/a.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="out::B", name="B", kind="Function", language="python",
                      file_id=outside_id, file_path="src/outside/b.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)
        inside_fqn_id = provider.query_symbol_fqns_by_file(inside_id)["in::A"]
        outside_fqn_id = provider.query_symbol_fqns_by_file(outside_id)["out::B"]

        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=inside_fqn_id, from_fqn="in::A", from_file="src/inside/a.py",
                    to_symbol_id=outside_fqn_id, to_fqn="out::B", to_file="src/outside/b.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ]
        provider.insert_edges_batch(edges)

    def test_returns_cross_boundary_edges(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        self._build_cross_scope_graph(provider)

        edges = provider.graph_boundary("src/inside/", limit=100)
        assert len(edges) == 1
        e = edges[0]
        # Full field contract — MCP tool needs name/kind/file on both sides
        assert e["from_fqn"] == "in::A"
        assert e["from_name"] == "A"
        assert e["from_kind"] == "Function"
        assert e["from_file"] == "src/inside/a.py"
        assert e["to_fqn"] == "out::B"
        assert e["to_name"] == "B"
        assert e["to_kind"] == "Function"
        assert e["to_file"] == "src/outside/b.py"
        assert e["edge_kind"] == "calls"
        provider.disconnect()

    def test_internal_edges_excluded(self, tmp_path: Path) -> None:
        """Edges fully inside or fully outside the scope are excluded."""
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)  # all in src/example.py

        edges = provider.graph_boundary("src/example", limit=100)
        # All edges stay inside scope — none cross
        assert edges == []
        provider.disconnect()

    def test_empty_tables_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        assert provider.graph_boundary("src/", limit=100) == []
        provider.disconnect()


class TestDuckDBGraphOverview:
    """graph_overview returns symbols ranked by total edge count."""

    def test_ranks_by_edge_count(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)  # A->B, B->C

        result = provider.graph_overview(scope=None, limit=10)
        # B is central (touched by A->B and B->C), A and C touched once each
        by_fqn = {r["fqn"]: r for r in result}
        assert "mod::B" in by_fqn
        assert by_fqn["mod::B"]["total_edges"] >= by_fqn["mod::A"]["total_edges"]
        assert by_fqn["mod::B"]["total_edges"] >= by_fqn["mod::C"]["total_edges"]
        provider.disconnect()

    def test_scope_filter(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)

        result = provider.graph_overview(scope="src/example", limit=10)
        assert all(r["file_path"].startswith("src/example") for r in result)

        empty = provider.graph_overview(scope="nonexistent/", limit=10)
        assert empty == []
        provider.disconnect()

    def test_respects_limit(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)

        result = provider.graph_overview(scope=None, limit=2)
        assert len(result) <= 2
        provider.disconnect()

    def test_empty_tables_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        assert provider.graph_overview(scope=None, limit=10) == []
        provider.disconnect()

    def test_scope_with_like_special_chars_escaped(self, tmp_path: Path) -> None:
        """Scope containing LIKE metacharacters (_ % !) must not match literally.

        Regression for ch-nxu: the provider internally LIKE-escapes scope
        patterns so ``chunk_ound`` matches ``chunk_ound`` only, not ``chunkAound``.
        """
        provider = _connect_fresh(tmp_path)
        # Insert two files that would collide under unescaped LIKE
        provider.execute_query(
            "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
            ["chunk_ound/real.py", "real.py", ".py", "python", 100],
        )
        provider.execute_query(
            "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
            ["chunkAound/decoy.py", "decoy.py", ".py", "python", 100],
        )
        real_id = provider.execute_query(
            "SELECT id FROM files WHERE path = ?", ["chunk_ound/real.py"]
        )[0]["id"]
        decoy_id = provider.execute_query(
            "SELECT id FROM files WHERE path = ?", ["chunkAound/decoy.py"]
        )[0]["id"]

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="real::f", name="f", kind="Function", language="python",
                      file_id=real_id, file_path="chunk_ound/real.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="decoy::f", name="f", kind="Function", language="python",
                      file_id=decoy_id, file_path="chunkAound/decoy.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)
        # Give both symbols an edge so graph_overview can rank them
        real_fqn_id = provider.query_symbol_fqns_by_file(real_id)["real::f"]
        decoy_fqn_id = provider.query_symbol_fqns_by_file(decoy_id)["decoy::f"]
        provider.insert_edges_batch([
            EdgeRow(from_symbol_id=real_fqn_id, from_fqn="real::f", from_file="chunk_ound/real.py",
                    to_symbol_id=decoy_fqn_id, to_fqn="decoy::f", to_file="chunkAound/decoy.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
        ])

        result = provider.graph_overview(scope="chunk_ound/", limit=10)
        # Must NOT include the decoy file (unescaped _ would match 'chunkAound/')
        fqns = {r["fqn"] for r in result}
        assert "real::f" in fqns
        assert "decoy::f" not in fqns
        provider.disconnect()


class TestDuckDBGraphOverviewBreakdown:
    """graph_overview_breakdown returns per-edge_kind counts for a list of FQNs."""

    def test_counts_per_edge_kind(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)  # A->B, B->C, both 'calls'

        breakdown = provider.graph_overview_breakdown(
            ["mod::A", "mod::B", "mod::C"]
        )
        # mod::A touches 1 edge (A->B), mod::B touches 2 (A->B, B->C), mod::C touches 1 (B->C)
        assert breakdown["mod::A"]["calls"] == 1
        assert breakdown["mod::B"]["calls"] == 2
        assert breakdown["mod::C"]["calls"] == 1
        provider.disconnect()

    def test_multiple_edge_kinds(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_file(provider)

        symbols: list[SymbolRow] = [
            SymbolRow(fqn="mx::X", name="X", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=0, range_end=5, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
            SymbolRow(fqn="mx::Y", name="Y", kind="Function", language="python",
                      file_id=file_id, file_path="src/example.py",
                      range_start=10, range_end=15, confidence=1.0, lsp_server="pyright",
                      parent_fqn=None, type_signature=None),
        ]
        provider.insert_symbols_batch(symbols)
        fqn_map = provider.query_symbol_fqns_by_file(file_id)
        edges: list[EdgeRow] = [
            EdgeRow(from_symbol_id=fqn_map["mx::X"], from_fqn="mx::X", from_file="src/example.py",
                    to_symbol_id=fqn_map["mx::Y"], to_fqn="mx::Y", to_file="src/example.py",
                    edge_kind="calls", confidence=1.0, lsp_server="pyright"),
            EdgeRow(from_symbol_id=fqn_map["mx::X"], from_fqn="mx::X", from_file="src/example.py",
                    to_symbol_id=fqn_map["mx::Y"], to_fqn="mx::Y", to_file="src/example.py",
                    edge_kind="references", confidence=1.0, lsp_server="pyright"),
        ]
        provider.insert_edges_batch(edges)

        breakdown = provider.graph_overview_breakdown(["mx::X"])
        assert breakdown["mx::X"]["calls"] == 1
        assert breakdown["mx::X"]["references"] == 1
        provider.disconnect()

    def test_empty_fqns_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        assert provider.graph_overview_breakdown([]) == {}
        provider.disconnect()

    def test_unknown_fqn_absent_from_result(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _build_graph(provider, tmp_path)
        breakdown = provider.graph_overview_breakdown(["does::not::exist"])
        # Absent (not None entry) — matches dict-missing semantics for downstream merge
        assert "does::not::exist" not in breakdown
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
