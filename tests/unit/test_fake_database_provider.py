"""Tests for FakeDatabaseProvider — verifies dict-backed DatabaseProvider protocol compliance.

These tests verify the test infrastructure itself. FakeDatabaseProvider must faithfully
implement the DatabaseProvider protocol so it can replace real DuckDB/LanceDB in unit tests.
"""

import pytest

pytestmark = pytest.mark.unit

from chunkhound.core.models import Chunk, Embedding, File
from chunkhound.core.types import (
    ChunkId,
    ChunkType,
    Dimensions,
    FileId,
    FilePath,
    Language,
    LineNumber,
    ModelName,
    ProviderName,
    Timestamp,
)
from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from tests.fixtures.fake_providers import FakeDatabaseProvider


# --- Helpers ---


def _make_embedding(
    chunk_id: int,
    provider: str = "fake",
    model: str = "test-model",
    dims: int = 3,
    vector: list[float] | None = None,
) -> Embedding:
    return Embedding(
        chunk_id=ChunkId(chunk_id),
        provider=ProviderName(provider),
        model=ModelName(model),
        dims=Dimensions(dims),
        vector=vector or [0.1, 0.2, 0.3],
    )


def _make_file(path: str = "src/main.py", language: Language = Language.PYTHON) -> File:
    return File(
        path=FilePath(path),
        mtime=Timestamp(1000.0),
        language=language,
        size_bytes=100,
    )


def _make_chunk(
    file_id: int = 1,
    symbol: str = "main",
    start_line: int = 1,
    end_line: int = 10,
    code: str = "def main(): pass",
    chunk_type: ChunkType = ChunkType.FUNCTION,
    language: Language = Language.PYTHON,
    file_path: str | None = "src/main.py",
) -> Chunk:
    return Chunk(
        symbol=symbol,
        start_line=LineNumber(start_line),
        end_line=LineNumber(end_line),
        code=code,
        chunk_type=chunk_type,
        file_id=FileId(file_id),
        language=language,
        file_path=FilePath(file_path) if file_path else None,
    )


# --- Lifecycle ---


class TestLifecycle:
    def test_connect_sets_connected(self):
        provider = FakeDatabaseProvider()
        provider.connect()
        assert provider.is_connected

    def test_disconnect_clears_connected(self):
        provider = FakeDatabaseProvider()
        provider.connect()
        provider.disconnect()
        assert not provider.is_connected

    def test_db_path_returns_fake(self):
        provider = FakeDatabaseProvider()
        assert provider.db_path == ":memory:"

    def test_create_schema_is_noop(self):
        provider = FakeDatabaseProvider()
        provider.connect()
        provider.create_schema()  # Should not raise


# --- File CRUD ---


class TestFileCRUD:
    @pytest.fixture
    def provider(self):
        p = FakeDatabaseProvider()
        p.connect()
        return p

    def test_insert_file_returns_id(self, provider):
        f = _make_file()
        file_id = provider.insert_file(f)
        assert file_id == 1

    def test_insert_file_auto_increments(self, provider):
        id1 = provider.insert_file(_make_file("a.py"))
        id2 = provider.insert_file(_make_file("b.py"))
        assert id2 == id1 + 1

    def test_get_file_by_id_returns_dict(self, provider):
        f = _make_file("src/main.py")
        file_id = provider.insert_file(f)
        result = provider.get_file_by_id(file_id)
        assert result is not None
        assert result["path"] == "src/main.py"
        assert result["id"] == file_id

    def test_get_file_by_id_as_model(self, provider):
        f = _make_file("src/main.py")
        file_id = provider.insert_file(f)
        result = provider.get_file_by_id(file_id, as_model=True)
        assert isinstance(result, File)
        assert result.id == file_id

    def test_get_file_by_id_missing_returns_none(self, provider):
        assert provider.get_file_by_id(999) is None

    def test_get_file_by_path_returns_dict(self, provider):
        f = _make_file("src/main.py")
        provider.insert_file(f)
        result = provider.get_file_by_path("src/main.py")
        assert result is not None
        assert result["path"] == "src/main.py"

    def test_get_file_by_path_missing_returns_none(self, provider):
        assert provider.get_file_by_path("nonexistent.py") is None

    def test_update_file(self, provider):
        f = _make_file("src/main.py")
        file_id = provider.insert_file(f)
        provider.update_file(file_id, mtime=2000.0, size_bytes=200)
        result = provider.get_file_by_id(file_id)
        assert result["mtime"] == 2000.0
        assert result["size_bytes"] == 200

    def test_delete_file_completely(self, provider):
        f = _make_file("src/main.py")
        provider.insert_file(f)
        deleted = provider.delete_file_completely("src/main.py")
        assert deleted is True
        assert provider.get_file_by_path("src/main.py") is None

    def test_delete_file_completely_removes_chunks(self, provider):
        f = _make_file("src/main.py")
        file_id = provider.insert_file(f)
        provider.insert_chunk(_make_chunk(file_id=file_id))
        provider.delete_file_completely("src/main.py")
        assert provider.get_chunks_by_file_id(file_id) == []

    def test_delete_file_completely_nonexistent_returns_false(self, provider):
        assert provider.delete_file_completely("nonexistent.py") is False


