"""Fusion MCP tools — compose Phase 3 primitives into higher-level queries.

All fusion tools are deterministic (no LLM, no embeddings). They combine
graph walks, symbol lookups, and type signatures into single-call answers
for common agent questions.
"""

import os
from pathlib import Path
from typing import Any

import pygit2

from .graph import _graph_walk
from .queries.common import escape_like, scope_filter
from .registry import register_tool

# ---------------------------------------------------------------------------
# semantic_diff helpers — git integration
# ---------------------------------------------------------------------------

_GIT_DELTA_DELETED = 2


def _git_changed_lines(
    repo_path: str,
    base: str,
    head: str,
) -> dict[str, list[int]] | dict[str, str]:
    """Extract changed line numbers from a git diff between two refs.

    Returns a dict mapping file_path → sorted list of 0-based changed line
    numbers (addition-side only). Binary files and deleted files are excluded.

    On error (invalid ref, bad repo), returns a dict with an 'error' key.

    Args:
        repo_path: Path to the git repository
        base: Base git ref (branch, tag, SHA, HEAD~N, etc.)
        head: Head git ref

    Returns:
        File→lines mapping, or error dict.
    """
    # Validate non-empty refs
    if not base or not head:
        return {
            "error": "invalid_ref",
            "message": "Base and head refs must be non-empty strings",
        }

    try:
        repo = pygit2.Repository(repo_path)
        base_commit = repo.revparse_single(base).peel(pygit2.Commit)
        head_commit = repo.revparse_single(head).peel(pygit2.Commit)
    except (KeyError, pygit2.GitError) as e:
        return {"error": "invalid_ref", "message": str(e)}

    diff = repo.diff(base_commit, head_commit)

    result: dict[str, list[int]] = {}
    for patch in diff:
        delta = patch.delta

        # Skip binary files and deleted files
        if delta.is_binary or delta.status == _GIT_DELTA_DELETED:
            continue

        file_path = delta.new_file.path
        lines: list[int] = []

        for hunk in patch.hunks:
            for line in hunk.lines:
                if line.origin == "+" and line.new_lineno > 0:
                    lines.append(line.new_lineno - 1)  # Convert 1-based to 0-based

        # Skip files with no addition lines (deletion-only patches)
        if lines:
            result[file_path] = sorted(lines)

    return result


def _map_lines_to_symbols(
    services: Any,
    changed_lines: dict[str, list[int]],
) -> list[dict[str, Any]]:
    """Map changed lines to symbols via range overlap and classify changes.

    For each file with changed lines, queries symbols whose range overlaps the
    changed region. Symbols with no actual overlapping lines are excluded.
    Classification: range_start in changed_lines → "signature_change", else "body_only".

    Args:
        services: DatabaseServices instance
        changed_lines: Dict mapping file_path → sorted list of 0-based changed line numbers

    Returns:
        List of symbol dicts with fqn, name, kind, file_path, type_signature,
        change_type, and changed_lines.
    """
    if not changed_lines:
        return []

    result: list[dict[str, Any]] = []

    for file_path, lines in changed_lines.items():
        if not lines:
            continue

        min_line = min(lines)
        max_line = max(lines)
        line_set = set(lines)

        # Broad query: symbols whose range overlaps [min_line, max_line]
        rows = services.provider.execute_query(
            "SELECT fqn, name, kind, file_path, type_signature, range_start, range_end "
            "FROM symbols WHERE file_path = ? AND range_start <= ? AND range_end >= ?",
            [file_path, max_line, min_line],
        )

        for row in rows:
            # Narrow: intersect symbol range with actual changed lines
            sym_lines = [ln for ln in lines if row["range_start"] <= ln <= row["range_end"]]

            # Exclude symbols with no overlapping lines
            if not sym_lines:
                continue

            # Classify: range_start touched → signature_change
            change_type = "signature_change" if row["range_start"] in line_set else "body_only"

            result.append(
                {
                    "fqn": row["fqn"],
                    "name": row["name"],
                    "kind": row["kind"],
                    "file_path": row["file_path"],
                    "type_signature": row["type_signature"],
                    "change_type": change_type,
                    "changed_lines": sym_lines,
                }
            )

    return result


def _query_scope_symbols(
    services: Any,
    scope: str,
) -> dict[str, list[dict[str, Any]]]:
    """Query symbols grouped by name for a scope prefix.

    Args:
        services: DatabaseServices instance
        scope: File path prefix to scope the query

    Returns:
        Dict mapping symbol name → list of symbol dicts, each with
        fqn, kind, language, file_path, type_signature.
    """
    scope_sql, scope_params = scope_filter(scope)
    sql = f"SELECT name, fqn, kind, language, file_path, type_signature FROM symbols WHERE {scope_sql}"
    rows = services.provider.execute_query(sql, scope_params)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        entry = {
            "fqn": row["fqn"],
            "kind": row["kind"],
            "language": row["language"],
            "file_path": row["file_path"],
            "type_signature": row["type_signature"],
        }
        grouped.setdefault(row["name"], []).append(entry)

    return grouped


