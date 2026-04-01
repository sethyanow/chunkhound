"""Edge population tests — _collect_edges, populate_file edges, delete, dedup, cross-file."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from chunkhound.lsp.types import Location, LSPCapability, SymbolInfo
from chunkhound.services.lsp_population import LSPPopulationService, PopulateResult
from tests.lsp.conftest import (
    _insert_file,
    _insert_symbols,
    _make_mock_pool,
    _make_provider,
    _sample_symbols,
)

pytestmark = pytest.mark.unit


class TestCollectEdges:
    """
    Feature: Collect edges from LSP operations per symbol

    As the edge population service
    I want to call definition/references/implementation/calls per symbol
    So that the symbol_edges table contains cross-symbol relationships
    """

    @pytest.mark.asyncio
    async def test_definition_and_references_produce_correct_edge_tuples(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: go_to_definition and find_references produce edges with correct edge_kind
        Given two files with symbols: main.py has 'caller' (line 1-3), greeter.py has 'Greeter' (line 1-8)
        When _collect_edges is called for main.py's symbols
             and go_to_definition returns a Location in greeter.py
             and find_references returns a Location in greeter.py
        Then edges are returned with kind 'defines' and 'references' respectively
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/greeter.py")

        # Source file symbols
        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("caller", "caller", "Function", 1, 3),
        ])
        # Target file symbols
        tgt_ids = _insert_symbols(provider, 2, "src/greeter.py", [
            ("Greeter", "Greeter", "Class", 1, 8),
        ])

        fqn_to_id = {"caller": src_ids[0]}

        # Mock LSP client
        greeter_uri = (tmp_path / "src" / "greeter.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DEFINITION,
            LSPCapability.REFERENCES,
        })
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=greeter_uri, range_start_line=1, range_start_char=0,
                     range_end_line=8, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri=greeter_uri, range_start_line=1, range_start_char=0,
                     range_end_line=8, range_end_char=0),
        ])
        # No implementation or call hierarchy capability
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # Should have exactly 1 edge (defines and references resolve to same target,
        # but edge_kind differs so they're both kept)
        edge_kinds = {e[6] for e in edges}  # index 6 = edge_kind
        assert "defines" in edge_kinds
        assert "references" in edge_kinds

        # Verify from/to fields on a 'defines' edge
        defines_edge = [e for e in edges if e[6] == "defines"][0]
        assert defines_edge[0] == src_ids[0]     # from_symbol_id
        assert defines_edge[1] == "caller"        # from_fqn
        assert defines_edge[2] == "src/main.py"   # from_file
        assert defines_edge[3] == tgt_ids[0]      # to_symbol_id
        assert defines_edge[4] == "Greeter"       # to_fqn
        assert defines_edge[5] == "src/greeter.py" # to_file

    @pytest.mark.asyncio
    async def test_skips_operations_for_missing_capabilities(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: Server lacks CALL_HIERARCHY — calls/called_by edges skipped
        Given a client with only DEFINITION capability (no REFERENCES, CALL_HIERARCHY, IMPLEMENTATION)
        When _collect_edges is called
        Then only 'defines' edges are produced (others silently skipped)
             and incoming_calls/outgoing_calls/find_references/go_to_implementation are never called
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        client = AsyncMock()
        # Only DEFINITION — nothing else
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func_a", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # Only 'defines' edges — others skipped due to missing capabilities
        edge_kinds = {e[6] for e in edges}
        assert edge_kinds == {"defines"}

        # Verify skipped operations were never called
        client.find_references.assert_not_called()
        client.go_to_implementation.assert_not_called()
        client.incoming_calls.assert_not_called()
        client.outgoing_calls.assert_not_called()

    @pytest.mark.asyncio
    async def test_failure_on_one_symbol_does_not_block_others(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: LSP operation fails for one symbol, other symbols still produce edges
        Given two symbols where go_to_definition raises for the first
        When _collect_edges is called
        Then edges from the second symbol are still collected
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 3),
            ("func_b", "func_b", "Function", 5, 8),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0], "func_b": src_ids[1]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()

        async def definition_side_effect(_uri: str, line: int, _char: int) -> list[Location]:
            if line == 1:  # func_a — blow up
                raise RuntimeError("LSP server error")
            return [Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                             range_end_line=3, range_end_char=0)]

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock(side_effect=definition_side_effect)
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [
            SymbolInfo(name="func_a", kind=12, range_start_line=1, range_start_char=0,
                       range_end_line=3, range_end_char=0, children=[]),
            SymbolInfo(name="func_b", kind=12, range_start_line=5, range_start_char=0,
                       range_end_line=8, range_end_char=0, children=[]),
        ]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # func_a's edge lost to exception, but func_b's edge is collected
        assert len(edges) >= 1
        from_fqns = {e[1] for e in edges}
        assert "func_b" in from_fqns
        assert "func_a" not in from_fqns


class TestPopulateFileEdges:
    """
    Feature: populate_file writes edges to the symbol_edges table

    As the indexing pipeline
    I want populate_file to collect and store edges alongside symbols
    So that the graph is built during indexing
    """

    @pytest.mark.asyncio
    async def test_populate_file_stores_edges_in_symbol_edges_table(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: End-to-end edge population via populate_file
        Given two files where main.py's 'caller' function has a definition pointing to lib.py's 'helper'
        When populate_file is called for main.py
        Then symbol_edges table contains an edge with correct from/to IDs and edge_kind='defines'
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        # Pre-insert target symbols (lib.py already populated by prior pass)
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        # Source file symbols (returned by documentSymbol)
        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()

        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DOCUMENT_SYMBOL, LSPCapability.HOVER,
            LSPCapability.DEFINITION,
        })
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        # Hover returns nothing (simplify test)
        client.hover = AsyncMock(return_value=None)
        # Definition returns location in lib.py
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "main.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def caller():\n    helper()\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(
            file_path=Path("src/main.py"), file_id=1, language="python",
        )

        # Verify symbols were inserted
        sym_rows = provider.execute_query(
            "SELECT id, fqn FROM symbols WHERE file_id = 1"
        )
        assert len(sym_rows) == 1
        assert sym_rows[0]["fqn"] == "caller"

        # Verify edges were inserted
        edge_rows = provider.execute_query(
            "SELECT from_fqn, to_fqn, edge_kind, from_file, to_file "
            "FROM symbol_edges"
        )
        assert len(edge_rows) == 1
        edge = edge_rows[0]
        assert edge["from_fqn"] == "caller"
        assert edge["to_fqn"] == "helper"
        assert edge["edge_kind"] == "defines"
        assert edge["from_file"] == "src/main.py"
        assert edge["to_file"] == "src/lib.py"


class TestDeleteFileEdges:
    """
    Feature: delete_file_edges removes edges referencing a file's symbols

    As the incremental repopulation pipeline
    I want to delete a file's edges before deleting its symbols
    So that FK integrity is maintained and stale edges don't persist
    """

    @pytest.mark.asyncio
    async def test_deletes_edges_for_file_symbols(self, tmp_path: Path) -> None:
        """
        Scenario: Edges from and to a file's symbols are removed
        Given file 1 has a symbol with an edge to file 2's symbol
             and file 2 has a symbol with an edge to file 1's symbol
        When delete_file_edges is called for file 1
        Then both edges are removed (from file1 and to file1)
             but file 2's symbols remain intact
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        ids_a = _insert_symbols(provider, 1, "src/a.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        ids_b = _insert_symbols(provider, 2, "src/b.py", [
            ("func_b", "func_b", "Function", 1, 5),
        ])

        # Insert edges in both directions
        provider.execute_query(
            "INSERT INTO symbol_edges "
            "(from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
            "VALUES (?, 'func_a', 'src/a.py', ?, 'func_b', 'src/b.py', 'calls', 1.0, 'pyright'), "
            "(?, 'func_b', 'src/b.py', ?, 'func_a', 'src/a.py', 'references', 1.0, 'pyright')",
            [ids_a[0], ids_b[0], ids_b[0], ids_a[0]],
        )

        # Verify both edges exist
        assert len(provider.execute_query("SELECT * FROM symbol_edges")) == 2

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        await service.delete_file_edges(1)  # Delete edges for file 1

        # Both edges removed — one had from_symbol in file 1, other had to_symbol in file 1
        remaining = provider.execute_query("SELECT * FROM symbol_edges")
        assert len(remaining) == 0

        # Symbols from both files still exist
        sym_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols")
        assert sym_count[0]["cnt"] == 2


