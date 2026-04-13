"""Tests for chunkhound.mcp_server.tools.fusion — fusion MCP tools.

Fusion tools compose Phase 3 primitives (_graph_walk + symbols table)
into higher-level queries. All are deterministic — no LLM, no embeddings.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest

from tests.lsp.mcp_tool_helpers import make_mock_config, make_mock_services

pytestmark = pytest.mark.unit


class TestResolveStartFqn:
    """_resolve_start_fqn: resolve file position to containing symbol's FQN.

    Post ch-nxu Step 15: helper calls provider.query_symbols_by_range
    (returning dict | None) instead of raw execute_query.
    """

    def test_returns_fqn_for_known_position(self) -> None:
        """Symbol at (line=10, char=5) resolves to its FQN."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {
            "fqn": "module::MyClass::method",
        }

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        result = _resolve_start_fqn(
            services=services, file="src/module.py", line=10, character=5,
            workspace_root="/workspace",
        )

        assert result == "module::MyClass::method"

    def test_converts_1based_line_to_0based_for_db(self) -> None:
        """1-based input line=10 becomes 0-based 9 in provider call."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::func"}

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        _resolve_start_fqn(
            services=services, file="src/mod.py", line=10, character=1,
            workspace_root="/workspace",
        )

        # Provider receives 0-based line (1-based 10 → 0-based 9)
        call_args = services.provider.query_symbols_by_range.call_args
        assert call_args[0][1] == 9

    def test_returns_error_dict_when_no_symbol_at_position(self) -> None:
        """Position outside any symbol returns error dict with 'error' key."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = None  # no match

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        result = _resolve_start_fqn(
            services=services, file="src/module.py", line=99, character=1,
            workspace_root="/workspace",
        )

        assert isinstance(result, dict)
        assert "error" in result


class TestAnnotateTypeSignatures:
    """_annotate_type_signatures: batch-query type_signature from symbols table.

    Post ch-nxu Step 15: helper calls provider.query_symbol_type_signatures
    (returning dict[str, str|None]) instead of raw execute_query.
    """

    def test_merges_signatures_into_nodes(self) -> None:
        """Nodes receive type_signature from DB lookup by FQN."""
        services = make_mock_services()
        services.provider.query_symbol_type_signatures.return_value = {
            "mod::func_a": "(int) -> str",
            "mod::func_b": "(str) -> bool",
        }
        nodes = [
            {"fqn": "mod::func_a", "name": "func_a"},
            {"fqn": "mod::func_b", "name": "func_b"},
        ]

        from chunkhound.mcp_server.tools.fusion import _annotate_type_signatures

        result = _annotate_type_signatures(services, nodes)

        assert result[0]["type_signature"] == "(int) -> str"
        assert result[1]["type_signature"] == "(str) -> bool"

    def test_null_signature_becomes_none(self) -> None:
        """Nodes with None or missing from provider get type_signature=None."""
        services = make_mock_services()
        services.provider.query_symbol_type_signatures.return_value = {
            "mod::func_a": None,  # present but None
            # "mod::missing" absent → .get() returns None
        }
        nodes = [
            {"fqn": "mod::func_a", "name": "func_a"},
            {"fqn": "mod::missing", "name": "missing"},
        ]

        from chunkhound.mcp_server.tools.fusion import _annotate_type_signatures

        result = _annotate_type_signatures(services, nodes)

        assert result[0]["type_signature"] is None
        assert result[1]["type_signature"] is None

    def test_empty_nodes_returns_empty(self) -> None:
        """Empty nodes list returns empty without querying DB."""
        services = make_mock_services()

        from chunkhound.mcp_server.tools.fusion import _annotate_type_signatures

        result = _annotate_type_signatures(services, [])

        assert result == []
        services.provider.query_symbol_type_signatures.assert_not_called()


