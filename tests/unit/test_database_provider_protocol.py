"""Tests for DatabaseProvider protocol — symbol/edge/graph method declarations.

Verifies the protocol declares the methods needed to abstract the graph layer
behind DatabaseProvider, so both DuckDB and LanceDB can implement them.
"""

import inspect
from typing import get_type_hints

import pytest

from chunkhound.interfaces.database_provider import DatabaseProvider


@pytest.mark.unit
class TestProtocolSymbolCRUDMethods:
    """Protocol must declare symbol/edge CRUD methods."""

    def test_insert_symbols_batch_exists(self) -> None:
        assert hasattr(DatabaseProvider, "insert_symbols_batch"), (
            "DatabaseProvider must declare insert_symbols_batch"
        )
        sig = inspect.signature(DatabaseProvider.insert_symbols_batch)
        params = list(sig.parameters.keys())
        assert "symbols" in params, "insert_symbols_batch must accept 'symbols' param"

    def test_delete_symbols_by_file_exists(self) -> None:
        assert hasattr(DatabaseProvider, "delete_symbols_by_file"), (
            "DatabaseProvider must declare delete_symbols_by_file"
        )
        sig = inspect.signature(DatabaseProvider.delete_symbols_by_file)
        params = list(sig.parameters.keys())
        assert "file_id" in params

    def test_delete_edges_by_file_exists(self) -> None:
        assert hasattr(DatabaseProvider, "delete_edges_by_file"), (
            "DatabaseProvider must declare delete_edges_by_file"
        )
        sig = inspect.signature(DatabaseProvider.delete_edges_by_file)
        params = list(sig.parameters.keys())
        assert "file_id" in params

    def test_query_symbols_by_file_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbols_by_file"), (
            "DatabaseProvider must declare query_symbols_by_file"
        )
        sig = inspect.signature(DatabaseProvider.query_symbols_by_file)
        params = list(sig.parameters.keys())
        assert "file_id" in params

    def test_query_symbols_by_range_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbols_by_range"), (
            "DatabaseProvider must declare query_symbols_by_range"
        )
        sig = inspect.signature(DatabaseProvider.query_symbols_by_range)
        params = list(sig.parameters.keys())
        assert "file_path" in params
        assert "line" in params

    def test_query_symbols_by_range_overlap_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbols_by_range_overlap"), (
            "DatabaseProvider must declare query_symbols_by_range_overlap"
        )
        sig = inspect.signature(DatabaseProvider.query_symbols_by_range_overlap)
        params = list(sig.parameters.keys())
        assert "file_path" in params
        assert "min_line" in params
        assert "max_line" in params

    def test_query_symbol_fqns_by_file_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbol_fqns_by_file"), (
            "DatabaseProvider must declare query_symbol_fqns_by_file"
        )
        sig = inspect.signature(DatabaseProvider.query_symbol_fqns_by_file)
        params = list(sig.parameters.keys())
        assert "file_id" in params

    def test_query_symbols_by_fqn_exists_method(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbols_by_fqn_exists"), (
            "DatabaseProvider must declare query_symbols_by_fqn_exists"
        )
        sig = inspect.signature(DatabaseProvider.query_symbols_by_fqn_exists)
        params = list(sig.parameters.keys())
        assert "fqn" in params
        assert "file_path" in params

    def test_insert_edges_batch_exists(self) -> None:
        assert hasattr(DatabaseProvider, "insert_edges_batch"), (
            "DatabaseProvider must declare insert_edges_batch"
        )
        sig = inspect.signature(DatabaseProvider.insert_edges_batch)
        params = list(sig.parameters.keys())
        assert "edges" in params

    def test_symbol_row_type_importable(self) -> None:
        """SymbolRow type must be importable from core models."""
        from chunkhound.core.models.symbol import SymbolRow
        # Verify it's a TypedDict with expected keys
        hints = get_type_hints(SymbolRow)
        assert "fqn" in hints
        assert "name" in hints
        assert "kind" in hints
        assert "file_id" in hints
        assert "file_path" in hints
        assert "range_start" in hints
        assert "range_end" in hints

    def test_edge_row_type_importable(self) -> None:
        """EdgeRow type must be importable from core models."""
        from chunkhound.core.models.symbol import EdgeRow
        hints = get_type_hints(EdgeRow)
        assert "from_symbol_id" in hints
        assert "from_fqn" in hints
        assert "to_symbol_id" in hints
        assert "to_fqn" in hints
        assert "edge_kind" in hints

    def test_async_variants_exist(self) -> None:
        """Protocol must declare async variants for symbol CRUD methods."""
        async_methods = [
            "insert_symbols_batch_async",
            "delete_symbols_by_file_async",
            "delete_edges_by_file_async",
            "query_symbols_by_file_async",
            "query_symbol_fqns_by_file_async",
            "insert_edges_batch_async",
        ]
        for method_name in async_methods:
            assert hasattr(DatabaseProvider, method_name), (
                f"DatabaseProvider must declare {method_name}"
            )
            method = getattr(DatabaseProvider, method_name)
            assert inspect.iscoroutinefunction(method), (
                f"{method_name} must be async"
            )


