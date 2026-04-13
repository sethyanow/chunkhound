"""Integration tests for LanceDB provider symbol/edge CRUD protocol methods.

Tests that LanceDBProvider correctly implements the symbol/edge protocol
methods declared on DatabaseProvider — same contract as DuckDB tests.
"""

import pytest

from chunkhound.core.models.symbol import EdgeRow, SymbolRow

pytestmark = pytest.mark.integration


def _insert_test_file(provider) -> int:
    """Insert a test file and return its file_id."""
    from chunkhound.core.models import File
    from chunkhound.core.types.common import Language

    test_file = File(
        path="src/example.py",
        mtime=1234567890.0,
        language=Language.PYTHON,
        size_bytes=200,
    )
    return provider.insert_file(test_file)


class TestLanceDBInsertSymbolsBatch:
    """insert_symbols_batch must batch-insert symbol rows."""

    def test_insert_and_query_roundtrip(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::MyClass",
                name="MyClass",
                kind="Class",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=50,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::MyClass::method",
                name="method",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::MyClass",
                type_signature="(self) -> None",
            ),
        ]

        lancedb_provider.insert_symbols_batch(symbols)
        result = lancedb_provider.query_symbols_by_file(file_id)

        assert len(result) == 2
        fqns = {r["fqn"] for r in result}
        assert fqns == {"example::MyClass", "example::MyClass::method"}

    def test_insert_empty_batch_is_noop(self, lancedb_provider) -> None:
        lancedb_provider.insert_symbols_batch([])  # Should not raise

    def test_large_batch_chunking(self, lancedb_provider) -> None:
        """Batch insert >500 symbols must succeed (internal chunking)."""
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn=f"example::func_{i}",
                name=f"func_{i}",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=i * 10,
                range_end=i * 10 + 9,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            )
            for i in range(600)
        ]

        lancedb_provider.insert_symbols_batch(symbols)
        result = lancedb_provider.query_symbols_by_file(file_id)
        assert len(result) == 600


class TestLanceDBDeleteSymbolsByFile:
    """delete_symbols_by_file must remove all symbols for a file."""

    def test_delete_clears_file_symbols(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::func",
                name="func",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)
        assert len(lancedb_provider.query_symbols_by_file(file_id)) == 1

        lancedb_provider.delete_symbols_by_file(file_id)
        assert len(lancedb_provider.query_symbols_by_file(file_id)) == 0