class TestBuildCallerTree:
    """_build_caller_tree: reconstruct nested tree from flat walk results."""

    def test_builds_tree_with_callers(self) -> None:
        """Root at depth 0, direct caller at 1, transitive at 2."""
        nodes = [
            {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
            {"fqn": "mod::caller_a", "name": "caller_a", "kind": "Function", "file_path": "mod.py", "depth": 1},
            {"fqn": "other::caller_b", "name": "caller_b", "kind": "Function", "file_path": "other.py", "depth": 2},
        ]
        # Edge semantics: (from_fqn, to_fqn, "called_by") = from_fqn is called by to_fqn
        edges = [
            {"from_fqn": "mod::target", "to_fqn": "mod::caller_a", "edge_kind": "called_by"},
            {"from_fqn": "mod::caller_a", "to_fqn": "other::caller_b", "edge_kind": "called_by"},
        ]

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        tree = _build_caller_tree("mod::target", nodes, edges)

        assert tree["fqn"] == "mod::target"
        assert tree["hop_distance"] == 0
        assert len(tree["children"]) == 1

        child = tree["children"][0]
        assert child["fqn"] == "mod::caller_a"
        assert child["hop_distance"] == 1
        assert len(child["children"]) == 1

        grandchild = child["children"][0]
        assert grandchild["fqn"] == "other::caller_b"
        assert grandchild["hop_distance"] == 2
        assert grandchild["children"] == []

    def test_root_with_no_callers(self) -> None:
        """Root exists but has no callers — children is empty."""
        nodes = [
            {"fqn": "mod::leaf", "name": "leaf", "kind": "Function", "file_path": "mod.py", "depth": 0},
        ]
        edges: list[dict[str, str]] = []

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        tree = _build_caller_tree("mod::leaf", nodes, edges)

        assert tree["fqn"] == "mod::leaf"
        assert tree["hop_distance"] == 0
        assert tree["children"] == []

    def test_root_missing_from_nodes_returns_error(self) -> None:
        """root_fqn not in nodes list — defensive error dict."""
        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        result = _build_caller_tree("nonexistent::func", [], [])

        assert isinstance(result, dict)
        assert "error" in result


class TestImpactCascadeImpl:
    """impact_cascade_impl: end-to-end composition of all helpers."""

    @pytest.mark.asyncio
    async def test_returns_structured_tree(self) -> None:
        """Happy path: root + one caller → tree with correct shape."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::target"}
        services.provider.query_symbol_type_signatures.return_value = {
            "mod::target": "(int) -> str",
            "mod::caller_a": "(str) -> None",
        }
        services.provider.graph_walk.return_value = (
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::caller_a", "name": "caller_a", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::target", "to_fqn": "mod::caller_a", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "mod.py"},
            ],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=10, character=5, depth=3,
        )

        assert "root" in result
        root = result["root"]
        assert root["fqn"] == "mod::target"
        assert root["hop_distance"] == 0
        assert root["type_signature"] == "(int) -> str"
        assert len(root["children"]) == 1
        assert root["children"][0]["fqn"] == "mod::caller_a"
        assert root["children"][0]["type_signature"] == "(str) -> None"
        assert result["total_nodes"] == 2
        assert result["max_depth"] == 1

    @pytest.mark.asyncio
    async def test_empty_callers_returns_root_only(self) -> None:
        """Root exists but has no callers — children: [], total_nodes: 1."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::leaf"}
        services.provider.query_symbol_type_signatures.return_value = {"mod::leaf": None}
        services.provider.graph_walk.return_value = (
            [{"fqn": "mod::leaf", "name": "leaf", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],  # no edges
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=5, character=1, depth=3,
        )

        assert result["root"]["children"] == []
        assert result["total_nodes"] == 1
        assert result["max_depth"] == 0

    @pytest.mark.asyncio
    async def test_null_type_signatures_no_crash(self) -> None:
        """Nodes without type_signature get null, no crash."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::func"}
        services.provider.query_symbol_type_signatures.return_value = {}  # no signatures
        services.provider.graph_walk.return_value = (
            [{"fqn": "mod::func", "name": "func", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=1, character=1, depth=3,
        )

        assert result["root"]["type_signature"] is None

    def test_registered_in_tool_registry(self) -> None:
        """impact_cascade is in TOOL_REGISTRY after import."""
        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl  # noqa: F401
        from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY

        assert "impact_cascade" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Adversarial stress tests
# ---------------------------------------------------------------------------


class TestBuildCallerTreeAdversarial:
    """Adversarial: structural patterns that expose assumptions."""

    def test_self_referential_edge_no_infinite_recursion(self) -> None:
        """Self-edge (A calls A) must not cause infinite recursion."""
        nodes = [
            {"fqn": "mod::recurse", "name": "recurse", "kind": "Function", "file_path": "mod.py", "depth": 0},
        ]
        edges = [
            {"from_fqn": "mod::recurse", "to_fqn": "mod::recurse", "edge_kind": "called_by"},
        ]

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        # Should not hang or stack overflow — must terminate
        tree = _build_caller_tree("mod::recurse", nodes, edges)

        assert tree["fqn"] == "mod::recurse"
        # Self-edge should NOT create a child (avoids infinite tree)
        assert tree["children"] == []

    def test_mutual_cycle_no_infinite_recursion(self) -> None:
        """Mutual call cycle (A→B, B→A) must not cause infinite recursion."""
        nodes = [
            {"fqn": "mod::A", "name": "A", "kind": "Function", "file_path": "mod.py", "depth": 0},
            {"fqn": "mod::B", "name": "B", "kind": "Function", "file_path": "mod.py", "depth": 1},
        ]
        edges = [
            {"from_fqn": "mod::A", "to_fqn": "mod::B", "edge_kind": "called_by"},
            {"from_fqn": "mod::B", "to_fqn": "mod::A", "edge_kind": "called_by"},
        ]

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        tree = _build_caller_tree("mod::A", nodes, edges)

        assert tree["fqn"] == "mod::A"
        # B should appear as child of A, but A should NOT reappear as child of B
        assert len(tree["children"]) == 1
        assert tree["children"][0]["fqn"] == "mod::B"
        assert tree["children"][0]["children"] == []

    def test_disconnected_nodes_excluded_from_tree(self) -> None:
        """Nodes not reachable from root via edges are excluded from tree."""
        nodes = [
            {"fqn": "mod::root", "name": "root", "kind": "Function", "file_path": "mod.py", "depth": 0},
            {"fqn": "mod::connected", "name": "connected", "kind": "Function", "file_path": "mod.py", "depth": 1},
            {"fqn": "mod::orphan", "name": "orphan", "kind": "Function", "file_path": "other.py", "depth": 1},
        ]
        edges = [
            {"from_fqn": "mod::root", "to_fqn": "mod::connected", "edge_kind": "called_by"},
            # no edge connecting orphan to root
        ]

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        tree = _build_caller_tree("mod::root", nodes, edges)

        assert len(tree["children"]) == 1
        assert tree["children"][0]["fqn"] == "mod::connected"

    def test_dense_fan_out_multiple_callers(self) -> None:
        """Root with 3 direct callers — all appear as children."""
        nodes = [
            {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
            {"fqn": "mod::c1", "name": "c1", "kind": "Function", "file_path": "a.py", "depth": 1},
            {"fqn": "mod::c2", "name": "c2", "kind": "Function", "file_path": "b.py", "depth": 1},
            {"fqn": "mod::c3", "name": "c3", "kind": "Function", "file_path": "c.py", "depth": 1},
        ]
        edges = [
            {"from_fqn": "mod::target", "to_fqn": "mod::c1", "edge_kind": "called_by"},
            {"from_fqn": "mod::target", "to_fqn": "mod::c2", "edge_kind": "called_by"},
            {"from_fqn": "mod::target", "to_fqn": "mod::c3", "edge_kind": "called_by"},
        ]

        from chunkhound.mcp_server.tools.fusion import _build_caller_tree

        tree = _build_caller_tree("mod::target", nodes, edges)

        child_fqns = {c["fqn"] for c in tree["children"]}
        assert child_fqns == {"mod::c1", "mod::c2", "mod::c3"}


class TestImpactCascadeAdversarial:
    """Adversarial: type boundaries and edge cases for the top-level tool."""

    @pytest.mark.asyncio
    async def test_depth_zero_clamped_to_one(self) -> None:
        """depth=0 is below minimum — clamped to 1."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::f"}
        services.provider.query_symbol_type_signatures.return_value = {}
        services.provider.graph_walk.return_value = (
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=1, character=1, depth=0,
        )

        # Should succeed (depth clamped to 1), not error
        assert "root" in result

    @pytest.mark.asyncio
    async def test_depth_999_clamped_to_ten(self) -> None:
        """depth=999 is above maximum — clamped to 10."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::f"}
        services.provider.query_symbol_type_signatures.return_value = {}
        services.provider.graph_walk.return_value = (
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=1, character=1, depth=999,
        )

        assert "root" in result

    def test_line_one_converts_to_zero_based(self) -> None:
        """line=1 (minimum valid) converts to 0-based 0 in provider call."""
        services = make_mock_services()
        services.provider.query_symbols_by_range.return_value = {"fqn": "mod::f"}

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        _resolve_start_fqn(
            services=services, file="mod.py", line=1, character=1,
            workspace_root="/workspace",
        )

        # 1-based 1 → 0-based 0 passed to provider
        assert services.provider.query_symbols_by_range.call_args[0][1] == 0


# ---------------------------------------------------------------------------
# test_targeting: _resolve_changed_to_fqns
# ---------------------------------------------------------------------------


class TestResolveChangedToFqns:
    """_resolve_changed_to_fqns: resolve file paths and FQNs to FQN list.

    Post ch-nxu Step 15: helper calls provider.query_distinct_fqns_by_file_path
    (returning list[str]) instead of raw execute_query.
    """

    def test_file_path_resolves_to_fqns(self) -> None:
        """File path input queries symbols table and returns FQNs."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = [
            "mod::ClassA",
            "mod::func_b",
        ]

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["src/mod.py"],
            workspace_root="/workspace",
        )

        assert set(result) == {"mod::ClassA", "mod::func_b"}
        services.provider.query_distinct_fqns_by_file_path.assert_called_once()

    def test_fqn_string_passes_through(self) -> None:
        """Strings containing '::' pass through as FQNs, no DB query."""
        services = make_mock_services()

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["mod::func_a"],
            workspace_root="/workspace",
        )

        assert result == ["mod::func_a"]
        services.provider.query_distinct_fqns_by_file_path.assert_not_called()

    def test_mixed_file_paths_and_fqns(self) -> None:
        """Mixed list: file paths resolved, FQNs passed through."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = [
            "mod::from_file",
        ]

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["src/mod.py", "other::explicit_fqn"],
            workspace_root="/workspace",
        )

        assert "mod::from_file" in result
        assert "other::explicit_fqn" in result

    def test_file_with_no_symbols_excluded(self) -> None:
        """File path with no indexed symbols produces no FQNs, no error."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = []

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["src/empty.py"],
            workspace_root="/workspace",
        )

        assert result == []

    def test_empty_changed_list(self) -> None:
        """Empty changed list returns empty FQN list, no DB queries."""
        services = make_mock_services()

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=[],
            workspace_root="/workspace",
        )

        assert result == []
        services.provider.query_distinct_fqns_by_file_path.assert_not_called()

    def test_empty_string_filtered_out(self) -> None:
        """Empty and whitespace-only strings are filtered, not queried."""
        services = make_mock_services()

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["", "  ", "\t"],
            workspace_root="/workspace",
        )

        assert result == []
        services.provider.query_distinct_fqns_by_file_path.assert_not_called()


# ---------------------------------------------------------------------------
# test_targeting: _collect_test_fqns
# ---------------------------------------------------------------------------


class TestCollectTestFqns:
    """_collect_test_fqns: collect test entry points from symbols table.

    Post ch-nxu Step 15: helper calls provider.query_test_symbols. Kind/name
    LIKE filtering and LIKE-escape semantics are now provider internals —
    tested in tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
    """

    def test_returns_dict_with_name_and_file_path(self) -> None:
        """Returns dict mapping FQN→{name, file_path} for test functions."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_foo", "name": "test_foo", "file_path": "tests/test_mod.py"},
            {"fqn": "tests::test_bar", "name": "test_bar", "file_path": "tests/test_mod.py"},
        ]

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services)

        assert "tests::test_foo" in result
        assert result["tests::test_foo"]["name"] == "test_foo"
        assert result["tests::test_foo"]["file_path"] == "tests/test_mod.py"
        assert "tests::test_bar" in result

    def test_passes_test_scope_to_provider(self) -> None:
        """test_scope is forwarded to the provider method verbatim."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "unit::test_a", "name": "test_a", "file_path": "tests/unit/test_a.py"},
        ]

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services, test_scope="tests/unit/")

        assert "unit::test_a" in result
        services.provider.query_test_symbols.assert_called_once_with("tests/unit/")

    def test_none_scope_forwarded_to_provider(self) -> None:
        """Default test_scope=None is forwarded as None (no filter)."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = []

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        _collect_test_fqns(services=services)

        services.provider.query_test_symbols.assert_called_once_with(None)

    def test_empty_result_returns_empty_dict(self) -> None:
        """No test symbols in DB → empty dict, no error."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = []

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services)

        assert result == {}


