"""Graph MCP tool — thin functions composing validation, query builders, and formatters.

Each operation: validate params → build SQL via queries/graph.py → execute → format.
"""

from typing import Any

from .formatters import format_edge, format_node
from .queries.graph import (
    build_boundary_query,
    build_overview_breakdown_query,
    build_overview_query,
    build_reachability_all_symbols_query,
    build_reachability_reachable_query,
    build_walk_edges_query,
    build_walk_query,
)
from .registry import register_tool
from .validation import clamp, require_param

GRAPH_DESCRIPTION = (
    "Query the pre-computed symbol dependency graph from the DuckDB database. "
    "All operations are deterministic DuckDB queries — no live LSP calls. "
    "Operations:\n"
    "  walk: Traverse connected symbols from a starting FQN. Returns nodes + edges. "
    "Params: symbol (required), depth (1-20, default 2), edge_kind (optional filter), limit (max nodes, 1-100, default 20).\n"
    "  reachability: Find symbols unreachable from any entry point in a scope. "
    "Params: scope (required, path prefix like 'chunkhound/mcp_server/').\n"
    "  boundary: Find edges that cross a scope boundary (internal→external or external→internal). "
    "Params: scope (required).\n"
    "  overview: Return the most-connected symbols with per-edge_kind breakdown. "
    "Params: scope (optional), limit (1-100, default 20).\n"
    "Edge kinds in the graph: 'defines', 'references', 'implements', 'called_by', 'calls'. "
    "If results are empty, the graph may not be populated — check lsp_status or get_stats."
)


@register_tool(
    description=GRAPH_DESCRIPTION,
    name="graph",
)
async def graph_impl(
    services: Any,
    operation: str,
    symbol: str | None = None,
    depth: int = 2,
    edge_kind: str | None = None,
    scope: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Unified graph query tool dispatching to walk/reachability/boundary/overview."""
    valid_ops = {"walk", "reachability", "boundary", "overview"}
    if operation not in valid_ops:
        return {
            "error": "invalid_operation",
            "message": f"Unknown operation '{operation}'. Valid: {', '.join(sorted(valid_ops))}",
        }

    depth = clamp(depth, 1, 20)
    limit = clamp(limit, 1, 100)

    if operation == "walk":
        return _graph_walk(services, symbol, depth, edge_kind, limit)
    elif operation == "reachability":
        return _graph_reachability(services, scope, limit)
    elif operation == "boundary":
        return _graph_boundary(services, scope, limit)
    else:
        return _graph_overview(services, scope, limit)


def _graph_walk(
    services: Any,
    symbol: str | None,
    depth: int,
    edge_kind: str | None,
    limit: int,
    directed: bool = False,
) -> dict[str, Any]:
    """Walk connected symbols from a starting FQN."""
    err = require_param("symbol", symbol)
    if err:
        return err

    assert symbol is not None  # narrowing after require_param

    sql, params = build_walk_query(
        symbol=symbol,
        depth=depth,
        edge_kind=edge_kind,
        limit=limit,
        directed=directed,
    )
    nodes = services.provider.execute_query(sql, params)

    fqns = [n["fqn"] for n in nodes]
    if not fqns:
        return {"results": [], "edges": [], "count": 0}

    edges_sql, edges_params = build_walk_edges_query(fqns=fqns, edge_kind=edge_kind)
    raw_edges = services.provider.execute_query(edges_sql, edges_params)

    return {
        "results": [format_node(n) for n in nodes],
        "edges": [format_edge(e) for e in raw_edges],
        "count": len(nodes),
    }


def _graph_reachability(
    services: Any,
    scope: str | None,
    limit: int,
) -> dict[str, Any]:
    """Find symbols unreachable from scope entry points."""
    err = require_param("scope", scope)
    if err:
        return err

    assert scope is not None

    # Query 1: all symbols in scope
    all_sql, all_params = build_reachability_all_symbols_query(scope=scope)
    all_symbols = services.provider.execute_query(all_sql, all_params)

    # Query 2: reachable FQNs via outbound-only CTE
    reach_sql, reach_params = build_reachability_reachable_query(scope=scope)
    reachable_rows = services.provider.execute_query(reach_sql, reach_params)
    reachable_fqns = {r["fqn"] for r in reachable_rows}

    # Set difference in Python
    unreachable = [
        {
            "fqn": s["fqn"],
            "name": s["name"],
            "kind": s["kind"],
            "file_path": s["file_path"],
        }
        for s in all_symbols
        if s["fqn"] not in reachable_fqns
    ][:limit]

    return {"unreachable": unreachable, "count": len(unreachable)}


def _graph_boundary(
    services: Any,
    scope: str | None,
    limit: int,
) -> dict[str, Any]:
    """Find edges crossing a scope boundary."""
    err = require_param("scope", scope)
    if err:
        return err

    assert scope is not None

    sql, params = build_boundary_query(scope=scope, limit=limit)
    raw_edges = services.provider.execute_query(sql, params)

    edges = [
        {
            "from_symbol": e["from_fqn"],
            "from_name": e["from_name"],
            "from_kind": e["from_kind"],
            "from_file": e["from_file"],
            "to_symbol": e["to_fqn"],
            "to_name": e["to_name"],
            "to_kind": e["to_kind"],
            "to_file": e["to_file"],
            "edge_kind": e["edge_kind"],
        }
        for e in raw_edges
    ]

    return {"edges": edges, "count": len(edges)}


def _graph_overview(
    services: Any,
    scope: str | None,
    limit: int,
) -> dict[str, Any]:
    """Return most-connected symbols with edge_kind breakdown."""
    sql, params = build_overview_query(scope=scope, limit=limit)
    top_symbols = services.provider.execute_query(sql, params)

    if not top_symbols:
        return {"symbols": [], "count": 0}

    fqns = [s["fqn"] for s in top_symbols]
    breakdown_sql, breakdown_params = build_overview_breakdown_query(fqns=fqns)
    breakdown_rows = services.provider.execute_query(breakdown_sql, breakdown_params)

    breakdown_map: dict[str, dict[str, int]] = {}
    for row in breakdown_rows:
        fqn = row["fqn"]
        if fqn not in breakdown_map:
            breakdown_map[fqn] = {}
        breakdown_map[fqn][row["edge_kind"]] = row["edge_count"]

    symbols = [
        {
            "fqn": s["fqn"],
            "name": s["name"],
            "kind": s["kind"],
            "file_path": s["file_path"],
            "total_edges": s["total_edges"],
            "breakdown": breakdown_map.get(s["fqn"], {}),
        }
        for s in top_symbols
    ]

    return {"symbols": symbols, "count": len(symbols)}
