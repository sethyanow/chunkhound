"""Stats MCP tool — database and LSP status summary."""

from typing import Any

from .registry import register_tool

GET_STATS_DESCRIPTION = (
    "Get database and index statistics — file, chunk, symbol, and edge counts "
    "with per-language breakdown and optional LSP server status. "
    "No parameters required."
)


def _safe_count(services: Any, sql: str) -> int:
    """Execute a COUNT query, returning 0 if the table doesn't exist."""
    try:
        rows = services.provider.execute_query(sql, [])
        return rows[0]["count"] if rows else 0
    except Exception:
        # CatalogException for missing tables (pre-population state)
        return 0


def _safe_language_breakdown(services: Any) -> list[dict[str, Any]]:
    """Get per-language symbol counts, returning [] if table doesn't exist."""
    try:
        rows = services.provider.execute_query(
            "SELECT language, COUNT(*) as count FROM symbols "
            "GROUP BY language ORDER BY count DESC",
            [],
        )
        return [{"language": row["language"], "count": row["count"]} for row in rows]
    except Exception:
        return []


@register_tool(
    description=GET_STATS_DESCRIPTION,
    name="get_stats",
)
async def get_stats_impl(
    services: Any,
    lsp_client_pool: Any = None,
) -> dict[str, Any]:
    """Return database and LSP statistics summary.

    Args:
        services: Database services bundle
        lsp_client_pool: Optional LSP client pool for server status
    """
    result: dict[str, Any] = {
        "files": _safe_count(services, "SELECT COUNT(*) as count FROM files"),
        "chunks": _safe_count(services, "SELECT COUNT(*) as count FROM chunks"),
        "symbols": _safe_count(services, "SELECT COUNT(*) as count FROM symbols"),
        "symbol_edges": _safe_count(
            services, "SELECT COUNT(*) as count FROM symbol_edges"
        ),
        "languages": _safe_language_breakdown(services),
        "lsp_servers": None,
    }

    # Add LSP server status if pool available
    if lsp_client_pool is not None:
        clients = list(lsp_client_pool._clients.items())
        result["lsp_servers"] = {
            "total": len(clients),
            "languages": [lang for (lang, _ws), _client in clients],
        }

    return result