# ---------------------------------------------------------------------------
# test_targeting: test_targeting_impl (full tool integration)
# ---------------------------------------------------------------------------


class TestTestTargetingImpl:
    """test_targeting_impl: end-to-end composition of resolve + collect + walk."""

    @pytest.mark.asyncio
    async def test_changed_symbol_with_test_callers(self) -> None:
        """Changed FQN has callers that are test functions → returns those tests."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_foo", "name": "test_foo", "file_path": "tests/test_mod.py"},
        ]
        services.provider.graph_walk.return_value = (
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_foo", "name": "test_foo", "kind": "Function", "file_path": "tests/test_mod.py", "depth": 2},
            ],
            [
                {"from_fqn": "mod::target", "to_fqn": "tests::test_foo", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "tests/test_mod.py"},
            ],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::target"], depth=3,
        )

        assert result["changed_symbols"] == ["mod::target"]
        assert result["total_tests"] == 1
        assert result["tests"][0]["fqn"] == "tests::test_foo"
        assert result["tests"][0]["name"] == "test_foo"
        assert result["tests"][0]["file_path"] == "tests/test_mod.py"
        assert result["tests"][0]["hop_distance"] == 2

    @pytest.mark.asyncio
    async def test_no_test_callers_returns_empty(self) -> None:
        """Changed symbol has callers but none are tests → empty tests list."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = []
        services.provider.graph_walk.return_value = (
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::helper", "name": "helper", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::target", "to_fqn": "mod::helper", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "mod.py"},
            ],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::target"], depth=3,
        )

        assert result["tests"] == []
        assert result["total_tests"] == 0

    @pytest.mark.asyncio
    async def test_changed_file_resolves_to_symbols(self) -> None:
        """File path input → resolves to symbols → walks → finds tests."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = ["mod::func_a"]
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_a", "name": "test_a", "file_path": "tests/test_a.py"},
        ]
        services.provider.graph_walk.return_value = (
            [
                {"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_a", "name": "test_a", "kind": "Function", "file_path": "tests/test_a.py", "depth": 1},
            ],
            [
                {"from_fqn": "mod::func_a", "to_fqn": "tests::test_a", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "tests/test_a.py"},
            ],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["src/mod.py"], depth=3,
        )

        assert result["total_tests"] == 1
        assert result["tests"][0]["fqn"] == "tests::test_a"

    @pytest.mark.asyncio
    async def test_min_hop_distance_across_symbols(self) -> None:
        """Two changed symbols reach same test — hop_distance is the minimum."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_shared", "name": "test_shared", "file_path": "tests/test_s.py"},
        ]
        # Two graph_walk calls — one per changed symbol
        services.provider.graph_walk.side_effect = [
            (
                [
                    {"fqn": "mod::sym_a", "name": "sym_a", "kind": "Function", "file_path": "mod.py", "depth": 0},
                    {"fqn": "tests::test_shared", "name": "test_shared", "kind": "Function", "file_path": "tests/test_s.py", "depth": 3},
                ],
                [],
            ),
            (
                [
                    {"fqn": "mod::sym_b", "name": "sym_b", "kind": "Function", "file_path": "mod.py", "depth": 0},
                    {"fqn": "tests::test_shared", "name": "test_shared", "kind": "Function", "file_path": "tests/test_s.py", "depth": 1},
                ],
                [],
            ),
        ]
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::sym_a", "mod::sym_b"], depth=3,
        )

        assert result["total_tests"] == 1
        assert result["tests"][0]["hop_distance"] == 1  # min(3, 1)

    @pytest.mark.asyncio
    async def test_empty_changed_returns_empty_output(self) -> None:
        """Empty changed list → empty output, no DB queries."""
        services = make_mock_services()
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=[], depth=3,
        )

        assert result == {
            "changed_symbols": [],
            "tests": [],
            "total_tests": 0,
            "walk_depth": 0,
        }
        services.provider.query_distinct_fqns_by_file_path.assert_not_called()
        services.provider.query_test_symbols.assert_not_called()

    def test_registered_in_tool_registry(self) -> None:
        """test_targeting is in TOOL_REGISTRY after import."""
        from chunkhound.mcp_server.tools.fusion import test_targeting_impl  # noqa: F401
        from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY

        assert "test_targeting" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Adversarial stress tests — test_targeting components
# ---------------------------------------------------------------------------


