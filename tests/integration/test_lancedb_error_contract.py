"""Regression tests for LanceDB error contract — exceptions must propagate.

Filed as ch-9aa. The LanceDB provider's graph/symbol _executor_* methods
silently swallow exceptions, violating the ProviderError contract that
DuckDB follows via _run_and_wrap_exceptions (serial_executor.py:85-109).
"""
import types

import pytest

from chunkhound.core.models import File
from chunkhound.core.models.symbol import SymbolRow
from chunkhound.core.types.common import Language
from chunkhound.interfaces.database_provider import ProviderError

pytestmark = pytest.mark.integration


def _monkey_patch_symbol_table_delete(provider) -> None:
    """Inject a broken .delete() on the symbol table handle."""

    def probe(self, conn, state):
        sym_tbl, _ = self._ensure_symbol_tables(conn, state)

        def broken_delete(*args, **kwargs):
            raise RuntimeError("simulated lance backend failure")

        sym_tbl.delete = broken_delete

    provider._execute_in_db_thread_sync(types.MethodType(probe, provider))


def _monkey_patch_edge_table_delete(provider) -> None:
    """Inject a broken .delete() on the edge table handle."""

    def probe(self, conn, state):
        _, edge_tbl = self._ensure_symbol_tables(conn, state)

        def broken_delete(*args, **kwargs):
            raise RuntimeError("simulated lance backend failure")

        edge_tbl.delete = broken_delete

    provider._execute_in_db_thread_sync(types.MethodType(probe, provider))


def _seed_symbol(provider) -> tuple[int, list[SymbolRow]]:
    """Insert a file + one symbol. Returns (file_id, symbols)."""
    fid = provider.insert_file(
        File(path="src/a.py", mtime=1.0, language=Language.PYTHON, size_bytes=100)  # type: ignore[arg-type]
    )
    symbols = [
        SymbolRow(
            fqn="mod::A",
            name="A",
            kind="Function",
            language="python",
            file_id=fid,
            file_path="src/a.py",
            range_start=0,
            range_end=10,
            confidence=1.0,
            lsp_server="pyright",
            parent_fqn=None,
            type_signature=None,
        ),
    ]
    provider.insert_symbols_batch(symbols)
    return fid, symbols


class TestDeleteSymbolsByFileErrorContract:
    """delete_symbols_by_file must raise ProviderError when the backend fails."""

    def test_raises_provider_error_on_backend_failure(self, lancedb_provider) -> None:
        """Regression: was silently swallowing RuntimeError, returning normally."""
        p = lancedb_provider
        fid, _ = _seed_symbol(p)

        # Verify symbol exists
        assert len(p.query_symbols_by_file(fid)) == 1

        # Break the delete path
        _monkey_patch_symbol_table_delete(p)

        # Must raise ProviderError — not return normally
        with pytest.raises(ProviderError):
            p.delete_symbols_by_file(fid)

    def test_no_duplicate_rows_when_delete_propagates(self, lancedb_provider) -> None:
        """Regression: silent delete + re-insert created duplicate rows."""
        p = lancedb_provider
        fid, symbols = _seed_symbol(p)

        _monkey_patch_symbol_table_delete(p)

        # Delete fails — caller knows via ProviderError
        with pytest.raises(ProviderError):
            p.delete_symbols_by_file(fid)

        # Caller should NOT proceed to re-insert after ProviderError.
        # But if they did, verify original row still exists (1, not 2).
        rows = p.query_symbols_by_file(fid)
        assert len(rows) == 1, f"Expected 1 row (delete failed), got {len(rows)}"