# --- Chunk CRUD ---


class TestChunkCRUD:
    @pytest.fixture
    def provider(self):
        p = FakeDatabaseProvider()
        p.connect()
        return p

    @pytest.fixture
    def file_id(self, provider):
        return provider.insert_file(_make_file("src/main.py"))

    def test_insert_chunk_returns_id(self, provider, file_id):
        chunk = _make_chunk(file_id=file_id)
        chunk_id = provider.insert_chunk(chunk)
        assert chunk_id == 1

    def test_insert_chunks_batch_returns_ids(self, provider, file_id):
        chunks = [
            _make_chunk(file_id=file_id, symbol="fn_a", start_line=1, end_line=5),
            _make_chunk(file_id=file_id, symbol="fn_b", start_line=6, end_line=10),
        ]
        ids = provider.insert_chunks_batch(chunks)
        assert len(ids) == 2
        assert ids[1] == ids[0] + 1

    def test_get_chunk_by_id(self, provider, file_id):
        chunk = _make_chunk(file_id=file_id, symbol="main")
        chunk_id = provider.insert_chunk(chunk)
        result = provider.get_chunk_by_id(chunk_id)
        assert result is not None
        assert result["symbol"] == "main"
        assert result["id"] == chunk_id

    def test_get_chunk_by_id_as_model(self, provider, file_id):
        chunk = _make_chunk(file_id=file_id, symbol="main")
        chunk_id = provider.insert_chunk(chunk)
        result = provider.get_chunk_by_id(chunk_id, as_model=True)
        assert isinstance(result, Chunk)
        assert result.symbol == "main"

    def test_get_chunk_by_id_missing(self, provider):
        assert provider.get_chunk_by_id(999) is None

    def test_get_chunks_by_file_id(self, provider, file_id):
        provider.insert_chunk(_make_chunk(file_id=file_id, symbol="fn_a"))
        provider.insert_chunk(_make_chunk(file_id=file_id, symbol="fn_b"))
        results = provider.get_chunks_by_file_id(file_id)
        assert len(results) == 2
        symbols = {r["symbol"] for r in results}
        assert symbols == {"fn_a", "fn_b"}

    def test_get_chunks_by_file_id_empty(self, provider):
        assert provider.get_chunks_by_file_id(999) == []

    def test_delete_file_chunks(self, provider, file_id):
        provider.insert_chunk(_make_chunk(file_id=file_id))
        provider.delete_file_chunks(file_id)
        assert provider.get_chunks_by_file_id(file_id) == []

    def test_delete_chunks_batch(self, provider, file_id):
        id1 = provider.insert_chunk(
            _make_chunk(file_id=file_id, symbol="a", start_line=1, end_line=5)
        )
        id2 = provider.insert_chunk(
            _make_chunk(file_id=file_id, symbol="b", start_line=6, end_line=10)
        )
        provider.delete_chunks_batch([id1])
        assert provider.get_chunk_by_id(id1) is None
        assert provider.get_chunk_by_id(id2) is not None

    def test_delete_chunk(self, provider, file_id):
        chunk_id = provider.insert_chunk(_make_chunk(file_id=file_id))
        provider.delete_chunk(chunk_id)
        assert provider.get_chunk_by_id(chunk_id) is None

    def test_update_chunk(self, provider, file_id):
        chunk_id = provider.insert_chunk(_make_chunk(file_id=file_id, symbol="old"))
        provider.update_chunk(chunk_id, symbol="new")
        result = provider.get_chunk_by_id(chunk_id)
        assert result["symbol"] == "new"

    def test_get_chunks_in_range(self, provider, file_id):
        provider.insert_chunk(
            _make_chunk(file_id=file_id, symbol="top", start_line=1, end_line=10)
        )
        provider.insert_chunk(
            _make_chunk(file_id=file_id, symbol="mid", start_line=11, end_line=20)
        )
        provider.insert_chunk(
            _make_chunk(file_id=file_id, symbol="bot", start_line=21, end_line=30)
        )
        # Query range 5-15 should overlap with top (1-10) and mid (11-20)
        results = provider.get_chunks_in_range(file_id, 5, 15)
        symbols = {r["symbol"] for r in results}
        assert symbols == {"top", "mid"}