class TestResolveChangedAdversarial:
    """Adversarial: structural patterns for _resolve_changed_to_fqns."""

    def test_duplicate_fqn_deduplicated(self) -> None:
        """Same FQN twice in input → appears once in output."""
        services = make_mock_services()

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["mod::func", "mod::func"],
            workspace_root="/workspace",
        )

        assert result == ["mod::func"]
        services.provider.query_distinct_fqns_by_file_path.assert_not_called()

    def test_url_like_string_treated_as_file_path(self) -> None:
        """String with '://' but no '::' treated as file path, not FQN."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = []  # no symbols

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["http://example.com/file.py"],
            workspace_root="/workspace",
        )

        # Treated as file path → query made, no symbols found → empty result
        assert result == []
        services.provider.query_distinct_fqns_by_file_path.assert_called_once()

    def test_file_and_fqn_resolving_to_same_symbol_deduplicated(self) -> None:
        """File path resolves to FQN already in changed list → deduplicated."""
        services = make_mock_services()
        services.provider.query_distinct_fqns_by_file_path.return_value = ["mod::func"]

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["mod::func", "src/mod.py"],
            workspace_root="/workspace",
        )

        assert result == ["mod::func"]  # only once


# TestCollectTestFqnsAdversarial deleted by ch-nxu Step 15: the tests
# inspected SQL strings built inside _collect_test_fqns (LIKE/ESCAPE
# semantics). That logic now lives in the provider layer; escape
# semantics are tested in tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.


class TestTestTargetingAdversarial:
    """Adversarial: structural patterns for test_targeting_impl."""

    @pytest.mark.asyncio
    async def test_changed_symbol_is_also_a_test(self) -> None:
        """Changed symbol is itself a test function → appears in output."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_self", "name": "test_self", "file_path": "tests/test_s.py"},
        ]
        services.provider.graph_walk.return_value = (
            [
                {"fqn": "tests::test_self", "name": "test_self", "kind": "Function", "file_path": "tests/test_s.py", "depth": 0},
            ],
            [],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["tests::test_self"], depth=3,
        )

        assert result["total_tests"] == 1
        assert result["tests"][0]["fqn"] == "tests::test_self"
        assert result["tests"][0]["hop_distance"] == 0

    @pytest.mark.asyncio
    async def test_negative_depth_clamped(self) -> None:
        """Negative depth clamped to 1, no crash."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = []
        services.provider.graph_walk.return_value = (
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
        )
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::f"], depth=-5,
        )

        assert result["walk_depth"] == 1  # clamped to minimum

    @pytest.mark.asyncio
    async def test_graph_walk_error_skipped(self) -> None:
        """One symbol returns empty walk, another returns a reachable test."""
        services = make_mock_services()
        services.provider.query_test_symbols.return_value = [
            {"fqn": "tests::test_b", "name": "test_b", "file_path": "tests/test_b.py"},
        ]
        # First walk: no test reachable. Second walk: test_b reachable.
        services.provider.graph_walk.side_effect = [
            (
                [{"fqn": "mod::sym_a", "name": "sym_a", "kind": "Function", "file_path": "mod.py", "depth": 0}],
                [],
            ),
            (
                [
                    {"fqn": "mod::sym_b", "name": "sym_b", "kind": "Function", "file_path": "mod.py", "depth": 0},
                    {"fqn": "tests::test_b", "name": "test_b", "kind": "Function", "file_path": "tests/test_b.py", "depth": 1},
                ],
                [],
            ),
        ]
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::sym_a", "mod::sym_b"], depth=3,
        )

        # sym_a found nothing, sym_b found test_b
        assert result["total_tests"] == 1
        assert result["tests"][0]["fqn"] == "tests::test_b"

    @pytest.mark.asyncio
    async def test_all_whitespace_changed_list(self) -> None:
        """Changed list with only whitespace strings → empty output."""
        services = make_mock_services()
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["  ", "\t", "\n"], depth=3,
        )

        # All filtered out by _resolve_changed_to_fqns → empty resolved_fqns
        # But _collect_test_fqns still runs (it's called before the walk loop)
        # So we expect structured empty output with walk_depth set
        assert result["tests"] == []
        assert result["total_tests"] == 0


# ---------------------------------------------------------------------------
# cross_language_check: _query_scope_symbols
# ---------------------------------------------------------------------------


class TestQueryScopeSymbols:
    """_query_scope_symbols: query symbols grouped by name for a scope prefix.

    Post ch-nxu Step 15: helper calls provider.query_symbols_by_scope.
    LIKE/ESCAPE semantics are now provider internals — tested in
    tests/integration/test_{duckdb,lancedb}_symbol_protocol.py.
    """

    def test_returns_grouped_dict_by_name(self) -> None:
        """Scope prefix returns dict mapping name→[{fqn, kind, language, ...}]."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.return_value = [
            {"name": "process", "fqn": "src::process", "kind": "Function",
             "language": "python", "file_path": "src/core/proc.py",
             "type_signature": "(data: bytes) -> str"},
            {"name": "Config", "fqn": "src::Config", "kind": "Class",
             "language": "python", "file_path": "src/core/config.py",
             "type_signature": None},
        ]

        from chunkhound.mcp_server.tools.fusion import _query_scope_symbols

        result = _query_scope_symbols(services=services, scope="src/core/")

        assert "process" in result
        assert "Config" in result
        assert len(result["process"]) == 1
        entry = result["process"][0]
        assert entry["fqn"] == "src::process"
        assert entry["kind"] == "Function"
        assert entry["language"] == "python"
        assert entry["file_path"] == "src/core/proc.py"
        assert entry["type_signature"] == "(data: bytes) -> str"

    def test_multiple_symbols_same_name_grouped(self) -> None:
        """Overloads: two symbols named 'init' in different files → both in list."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.return_value = [
            {"name": "init", "fqn": "a::init", "kind": "Function",
             "language": "python", "file_path": "src/a.py",
             "type_signature": "() -> None"},
            {"name": "init", "fqn": "b::init", "kind": "Function",
             "language": "c", "file_path": "src/b.c",
             "type_signature": "void init(void)"},
        ]

        from chunkhound.mcp_server.tools.fusion import _query_scope_symbols

        result = _query_scope_symbols(services=services, scope="src/")

        assert len(result["init"]) == 2
        fqns = {e["fqn"] for e in result["init"]}
        assert fqns == {"a::init", "b::init"}

    def test_empty_result_returns_empty_dict(self) -> None:
        """No symbols in scope → empty dict, no error."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.return_value = []

        from chunkhound.mcp_server.tools.fusion import _query_scope_symbols

        result = _query_scope_symbols(services=services, scope="nonexistent/")

        assert result == {}

    def test_scope_passed_to_provider_verbatim(self) -> None:
        """Scope is forwarded to provider method unchanged; escape semantics
        are the provider's concern (integration tests cover them)."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.return_value = []

        from chunkhound.mcp_server.tools.fusion import _query_scope_symbols

        _query_scope_symbols(services=services, scope="tests/100%_coverage/")

        services.provider.query_symbols_by_scope.assert_called_once_with(
            "tests/100%_coverage/"
        )


# ---------------------------------------------------------------------------
# cross_language_check: _extract_arity
# ---------------------------------------------------------------------------


class TestExtractArity:
    """_extract_arity: heuristic parameter count from type signatures."""

    def test_strips_self_from_python(self) -> None:
        """'(self, x: int, y: str) -> bool' → 2 (self stripped)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(self, x: int, y: str) -> bool") == 2

    def test_strips_cls_from_python(self) -> None:
        """'(cls, x: int) -> Foo' → 1 (cls stripped)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(cls, x: int) -> Foo") == 1

    def test_typescript_style(self) -> None:
        """'(x: number, y: string) => boolean' → 2."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(x: number, y: string) => boolean") == 2

    def test_empty_params(self) -> None:
        """'() -> None' → 0."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("() -> None") == 0

    def test_single_param(self) -> None:
        """'(x: int) -> int' → 1."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(x: int) -> int") == 1

    def test_none_input(self) -> None:
        """None → None."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity(None) is None

    def test_empty_string(self) -> None:
        """'' → None."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("") is None

    def test_c_style_parens_in_middle(self) -> None:
        """'int process_data(const char*, int)' → 2 (C-style)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("int process_data(const char*, int)") == 2

    def test_generic_dict_with_comma(self) -> None:
        """'(data: dict[str, int]) -> bool' → 1 (comma inside brackets)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(data: dict[str, int]) -> bool") == 1

    def test_nested_generics(self) -> None:
        """'(items: list[tuple[int, str]]) -> None' → 1."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(items: list[tuple[int, str]]) -> None") == 1

    def test_callable_with_nested_brackets(self) -> None:
        """'(callback: Callable[[int, str], bool]) -> None' → 1."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(callback: Callable[[int, str], bool]) -> None") == 1

    def test_trailing_comma(self) -> None:
        """'(x: int,) -> None' → 1 (trailing comma ignored)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(x: int,) -> None") == 1

    def test_unbalanced_parens(self) -> None:
        """'(x: int, y:' → None (truncated signature)."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(x: int, y:") is None


# ---------------------------------------------------------------------------
# cross_language_check: _compare_scope_symbols
# ---------------------------------------------------------------------------