def _extract_arity(type_signature: str | None) -> int | None:
    """Extract parameter count from a type signature string.

    Heuristic parser that handles Python, TypeScript, and C-style signatures.
    Tracks nesting depth to avoid counting commas inside generic types.

    Args:
        type_signature: Type signature string, or None

    Returns:
        Parameter count, or None if signature is missing/unparseable.
    """
    if not type_signature:
        return None

    # Find first '(' and its matching ')' tracking nesting
    open_idx = type_signature.find("(")
    if open_idx == -1:
        return None

    depth = 1
    close_idx = -1
    for i in range(open_idx + 1, len(type_signature)):
        ch = type_signature[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                close_idx = i
                break

    if close_idx == -1:
        return None  # unbalanced parens

    content = type_signature[open_idx + 1 : close_idx].strip()

    # Strip leading self/cls (Python convention)
    for prefix in ("self,", "cls,"):
        if content.startswith(prefix):
            content = content[len(prefix) :].strip()
            break
        # Handle 'self' or 'cls' as only param
        if content == prefix.rstrip(","):
            return 0

    if not content:
        return 0

    # Count commas at nesting depth 0 only
    # Track [, (, {, < for generic type parameters
    nesting = 0
    segments = []
    start = 0
    for i, ch in enumerate(content):
        if ch in "([{<":
            nesting += 1
        elif ch in ")]}>" and nesting > 0:
            nesting -= 1
        elif ch == "," and nesting == 0:
            segments.append(content[start:i].strip())
            start = i + 1

    segments.append(content[start:].strip())

    # Filter out empty segments (handles trailing commas)
    segments = [s for s in segments if s]

    return len(segments)


def _compare_scope_symbols(
    symbols_a: dict[str, list[dict[str, Any]]],
    symbols_b: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Compare two scope symbol sets and find mismatches.

    Pure function. For matched names (intersection), compares kind and arity
    via cross-product. Reports missing symbols from symmetric difference.

    Args:
        symbols_a: Grouped symbols from scope A (name → [symbol dicts])
        symbols_b: Grouped symbols from scope B (name → [symbol dicts])

    Returns:
        Dict with mismatches, missing_in_a, missing_in_b lists.
    """
    names_a = set(symbols_a.keys())
    names_b = set(symbols_b.keys())

    matched = names_a & names_b
    only_a = names_a - names_b
    only_b = names_b - names_a

    mismatches: list[dict[str, Any]] = []

    for name in sorted(matched):
        entries_a = symbols_a[name]
        entries_b = symbols_b[name]

        # Cross-product comparison
        for ea in entries_a:
            for eb in entries_b:
                # Kind mismatch takes priority
                if ea["kind"] != eb["kind"]:
                    mismatches.append(
                        {
                            "name": name,
                            "scope_a": {
                                **ea,
                                "arity": _extract_arity(ea.get("type_signature")),
                            },
                            "scope_b": {
                                **eb,
                                "arity": _extract_arity(eb.get("type_signature")),
                            },
                            "mismatch_type": "kind",
                        }
                    )
                    continue

                # Arity comparison
                arity_a = _extract_arity(ea.get("type_signature"))
                arity_b = _extract_arity(eb.get("type_signature"))

                # Both None → insufficient data, not a mismatch
                if arity_a is None and arity_b is None:
                    continue

                if arity_a != arity_b:
                    mismatches.append(
                        {
                            "name": name,
                            "scope_a": {**ea, "arity": arity_a},
                            "scope_b": {**eb, "arity": arity_b},
                            "mismatch_type": "arity",
                        }
                    )

    # Build missing lists
    missing_in_b = [
        {
            "name": name,
            "fqn": symbols_a[name][0]["fqn"],
            "kind": symbols_a[name][0]["kind"],
        }
        for name in sorted(only_a)
    ]
    missing_in_a = [
        {
            "name": name,
            "fqn": symbols_b[name][0]["fqn"],
            "kind": symbols_b[name][0]["kind"],
        }
        for name in sorted(only_b)
    ]

    return {
        "mismatches": mismatches,
        "missing_in_a": missing_in_a,
        "missing_in_b": missing_in_b,
    }


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
            relative_path = os.path.relpath(str(Path(item).resolve()), workspace_root)
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
    sql = "SELECT fqn, name, file_path FROM symbols WHERE kind = 'Function' AND name LIKE 'test_%'"
    params: list[str] = []

    if test_scope:
        escaped = escape_like(test_scope)
        sql += " AND file_path LIKE ? ESCAPE '!'"
        params.append(f"{escaped}%")

    rows = services.provider.execute_query(sql, params)

    return {row["fqn"]: {"name": row["name"], "file_path": row["file_path"]} for row in rows}


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
        resolved_file = file[len("file://") :]

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
        config.target_dir if config and hasattr(config, "target_dir") and config.target_dir else Path(".").resolve()
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
        config.target_dir if config and hasattr(config, "target_dir") and config.target_dir else Path(".").resolve()
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


# ---------------------------------------------------------------------------
# cross_language_check tool
# ---------------------------------------------------------------------------

CROSS_LANGUAGE_CHECK_DESCRIPTION = """Compare exported symbols across two scopes.

Given two file path prefixes (scopes), queries all symbols in each scope,
matches by name, and compares arity to find binding mismatches. Pure
symbols-table comparison — deterministic, no LLM, no graph walks.

Use this to answer: "Do the Python bindings match the C core API?"

Parameters:
  scope_a: File path prefix for the first scope (e.g., "bindings/python/")
  scope_b: File path prefix for the second scope (e.g., "src/core/")
"""


@register_tool(
    description=CROSS_LANGUAGE_CHECK_DESCRIPTION,
    name="cross_language_check",
)
async def cross_language_check_impl(
    services: Any,
    config: Any,
    scope_a: str,
    scope_b: str,
) -> dict[str, Any]:
    """Compare exported symbols across two scopes.

    Args:
        services: DatabaseServices instance
        config: Config instance (unused — no workspace root needed)
        scope_a: File path prefix for first scope
        scope_b: File path prefix for second scope
    """
    # Query symbols in each scope
    symbols_a = _query_scope_symbols(services, scope_a)
    symbols_b = _query_scope_symbols(services, scope_b)

    # Compare
    comparison = _compare_scope_symbols(symbols_a, symbols_b)

    # Count names in intersection
    names_a = set(symbols_a.keys())
    names_b = set(symbols_b.keys())
    total_compared = len(names_a & names_b)

    return {
        "scope_a": scope_a,
        "scope_b": scope_b,
        "mismatches": comparison["mismatches"],
        "missing_in_a": comparison["missing_in_a"],
        "missing_in_b": comparison["missing_in_b"],
        "total_compared": total_compared,
        "total_mismatches": len(comparison["mismatches"]),
    }


# ---------------------------------------------------------------------------
# semantic_diff tool
# ---------------------------------------------------------------------------

SEMANTIC_DIFF_DESCRIPTION = """Behavior-level change classification between git refs.

Given two git refs (branches, tags, SHAs, HEAD~N), identifies changed symbols,
walks their caller graph, and classifies affected callers. Distinguishes
signature changes (declaration line modified) from body-only changes.

Use this to answer: "What changed between these commits and what's affected?"

Parameters:
  base: Base git ref (e.g., "main", "HEAD~3", commit SHA)
  head: Head git ref (e.g., "feature-branch", "HEAD")
  depth: How many hops of callers to traverse (1-10, default 3)
"""


@register_tool(
    description=SEMANTIC_DIFF_DESCRIPTION,
    name="semantic_diff",
)
async def semantic_diff_impl(
    services: Any,
    config: Any,
    base: str,
    head: str,
    depth: int = 3,
) -> dict[str, Any]:
    """Behavior-level change classification between git refs.

    Args:
        services: DatabaseServices instance
        config: Config instance (provides target_dir as workspace_root)
        base: Base git ref
        head: Head git ref
        depth: Caller hops to traverse (1-10, default 3)
    """
    # Clamp depth 1-10
    depth = max(1, min(10, depth))

    # Resolve workspace root
    workspace_root = str(
        config.target_dir if config and hasattr(config, "target_dir") and config.target_dir else Path(".").resolve()
    )

    # Step 1: Get changed lines from git diff
    changed_lines = _git_changed_lines(workspace_root, base, head)
    if "error" in changed_lines:
        return changed_lines

    # Step 2: Map changed lines to symbols
    changed_symbols = _map_lines_to_symbols(services, changed_lines)

    # Step 3: Walk caller graph for each changed symbol
    reachable: dict[str, dict[str, Any]] = {}  # fqn → {hop_distance, triggered_by}

    for sym in changed_symbols:
        walk_result = _graph_walk(
            services=services,
            symbol=sym["fqn"],
            depth=depth,
            edge_kind="called_by",
            limit=100,
            directed=True,
        )

        # Skip if _graph_walk returns error dict
        if "error" in walk_result:
            continue

        for node in walk_result["results"]:
            fqn = node["fqn"]
            node_depth = node["depth"]
            # Skip the root symbol itself (depth 0)
            if node_depth == 0:
                continue
            if fqn not in reachable or node_depth < reachable[fqn]["hop_distance"]:
                reachable[fqn] = {
                    "fqn": fqn,
                    "name": node["name"],
                    "kind": node["kind"],
                    "file_path": node["file_path"],
                    "hop_distance": node_depth,
                    "triggered_by": sym["fqn"],
                }

    # Step 4: Annotate affected callers with type signatures
    affected_list = sorted(
        reachable.values(),
        key=lambda x: (x["hop_distance"], x["fqn"]),
    )
    _annotate_type_signatures(services, affected_list)

    # Step 5: Build summary
    sig_count = sum(1 for s in changed_symbols if s["change_type"] == "signature_change")
    body_count = sum(1 for s in changed_symbols if s["change_type"] == "body_only")

    return {
        "base": base,
        "head": head,
        "changed_symbols": changed_symbols,
        "affected_callers": affected_list,
        "total_changed": len(changed_symbols),
        "total_affected": len(affected_list),
        "summary": {
            "signature_changes": sig_count,
            "body_only": body_count,
        },
    }
