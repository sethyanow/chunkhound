"""Tests for chunkhound.mcp_server.tools.fusion — fusion MCP tools.

Fusion tools compose Phase 3 primitives (_graph_walk + symbols table)
into higher-level queries. All are deterministic — no LLM, no embeddings.
"""

import pytest

from tests.lsp.mcp_tool_helpers import make_mock_config, make_mock_services

pytestmark = pytest.mark.unit


class TestResolveStartFqn:
    """_resolve_start_fqn: resolve file position to containing symbol's FQN."""

    def test_returns_fqn_for_known_position(self) -> None:
        """Symbol at (line=10, char=5) resolves to its FQN."""
        services = make_mock_services([
            [{"fqn": "module::MyClass::method"}],
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        result = _resolve_start_fqn(
            services=services, file="src/module.py", line=10, character=5,
            workspace_root="/workspace",
        )

        assert result == "module::MyClass::method"

    def test_converts_1based_line_to_0based_for_db(self) -> None:
        """1-based input line=10 becomes 0-based 9 in DB query params."""
        services = make_mock_services([
            [{"fqn": "mod::func"}],
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        _resolve_start_fqn(
            services=services, file="src/mod.py", line=10, character=1,
            workspace_root="/workspace",
        )

        call_args = services.provider.execute_query.call_args
        params = call_args[0][1]  # second positional arg = params list
        # range_start <= 9 AND range_end >= 9  (0-based)
        assert params[1] == 9
        assert params[2] == 9

    def test_returns_error_dict_when_no_symbol_at_position(self) -> None:
        """Position outside any symbol returns error dict with 'error' key."""
        services = make_mock_services([
            [],  # no symbol at position
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        result = _resolve_start_fqn(
            services=services, file="src/module.py", line=99, character=1,
            workspace_root="/workspace",
        )

        assert isinstance(result, dict)
        assert "error" in result


class TestAnnotateTypeSignatures:
    """_annotate_type_signatures: batch-query type_signature from symbols table."""

    def test_merges_signatures_into_nodes(self) -> None:
        """Nodes receive type_signature from DB lookup by FQN."""
        services = make_mock_services([
            [
                {"fqn": "mod::func_a", "type_signature": "(int) -> str"},
                {"fqn": "mod::func_b", "type_signature": "(str) -> bool"},
            ],
        ])
        nodes = [
            {"fqn": "mod::func_a", "name": "func_a"},
            {"fqn": "mod::func_b", "name": "func_b"},
        ]

        from chunkhound.mcp_server.tools.fusion import _annotate_type_signatures

        result = _annotate_type_signatures(services, nodes)

        assert result[0]["type_signature"] == "(int) -> str"
        assert result[1]["type_signature"] == "(str) -> bool"

    def test_null_signature_becomes_none(self) -> None:
        """Nodes not in DB get type_signature=None, no crash."""
        services = make_mock_services([
            [{"fqn": "mod::func_a", "type_signature": None}],
        ])
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
        services.provider.execute_query.assert_not_called()


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
        services = make_mock_services([
            # 1. _resolve_start_fqn: symbol lookup
            [{"fqn": "mod::target"}],
            # 2. _graph_walk: nodes from walk CTE
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::caller_a", "name": "caller_a", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            # 3. _graph_walk: edges between discovered FQNs
            [
                {"from_fqn": "mod::target", "to_fqn": "mod::caller_a", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "mod.py"},
            ],
            # 4. _annotate_type_signatures: type lookups
            [
                {"fqn": "mod::target", "type_signature": "(int) -> str"},
                {"fqn": "mod::caller_a", "type_signature": "(str) -> None"},
            ],
        ])
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
        services = make_mock_services([
            [{"fqn": "mod::leaf"}],  # resolve
            [{"fqn": "mod::leaf", "name": "leaf", "kind": "Function", "file_path": "mod.py", "depth": 0}],  # nodes
            [],  # no edges
            [{"fqn": "mod::leaf", "type_signature": None}],  # signatures
        ])
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
        services = make_mock_services([
            [{"fqn": "mod::func"}],
            [{"fqn": "mod::func", "name": "func", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
            [],  # no signatures at all
        ])
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
        services = make_mock_services([
            [{"fqn": "mod::f"}],
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
            [],
        ])
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
        services = make_mock_services([
            [{"fqn": "mod::f"}],
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            [],
            [],
        ])
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import impact_cascade_impl

        result = await impact_cascade_impl(
            services=services, config=config,
            file="mod.py", line=1, character=1, depth=999,
        )

        assert "root" in result

    def test_line_one_converts_to_zero_based(self) -> None:
        """line=1 (minimum valid) converts to 0-based 0 in DB query."""
        services = make_mock_services([
            [{"fqn": "mod::f"}],
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_start_fqn

        _resolve_start_fqn(
            services=services, file="mod.py", line=1, character=1,
            workspace_root="/workspace",
        )

        params = services.provider.execute_query.call_args[0][1]
        assert params[1] == 0  # 1-based 1 → 0-based 0
        assert params[2] == 0