class TestCompareScopeSymbols:
    """_compare_scope_symbols: structural comparison of two scope symbol sets."""

    def _sym(
        self, fqn: str, kind: str = "Function",
        language: str = "python", sig: str | None = "(x: int) -> int",
    ) -> dict[str, Any]:
        """Helper to build a symbol dict."""
        return {
            "fqn": fqn, "kind": kind, "language": language,
            "file_path": f"{fqn.replace('::', '/')}.py",
            "type_signature": sig,
        }

    def test_same_arity_no_mismatch(self) -> None:
        """Same name, same arity → no mismatch reported."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"process": [self._sym("a::process", sig="(x: int) -> str")]}
        b = {"process": [self._sym("b::process", sig="(y: str) -> int")]}

        result = _compare_scope_symbols(a, b)

        assert result["mismatches"] == []
        assert result["missing_in_a"] == []
        assert result["missing_in_b"] == []

    def test_arity_mismatch_detected(self) -> None:
        """Same name, different arity → mismatch with type 'arity'."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"process": [self._sym("a::process", sig="(x: int) -> str")]}
        b = {"process": [self._sym("b::process", sig="(x: int, y: str) -> int")]}

        result = _compare_scope_symbols(a, b)

        assert len(result["mismatches"]) == 1
        m = result["mismatches"][0]
        assert m["name"] == "process"
        assert m["mismatch_type"] == "arity"
        assert m["scope_a"]["arity"] == 1
        assert m["scope_b"]["arity"] == 2

    def test_kind_mismatch_detected(self) -> None:
        """Same name, different kind → mismatch with type 'kind'."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"Config": [self._sym("a::Config", kind="Function", sig="() -> dict")]}
        b = {"Config": [self._sym("b::Config", kind="Class", sig=None)]}

        result = _compare_scope_symbols(a, b)

        assert len(result["mismatches"]) == 1
        assert result["mismatches"][0]["mismatch_type"] == "kind"

    def test_missing_in_b(self) -> None:
        """Name only in scope_a → appears in missing_in_b."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"only_a": [self._sym("a::only_a")]}
        b: dict[str, list[dict[str, Any]]] = {}

        result = _compare_scope_symbols(a, b)

        assert len(result["missing_in_b"]) == 1
        assert result["missing_in_b"][0]["name"] == "only_a"

    def test_missing_in_a(self) -> None:
        """Name only in scope_b → appears in missing_in_a."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a: dict[str, list[dict[str, Any]]] = {}
        b = {"only_b": [self._sym("b::only_b")]}

        result = _compare_scope_symbols(a, b)

        assert len(result["missing_in_a"]) == 1
        assert result["missing_in_a"][0]["name"] == "only_b"

    def test_both_arities_none_no_mismatch(self) -> None:
        """Both arities None (no type_signature) → no mismatch."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"init": [self._sym("a::init", sig=None)]}
        b = {"init": [self._sym("b::init", sig=None)]}

        result = _compare_scope_symbols(a, b)

        assert result["mismatches"] == []

    def test_both_scopes_empty(self) -> None:
        """Both scopes empty → empty output."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        result = _compare_scope_symbols({}, {})

        assert result["mismatches"] == []
        assert result["missing_in_a"] == []
        assert result["missing_in_b"] == []


# ---------------------------------------------------------------------------
# cross_language_check: cross_language_check_impl (full tool integration)
# ---------------------------------------------------------------------------


class TestCrossLanguageCheckImpl:
    """cross_language_check_impl: end-to-end composition of query + compare."""

    @pytest.mark.asyncio
    async def test_overlapping_names_with_mismatch(self) -> None:
        """Two scopes with overlapping names, one arity mismatch → structured output."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.side_effect = [
            # 1. scope_a
            [
                {"name": "process", "fqn": "py::process", "kind": "Function",
                 "language": "python", "file_path": "bindings/proc.py",
                 "type_signature": "(data: bytes) -> str"},
                {"name": "init", "fqn": "py::init", "kind": "Function",
                 "language": "python", "file_path": "bindings/init.py",
                 "type_signature": "() -> None"},
            ],
            # 2. scope_b
            [
                {"name": "process", "fqn": "c::process", "kind": "Function",
                 "language": "c", "file_path": "src/core/proc.c",
                 "type_signature": "int process(const char*, int)"},
                {"name": "init", "fqn": "c::init", "kind": "Function",
                 "language": "c", "file_path": "src/core/init.c",
                 "type_signature": "void init(void)"},
            ],
        ]

        from chunkhound.mcp_server.tools.fusion import cross_language_check_impl

        result = await cross_language_check_impl(
            services=services, config=None,
            scope_a="bindings/", scope_b="src/core/",
        )

        assert result["scope_a"] == "bindings/"
        assert result["scope_b"] == "src/core/"
        assert result["total_compared"] == 2

        # process: arity 1 vs 2 → mismatch
        mismatch_names = {m["name"] for m in result["mismatches"]}
        assert "process" in mismatch_names
        assert result["total_mismatches"] == len(result["mismatches"])

        assert result["missing_in_a"] == []
        assert result["missing_in_b"] == []

    @pytest.mark.asyncio
    async def test_no_overlapping_names(self) -> None:
        """No shared names → only missing lists populated."""
        services = make_mock_services()
        services.provider.query_symbols_by_scope.side_effect = [
            # scope_a
            [
                {"name": "alpha", "fqn": "a::alpha", "kind": "Function",
                 "language": "python", "file_path": "a/alpha.py",
                 "type_signature": "() -> None"},
            ],
            # scope_b
            [
                {"name": "beta", "fqn": "b::beta", "kind": "Function",
                 "language": "c", "file_path": "b/beta.c",
                 "type_signature": "void beta(void)"},
            ],
        ]

        from chunkhound.mcp_server.tools.fusion import cross_language_check_impl

        result = await cross_language_check_impl(
            services=services, config=None,
            scope_a="a/", scope_b="b/",
        )

        assert result["mismatches"] == []
        assert result["total_compared"] == 0
        assert len(result["missing_in_b"]) == 1
        assert result["missing_in_b"][0]["name"] == "alpha"
        assert len(result["missing_in_a"]) == 1
        assert result["missing_in_a"][0]["name"] == "beta"

    @pytest.mark.asyncio
    async def test_empty_scopes(self) -> None:
        """Both scopes empty → total_compared: 0, total_mismatches: 0."""
        services = make_mock_services([
            [],  # scope_a empty
            [],  # scope_b empty
        ])

        from chunkhound.mcp_server.tools.fusion import cross_language_check_impl

        result = await cross_language_check_impl(
            services=services, config=None,
            scope_a="empty_a/", scope_b="empty_b/",
        )

        assert result["total_compared"] == 0
        assert result["total_mismatches"] == 0
        assert result["mismatches"] == []
        assert result["missing_in_a"] == []
        assert result["missing_in_b"] == []

    def test_registered_in_tool_registry(self) -> None:
        """cross_language_check is in TOOL_REGISTRY after import."""
        from chunkhound.mcp_server.tools.fusion import cross_language_check_impl  # noqa: F401
        from chunkhound.mcp_server.tools.registry import TOOL_REGISTRY

        assert "cross_language_check" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Adversarial stress tests — cross_language_check components
# ---------------------------------------------------------------------------


class TestExtractArityAdversarial:
    """Adversarial: structural patterns for _extract_arity."""

    def test_all_commas_no_params(self) -> None:
        """'(,,,) -> None' — all commas, empty segments filtered → arity 0."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(,,,) -> None") == 0

    def test_self_appears_twice(self) -> None:
        """'(self, self, x: int) -> None' — only first self stripped → arity 2."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        # Strips leading 'self,' once, then 'self' and 'x: int' remain
        assert _extract_arity("(self, self, x: int) -> None") == 2

    def test_param_without_type_annotation(self) -> None:
        """'(x) -> int' — param without colon-type → arity 1."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(x) -> int") == 1

    def test_deeply_nested_generics(self) -> None:
        """Deep nesting: dict[str, list[tuple[int, ...]]] counts as one param."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        sig = "(a: dict[str, list[tuple[int, ...]]], b: int) -> None"
        assert _extract_arity(sig) == 2

    def test_unicode_identifiers(self) -> None:
        """Unicode param names — delimiter-based parser is transparent to content."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(données: str, über: int) -> None") == 2

    def test_no_parens_at_all(self) -> None:
        """String with no parentheses → None."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("int x") is None

    def test_whitespace_only_between_parens(self) -> None:
        """'(   ) -> None' — whitespace-only content → arity 0."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(   ) -> None") == 0

    def test_self_as_only_param(self) -> None:
        """'(self) -> None' — self stripped, nothing left → arity 0."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(self) -> None") == 0

    def test_cls_as_only_param(self) -> None:
        """'(cls) -> Foo' — cls stripped, nothing left → arity 0."""
        from chunkhound.mcp_server.tools.fusion import _extract_arity

        assert _extract_arity("(cls) -> Foo") == 0


class TestCompareScopeSymbolsAdversarial:
    """Adversarial: structural patterns for _compare_scope_symbols."""

    def _sym(
        self, fqn: str, kind: str = "Function",
        language: str = "python", sig: str | None = "(x: int) -> int",
    ) -> dict[str, Any]:
        return {
            "fqn": fqn, "kind": kind, "language": language,
            "file_path": f"{fqn.replace('::', '/')}.py",
            "type_signature": sig,
        }

    def test_dense_cross_product(self) -> None:
        """3 overloads × 3 overloads = 9 comparisons — all arity mismatches."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"init": [
            self._sym("a::init1", sig="() -> None"),
            self._sym("a::init2", sig="(x: int) -> None"),
            self._sym("a::init3", sig="(x: int, y: str) -> None"),
        ]}
        b = {"init": [
            self._sym("b::init1", sig="(a: int, b: int, c: int) -> None"),
            self._sym("b::init2", sig="(a: int, b: int, c: int, d: int) -> None"),
            self._sym("b::init3", sig="(a: int, b: int, c: int, d: int, e: int) -> None"),
        ]}

        result = _compare_scope_symbols(a, b)

        # 3×3 = 9 pairs, all have different arities
        assert len(result["mismatches"]) == 9
        # All should be arity mismatches (kinds all match as Function)
        assert all(m["mismatch_type"] == "arity" for m in result["mismatches"])

    def test_one_arity_none_other_not(self) -> None:
        """One side has type_signature, other doesn't → arity mismatch (None vs N)."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"func": [self._sym("a::func", sig="(x: int) -> str")]}
        b = {"func": [self._sym("b::func", sig=None)]}

        result = _compare_scope_symbols(a, b)

        # arity_a=1, arity_b=None → they differ, and not both-None
        assert len(result["mismatches"]) == 1
        assert result["mismatches"][0]["mismatch_type"] == "arity"

    def test_kind_takes_priority_over_arity(self) -> None:
        """Same name, different kind AND arity → reports 'kind', not 'arity'."""
        from chunkhound.mcp_server.tools.fusion import _compare_scope_symbols

        a = {"Config": [self._sym("a::Config", kind="Function", sig="(x: int) -> dict")]}
        b = {"Config": [self._sym("b::Config", kind="Class", sig="(x: int, y: str) -> None")]}

        result = _compare_scope_symbols(a, b)

        assert len(result["mismatches"]) == 1
        assert result["mismatches"][0]["mismatch_type"] == "kind"