# --- Embedding Operations ---


class TestEmbeddingCRUD:
    @pytest.fixture
    def provider(self):
        p = FakeDatabaseProvider()
        p.connect()
        return p

    @pytest.fixture
    def file_and_chunk(self, provider):
        file_id = provider.insert_file(_make_file("src/main.py"))
        chunk_id = provider.insert_chunk(_make_chunk(file_id=file_id))
        return file_id, chunk_id

    def test_insert_embedding_returns_id(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        emb = _make_embedding(chunk_id)
        emb_id = provider.insert_embedding(emb)
        assert emb_id == 1

    def test_get_embedding_by_chunk_id(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        provider.insert_embedding(_make_embedding(chunk_id))
        result = provider.get_embedding_by_chunk_id(chunk_id, "fake", "test-model")
        assert result is not None
        assert result.chunk_id == chunk_id

    def test_get_embedding_by_chunk_id_wrong_provider(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        provider.insert_embedding(_make_embedding(chunk_id))
        assert provider.get_embedding_by_chunk_id(chunk_id, "other", "test-model") is None

    def test_get_existing_embeddings(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        provider.insert_embedding(_make_embedding(chunk_id))
        existing = provider.get_existing_embeddings([chunk_id, 999], "fake", "test-model")
        assert existing == {chunk_id}

    def test_delete_embeddings_by_chunk_id(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        provider.insert_embedding(_make_embedding(chunk_id))
        provider.delete_embeddings_by_chunk_id(chunk_id)
        assert provider.get_embedding_by_chunk_id(chunk_id, "fake", "test-model") is None

    def test_insert_embeddings_batch(self, provider, file_and_chunk):
        _, chunk_id = file_and_chunk
        data = [
            {"chunk_id": chunk_id, "provider": "fake", "model": "m", "dims": 2, "vector": [0.1, 0.2]},
        ]
        count = provider.insert_embeddings_batch(data)
        assert count == 1


# --- Search Operations ---


class TestSearch:
    @pytest.fixture
    def provider(self):
        p = FakeDatabaseProvider()
        p.connect()
        return p

    @pytest.fixture
    def seeded_provider(self, provider):
        """Provider with 2 files, 2 chunks, 2 embeddings."""
        fid = provider.insert_file(_make_file("src/foo.py"))
        c1 = provider.insert_chunk(
            _make_chunk(file_id=fid, symbol="foo", code="def foo(): pass", file_path="src/foo.py")
        )
        c2 = provider.insert_chunk(
            _make_chunk(
                file_id=fid, symbol="bar", code="def bar(): return 42",
                start_line=11, end_line=20, file_path="src/foo.py",
            )
        )
        provider.insert_embedding(
            _make_embedding(c1, provider="p", model="m", vector=[1.0, 0.0, 0.0])
        )
        provider.insert_embedding(
            _make_embedding(c2, provider="p", model="m", vector=[0.0, 1.0, 0.0])
        )
        return provider

    def test_search_regex_finds_match(self, seeded_provider):
        results, _ = seeded_provider.search_regex(r"def foo")
        assert len(results) == 1
        assert results[0]["symbol"] == "foo"

    def test_search_regex_no_match(self, seeded_provider):
        results, _ = seeded_provider.search_regex(r"class Baz")
        assert len(results) == 0

    def test_search_text_case_insensitive(self, seeded_provider):
        results, _ = seeded_provider.search_text("DEF FOO")
        assert len(results) == 1

    def test_search_semantic_returns_scored_results(self, seeded_provider):
        # Query vector close to c1's [1,0,0]
        results, _ = seeded_provider.search_semantic(
            [1.0, 0.0, 0.0], "p", "m", page_size=10
        )
        assert len(results) == 2
        # First result should be the one with vector [1,0,0] (perfect match)
        assert results[0]["symbol"] == "foo"
        assert results[0]["score"] == pytest.approx(1.0)

    def test_search_semantic_path_filter(self, seeded_provider):
        results, _ = seeded_provider.search_semantic(
            [1.0, 0.0, 0.0], "p", "m", path_filter="lib/"
        )
        assert len(results) == 0


# --- Symbol/Edge Operations ---


class TestSymbolEdge:
    @pytest.fixture
    def provider(self):
        p = FakeDatabaseProvider()
        p.connect()
        fid = p.insert_file(_make_file("src/main.py"))
        return p, fid

    def test_insert_and_query_symbols(self, provider):
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="src.main::MyClass",
                name="MyClass",
                kind="Class",
                language="python",
                file_id=fid,
                file_path="src/main.py",
                range_start=1,
                range_end=50,
                confidence=1.0,
                lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        results = p.query_symbols_by_file(fid)
        assert len(results) == 1
        assert results[0]["fqn"] == "src.main::MyClass"

    def test_delete_symbols_by_file(self, provider):
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="src.main::f", name="f", kind="Function",
                language="python", file_id=fid, file_path="src/main.py",
                range_start=1, range_end=5, confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        p.delete_symbols_by_file(fid)
        assert p.query_symbols_by_file(fid) == []

    def test_query_symbol_fqns_by_file(self, provider):
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="src.main::a", name="a", kind="Function",
                language="python", file_id=fid, file_path="src/main.py",
                range_start=1, range_end=5, confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        fqn_map = p.query_symbol_fqns_by_file(fid)
        assert "src.main::a" in fqn_map

    def test_insert_edges_and_graph_walk(self, provider):
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="A", name="A", kind="Class", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=1, range_end=10, confidence=1.0, lsp_server="pyright",
            ),
            SymbolRow(
                fqn="B", name="B", kind="Class", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=11, range_end=20, confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=1, from_fqn="A", from_file="src/main.py",
                to_symbol_id=2, to_fqn="B", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_edges_batch(edges)
        nodes, walked_edges = p.graph_walk(["A"], depth=1, directed=True, edge_kind=None, limit=10)
        fqns = {n["fqn"] for n in nodes}
        assert "A" in fqns
        assert "B" in fqns
        assert len(walked_edges) >= 1

    def test_symbol_stats(self, provider):
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="x", name="x", kind="Variable", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=1, range_end=1, confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        stats = p.symbol_stats()
        assert stats["symbol_count"] == 1
        assert stats["edge_count"] == 0

    def test_graph_reachability_dead_code_cycle(self, provider):
        """X↔Y mutual cycle with no entry point → both unreachable."""
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn="mod::A", name="A", kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=1, range_end=5, confidence=1.0, lsp_server="pyright",
            ),
            SymbolRow(
                fqn="mod::B", name="B", kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=6, range_end=10, confidence=1.0, lsp_server="pyright",
            ),
            SymbolRow(
                fqn="mod::X", name="X", kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=11, range_end=15, confidence=1.0, lsp_server="pyright",
            ),
            SymbolRow(
                fqn="mod::Y", name="Y", kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=16, range_end=20, confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_symbols_batch(syms)
        fqn_map = p.query_symbol_fqns_by_file(fid)
        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=fqn_map["mod::A"], from_fqn="mod::A", from_file="src/main.py",
                to_symbol_id=fqn_map["mod::B"], to_fqn="mod::B", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            ),
            EdgeRow(
                from_symbol_id=fqn_map["mod::X"], from_fqn="mod::X", from_file="src/main.py",
                to_symbol_id=fqn_map["mod::Y"], to_fqn="mod::Y", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            ),
            EdgeRow(
                from_symbol_id=fqn_map["mod::Y"], from_fqn="mod::Y", from_file="src/main.py",
                to_symbol_id=fqn_map["mod::X"], to_fqn="mod::X", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            ),
        ]
        p.insert_edges_batch(edges)
        result = p.graph_reachability("src/main")
        unreachable = {s["fqn"] for s in result}
        assert unreachable == {"mod::X", "mod::Y"}

    def test_graph_reachability_transitive(self, provider):
        """A→B→C→D, all reachable from entry point A → no unreachable."""
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn=f"mod::{n}", name=n, kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=i * 10, range_end=i * 10 + 5, confidence=1.0, lsp_server="pyright",
            )
            for i, n in enumerate(["A", "B", "C", "D"])
        ]
        p.insert_symbols_batch(syms)
        fqn_map = p.query_symbol_fqns_by_file(fid)
        chain = [("A", "B"), ("B", "C"), ("C", "D")]
        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=fqn_map[f"mod::{a}"], from_fqn=f"mod::{a}", from_file="src/main.py",
                to_symbol_id=fqn_map[f"mod::{b}"], to_fqn=f"mod::{b}", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            )
            for a, b in chain
        ]
        p.insert_edges_batch(edges)
        result = p.graph_reachability("src/main")
        assert result == []

    def test_graph_reachability_full_cycle_all_unreachable(self, provider):
        """P→Q→R→P: every symbol has inbound → no entry points → all unreachable."""
        p, fid = provider
        syms: list[SymbolRow] = [
            SymbolRow(
                fqn=f"mod::{n}", name=n, kind="Function", language="python",
                file_id=fid, file_path="src/main.py",
                range_start=i * 10, range_end=i * 10 + 5, confidence=1.0, lsp_server="pyright",
            )
            for i, n in enumerate(["P", "Q", "R"])
        ]
        p.insert_symbols_batch(syms)
        fqn_map = p.query_symbol_fqns_by_file(fid)
        cycle = [("P", "Q"), ("Q", "R"), ("R", "P")]
        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=fqn_map[f"mod::{a}"], from_fqn=f"mod::{a}", from_file="src/main.py",
                to_symbol_id=fqn_map[f"mod::{b}"], to_fqn=f"mod::{b}", to_file="src/main.py",
                edge_kind="calls", confidence=1.0, lsp_server="pyright",
            )
            for a, b in cycle
        ]
        p.insert_edges_batch(edges)
        result = p.graph_reachability("src/main")
        unreachable = {s["fqn"] for s in result}
        assert unreachable == {"mod::P", "mod::Q", "mod::R"}


# --- Stats ---


class TestStats:
    def test_get_stats(self):
        p = FakeDatabaseProvider()
        p.connect()
        fid = p.insert_file(_make_file())
        p.insert_chunk(_make_chunk(file_id=fid))
        stats = p.get_stats()
        assert stats["total_files"] == 1
        assert stats["total_chunks"] == 1
        assert stats["total_embeddings"] == 0