class TestLanceDBDeleteEdgesByFile:
    """delete_edges_by_file must remove edges referencing a file's symbols."""

    def test_delete_clears_file_edges(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        # Insert two symbols
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::caller",
                name="caller",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::callee",
                name="callee",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=15,
                range_end=25,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        # Get symbol IDs
        fqn_map = lancedb_provider.query_symbol_fqns_by_file(file_id)

        edges: list[EdgeRow] = [
            EdgeRow(
                from_symbol_id=fqn_map["example::caller"],
                from_fqn="example::caller",
                from_file="src/example.py",
                to_symbol_id=fqn_map["example::callee"],
                to_fqn="example::callee",
                to_file="src/example.py",
                edge_kind="calls",
                confidence=1.0,
                lsp_server="pyright",
            ),
        ]
        lancedb_provider.insert_edges_batch(edges)

        # Verify edge exists via symbol_stats
        stats = lancedb_provider.symbol_stats()
        assert stats["edge_count"] >= 1

        lancedb_provider.delete_edges_by_file(file_id)

        stats_after = lancedb_provider.symbol_stats()
        assert stats_after["edge_count"] == 0


class TestLanceDBQuerySymbolsByRange:
    """query_symbols_by_range returns the innermost symbol at a line."""

    def test_returns_innermost_symbol(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::MyClass",
                name="MyClass",
                kind="Class",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=50,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::MyClass::method",
                name="method",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::MyClass",
                type_signature="(self) -> None",
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        result = lancedb_provider.query_symbols_by_range("src/example.py", 10)
        assert result is not None
        assert result["fqn"] == "example::MyClass::method"  # innermost

    def test_returns_none_for_no_match(self, lancedb_provider) -> None:
        _insert_test_file(lancedb_provider)

        result = lancedb_provider.query_symbols_by_range("src/example.py", 999)
        assert result is None


class TestLanceDBQuerySymbolsByRangeOverlap:
    """query_symbols_by_range_overlap returns all overlapping symbols."""

    def test_returns_overlapping_symbols(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::func_a",
                name="func_a",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::func_b",
                name="func_b",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=8,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::func_c",
                name="func_c",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=30,
                range_end=40,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        result = lancedb_provider.query_symbols_by_range_overlap("src/example.py", 5, 15)
        fqns = {r["fqn"] for r in result}
        # func_a (0-10) and func_b (8-20) overlap [5,15]; func_c (30-40) does not
        assert fqns == {"example::func_a", "example::func_b"}


class TestLanceDBQuerySymbolFqnsByFile:
    """query_symbol_fqns_by_file returns fqn→id mapping."""

    def test_returns_fqn_to_id_mapping(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::alpha",
                name="alpha",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        fqn_map = lancedb_provider.query_symbol_fqns_by_file(file_id)
        assert "example::alpha" in fqn_map
        assert isinstance(fqn_map["example::alpha"], int)


class TestLanceDBQuerySymbolsByFqnExists:
    """query_symbols_by_fqn_exists checks for symbol existence."""

    def test_returns_true_when_exists(self, lancedb_provider) -> None:
        file_id = _insert_test_file(lancedb_provider)

        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::exists",
                name="exists",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=5,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        lancedb_provider.insert_symbols_batch(symbols)

        assert lancedb_provider.query_symbols_by_fqn_exists("example::exists", "src/example.py") is True
        assert lancedb_provider.query_symbols_by_fqn_exists("example::nope", "src/example.py") is False


class TestLanceDBFusionContractFields:
    """Strong field-contract assertions for the 6 methods fusion.py calls.

    Mirror of TestDuckDBFusionContractFields. Before ch-nxu Step 15 migrates
    fusion.py helpers off raw execute_query, verify LanceDB returns every
    field fusion.py dereferences. Cross-backend divergence surfaces as one
    backend passing and the other failing on the same assertion.
    """

    def _seed(self, provider) -> int:
        file_id = _insert_test_file(provider)
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::Outer",
                name="Outer",
                kind="Class",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=0,
                range_end=100,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
            SymbolRow(
                fqn="example::Outer::compute",
                name="compute",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=10,
                range_end=25,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::Outer",
                type_signature="(self, x: int) -> int",
            ),
            SymbolRow(
                fqn="example::test_compute",
                name="test_compute",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=40,
                range_end=55,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="() -> None",
            ),
        ]
        provider.insert_symbols_batch(symbols)
        return file_id

    def test_range_overlap_returns_all_fusion_fields(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows = lancedb_provider.query_symbols_by_range_overlap("src/example.py", 12, 20)
        assert len(rows) >= 1
        hit = next(r for r in rows if r["fqn"] == "example::Outer::compute")
        assert hit["name"] == "compute"
        assert hit["kind"] == "Method"
        assert hit["file_path"] == "src/example.py"
        assert hit["type_signature"] == "(self, x: int) -> int"
        assert int(hit["range_start"]) == 10
        assert int(hit["range_end"]) == 25

    def test_scope_returns_all_fusion_fields(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows = lancedb_provider.query_symbols_by_scope("src/")
        hit = next(r for r in rows if r["fqn"] == "example::Outer::compute")
        assert hit["name"] == "compute"
        assert hit["kind"] == "Method"
        assert hit["language"] == "python"
        assert hit["file_path"] == "src/example.py"
        assert hit["type_signature"] == "(self, x: int) -> int"

    def test_scope_excludes_non_matches(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        assert lancedb_provider.query_symbols_by_scope("other/") == []

    def test_test_symbols_returns_fqn_name_file_path(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows = lancedb_provider.query_test_symbols(scope=None)
        assert len(rows) == 1
        row = rows[0]
        assert row["fqn"] == "example::test_compute"
        assert row["name"] == "test_compute"
        assert row["file_path"] == "src/example.py"

    def test_test_symbols_with_scope_restricts(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        assert len(lancedb_provider.query_test_symbols(scope="src/")) == 1
        assert lancedb_provider.query_test_symbols(scope="other/") == []

    def test_range_returns_fqn_for_innermost(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        row = lancedb_provider.query_symbols_by_range("src/example.py", 15)
        assert row is not None
        assert row["fqn"] == "example::Outer::compute"  # innermost at line 15

    def test_type_signatures_value_maps_fqn_to_signature(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        result = lancedb_provider.query_symbol_type_signatures(
            ["example::Outer::compute", "example::Outer", "example::missing"]
        )
        assert result["example::Outer::compute"] == "(self, x: int) -> int"
        # Outer has type_signature=None → present with value None
        assert result.get("example::Outer") is None
        # Missing FQN absent from dict
        assert "example::missing" not in result

    def test_type_signatures_empty_input(self, lancedb_provider) -> None:
        assert lancedb_provider.query_symbol_type_signatures([]) == {}

    def test_distinct_fqns_returns_list_of_strings(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        fqns = lancedb_provider.query_distinct_fqns_by_file_path("src/example.py")
        assert isinstance(fqns, list)
        assert all(isinstance(f, str) for f in fqns)
        assert set(fqns) == {
            "example::Outer",
            "example::Outer::compute",
            "example::test_compute",
        }

    def test_distinct_fqns_empty_for_missing_file(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        assert lancedb_provider.query_distinct_fqns_by_file_path("src/missing.py") == []


class TestLanceDBSearchSymbolsContract:
    """Mirror of TestDuckDBSearchSymbolsContract — same field contract on LanceDB.

    Cross-backend divergence surfaces as one backend passing and the
    other failing on identical assertions.
    """

    def _seed(self, provider) -> int:
        file_id = _insert_test_file(provider)
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::parse_config",
                name="parse_config",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=10,
                range_end=25,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="(path: str) -> dict[str, Any]",
            ),
            SymbolRow(
                fqn="example::parse_args",
                name="parse_args",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=30,
                range_end=45,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="(argv: list[str]) -> Namespace",
            ),
            SymbolRow(
                fqn="example::helper",
                name="helper",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/other.py",
                range_start=0,
                range_end=10,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="() -> None",
            ),
        ]
        provider.insert_symbols_batch(symbols)
        return file_id

    def test_search_returns_rows_with_all_fields_and_total(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows, total = lancedb_provider.search_symbols(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        assert total == 2
        assert len(rows) == 2
        hit = next(r for r in rows if r["fqn"] == "example::parse_config")
        assert hit["name"] == "parse_config"
        assert hit["kind"] == "Function"
        assert hit["language"] == "python"
        assert hit["file_path"] == "src/example.py"
        assert int(hit["range_start"]) == 10
        assert int(hit["range_end"]) == 25
        assert hit["type_signature"] == "(path: str) -> dict[str, Any]"

    def test_search_ordered_by_name_ascending(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows, _ = lancedb_provider.search_symbols(
            query="parse", path=None, type_filter=None, limit=10, offset=0
        )
        assert [r["name"] for r in rows] == ["parse_args", "parse_config"]

    def test_search_path_filter_restricts_results(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows, total = lancedb_provider.search_symbols(
            query="", path="src/other", type_filter=None, limit=10, offset=0
        )
        assert total == 1
        assert rows[0]["fqn"] == "example::helper"

    def test_search_type_filter_restricts_results(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows, total = lancedb_provider.search_symbols(
            query="", path=None, type_filter="Namespace", limit=10, offset=0
        )
        assert total == 1
        assert rows[0]["fqn"] == "example::parse_args"

    def test_search_pagination_limit_and_offset(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        page1, total1 = lancedb_provider.search_symbols(
            query="", path=None, type_filter=None, limit=2, offset=0
        )
        page2, total2 = lancedb_provider.search_symbols(
            query="", path=None, type_filter=None, limit=2, offset=2
        )
        assert total1 == 3 == total2
        assert len(page1) == 2
        assert len(page2) == 1
        assert {r["fqn"] for r in page1} | {r["fqn"] for r in page2} == {
            "example::parse_config",
            "example::parse_args",
            "example::helper",
        }

    def test_search_empty_query_matches_all(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        rows, total = lancedb_provider.search_symbols(
            query="", path=None, type_filter=None, limit=100, offset=0
        )
        assert total == 3
        assert len(rows) == 3


class TestLanceDBFilterChunksByTypeSignature:
    """Mirror of TestDuckDBFilterChunksByTypeSignature on LanceDB."""

    def _seed(self, provider) -> int:
        file_id = _insert_test_file(provider)
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::returns_int",
                name="returns_int",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=15,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="(x: int) -> int",
            ),
            SymbolRow(
                fqn="example::returns_str",
                name="returns_str",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=20,
                range_end=30,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="() -> str",
            ),
        ]
        provider.insert_symbols_batch(symbols)
        return file_id

    def test_keeps_chunks_with_matching_type(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        chunks = [
            {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "a"},
            {"file_path": "src/example.py", "start_line": 20, "end_line": 30, "code": "b"},
        ]
        kept = lancedb_provider.filter_chunks_by_symbol_type_signature(chunks, "int")
        assert len(kept) == 1
        assert kept[0]["code"] == "a"

    def test_empty_chunks_returns_empty(self, lancedb_provider) -> None:
        assert lancedb_provider.filter_chunks_by_symbol_type_signature([], "int") == []

    def test_no_matching_symbol_returns_empty(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        chunks = [
            {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "a"},
        ]
        kept = lancedb_provider.filter_chunks_by_symbol_type_signature(chunks, "Decimal")
        assert kept == []

    def test_preserves_input_order(self, lancedb_provider) -> None:
        self._seed(lancedb_provider)
        chunks = [
            {"file_path": "src/example.py", "start_line": 20, "end_line": 30, "code": "second"},
            {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "first"},
        ]
        kept = lancedb_provider.filter_chunks_by_symbol_type_signature(chunks, "")
        assert [c["code"] for c in kept] == ["second", "first"]