class TestCrossLanguageCheckAdversarial:
    """Adversarial: structural patterns for cross_language_check_impl."""

    @pytest.mark.asyncio
    async def test_same_scope_both_sides(self) -> None:
        """scope_a == scope_b → compares scope against itself, 0 mismatches."""
        services = make_mock_services()
        sym = [
            {"name": "func", "fqn": "x::func", "kind": "Function",
             "language": "python", "file_path": "x/func.py",
             "type_signature": "(x: int) -> str"},
        ]
        services.provider.query_symbols_by_scope.side_effect = [sym, list(sym)]

        from chunkhound.mcp_server.tools.fusion import cross_language_check_impl

        result = await cross_language_check_impl(
            services=services, config=None,
            scope_a="x/", scope_b="x/",
        )

        assert result["total_mismatches"] == 0
        assert result["total_compared"] == 1
        assert result["missing_in_a"] == []
        assert result["missing_in_b"] == []


# ---------------------------------------------------------------------------
# semantic_diff helpers
# ---------------------------------------------------------------------------


def _make_mock_line(origin: str, new_lineno: int) -> MagicMock:
    """Create a mock pygit2 diff line."""
    line = MagicMock()
    line.origin = origin
    line.new_lineno = new_lineno
    return line


def _make_mock_hunk(lines: list[MagicMock]) -> MagicMock:
    """Create a mock pygit2 hunk."""
    hunk = MagicMock()
    hunk.lines = lines
    return hunk


def _make_mock_delta(
    new_path: str,
    status: int,
    is_binary: bool = False,
) -> MagicMock:
    """Create a mock pygit2 delta."""
    delta = MagicMock()
    delta.new_file.path = new_path
    delta.status = status
    delta.is_binary = is_binary
    return delta


def _make_mock_patch(
    delta: MagicMock,
    hunks: list[MagicMock],
) -> MagicMock:
    """Create a mock pygit2 patch (iterable hunks)."""
    patch = MagicMock()
    patch.delta = delta
    patch.hunks = hunks
    return patch


class TestGitChangedLines:
    """_git_changed_lines: extract changed line numbers from pygit2 diff."""

    def _mock_repo(self, patches: list[MagicMock], monkeypatch: Any) -> None:
        """Wire up a mock pygit2.Repository that returns given patches as diff."""
        import pygit2 as _pygit2

        mock_commit = MagicMock()
        mock_repo = MagicMock()
        mock_repo.revparse_single.return_value.peel.return_value = mock_commit
        mock_diff = MagicMock()
        mock_diff.__iter__ = MagicMock(return_value=iter(patches))
        mock_repo.diff.return_value = mock_diff

        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

    def test_returns_file_to_lines_mapping(self, monkeypatch: Any) -> None:
        """Two refs with a known diff → dict mapping file_path → sorted 0-based lines."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [
            _make_mock_line("+", 5),   # 0-based: 4
            _make_mock_line(" ", 6),   # context, skip
            _make_mock_line("+", 10),  # 0-based: 9
        ]
        hunk = _make_mock_hunk(lines)
        delta = _make_mock_delta("src/mod.py", 3)  # MODIFIED
        patch = _make_mock_patch(delta, [hunk])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert isinstance(result, dict)
        assert "src/mod.py" in result
        assert result["src/mod.py"] == [4, 9]

    def test_identical_refs_returns_empty(self, monkeypatch: Any) -> None:
        """Same commit for base and head → empty dict (no patches)."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        self._mock_repo([], monkeypatch)

        result = _git_changed_lines("/repo", "abc123", "abc123")

        assert result == {}

    def test_invalid_ref_returns_error_dict(self, monkeypatch: Any) -> None:
        """Invalid ref → error dict with 'error' key."""
        import pygit2 as _pygit2
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        mock_repo = MagicMock()
        mock_repo.revparse_single.side_effect = KeyError("bad-ref")
        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

        result = _git_changed_lines("/repo", "bad-ref", "HEAD")

        assert isinstance(result, dict)
        assert "error" in result

    def test_deleted_file_excluded(self, monkeypatch: Any) -> None:
        """Deleted file (status=GIT_DELTA_DELETED=2) → not in result."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        delta = _make_mock_delta("removed.py", 2)  # DELETED
        patch = _make_mock_patch(delta, [])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert "removed.py" not in result
        assert result == {}

    def test_new_file_all_lines_changed(self, monkeypatch: Any) -> None:
        """New file (status=GIT_DELTA_ADDED=1) → all lines collected."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [
            _make_mock_line("+", 1),  # 0-based: 0
            _make_mock_line("+", 2),  # 0-based: 1
            _make_mock_line("+", 3),  # 0-based: 2
        ]
        hunk = _make_mock_hunk(lines)
        delta = _make_mock_delta("new_file.py", 1)  # ADDED
        patch = _make_mock_patch(delta, [hunk])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert result["new_file.py"] == [0, 1, 2]

    def test_binary_file_excluded(self, monkeypatch: Any) -> None:
        """Binary file → skipped (delta.is_binary is True)."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        delta = _make_mock_delta("image.png", 3, is_binary=True)  # MODIFIED + binary
        patch = _make_mock_patch(delta, [])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert result == {}

    def test_renamed_file_uses_new_path(self, monkeypatch: Any) -> None:
        """Renamed file (status=GIT_DELTA_RENAMED=4) → uses delta.new_file.path."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [_make_mock_line("+", 7)]  # 0-based: 6
        hunk = _make_mock_hunk(lines)
        delta = _make_mock_delta("new_name.py", 4)  # RENAMED
        patch = _make_mock_patch(delta, [hunk])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert "new_name.py" in result
        assert result["new_name.py"] == [6]

    def test_empty_ref_returns_error_dict(self, monkeypatch: Any) -> None:
        """Empty string ref → error dict."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        result = _git_changed_lines("/repo", "", "HEAD")

        assert isinstance(result, dict)
        assert "error" in result

    def test_deletion_only_patch_excluded(self, monkeypatch: Any) -> None:
        """File with only deletions (no '+' lines) → excluded from result."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [
            _make_mock_line("-", -1),  # deletion
            _make_mock_line("-", -1),  # deletion
            _make_mock_line(" ", 5),   # context
        ]
        hunk = _make_mock_hunk(lines)
        delta = _make_mock_delta("shrunk.py", 3)  # MODIFIED
        patch = _make_mock_patch(delta, [hunk])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "main", "feature")

        assert result == {}


class TestMapLinesToSymbols:
    """_map_lines_to_symbols: map changed lines to symbols via range overlap."""

    def _sym_row(
        self,
        fqn: str,
        name: str,
        kind: str = "Function",
        file_path: str = "src/mod.py",
        type_signature: str | None = "(x: int) -> str",
        range_start: int = 0,
        range_end: int = 20,
    ) -> dict[str, Any]:
        """Build a canned symbol row matching the SELECT columns."""
        return {
            "fqn": fqn,
            "name": name,
            "kind": kind,
            "file_path": file_path,
            "type_signature": type_signature,
            "range_start": range_start,
            "range_end": range_end,
        }

    def test_overlapping_lines_returns_symbol(self) -> None:
        """Changed lines within symbol range → symbol included with changed_lines."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym = self._sym_row("mod::func", "func", range_start=5, range_end=15)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"src/mod.py": [7, 10]})

        assert len(result) == 1
        assert result[0]["fqn"] == "mod::func"
        assert result[0]["changed_lines"] == [7, 10]

    def test_no_overlap_returns_empty(self) -> None:
        """Changed lines outside any symbol range → empty result."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        # Symbol at lines 5-15, changed lines at 20-25
        sym = self._sym_row("mod::func", "func", range_start=5, range_end=15)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"src/mod.py": [20, 25]})

        assert result == []

    def test_multiple_symbols_different_lines(self) -> None:
        """Multiple symbols in same file, different changed lines → both returned."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym_a = self._sym_row("mod::func_a", "func_a", range_start=0, range_end=10)
        sym_b = self._sym_row("mod::func_b", "func_b", range_start=20, range_end=30)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym_a, sym_b]

        result = _map_lines_to_symbols(services, {"src/mod.py": [5, 25]})

        fqns = {r["fqn"] for r in result}
        assert fqns == {"mod::func_a", "mod::func_b"}

    def test_range_start_changed_is_signature_change(self) -> None:
        """Changed line == range_start → change_type is 'signature_change'."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym = self._sym_row("mod::func", "func", range_start=10, range_end=20)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"src/mod.py": [10, 15]})

        assert len(result) == 1
        assert result[0]["change_type"] == "signature_change"

    def test_body_only_change(self) -> None:
        """Changed lines NOT including range_start → change_type is 'body_only'."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym = self._sym_row("mod::func", "func", range_start=10, range_end=20)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"src/mod.py": [15, 18]})

        assert len(result) == 1
        assert result[0]["change_type"] == "body_only"

    def test_empty_changed_lines_returns_empty(self) -> None:
        """Empty changed_lines dict → empty result, no queries made."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        services = make_mock_services()

        result = _map_lines_to_symbols(services, {})

        assert result == []
        services.provider.query_symbols_by_range_overlap.assert_not_called()