@pytest.mark.unit
class TestProtocolGraphQueryMethods:
    """Protocol must declare graph walk/query methods."""

    def test_graph_walk_exists(self) -> None:
        assert hasattr(DatabaseProvider, "graph_walk")
        sig = inspect.signature(DatabaseProvider.graph_walk)
        params = list(sig.parameters.keys())
        assert "seed_fqns" in params
        assert "depth" in params
        assert "limit" in params

    def test_graph_reachability_exists(self) -> None:
        assert hasattr(DatabaseProvider, "graph_reachability")
        sig = inspect.signature(DatabaseProvider.graph_reachability)
        assert "scope" in sig.parameters

    def test_graph_boundary_exists(self) -> None:
        assert hasattr(DatabaseProvider, "graph_boundary")
        sig = inspect.signature(DatabaseProvider.graph_boundary)
        assert "scope" in sig.parameters
        assert "limit" in sig.parameters

    def test_graph_overview_exists(self) -> None:
        assert hasattr(DatabaseProvider, "graph_overview")
        sig = inspect.signature(DatabaseProvider.graph_overview)
        assert "limit" in sig.parameters

    def test_symbol_overlap_exists(self) -> None:
        assert hasattr(DatabaseProvider, "symbol_overlap")
        sig = inspect.signature(DatabaseProvider.symbol_overlap)
        assert "chunks" in sig.parameters

    def test_chunk_resolution_exists(self) -> None:
        assert hasattr(DatabaseProvider, "chunk_resolution")
        sig = inspect.signature(DatabaseProvider.chunk_resolution)
        assert "fqns" in sig.parameters

    def test_symbol_stats_exists(self) -> None:
        assert hasattr(DatabaseProvider, "symbol_stats")


@pytest.mark.unit
class TestProtocolSymbolReadQueryMethods:
    """Protocol must declare symbol read query methods for fusion/search callers."""

    def test_query_symbols_by_scope_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbols_by_scope")
        sig = inspect.signature(DatabaseProvider.query_symbols_by_scope)
        assert "scope" in sig.parameters

    def test_query_test_symbols_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_test_symbols")
        sig = inspect.signature(DatabaseProvider.query_test_symbols)
        assert "scope" in sig.parameters

    def test_query_symbol_type_signatures_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_symbol_type_signatures")
        sig = inspect.signature(DatabaseProvider.query_symbol_type_signatures)
        assert "fqns" in sig.parameters

    def test_query_distinct_fqns_by_file_path_exists(self) -> None:
        assert hasattr(DatabaseProvider, "query_distinct_fqns_by_file_path")
        sig = inspect.signature(DatabaseProvider.query_distinct_fqns_by_file_path)
        assert "file_path" in sig.parameters