class TestEdgeDeduplication:
    """
    Feature: Edge deduplication on (from_fqn, to_fqn, edge_kind)

    As the edge collection pipeline
    I want duplicate edges deduplicated before batch insert
    So that the same relationship isn't stored multiple times
    """

    @pytest.mark.asyncio
    async def test_same_edge_from_definition_and_references_deduplicates(
        self, tmp_path: Path,
    ) -> None:
        """
        Scenario: go_to_definition and find_references both discover the same (from, to) pair
        Given func_a → helper discovered via go_to_definition (edge_kind='defines')
             AND func_a → helper discovered via find_references (edge_kind='references')
        When _collect_edges returns
        Then both edges exist (different edge_kind = different dedup keys)
             But if the SAME (from_fqn, to_fqn, edge_kind) appeared twice, only one is kept
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func_a", "func_a", "Function", 1, 5),
        ])
        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        fqn_to_id = {"func_a": src_ids[0]}

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        # Both definition and references point to the same target
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DEFINITION, LSPCapability.REFERENCES,
        })
        # go_to_definition returns SAME location TWICE (e.g., overloaded resolve)
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func_a", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # 2 unique edges: (func_a, helper, defines) + (func_a, helper, references)
        # The duplicate 'defines' from the two go_to_definition results is deduped
        assert len(edges) == 2
        edge_kinds = {e[6] for e in edges}
        assert edge_kinds == {"defines", "references"}


class TestAdversarialEdges:
    """Adversarial stress tests for edge population (ch-zlg)."""

    @pytest.mark.asyncio
    async def test_empty_symbols_no_edges_no_lsp_calls(self, tmp_path: Path) -> None:
        """
        Pattern: Empty
        Hypothesis: _collect_edges with [] symbols → empty list, no LSP calls.
        """
        provider = _make_provider(tmp_path)
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock()

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        edges = await service._collect_edges(
            client, "file:///test.py", [], "test.py", {},
        )

        assert edges == []
        client.go_to_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_self_edge_filtered_out(self, tmp_path: Path) -> None:
        """
        Pattern: Self-referential
        Hypothesis: go_to_definition returns the symbol's own location → 'defines' self-edge filtered.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/self.py")

        ids = _insert_symbols(provider, 1, "src/self.py", [
            ("MyClass", "MyClass", "Class", 1, 10),
        ])

        fqn_to_id = {"MyClass": ids[0]}
        self_uri = (tmp_path / "src" / "self.py").as_uri()

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        # Definition points to itself
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=self_uri, range_start_line=1, range_start_char=0,
                     range_end_line=10, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="MyClass", kind=5, range_start_line=1, range_start_char=0,
            range_end_line=10, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        edges = await service._collect_edges(
            client, self_uri, symbols, "src/self.py", fqn_to_id,
        )

        # Self-edge (MyClass → MyClass, defines) should be filtered
        assert len(edges) == 0

    @pytest.mark.asyncio
    async def test_percent_encoded_uri_resolved_correctly(self, tmp_path: Path) -> None:
        """
        Pattern: Encoding boundaries
        Hypothesis: URI with %20 (space) in path is decoded and resolved correctly.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/my module/helper.py")
        _insert_symbols(provider, 1, "src/my module/helper.py", [
            ("helper", "helper", "Function", 1, 5),
        ])

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        # URI with percent-encoded space
        encoded_uri = (tmp_path / "src" / "my module" / "helper.py").as_uri()
        # Path.as_uri() encodes spaces as %20
        assert "%20" in encoded_uri or "my module" in encoded_uri

        result = service._resolve_symbol(encoded_uri, 3)
        assert result is not None
        assert result[1] == "helper"

    @pytest.mark.asyncio
    async def test_second_run_edges_idempotent(self, tmp_path: Path) -> None:
        """
        Pattern: The "second run"
        Hypothesis: populate_file called twice → same number of edges (delete-before-insert).
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")
        _insert_file(provider, 2, "src/lib.py")

        _insert_symbols(provider, 2, "src/lib.py", [
            ("helper", "helper", "Function", 1, 3),
        ])

        symbols = [SymbolInfo(
            name="caller", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=5, range_end_char=0, children=[],
        )]

        lib_uri = (tmp_path / "src" / "lib.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({
            LSPCapability.DOCUMENT_SYMBOL, LSPCapability.HOVER,
            LSPCapability.DEFINITION,
        })
        client.document_symbols = AsyncMock(return_value=symbols)
        client.notify_did_open = AsyncMock()
        client.notify_did_close = AsyncMock()
        client.hover = AsyncMock(return_value=None)
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri=lib_uri, range_start_line=1, range_start_char=0,
                     range_end_line=3, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        pool = AsyncMock()
        pool.get = AsyncMock(return_value=client)

        src_file = tmp_path / "src" / "main.py"
        src_file.parent.mkdir(parents=True, exist_ok=True)
        src_file.write_text("def caller(): pass\n")

        service = LSPPopulationService(
            pool=pool, provider=provider, workspace_root=tmp_path,
        )

        await service.populate_file(Path("src/main.py"), 1, "python")
        await service.populate_file(Path("src/main.py"), 1, "python")

        edge_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbol_edges")
        assert edge_count[0]["cnt"] == 1  # Idempotent — not doubled

        sym_count = provider.execute_query("SELECT COUNT(*) as cnt FROM symbols WHERE file_id = 1")
        assert sym_count[0]["cnt"] == 1  # Also idempotent

    @pytest.mark.asyncio
    async def test_fqn_missing_from_map_skips_edge_collection(self, tmp_path: Path) -> None:
        """
        Pattern: Semantically hostile
        Hypothesis: fqn_to_id doesn't contain a symbol's FQN → edges skipped for that symbol.
        """
        provider = _make_provider(tmp_path)

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        client.go_to_definition = AsyncMock()

        symbols = [SymbolInfo(
            name="ghost", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        # fqn_to_id is empty — "ghost" has no ID mapping
        edges = await service._collect_edges(
            client, "file:///test.py", symbols, "test.py", {},
        )

        assert edges == []
        # go_to_definition should never have been called (skipped due to missing ID)
        client.go_to_definition.assert_not_called()

    @pytest.mark.asyncio
    async def test_all_lsp_results_outside_workspace_produces_zero_edges(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Disconnected
        Hypothesis: All LSP results point to files outside workspace → zero edges.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/main.py")

        src_ids = _insert_symbols(provider, 1, "src/main.py", [
            ("func", "func", "Function", 1, 3),
        ])

        fqn_to_id = {"func": src_ids[0]}

        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION, LSPCapability.REFERENCES})
        # All results point to stdlib
        client.go_to_definition = AsyncMock(return_value=[
            Location(uri="file:///usr/lib/python3.13/builtins.py",
                     range_start_line=100, range_start_char=0,
                     range_end_line=105, range_end_char=0),
        ])
        client.find_references = AsyncMock(return_value=[
            Location(uri="file:///usr/lib/python3.13/typing.py",
                     range_start_line=50, range_start_char=0,
                     range_end_line=55, range_end_char=0),
        ])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        symbols = [SymbolInfo(
            name="func", kind=12, range_start_line=1, range_start_char=0,
            range_end_line=3, range_end_char=0, children=[],
        )]

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        main_uri = (tmp_path / "src" / "main.py").as_uri()
        edges = await service._collect_edges(
            client, main_uri, symbols, "src/main.py", fqn_to_id,
        )

        # All targets outside workspace → _resolve_symbol returns None → zero edges
        assert edges == []

    @pytest.mark.asyncio
    async def test_nested_symbols_collect_edges_for_children(
        self, tmp_path: Path,
    ) -> None:
        """
        Pattern: Singular (nested variant)
        Hypothesis: Nested symbol (method inside class) collects edges with correct parent FQN.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/cls.py")
        _insert_file(provider, 2, "src/dep.py")

        src_ids = _insert_symbols(provider, 1, "src/cls.py", [
            ("MyClass", "MyClass", "Class", 1, 10),
            ("MyClass::method", "method", "Method", 3, 8),
        ])
        _insert_symbols(provider, 2, "src/dep.py", [
            ("dep_func", "dep_func", "Function", 1, 5),
        ])

        fqn_to_id = {"MyClass": src_ids[0], "MyClass::method": src_ids[1]}

        dep_uri = (tmp_path / "src" / "dep.py").as_uri()
        client = AsyncMock()
        client.capabilities = frozenset({LSPCapability.DEFINITION})
        # Class definition → no results. Method definition → dep_func.
        async def defn_side_effect(_uri: str, line: int, _char: int) -> list[Location]:
            if line == 3:  # method
                return [Location(uri=dep_uri, range_start_line=1, range_start_char=0,
                                 range_end_line=5, range_end_char=0)]
            return []
        client.go_to_definition = AsyncMock(side_effect=defn_side_effect)
        client.find_references = AsyncMock(return_value=[])
        client.go_to_implementation = AsyncMock(return_value=[])
        client.incoming_calls = AsyncMock(return_value=[])
        client.outgoing_calls = AsyncMock(return_value=[])

        method = SymbolInfo(
            name="method", kind=6, range_start_line=3, range_start_char=4,
            range_end_line=8, range_end_char=0, children=[],
        )
        cls = SymbolInfo(
            name="MyClass", kind=5, range_start_line=1, range_start_char=0,
            range_end_line=10, range_end_char=0, children=[method],
        )

        service = LSPPopulationService(
            pool=AsyncMock(), provider=provider, workspace_root=tmp_path,
        )

        cls_uri = (tmp_path / "src" / "cls.py").as_uri()
        edges = await service._collect_edges(
            client, cls_uri, [cls], "src/cls.py", fqn_to_id,
        )

        # Only method→dep_func edge (class definition returned empty)
        assert len(edges) == 1
        edge = edges[0]
        assert edge[1] == "MyClass::method"  # from_fqn preserves parent::child
        assert edge[4] == "dep_func"          # to_fqn
        assert edge[6] == "defines"           # edge_kind


class TestDeleteSymbolsWithCrossFileEdges:
    """Regression: delete_file_symbols must not fail when cross-file edges reference the file's symbols."""

    @pytest.mark.asyncio
    async def test_repopulate_file_with_cross_file_edges(self, tmp_path: Path) -> None:
        """
        Scenario: File A has symbols referenced by edges from File B.
        When File A is repopulated (delete + reinsert), the delete pair
        must be atomic so FK constraints don't fire.
        """
        provider = _make_provider(tmp_path)

        # Insert two files
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        # Insert symbols for both files
        provider.execute_query(
            "INSERT INTO symbols (id, fqn, name, kind, language, file_id, file_path, range_start, range_end, confidence, lsp_server) "
            "VALUES (100, 'Foo', 'Foo', 'class', 'python', 1, 'src/a.py', 0, 10, 1.0, 'pyright'), "
            "       (200, 'Bar', 'Bar', 'class', 'python', 2, 'src/b.py', 0, 10, 1.0, 'pyright')"
        )

        # Insert a cross-file edge: Bar (file B) → Foo (file A)
        provider.execute_query(
            "INSERT INTO symbol_edges (from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
            "VALUES (200, 'Bar', 'src/b.py', 100, 'Foo', 'src/a.py', 'references', 1.0, 'pyright')"
        )

        # Verify edge exists
        edges = provider.execute_query("SELECT * FROM symbol_edges")
        assert len(edges) == 1, f"Expected 1 edge, got {len(edges)}"

        pool, _client = _make_mock_pool(_sample_symbols())
        src = tmp_path / "src" / "a.py"
        src.parent.mkdir(parents=True, exist_ok=True)
        src.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        # This must NOT raise ConstraintError — the delete pair must be atomic
        result = await service.populate_file(
            file_path=Path("src/a.py"),
            file_id=1,
            language="python",
        )
        assert result is PopulateResult.POPULATED

    @pytest.mark.asyncio
    async def test_full_reindex_with_cross_file_edges(self, tmp_path: Path) -> None:
        """
        Scenario: Full populate_files run with existing symbols+edges from a prior run.
        Simulates the actual reindex crash: File A has edges pointing to File B's symbols.
        When File B is repopulated, its symbols must be deletable.
        """
        provider = _make_provider(tmp_path)

        # Insert two files
        _insert_file(provider, 1, "src/a.py")
        _insert_file(provider, 2, "src/b.py")

        # Simulate prior run: symbols exist with sequence-assigned IDs
        provider.execute_query(
            "INSERT INTO symbols (id, fqn, name, kind, language, file_id, file_path, range_start, range_end, confidence, lsp_server) "
            "VALUES (10001, 'Foo', 'Foo', 'class', 'python', 1, 'src/a.py', 0, 10, 1.0, 'pyright'), "
            "       (10002, 'Bar', 'Bar', 'class', 'python', 2, 'src/b.py', 0, 10, 1.0, 'pyright')"
        )

        # Prior run edges: bidirectional cross-file references
        provider.execute_query(
            "INSERT INTO symbol_edges (from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
            "VALUES (10001, 'Foo', 'src/a.py', 10002, 'Bar', 'src/b.py', 'references', 1.0, 'pyright'), "
            "       (10002, 'Bar', 'src/b.py', 10001, 'Foo', 'src/a.py', 'references', 1.0, 'pyright')"
        )

        pool, client = _make_mock_pool(_sample_symbols())
        client.workspace_symbols = AsyncMock(return_value=[])
        client.capabilities = {LSPCapability.DOCUMENT_SYMBOL, LSPCapability.WORKSPACE_SYMBOL}

        for name in ("src/a.py", "src/b.py"):
            f = tmp_path / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("class Foo:\n    def bar(self): ...\n")

        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        # Full reindex — must NOT raise ConstraintError
        await service.populate_files()

        # Both files should be populated with new symbols
        rows = provider.execute_query("SELECT file_id FROM symbols")
        file_ids = {r["file_id"] for r in rows}
        assert 1 in file_ids, "File A should have symbols after reindex"
        assert 2 in file_ids, "File B should have symbols after reindex"

    @pytest.mark.asyncio
    async def test_delete_edges_before_symbols_ordering(self, tmp_path: Path) -> None:
        """
        Verify edges are deleted BEFORE symbols so FK constraints don't fire.
        DuckDB auto-commits per statement — ordering is the safety mechanism.
        """
        provider = _make_provider(tmp_path)
        _insert_file(provider, 1, "src/a.py")

        # Insert symbol and a self-referencing edge
        provider.execute_query(
            "INSERT INTO symbols (id, fqn, name, kind, language, file_id, file_path, range_start, range_end, confidence, lsp_server) "
            "VALUES (100, 'Foo', 'Foo', 'class', 'python', 1, 'src/a.py', 0, 10, 1.0, 'pyright')"
        )
        provider.execute_query(
            "INSERT INTO symbol_edges (from_symbol_id, from_fqn, from_file, to_symbol_id, to_fqn, to_file, edge_kind, confidence, lsp_server) "
            "VALUES (100, 'Foo', 'src/a.py', 100, 'Foo', 'src/a.py', 'references', 1.0, 'pyright')"
        )

        pool, _client = _make_mock_pool(_sample_symbols())
        service = LSPPopulationService(pool, provider, workspace_root=tmp_path)

        # Track call order
        call_order: list[str] = []
        original_edges = service.delete_file_edges
        original_symbols = service.delete_file_symbols

        async def tracked_edges(file_id: int) -> None:
            call_order.append("edges")
            await original_edges(file_id)

        async def tracked_symbols(file_id: int) -> None:
            call_order.append("symbols")
            await original_symbols(file_id)

        service.delete_file_edges = tracked_edges  # type: ignore[assignment]
        service.delete_file_symbols = tracked_symbols  # type: ignore[assignment]

        (tmp_path / "src" / "a.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "a.py").write_text("class Foo:\n    def bar(self): ...\n")

        result = await service.populate_file(Path("src/a.py"), 1, "python")
        assert result is PopulateResult.POPULATED

        # Edges must be deleted before symbols — this is the FK safety mechanism
        assert call_order == ["edges", "symbols"], (
            f"Expected edges-before-symbols ordering, got {call_order}"
        )