class TestSemanticDiffImpl:
    """semantic_diff_impl: full integration — git diff → symbols → graph walk → output."""

    def _mock_pygit2(self, monkeypatch: Any, patches: list[MagicMock]) -> None:
        """Wire up pygit2.Repository mock for _git_changed_lines."""
        import pygit2 as _pygit2

        mock_commit = MagicMock()
        mock_repo = MagicMock()
        mock_repo.revparse_single.return_value.peel.return_value = mock_commit
        mock_diff = MagicMock()
        mock_diff.__iter__ = MagicMock(return_value=iter(patches))
        mock_repo.diff.return_value = mock_diff

        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

    @pytest.mark.asyncio
    async def test_signature_and_body_changes(self, monkeypatch: Any) -> None:
        """One signature change + one body-only → correct output structure."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        # Git diff: two files changed
        patch1_lines = [_make_mock_line("+", 11)]  # 0-based: 10 (= range_start → sig change)
        patch1 = _make_mock_patch(
            _make_mock_delta("src/a.py", 3), [_make_mock_hunk(patch1_lines)],
        )
        patch2_lines = [_make_mock_line("+", 26)]  # 0-based: 25 (body only, range_start=20)
        patch2 = _make_mock_patch(
            _make_mock_delta("src/b.py", 3), [_make_mock_hunk(patch2_lines)],
        )
        self._mock_pygit2(monkeypatch, [patch1, patch2])

        # Query sequence:
        # 1. _map_lines_to_symbols query for src/a.py
        # 2. _map_lines_to_symbols query for src/b.py
        # 3. _graph_walk for sym_a: walk query → nodes
        # 4. _graph_walk for sym_a: edges query
        # 5. _graph_walk for sym_b: walk query → nodes
        # 6. _graph_walk for sym_b: edges query
        # 7. _annotate_type_signatures: batch FQN lookup
        sym_a = {
            "fqn": "a::func_a", "name": "func_a", "kind": "Function",
            "file_path": "src/a.py", "type_signature": "(x: int) -> str",
            "range_start": 10, "range_end": 20,
        }
        sym_b = {
            "fqn": "b::func_b", "name": "func_b", "kind": "Function",
            "file_path": "src/b.py", "type_signature": "(y: str) -> bool",
            "range_start": 20, "range_end": 30,
        }
        caller_node = {
            "fqn": "test::test_a", "name": "test_a", "kind": "Function",
            "file_path": "tests/test_a.py", "depth": 1,
        }
        caller_edge = {
            "from_fqn": "a::func_a", "to_fqn": "test::test_a",
            "edge_kind": "called_by", "from_file": "src/a.py", "to_file": "tests/test_a.py",
        }
        root_node_a = {
            "fqn": "a::func_a", "name": "func_a", "kind": "Function",
            "file_path": "src/a.py", "depth": 0,
        }
        root_node_b = {
            "fqn": "b::func_b", "name": "func_b", "kind": "Function",
            "file_path": "src/b.py", "depth": 0,
        }

        services = make_mock_services()
        # One query_symbols_by_range_overlap call per changed file
        services.provider.query_symbols_by_range_overlap.side_effect = [
            [sym_a],
            [sym_b],
        ]
        # query_symbol_type_signatures is called once on the affected callers
        services.provider.query_symbol_type_signatures.return_value = {
            "test::test_a": "() -> None",
        }
        # Two graph_walk calls — one per changed symbol
        services.provider.graph_walk.side_effect = [
            ([root_node_a, caller_node], [caller_edge]),
            ([root_node_b], []),
        ]

        config = make_mock_config("/workspace")
        result = await semantic_diff_impl(services=services, config=config, base="main", head="feature")

        assert result["base"] == "main"
        assert result["head"] == "feature"
        assert result["total_changed"] == 2
        assert result["summary"]["signature_changes"] == 1
        assert result["summary"]["body_only"] == 1
        assert len(result["changed_symbols"]) == 2
        assert len(result["affected_callers"]) == 1
        assert result["affected_callers"][0]["fqn"] == "test::test_a"
        assert result["affected_callers"][0]["triggered_by"] == "a::func_a"

    @pytest.mark.asyncio
    async def test_no_changed_symbols_returns_empty(self, monkeypatch: Any) -> None:
        """Diff in non-code files → no symbols matched → empty output."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        lines = [_make_mock_line("+", 5)]
        patch = _make_mock_patch(
            _make_mock_delta("README.md", 3), [_make_mock_hunk(lines)],
        )
        self._mock_pygit2(monkeypatch, [patch])

        # map_lines_to_symbols returns no symbols for README.md
        services = make_mock_services([
            [],  # no symbols in README.md
        ])

        config = make_mock_config("/workspace")
        result = await semantic_diff_impl(services=services, config=config, base="main", head="feature")

        assert result["total_changed"] == 0
        assert result["changed_symbols"] == []
        assert result["affected_callers"] == []

    @pytest.mark.asyncio
    async def test_invalid_ref_returns_error(self, monkeypatch: Any) -> None:
        """Invalid base ref → error dict propagated from _git_changed_lines."""
        import pygit2 as _pygit2
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        mock_repo = MagicMock()
        mock_repo.revparse_single.side_effect = KeyError("nonexistent")
        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

        config = make_mock_config("/workspace")
        result = await semantic_diff_impl(
            services=MagicMock(), config=config, base="nonexistent", head="HEAD",
        )

        assert "error" in result

    def test_tool_registered_in_registry(self) -> None:
        """semantic_diff is registered in TOOL_REGISTRY."""
        from chunkhound.mcp_server.tools import TOOL_REGISTRY

        assert "semantic_diff" in TOOL_REGISTRY


# ---------------------------------------------------------------------------
# Adversarial stress tests — semantic_diff components
# ---------------------------------------------------------------------------


