"""Fusion MCP tools — compose Phase 3 primitives into higher-level queries.

All fusion tools are deterministic (no LLM, no embeddings). They combine
graph walks, symbol lookups, and type signatures into single-call answers
for common agent questions.
"""

import os
from pathlib import Path
from typing import Any

from .graph import _graph_walk
from .queries.common import escape_like
from .registry import register_tool


def _resolve_changed_to_fqns(
    services: Any,
    changed: list[str],
    workspace_root: str,
) -> list[str]:
    """Resolve a mixed list of file paths and FQNs to a deduplicated FQN list.

    Strings containing '::' are treated as FQNs and pass through unchanged.
    Other strings are treated as file paths: normalized to relative paths via
    os.path.relpath, then all symbols in that file are looked up.

    Args:
        services: DatabaseServices instance
        changed: List of file paths or FQN strings
        workspace_root: Project root for path normalization

    Returns:
        Deduplicated list of FQN strings.
    """
    if not changed:
        return []

    fqns: list[str] = []
    seen: set[str] = set()

    for item in changed:
        if not item or not item.strip():
            continue

        if "::" in item:
            # FQN — pass through
            if item not in seen:
                fqns.append(item)
                seen.add(item)
        else:
            # File path — resolve to relative and query symbols
            relative_path = os.path.relpath(
                str(Path(item).resolve()), workspace_root
            )
            rows = services.provider.execute_query(
                "SELECT DISTINCT fqn FROM symbols WHERE file_path = ?",
                [relative_path],
            )
            for row in rows:
                fqn = row["fqn"]
                if fqn not in seen:
                    fqns.append(fqn)
                    seen.add(fqn)

    return fqns


def _collect_test_fqns(
    services: Any,
    test_scope: str | None = None,
) -> dict[str, dict[str, str]]:
    """Collect test entry point symbols from the symbols table.

    Test functions are identified by kind='Function' and name starting
    with 'test_'. An optional test_scope narrows results to files under
    a specific path prefix.

    Args:
        services: DatabaseServices instance
        test_scope: Optional path prefix to restrict test file lookup

    Returns:
        Dict mapping FQN → {name, file_path} for each test function.
    """
    sql = (
        "SELECT fqn, name, file_path FROM symbols "
        "WHERE kind = 'Function' AND name LIKE 'test_%'"
    )
    params: list[str] = []

    if test_scope:
        escaped = escape_like(test_scope)
        sql += " AND file_path LIKE ?"
        params.append(f"{escaped}%")

    rows = services.provider.execute_query(sql, params)

    return {
        row["fqn"]: {"name": row["name"], "file_path": row["file_path"]}
        for row in rows
    }


def _resolve_start_fqn(
    services: Any,
    file: str,
    line: int,
    character: int,
    workspace_root: str,
) -> str | dict[str, Any]:
    """Resolve a file position to the containing symbol's FQN.

    Args:
        services: DatabaseServices instance
        file: Path to source file (absolute, relative, or file:// URI)
        line: 1-based line number
        character: 1-based character offset (accepted for API consistency, unused in query)
        workspace_root: Project root for path normalization

    Returns:
        FQN string if symbol found, or error dict with 'error' key.
    """
    # Handle file:// URI input
    resolved_file = file
    if file.startswith("file://"):
        resolved_file = file[len("file://"):]

    # Normalize to relative path matching symbols.file_path format
    relative_path = os.path.relpath(str(Path(resolved_file).resolve()), workspace_root)

    # Convert 1-based line to 0-based for DB range query
    db_line = line - 1

    rows = services.provider.execute_query(
        "SELECT fqn FROM symbols WHERE file_path = ? "
        "AND range_start <= ? AND range_end >= ? "
        "ORDER BY (range_end - range_start) ASC LIMIT 1",
        [relative_path, db_line, db_line],
    )

    if not rows:
        return {
            "error": "no_symbol_at_position",
            "message": f"No symbol found at {file}:{line}:{character}",
        }

    return rows[0]["fqn"]


