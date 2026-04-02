"""Reusable sqlglot query fragments for MCP tool query builders."""

from typing import Any

import sqlglot
from sqlglot import exp


def escape_like(value: str) -> str:
    """Escape LIKE-special characters in a user-provided string."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def bidirectional_edges() -> exp.Union:
    """UNION ALL subquery normalizing symbol_edges to both directions.

    Returns a UNION of (from_fqn AS src, to_fqn AS dst, edge_kind)
    and (to_fqn AS src, from_fqn AS dst, edge_kind).
    """
    forward = sqlglot.parse_one(
        "SELECT from_fqn AS src, to_fqn AS dst, edge_kind FROM symbol_edges",
        dialect="duckdb",
    )
    reverse = sqlglot.parse_one(
        "SELECT to_fqn AS src, from_fqn AS dst, edge_kind FROM symbol_edges",
        dialect="duckdb",
    )
    return exp.Union(this=forward, expression=reverse, distinct=False)


def scope_filter(
    scope: str, column: str = "file_path"
) -> tuple[exp.Expr, list[Any]]:
    """Generate a LIKE clause with ESCAPE for scope filtering.

    Returns (expression, params) where expression is a LIKE condition
    and params contains the escaped scope pattern with wildcard.
    """
    escaped = escape_like(scope)
    pattern = escaped + "%"

    like_expr = sqlglot.parse_one(
        f"{column} LIKE ? ESCAPE '\\\\'",
        dialect="duckdb",
    )
    return like_expr, [pattern]


def visited_tracking_columns(
    table: str, column: str
) -> tuple[exp.Expr, exp.Expr]:
    """Generate list_concat (append) and list_contains (cycle check) expressions.

    Used in recursive CTEs to track visited nodes and prevent cycles.

    Returns:
        (append_expr, contains_expr) — sqlglot expressions for:
        - list_concat(r.visited, [table.column])
        - list_contains(r.visited, table.column)
    """
    append_expr = sqlglot.parse_one(
        f"list_concat(r.visited, [{table}.{column}])",
        dialect="duckdb",
    )
    contains_expr = sqlglot.parse_one(
        f"list_contains(r.visited, {table}.{column})",
        dialect="duckdb",
    )
    return append_expr, contains_expr