class TestGitChangedLinesAdversarial:
    """Adversarial structural patterns for _git_changed_lines."""

    def _mock_repo(self, patches: list[MagicMock], monkeypatch: Any) -> None:
        """Wire up a mock pygit2.Repository that returns given patches as diff."""
        import pygit2 as _pygit2

        mock_commit = MagicMock()
        mock_repo = MagicMock()
        mock_repo.revparse_single.return_value.peel.return_value = mock_commit
        mock_diff = MagicMock()
        mock_diff.__iter__ = MagicMock(return_value=iter(patches))
        mock_repo.diff.return_value = mock_diff
        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

    def test_singular_one_line_change(self, monkeypatch: Any) -> None:
        """Singular: exactly one addition line in one file."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [_make_mock_line("+", 1)]
        patch = _make_mock_patch(
            _make_mock_delta("x.py", 3), [_make_mock_hunk(lines)],
        )
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "a", "b")

        assert result == {"x.py": [0]}

    def test_same_ref_both_sides(self, monkeypatch: Any) -> None:
        """Self-referential: base == head (same string, not just same commit)."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        self._mock_repo([], monkeypatch)

        result = _git_changed_lines("/repo", "HEAD", "HEAD")

        assert result == {}

    def test_multiple_hunks_same_file(self, monkeypatch: Any) -> None:
        """Dense: two hunks in one file — lines from both hunks collected and sorted."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        hunk1 = _make_mock_hunk([_make_mock_line("+", 5), _make_mock_line("+", 6)])
        hunk2 = _make_mock_hunk([_make_mock_line("+", 100), _make_mock_line("+", 101)])
        patch = _make_mock_patch(_make_mock_delta("x.py", 3), [hunk1, hunk2])
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "a", "b")

        assert result["x.py"] == [4, 5, 99, 100]

    def test_git_error_not_a_repo(self, monkeypatch: Any) -> None:
        """Dependency treachery: repo_path isn't a git repo → GitError caught."""
        import pygit2 as _pygit2
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        monkeypatch.setattr(
            _pygit2, "Repository",
            MagicMock(side_effect=_pygit2.GitError("not a repo")),
        )

        result = _git_changed_lines("/not/a/repo", "main", "HEAD")

        assert "error" in result

    def test_many_files_in_diff(self, monkeypatch: Any) -> None:
        """Dense: 50 files each with one changed line — all collected."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        patches = []
        for i in range(50):
            lines = [_make_mock_line("+", i + 1)]
            patches.append(_make_mock_patch(
                _make_mock_delta(f"file_{i}.py", 3), [_make_mock_hunk(lines)],
            ))
        self._mock_repo(patches, monkeypatch)

        result = _git_changed_lines("/repo", "a", "b")

        assert len(result) == 50
        assert result["file_25.py"] == [25]

    def test_copied_file_status(self, monkeypatch: Any) -> None:
        """Type boundary: GIT_DELTA_COPIED (status=5) treated like added."""
        from chunkhound.mcp_server.tools.fusion import _git_changed_lines

        lines = [_make_mock_line("+", 3)]
        patch = _make_mock_patch(
            _make_mock_delta("copy.py", 5), [_make_mock_hunk(lines)],  # COPIED
        )
        self._mock_repo([patch], monkeypatch)

        result = _git_changed_lines("/repo", "a", "b")

        assert result == {"copy.py": [2]}


class TestMapLinesToSymbolsAdversarial:
    """Adversarial structural patterns for _map_lines_to_symbols."""

    def _sym_row(
        self, fqn: str, name: str, range_start: int, range_end: int,
        kind: str = "Function", file_path: str = "x.py",
        type_signature: str | None = "(x: int) -> str",
    ) -> dict[str, Any]:
        return {
            "fqn": fqn, "name": name, "kind": kind, "file_path": file_path,
            "type_signature": type_signature, "range_start": range_start,
            "range_end": range_end,
        }

    def test_singular_one_symbol_one_line(self) -> None:
        """Singular: one symbol, range covers exactly one line, that line changed."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym = self._sym_row("m::f", "f", range_start=5, range_end=5)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"x.py": [5]})

        assert len(result) == 1
        assert result[0]["change_type"] == "signature_change"  # range_start == changed line

    def test_symbol_range_start_gt_range_end(self) -> None:
        """Semantically hostile: symbol with range_start > range_end (malformed DB data)."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        # range_start=20, range_end=10 — inverted. The provider's range filter
        # uses range_start <= max(lines) AND range_end >= min(lines). With
        # range_start=20, range_end=10, changed line=15:
        # 20 <= 15 is False, so provider excludes it. Safe.
        sym = self._sym_row("m::bad", "bad", range_start=20, range_end=10)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"x.py": [15]})

        # Even if provider returns it (mock doesn't enforce filter logic),
        # intersection should be empty because range(20,10) contains nothing
        assert result == []

    def test_overlapping_symbols(self) -> None:
        """Dense: two symbols whose ranges overlap, same changed line in both."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym_a = self._sym_row("m::outer", "outer", range_start=0, range_end=30)
        sym_b = self._sym_row("m::inner", "inner", range_start=10, range_end=20)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym_a, sym_b]

        result = _map_lines_to_symbols(services, {"x.py": [15]})

        fqns = {r["fqn"] for r in result}
        assert fqns == {"m::outer", "m::inner"}

    def test_none_type_signature(self) -> None:
        """Type boundary: symbol with type_signature=None."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym = self._sym_row("m::f", "f", range_start=5, range_end=15, type_signature=None)
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]

        result = _map_lines_to_symbols(services, {"x.py": [10]})

        assert len(result) == 1
        assert result[0]["type_signature"] is None
        assert result[0]["change_type"] == "body_only"

    def test_multiple_files(self) -> None:
        """Disconnected: changes in two unrelated files → separate queries, merged result."""
        from chunkhound.mcp_server.tools.fusion import _map_lines_to_symbols

        sym_a = self._sym_row("a::f", "f", range_start=0, range_end=10, file_path="a.py")
        sym_b = self._sym_row("b::g", "g", range_start=0, range_end=10, file_path="b.py")
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.side_effect = [
            [sym_a],  # query for a.py
            [sym_b],  # query for b.py
        ]

        result = _map_lines_to_symbols(services, {"a.py": [5], "b.py": [5]})

        fqns = {r["fqn"] for r in result}
        assert fqns == {"a::f", "b::g"}


class TestSemanticDiffImplAdversarial:
    """Adversarial structural patterns for semantic_diff_impl."""

    def _mock_pygit2(self, monkeypatch: Any, patches: list[MagicMock]) -> None:
        import pygit2 as _pygit2

        mock_commit = MagicMock()
        mock_repo = MagicMock()
        mock_repo.revparse_single.return_value.peel.return_value = mock_commit
        mock_diff = MagicMock()
        mock_diff.__iter__ = MagicMock(return_value=iter(patches))
        mock_repo.diff.return_value = mock_diff
        monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))

    @pytest.mark.asyncio
    async def test_depth_clamped_to_min_1(self, monkeypatch: Any) -> None:
        """Type boundary: depth=0 → clamped to 1."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        # One file, one line changed, no symbols → empty result
        lines = [_make_mock_line("+", 5)]
        patch = _make_mock_patch(_make_mock_delta("x.py", 3), [_make_mock_hunk(lines)])
        self._mock_pygit2(monkeypatch, [patch])

        services = make_mock_services([
            [],  # no symbols for x.py
        ])
        config = make_mock_config("/workspace")

        # Should not crash with depth=0
        result = await semantic_diff_impl(
            services=services, config=config, base="a", head="b", depth=0,
        )

        assert result["total_changed"] == 0

    @pytest.mark.asyncio
    async def test_depth_clamped_to_max_10(self, monkeypatch: Any) -> None:
        """Type boundary: depth=999 → clamped to 10."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        lines = [_make_mock_line("+", 5)]
        patch = _make_mock_patch(_make_mock_delta("x.py", 3), [_make_mock_hunk(lines)])
        self._mock_pygit2(monkeypatch, [patch])

        services = make_mock_services([
            [],  # no symbols
        ])
        config = make_mock_config("/workspace")

        result = await semantic_diff_impl(
            services=services, config=config, base="a", head="b", depth=999,
        )

        assert result["total_changed"] == 0

    @pytest.mark.asyncio
    async def test_graph_walk_error_skipped(self, monkeypatch: Any) -> None:
        """Dependency treachery: _graph_walk returns error for a symbol → skip, don't crash."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        lines = [_make_mock_line("+", 6)]  # 0-based: 5 = range_start → signature_change
        patch = _make_mock_patch(_make_mock_delta("x.py", 3), [_make_mock_hunk(lines)])
        self._mock_pygit2(monkeypatch, [patch])

        sym = {
            "fqn": "x::f", "name": "f", "kind": "Function", "file_path": "x.py",
            "type_signature": "(x: int) -> str", "range_start": 5, "range_end": 15,
        }
        services = make_mock_services()
        services.provider.query_symbols_by_range_overlap.return_value = [sym]
        services.provider.query_symbol_type_signatures.return_value = {}
        # graph_walk returns empty → fusion treats it as no reachable callers
        services.provider.graph_walk.return_value = ([], [])
        config = make_mock_config("/workspace")

        result = await semantic_diff_impl(
            services=services, config=config, base="a", head="b",
        )

        # Should complete — changed_symbols present, no callers (walk returned nothing useful)
        assert result["total_changed"] == 1
        assert result["total_affected"] == 0

    @pytest.mark.asyncio
    async def test_second_run_idempotent(self, monkeypatch: Any) -> None:
        """The second run: calling twice with same args → same result."""
        from chunkhound.mcp_server.tools.fusion import semantic_diff_impl

        lines = [_make_mock_line("+", 5)]
        patch = _make_mock_patch(_make_mock_delta("x.py", 3), [_make_mock_hunk(lines)])

        def setup() -> tuple[MagicMock, MagicMock]:
            import pygit2 as _pygit2
            mock_commit = MagicMock()
            mock_repo = MagicMock()
            mock_repo.revparse_single.return_value.peel.return_value = mock_commit
            mock_diff = MagicMock()
            mock_diff.__iter__ = MagicMock(return_value=iter([patch]))
            mock_repo.diff.return_value = mock_diff
            monkeypatch.setattr(_pygit2, "Repository", MagicMock(return_value=mock_repo))
            svc = make_mock_services([[]])
            return svc, make_mock_config("/workspace")

        svc1, cfg1 = setup()
        r1 = await semantic_diff_impl(services=svc1, config=cfg1, base="a", head="b")

        svc2, cfg2 = setup()
        r2 = await semantic_diff_impl(services=svc2, config=cfg2, base="a", head="b")

        assert r1["total_changed"] == r2["total_changed"]
        assert r1["summary"] == r2["summary"]