class TestDeleteEdgesByFileErrorContract:
    """delete_edges_by_file must raise ProviderError when the backend fails."""

    def test_raises_provider_error_on_backend_failure(self, lancedb_provider) -> None:
        """Regression: was bare `except Exception: pass` on edge deletes."""
        p = lancedb_provider
        fid, _ = _seed_symbol(p)

        # Need at least one edge for the delete loop to execute
        from chunkhound.core.models.symbol import EdgeRow

        edges = [
            EdgeRow(
                from_symbol_id=1,
                to_symbol_id=2,
                edge_kind="calls",
                from_fqn="mod::A",
                to_fqn="mod::B",
                from_file="src/a.py",
                to_file="src/b.py",
                lsp_server="pyright",
            ),
        ]
        p.insert_edges_batch(edges)

        _monkey_patch_edge_table_delete(p)

        with pytest.raises(ProviderError):
            p.delete_edges_by_file(fid)


class TestFQNEscaping:
    """FQN values containing quotes must not break LanceDB where() clauses."""

    def _seed_symbol_with_apostrophe(self, provider) -> tuple[int, str]:
        """Insert a file + symbol with apostrophe in FQN. Returns (file_id, fqn)."""
        fid = provider.insert_file(
            File(path="src/helpers.py", mtime=1.0, language=Language.PYTHON, size_bytes=200)  # type: ignore[arg-type]
        )
        fqn = "mod::it's_helper"
        symbols = [
            SymbolRow(
                fqn=fqn,
                name="it's_helper",
                kind="Function",
                language="python",
                file_id=fid,
                file_path="src/helpers.py",
                range_start=0,
                range_end=20,
                confidence=1.0,
                lsp_server="pyright",
                parent_fqn=None,
                type_signature=None,
            ),
        ]
        provider.insert_symbols_batch(symbols)
        return fid, fqn

    def test_graph_walk_with_apostrophe_fqn(self, lancedb_provider) -> None:
        """Regression: unescaped FQN in where() produces LanceDB parse error."""
        p = lancedb_provider
        fid, fqn = self._seed_symbol_with_apostrophe(p)

        # graph_walk should find the node — not silently return empty or crash
        nodes, edges = p.graph_walk(
            seed_fqns=[fqn], depth=1, directed=False, edge_kind=None, limit=10
        )
        assert len(nodes) == 1
        assert nodes[0]["fqn"] == fqn

    def test_query_symbols_by_fqn_exists_with_apostrophe(self, lancedb_provider) -> None:
        """fqn_exists must handle apostrophes in both fqn and file_path."""
        p = lancedb_provider
        fid, fqn = self._seed_symbol_with_apostrophe(p)
        assert p.query_symbols_by_fqn_exists(fqn, "src/helpers.py") is True

    def test_graph_overview_with_apostrophe_fqn(self, lancedb_provider) -> None:
        """graph_overview looking up symbol details for apostrophe FQN."""
        p = lancedb_provider
        fid, fqn = self._seed_symbol_with_apostrophe(p)

        # Insert an edge so the FQN appears in edge_counts
        from chunkhound.core.models.symbol import EdgeRow

        fid2 = p.insert_file(
            File(path="src/main.py", mtime=1.0, language=Language.PYTHON, size_bytes=100)  # type: ignore[arg-type]
        )
        p.insert_symbols_batch(
            [
                SymbolRow(
                    fqn="mod::main",
                    name="main",
                    kind="Function",
                    language="python",
                    file_id=fid2,
                    file_path="src/main.py",
                    range_start=0,
                    range_end=10,
                    confidence=1.0,
                    lsp_server="pyright",
                    parent_fqn=None,
                    type_signature=None,
                ),
            ]
        )
        edges = [
            EdgeRow(
                from_symbol_id=1,
                to_symbol_id=2,
                edge_kind="calls",
                from_fqn="mod::main",
                to_fqn=fqn,
                from_file="src/main.py",
                to_file="src/helpers.py",
                lsp_server="pyright",
            ),
        ]
        p.insert_edges_batch(edges)

        results = p.graph_overview(scope=None, limit=10)
        fqns_in_results = {r["fqn"] for r in results}
        assert fqn in fqns_in_results