def _annotate_type_signatures(
    services: Any,
    nodes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Batch-query type_signature from symbols table and merge into nodes.

    Args:
        services: DatabaseServices instance
        nodes: Walk result nodes, each with at least an 'fqn' key

    Returns:
        Same nodes list with 'type_signature' added to each node.
        Nodes without a matching symbol get type_signature=None.
    """
    if not nodes:
        return []

    fqns = [n["fqn"] for n in nodes]
    placeholders = ", ".join(["?"] * len(fqns))
    rows = services.provider.execute_query(
        f"SELECT fqn, type_signature FROM symbols WHERE fqn IN ({placeholders})",
        fqns,
    )

    sig_map: dict[str, str | None] = {r["fqn"]: r["type_signature"] for r in rows}

    for node in nodes:
        node["type_signature"] = sig_map.get(node["fqn"])

    return nodes


def _build_caller_tree(
    root_fqn: str,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reconstruct a hierarchical caller tree from flat walk results.

    Edge semantics: (from_fqn, to_fqn, "called_by") means from_fqn is called
    by to_fqn — so to_fqn is a child in the tree (it's a caller).

    Args:
        root_fqn: FQN of the root symbol
        nodes: Flat list of walk result nodes with fqn, name, kind, file_path, depth
        edges: Edges between nodes with from_fqn, to_fqn, edge_kind

    Returns:
        Nested tree dict with children arrays and hop_distance, or error dict.
    """
    # Build node lookup
    node_map: dict[str, dict[str, Any]] = {}
    for node in nodes:
        node_map[node["fqn"]] = node

    if root_fqn not in node_map:
        return {
            "error": "root_not_in_results",
            "message": f"Root FQN {root_fqn} not found in walk results",
        }

    # Build adjacency: parent_fqn → [child_fqn, ...]
    # Edge (from_symbol, to_symbol, called_by) means to_symbol calls from_symbol
    # In the tree: from_symbol is parent, to_symbol is child (caller)
    # Keys use format_edge names (from_symbol/to_symbol), not raw DB names.
    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        parent = edge.get("from_symbol") or edge.get("from_fqn", "")
        child = edge.get("to_symbol") or edge.get("to_fqn", "")
        # Only include edges where both endpoints are in our node set
        if parent in node_map and child in node_map:
            adjacency.setdefault(parent, []).append(child)

    # Recursive tree build with visited tracking to prevent cycles
    def build_subtree(fqn: str, visited: set[str]) -> dict[str, Any]:
        visited.add(fqn)
        node = node_map[fqn]
        children_fqns = [c for c in adjacency.get(fqn, []) if c not in visited]
        return {
            "fqn": node["fqn"],
            "name": node["name"],
            "kind": node["kind"],
            "file_path": node["file_path"],
            "hop_distance": node["depth"],
            "type_signature": node.get("type_signature"),
            "children": [build_subtree(c, visited) for c in children_fqns],
        }

    return build_subtree(root_fqn, set())


# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------

IMPACT_CASCADE_DESCRIPTION = """Transitive caller tree with type annotations.

Given a file position, finds the containing symbol and walks its caller graph
to the specified depth. Each node includes type_signature for classifying
mechanical vs logic impact. Returns a structured tree, not prose.

Use this to answer: "What breaks if I change this symbol?"

Parameters:
  file: Path to the source file (relative, absolute, or file:// URI)
  line: 1-based line number
  character: 1-based character offset
  depth: How many hops of callers to traverse (1-10, default 3)
"""


@register_tool(
    description=IMPACT_CASCADE_DESCRIPTION,
    name="impact_cascade",
)
async def impact_cascade_impl(
    services: Any,
    config: Any,
    file: str,
    line: int,
    character: int,
    depth: int = 3,
) -> dict[str, Any]:
    """Transitive caller tree with type annotations.

    Args:
        services: DatabaseServices instance
        config: Config instance (provides target_dir as workspace_root)
        file: Path to source file
        line: 1-based line number
        character: 1-based character offset
        depth: Caller hops to traverse (1-10, default 3)
    """
    # Clamp depth 1-10
    depth = max(1, min(10, depth))

    # Resolve workspace root
    workspace_root = str(
        config.target_dir
        if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Step 1: Resolve position to FQN
    fqn = _resolve_start_fqn(services, file, line, character, workspace_root)
    if isinstance(fqn, dict):
        return fqn  # error dict

    # Step 2: Walk caller graph (directed, forward-only)
    walk_result = _graph_walk(
        services=services,
        symbol=fqn,
        depth=depth,
        edge_kind="called_by",
        limit=100,
        directed=True,
    )

    nodes = walk_result["results"]
    edges = walk_result["edges"]

    # Step 3: Annotate with type signatures
    _annotate_type_signatures(services, nodes)

    # Step 4: Build nested tree
    tree = _build_caller_tree(fqn, nodes, edges)
    if isinstance(tree, dict) and "error" in tree:
        return tree

    # Compute summary stats
    def count_nodes(node: dict[str, Any]) -> int:
        return 1 + sum(count_nodes(c) for c in node.get("children", []))

    def max_tree_depth(node: dict[str, Any]) -> int:
        children = node.get("children", [])
        if not children:
            return node.get("hop_distance", 0)
        return max(max_tree_depth(c) for c in children)

    return {
        "root": tree,
        "total_nodes": count_nodes(tree),
        "max_depth": max_tree_depth(tree),
    }


# ---------------------------------------------------------------------------
# test_targeting tool
# ---------------------------------------------------------------------------

TEST_TARGETING_DESCRIPTION = """Minimal test set from changed symbols.

Given changed files or symbol FQNs, walks their caller graph and intersects
with test entry points to return the minimal set of tests affected by the
changes. Each test includes hop_distance (shortest caller chain from any
changed symbol).

Use this to answer: "Which tests should I run after changing these files?"

Parameters:
  changed: List of file paths or symbol FQNs (strings with '::' are FQNs)
  depth: How many hops of callers to traverse (1-10, default 3)
  test_scope: Optional path prefix to restrict test file lookup
"""


@register_tool(
    description=TEST_TARGETING_DESCRIPTION,
    name="test_targeting",
)
async def test_targeting_impl(
    services: Any,
    config: Any,
    changed: list[str],
    depth: int = 3,
    test_scope: str | None = None,
) -> dict[str, Any]:
    """Minimal test set from changed symbols.

    Args:
        services: DatabaseServices instance
        config: Config instance (provides target_dir as workspace_root)
        changed: List of file paths or symbol FQNs
        depth: Caller hops to traverse (1-10, default 3)
        test_scope: Optional path prefix to restrict test file lookup
    """
    # Early return on empty input
    if not changed:
        return {
            "changed_symbols": [],
            "tests": [],
            "total_tests": 0,
            "walk_depth": 0,
        }

    # Clamp depth 1-10
    depth = max(1, min(10, depth))

    # Resolve workspace root
    workspace_root = str(
        config.target_dir
        if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Step 1: Resolve changed inputs to FQNs
    resolved_fqns = _resolve_changed_to_fqns(services, changed, workspace_root)

    # Step 2: Collect test entry points
    test_dict = _collect_test_fqns(services, test_scope=test_scope)

    # Step 3: Per-symbol walk, merge reachable FQNs with minimum depth
    reachable: dict[str, int] = {}  # fqn → min depth

    for symbol_fqn in resolved_fqns:
        walk_result = _graph_walk(
            services=services,
            symbol=symbol_fqn,
            depth=depth,
            edge_kind="called_by",
            limit=100,
            directed=True,
        )

        # Defensive: skip if _graph_walk returns error dict
        if "error" in walk_result:
            continue

        for node in walk_result["results"]:
            fqn = node["fqn"]
            node_depth = node["depth"]
            if fqn not in reachable or node_depth < reachable[fqn]:
                reachable[fqn] = node_depth

    # Step 4: Intersect reachable FQNs with test set
    test_fqns = set(test_dict.keys()) & set(reachable.keys())

    tests = sorted(
        [
            {
                "fqn": fqn,
                "name": test_dict[fqn]["name"],
                "file_path": test_dict[fqn]["file_path"],
                "hop_distance": reachable[fqn],
            }
            for fqn in test_fqns
        ],
        key=lambda t: (t["hop_distance"], t["fqn"]),
    )

    return {
        "changed_symbols": resolved_fqns,
        "tests": tests,
        "total_tests": len(tests),
        "walk_depth": depth,
    }
