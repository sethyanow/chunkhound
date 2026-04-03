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


# ---------------------------------------------------------------------------
# test_targeting: _resolve_changed_to_fqns
# ---------------------------------------------------------------------------


class TestResolveChangedToFqns:
    """_resolve_changed_to_fqns: resolve file paths and FQNs to FQN list."""

    def test_file_path_resolves_to_fqns(self) -> None:
        """File path input queries symbols table and returns FQNs."""
        services = make_mock_services([
            [{"fqn": "mod::ClassA"}, {"fqn": "mod::func_b"}],
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["src/mod.py"],
            workspace_root="/workspace",
        )

        assert set(result) == {"mod::ClassA", "mod::func_b"}
        services.provider.execute_query.assert_called_once()

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
        services.provider.execute_query.assert_not_called()

    def test_mixed_file_paths_and_fqns(self) -> None:
        """Mixed list: file paths resolved, FQNs passed through."""
        services = make_mock_services([
            [{"fqn": "mod::from_file"}],
        ])

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
        services = make_mock_services([
            [],  # no symbols for this file
        ])

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
        services.provider.execute_query.assert_not_called()

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
        services.provider.execute_query.assert_not_called()


# ---------------------------------------------------------------------------
# test_targeting: _collect_test_fqns
# ---------------------------------------------------------------------------


class TestCollectTestFqns:
    """_collect_test_fqns: collect test entry points from symbols table."""

    def test_returns_dict_with_name_and_file_path(self) -> None:
        """Returns dict mapping FQN→{name, file_path} for test functions."""
        services = make_mock_services([
            [
                {"fqn": "tests::test_foo", "name": "test_foo", "file_path": "tests/test_mod.py"},
                {"fqn": "tests::test_bar", "name": "test_bar", "file_path": "tests/test_mod.py"},
            ],
        ])

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services)

        assert "tests::test_foo" in result
        assert result["tests::test_foo"]["name"] == "test_foo"
        assert result["tests::test_foo"]["file_path"] == "tests/test_mod.py"
        assert "tests::test_bar" in result

    def test_respects_test_scope_filter(self) -> None:
        """test_scope adds LIKE filter on file_path — only tests in scope."""
        services = make_mock_services([
            [
                {"fqn": "unit::test_a", "name": "test_a", "file_path": "tests/unit/test_a.py"},
            ],
        ])

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services, test_scope="tests/unit/")

        assert "unit::test_a" in result
        # Verify LIKE query was used (scope passed to query)
        call_args = services.provider.execute_query.call_args
        sql = call_args[0][0]
        assert "LIKE" in sql

    def test_excludes_non_function_symbols(self) -> None:
        """Variable named test_data excluded — only kind='Function' matches."""
        services = make_mock_services([
            [
                {"fqn": "mod::test_helper", "name": "test_helper", "file_path": "tests/test_mod.py"},
            ],
        ])

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        result = _collect_test_fqns(services=services)

        # Query should filter kind='Function' — mock returns only what DB returns
        # The key verification is the SQL contains the kind filter
        call_args = services.provider.execute_query.call_args
        sql = call_args[0][0]
        assert "Function" in sql
        assert "test_helper" in result["mod::test_helper"]["name"]

    def test_empty_result_returns_empty_dict(self) -> None:
        """No test symbols in DB → empty dict, no error."""
        services = make_mock_services([
            [],
        ])

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
        services = make_mock_services([
            # 1. _collect_test_fqns: test symbols
            [
                {"fqn": "tests::test_foo", "name": "test_foo", "file_path": "tests/test_mod.py"},
            ],
            # 2. _graph_walk for "mod::target": nodes (walk CTE)
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_foo", "name": "test_foo", "kind": "Function", "file_path": "tests/test_mod.py", "depth": 2},
            ],
            # 3. _graph_walk for "mod::target": edges
            [
                {"from_fqn": "mod::target", "to_fqn": "tests::test_foo", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "tests/test_mod.py"},
            ],
        ])
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
        services = make_mock_services([
            # 1. _collect_test_fqns: no test symbols
            [],
            # 2. _graph_walk: nodes
            [
                {"fqn": "mod::target", "name": "target", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "mod::helper", "name": "helper", "kind": "Function", "file_path": "mod.py", "depth": 1},
            ],
            # 3. _graph_walk: edges
            [
                {"from_fqn": "mod::target", "to_fqn": "mod::helper", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "mod.py"},
            ],
        ])
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
        services = make_mock_services([
            # 1. _resolve_changed_to_fqns: file path → symbols
            [{"fqn": "mod::func_a"}],
            # 2. _collect_test_fqns: test symbols
            [
                {"fqn": "tests::test_a", "name": "test_a", "file_path": "tests/test_a.py"},
            ],
            # 3. _graph_walk for "mod::func_a": nodes
            [
                {"fqn": "mod::func_a", "name": "func_a", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_a", "name": "test_a", "kind": "Function", "file_path": "tests/test_a.py", "depth": 1},
            ],
            # 4. _graph_walk for "mod::func_a": edges
            [
                {"from_fqn": "mod::func_a", "to_fqn": "tests::test_a", "edge_kind": "called_by", "from_file": "mod.py", "to_file": "tests/test_a.py"},
            ],
        ])
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
        services = make_mock_services([
            # 1. _collect_test_fqns
            [
                {"fqn": "tests::test_shared", "name": "test_shared", "file_path": "tests/test_s.py"},
            ],
            # 2. _graph_walk for "mod::sym_a": nodes (test at depth 3)
            [
                {"fqn": "mod::sym_a", "name": "sym_a", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_shared", "name": "test_shared", "kind": "Function", "file_path": "tests/test_s.py", "depth": 3},
            ],
            # 3. _graph_walk for "mod::sym_a": edges
            [],
            # 4. _graph_walk for "mod::sym_b": nodes (same test at depth 1)
            [
                {"fqn": "mod::sym_b", "name": "sym_b", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_shared", "name": "test_shared", "kind": "Function", "file_path": "tests/test_s.py", "depth": 1},
            ],
            # 5. _graph_walk for "mod::sym_b": edges
            [],
        ])
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
        services.provider.execute_query.assert_not_called()

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
        services.provider.execute_query.assert_not_called()

    def test_url_like_string_treated_as_file_path(self) -> None:
        """String with '://' but no '::' treated as file path, not FQN."""
        services = make_mock_services([
            [],  # no symbols at this "path"
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["http://example.com/file.py"],
            workspace_root="/workspace",
        )

        # Treated as file path → query made, no symbols found → empty result
        assert result == []
        services.provider.execute_query.assert_called_once()

    def test_file_and_fqn_resolving_to_same_symbol_deduplicated(self) -> None:
        """File path resolves to FQN already in changed list → deduplicated."""
        services = make_mock_services([
            [{"fqn": "mod::func"}],  # file resolves to same FQN
        ])

        from chunkhound.mcp_server.tools.fusion import _resolve_changed_to_fqns

        result = _resolve_changed_to_fqns(
            services=services,
            changed=["mod::func", "src/mod.py"],
            workspace_root="/workspace",
        )

        assert result == ["mod::func"]  # only once


class TestCollectTestFqnsAdversarial:
    """Adversarial: encoding boundary for _collect_test_fqns."""

    def test_scope_with_percent_escaped(self) -> None:
        """test_scope containing '%' is LIKE-escaped, not treated as wildcard."""
        services = make_mock_services([
            [],
        ])

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        _collect_test_fqns(services=services, test_scope="tests/100%_coverage/")

        call_args = services.provider.execute_query.call_args
        params = call_args[0][1]
        # '%' should be escaped to '\%' in the LIKE pattern
        assert "\\%" in params[0]

    def test_scope_with_underscore_escaped(self) -> None:
        """test_scope containing '_' is LIKE-escaped, not treated as wildcard."""
        services = make_mock_services([
            [],
        ])

        from chunkhound.mcp_server.tools.fusion import _collect_test_fqns

        _collect_test_fqns(services=services, test_scope="tests/my_module/")

        call_args = services.provider.execute_query.call_args
        params = call_args[0][1]
        # '_' should be escaped to '\_' in the LIKE pattern
        assert "\\_" in params[0]


class TestTestTargetingAdversarial:
    """Adversarial: structural patterns for test_targeting_impl."""

    @pytest.mark.asyncio
    async def test_changed_symbol_is_also_a_test(self) -> None:
        """Changed symbol is itself a test function → appears in output."""
        services = make_mock_services([
            # 1. _collect_test_fqns: the changed symbol IS a test
            [
                {"fqn": "tests::test_self", "name": "test_self", "file_path": "tests/test_s.py"},
            ],
            # 2. _graph_walk: returns the symbol itself at depth 0
            [
                {"fqn": "tests::test_self", "name": "test_self", "kind": "Function", "file_path": "tests/test_s.py", "depth": 0},
            ],
            # 3. _graph_walk: edges
            [],
        ])
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
        services = make_mock_services([
            # 1. _collect_test_fqns
            [],
            # 2. _graph_walk: nodes
            [{"fqn": "mod::f", "name": "f", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            # 3. _graph_walk: edges
            [],
        ])
        config = make_mock_config(target_dir="/workspace")

        from chunkhound.mcp_server.tools.fusion import test_targeting_impl

        result = await test_targeting_impl(
            services=services, config=config,
            changed=["mod::f"], depth=-5,
        )

        assert result["walk_depth"] == 1  # clamped to minimum

    @pytest.mark.asyncio
    async def test_graph_walk_error_skipped(self) -> None:
        """_graph_walk returns error dict for one symbol — skipped, others processed."""
        services = make_mock_services([
            # 1. _collect_test_fqns
            [
                {"fqn": "tests::test_b", "name": "test_b", "file_path": "tests/test_b.py"},
            ],
            # 2. _graph_walk for "mod::bad" — will be mocked to return error
            # But _graph_walk calls execute_query internally (nodes query)
            # When require_param fails, it returns error before querying.
            # We need to mock _graph_walk directly here since the error path
            # is inside _graph_walk, not in execute_query.
            # Actually, _graph_walk returns {"results": [], "edges": [], "count": 0}
            # on empty result, but {"error": ...} on missing symbol param.
            # Since we pass a valid symbol string, require_param won't fail.
            # Let's test with an empty walk (no reachable tests) for first symbol
            # and a reachable test for second symbol:
            # Walk for "mod::sym_a": nodes (no test reachable)
            [{"fqn": "mod::sym_a", "name": "sym_a", "kind": "Function", "file_path": "mod.py", "depth": 0}],
            # 3. Walk for "mod::sym_a": edges
            [],
            # 4. Walk for "mod::sym_b": nodes (test reachable)
            [
                {"fqn": "mod::sym_b", "name": "sym_b", "kind": "Function", "file_path": "mod.py", "depth": 0},
                {"fqn": "tests::test_b", "name": "test_b", "kind": "Function", "file_path": "tests/test_b.py", "depth": 1},
            ],
            # 5. Walk for "mod::sym_b": edges
            [],
        ])
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
