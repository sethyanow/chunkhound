"""Integration tests for DuckDB provider symbol/edge CRUD protocol methods.

Tests that DuckDBProvider correctly implements the symbol/edge protocol
methods declared on DatabaseProvider.
"""

from pathlib import Path

import pytest

from chunkhound.core.models.symbol import EdgeRow, SymbolRow
from chunkhound.providers.database.duckdb_provider import DuckDBProvider

pytestmark = pytest.mark.integration


def _connect_fresh(tmp_path: Path) -> DuckDBProvider:
    """Create and connect a DuckDBProvider to a fresh temp DB."""
    provider = DuckDBProvider(db_path=tmp_path / "db", base_directory=tmp_path)
    provider.connect()
    return provider


def _insert_test_file(provider: DuckDBProvider) -> int:
    """Insert a test file and return its file_id."""
    provider.execute_query(
        "INSERT INTO files (path, name, extension, language, size) VALUES (?, ?, ?, ?, ?)",
        ["src/example.py", "example.py", ".py", "python", 200],
    )
    rows = provider.execute_query("SELECT id FROM files WHERE path = ?", ["src/example.py"])
    return rows[0]["id"]


class TestDuckDBInsertSymbolsBatch:
    """insert_symbols_batch must batch-insert symbol rows."""

    def test_insert_and_query_roundtrip(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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

        provider.insert_symbols_batch(symbols)
        result = provider.query_symbols_by_file(file_id)

        assert len(result) == 2
        fqns = {r["fqn"] for r in result}
        assert fqns == {"example::MyClass", "example::MyClass::method"}
        provider.disconnect()

    def test_insert_empty_batch_is_noop(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        provider.insert_symbols_batch([])  # Should not raise
        provider.disconnect()

    def test_large_batch_chunking(self, tmp_path: Path) -> None:
        """Batch insert >500 symbols must succeed (internal chunking)."""
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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

        provider.insert_symbols_batch(symbols)
        result = provider.query_symbols_by_file(file_id)
        assert len(result) == 600
        provider.disconnect()


class TestDuckDBDeleteSymbolsByFile:
    """delete_symbols_by_file must remove all symbols for a file."""

    def test_delete_clears_file_symbols(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)
        assert len(provider.query_symbols_by_file(file_id)) == 1

        provider.delete_symbols_by_file(file_id)
        assert len(provider.query_symbols_by_file(file_id)) == 0
        provider.disconnect()


class TestDuckDBDeleteEdgesByFile:
    """delete_edges_by_file must remove edges referencing a file's symbols."""

    def test_delete_clears_file_edges(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)

        # Get symbol IDs
        fqn_map = provider.query_symbol_fqns_by_file(file_id)

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
        provider.insert_edges_batch(edges)

        # Verify edge exists
        edge_rows = provider.execute_query("SELECT * FROM symbol_edges", [])
        assert len(edge_rows) == 1

        provider.delete_edges_by_file(file_id)

        edge_rows = provider.execute_query("SELECT * FROM symbol_edges", [])
        assert len(edge_rows) == 0
        provider.disconnect()


class TestDuckDBQuerySymbolsByRange:
    """query_symbols_by_range returns the innermost symbol at a line."""

    def test_returns_innermost_symbol(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)

        result = provider.query_symbols_by_range("src/example.py", 10)
        assert result is not None
        assert result["fqn"] == "example::MyClass::method"  # innermost

        provider.disconnect()

    def test_returns_none_for_no_match(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        _insert_test_file(provider)

        result = provider.query_symbols_by_range("src/example.py", 999)
        assert result is None
        provider.disconnect()


class TestDuckDBQuerySymbolsByRangeOverlap:
    """query_symbols_by_range_overlap returns all overlapping symbols."""

    def test_returns_overlapping_symbols(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)

        result = provider.query_symbols_by_range_overlap("src/example.py", 5, 15)
        fqns = {r["fqn"] for r in result}
        # func_a (0-10) and func_b (8-20) overlap [5,15]; func_c (30-40) does not
        assert fqns == {"example::func_a", "example::func_b"}
        provider.disconnect()


class TestDuckDBQuerySymbolFqnsByFile:
    """query_symbol_fqns_by_file returns fqn→id mapping."""

    def test_returns_fqn_to_id_mapping(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)

        fqn_map = provider.query_symbol_fqns_by_file(file_id)
        assert "example::alpha" in fqn_map
        assert isinstance(fqn_map["example::alpha"], int)
        provider.disconnect()


class TestDuckDBQuerySymbolsByFqnExists:
    """query_symbols_by_fqn_exists checks for symbol existence."""

    def test_returns_true_when_exists(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        file_id = _insert_test_file(provider)

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
        provider.insert_symbols_batch(symbols)

        assert provider.query_symbols_by_fqn_exists("example::exists", "src/example.py") is True
        assert provider.query_symbols_by_fqn_exists("example::nope", "src/example.py") is False
        provider.disconnect()


class TestDuckDBSymbolReadQueries:
    """Symbol read query methods used by fusion.py and search.py helpers.

    These methods were declared in the protocol in Steps 1-4 but were not
    implemented on DuckDBProvider in Steps 5-8. ch-nxu Step 13 surfaced the
    gap when lsp_population.py was retyped against the protocol.
    """

    def _seed(self, provider: DuckDBProvider) -> int:
        file_id = _insert_test_file(provider)
        symbols: list[SymbolRow] = [
            SymbolRow(
                fqn="example::Cls",
                name="Cls",
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
                fqn="example::Cls::method",
                name="method",
                kind="Method",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=5,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn="example::Cls",
                type_signature="(self) -> int",
            ),
            SymbolRow(
                fqn="example::test_thing",
                name="test_thing",
                kind="Function",
                language="python",
                file_id=file_id,
                file_path="src/example.py",
                range_start=60,
                range_end=70,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature="() -> None",
            ),
        ]
        provider.insert_symbols_batch(symbols)
        return file_id

    def test_query_symbols_by_scope_returns_prefix_matches(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            results = provider.query_symbols_by_scope("src/")
            assert len(results) == 3
            assert {r["fqn"] for r in results} == {
                "example::Cls",
                "example::Cls::method",
                "example::test_thing",
            }
        finally:
            provider.disconnect()

    def test_query_symbols_by_scope_excludes_non_matches(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            assert provider.query_symbols_by_scope("other/") == []
        finally:
            provider.disconnect()

    def test_query_test_symbols_filters_test_functions(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            results = provider.query_test_symbols(scope=None)
            assert len(results) == 1
            assert results[0]["fqn"] == "example::test_thing"
            # Method named "method" is not a test function — must be excluded
            assert all(r["name"].startswith("test_") for r in results)
        finally:
            provider.disconnect()

    def test_query_test_symbols_with_scope_restricts(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            results = provider.query_test_symbols(scope="src/")
            assert len(results) == 1
            assert provider.query_test_symbols(scope="other/") == []
        finally:
            provider.disconnect()

    def test_query_symbol_type_signatures_batch_lookup(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            result = provider.query_symbol_type_signatures(
                ["example::Cls::method", "example::test_thing", "example::missing"]
            )
            assert result["example::Cls::method"] == "(self) -> int"
            assert result["example::test_thing"] == "() -> None"
            # Missing FQN must be absent from the result, not a None entry.
            # Caller distinguishes "no signature collected" (None) from
            # "symbol not found" (missing key).
            assert "example::missing" not in result
        finally:
            provider.disconnect()

    def test_query_symbol_type_signatures_empty_input(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            assert provider.query_symbol_type_signatures([]) == {}
        finally:
            provider.disconnect()

    def test_query_distinct_fqns_by_file_path(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            fqns = provider.query_distinct_fqns_by_file_path("src/example.py")
            assert set(fqns) == {
                "example::Cls",
                "example::Cls::method",
                "example::test_thing",
            }
            assert provider.query_distinct_fqns_by_file_path("src/missing.py") == []
        finally:
            provider.disconnect()


class TestDuckDBFusionContractFields:
    """Strong field-contract assertions for the 6 methods fusion.py calls.

    Before ch-nxu Step 15 migrates fusion.py helpers off raw execute_query,
    verify each provider method returns every field the helpers dereference.
    Pattern from memory reference_strengthen_provider_tests_before_migration:
    run identical assertions against BOTH backends — cross-backend divergence
    surfaces as a test passing on one and failing on the other.
    """

    def _seed(self, provider: DuckDBProvider) -> int:
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

    def test_range_overlap_returns_all_fusion_fields(self, tmp_path: Path) -> None:
        """fusion._map_lines_to_symbols reads fqn, name, kind, file_path,
        type_signature, range_start, range_end on each row."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows = provider.query_symbols_by_range_overlap("src/example.py", 12, 20)
            assert len(rows) >= 1
            hit = next(r for r in rows if r["fqn"] == "example::Outer::compute")
            assert hit["name"] == "compute"
            assert hit["kind"] == "Method"
            assert hit["file_path"] == "src/example.py"
            assert hit["type_signature"] == "(self, x: int) -> int"
            assert int(hit["range_start"]) == 10
            assert int(hit["range_end"]) == 25
        finally:
            provider.disconnect()

    def test_scope_returns_all_fusion_fields(self, tmp_path: Path) -> None:
        """fusion._query_scope_symbols reads name, fqn, kind, language,
        file_path, type_signature on each row."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows = provider.query_symbols_by_scope("src/")
            hit = next(r for r in rows if r["fqn"] == "example::Outer::compute")
            assert hit["name"] == "compute"
            assert hit["kind"] == "Method"
            assert hit["language"] == "python"
            assert hit["file_path"] == "src/example.py"
            assert hit["type_signature"] == "(self, x: int) -> int"
        finally:
            provider.disconnect()

    def test_test_symbols_returns_fqn_name_file_path(self, tmp_path: Path) -> None:
        """fusion._collect_test_fqns reads fqn, name, file_path on each row."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows = provider.query_test_symbols(scope=None)
            assert len(rows) == 1
            row = rows[0]
            assert row["fqn"] == "example::test_compute"
            assert row["name"] == "test_compute"
            assert row["file_path"] == "src/example.py"
        finally:
            provider.disconnect()

    def test_range_returns_fqn_for_innermost(self, tmp_path: Path) -> None:
        """fusion._resolve_start_fqn reads row['fqn'] on the innermost match."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            row = provider.query_symbols_by_range("src/example.py", 15)
            assert row is not None
            assert row["fqn"] == "example::Outer::compute"  # innermost at line 15
        finally:
            provider.disconnect()

    def test_type_signatures_value_maps_fqn_to_signature(self, tmp_path: Path) -> None:
        """fusion._annotate_type_signatures consumes the dict[str, str|None]
        directly and calls .get(fqn) on it."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            result = provider.query_symbol_type_signatures(
                ["example::Outer::compute", "example::Outer", "example::missing"]
            )
            assert result["example::Outer::compute"] == "(self, x: int) -> int"
            # Outer has type_signature=None → present with value None
            assert result.get("example::Outer") is None
            # Missing FQN absent from dict
            assert "example::missing" not in result
        finally:
            provider.disconnect()

    def test_distinct_fqns_returns_list_of_strings(self, tmp_path: Path) -> None:
        """fusion._resolve_changed_to_fqns iterates list[str] directly."""
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            fqns = provider.query_distinct_fqns_by_file_path("src/example.py")
            assert isinstance(fqns, list)
            assert all(isinstance(f, str) for f in fqns)
            assert set(fqns) == {
                "example::Outer",
                "example::Outer::compute",
                "example::test_compute",
            }
        finally:
            provider.disconnect()


class TestDuckDBSearchSymbolsContract:
    """Field-contract tests for ch-nxu Step 16 protocol methods used by search.py.

    search.py reads: fqn, name, kind, language, file_path, range_start,
    range_end, type_signature. Mirror on LanceDB in
    TestLanceDBSearchSymbolsContract — cross-backend divergence surfaces
    as one backend passing and the other failing.
    """

    def _seed(self, provider: DuckDBProvider) -> int:
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

    def test_search_returns_rows_with_all_fields_and_total(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows, total = provider.search_symbols(
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
        finally:
            provider.disconnect()

    def test_search_ordered_by_name_ascending(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows, _ = provider.search_symbols(
                query="parse", path=None, type_filter=None, limit=10, offset=0
            )
            assert [r["name"] for r in rows] == ["parse_args", "parse_config"]
        finally:
            provider.disconnect()

    def test_search_path_filter_restricts_results(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows, total = provider.search_symbols(
                query="", path="src/other", type_filter=None, limit=10, offset=0
            )
            assert total == 1
            assert rows[0]["fqn"] == "example::helper"
        finally:
            provider.disconnect()

    def test_search_type_filter_restricts_results(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows, total = provider.search_symbols(
                query="", path=None, type_filter="Namespace", limit=10, offset=0
            )
            assert total == 1
            assert rows[0]["fqn"] == "example::parse_args"
        finally:
            provider.disconnect()

    def test_search_pagination_limit_and_offset(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            page1, total1 = provider.search_symbols(
                query="", path=None, type_filter=None, limit=2, offset=0
            )
            page2, total2 = provider.search_symbols(
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
        finally:
            provider.disconnect()

    def test_search_empty_query_matches_all(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            rows, total = provider.search_symbols(
                query="", path=None, type_filter=None, limit=100, offset=0
            )
            assert total == 3
            assert len(rows) == 3
        finally:
            provider.disconnect()


class TestDuckDBFilterChunksByTypeSignature:
    """Field-contract tests for filter_chunks_by_symbol_type_signature.

    search._apply_type_filter passes a list of chunk dicts (file_path,
    start_line, end_line) and a type_filter substring. The provider must
    return only chunks whose file has a symbol with matching type_signature
    whose range overlaps the chunk range.
    """

    def _seed(self, provider: DuckDBProvider) -> int:
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

    def test_keeps_chunks_with_matching_type(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            chunks = [
                {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "a"},
                {"file_path": "src/example.py", "start_line": 20, "end_line": 30, "code": "b"},
            ]
            kept = provider.filter_chunks_by_symbol_type_signature(chunks, "int")
            assert len(kept) == 1
            assert kept[0]["code"] == "a"
        finally:
            provider.disconnect()

    def test_empty_chunks_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            assert provider.filter_chunks_by_symbol_type_signature([], "int") == []
        finally:
            provider.disconnect()

    def test_no_matching_symbol_returns_empty(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            chunks = [
                {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "a"},
            ]
            kept = provider.filter_chunks_by_symbol_type_signature(chunks, "Decimal")
            assert kept == []
        finally:
            provider.disconnect()

    def test_preserves_input_order(self, tmp_path: Path) -> None:
        provider = _connect_fresh(tmp_path)
        try:
            self._seed(provider)
            # Both chunks match the "int" or "str" filter via different symbols.
            chunks = [
                {"file_path": "src/example.py", "start_line": 20, "end_line": 30, "code": "second"},
                {"file_path": "src/example.py", "start_line": 5, "end_line": 15, "code": "first"},
            ]
            kept = provider.filter_chunks_by_symbol_type_signature(chunks, "")
            # Empty filter matches any non-NULL type_signature; both kept.
            assert [c["code"] for c in kept] == ["second", "first"]
        finally:
            provider.disconnect()
