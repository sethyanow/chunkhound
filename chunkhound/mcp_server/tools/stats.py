"""Stats MCP tool — database and LSP status summary."""

from typing import Any

from .registry import register_tool

GET_STATS_DESCRIPTION = (
    "Get database and index statistics — file, chunk, symbol, and edge counts "
    "with per-language breakdown and optional LSP server status. "
    "No parameters required."
)


@register_tool(
    description=GET_STATS_DESCRIPTION,
    name="get_stats",
)
async def get_stats_impl(
    services: Any,
    lsp_client_pool: Any = None,
) -> dict[str, Any]:
    """Return database and LSP statistics summary.

    ch-nxu: provider-agnostic — uses ``provider.get_stats()`` for file/chunk
    counts and ``provider.symbol_stats()`` for symbol/edge counts plus
    per-language breakdown. Degrades gracefully when either raises
    (pre-population or schema-missing state).
    """
    # Database-level counts — files, chunks.
    try:
        provider_stats = services.provider.get_stats()
    except Exception:
        provider_stats = {}

    # Symbol/edge counts + language breakdown.
    try:
        sym_stats = services.provider.symbol_stats()
    except Exception:
        sym_stats = {"symbol_count": 0, "edge_count": 0, "languages": []}

    result: dict[str, Any] = {
        "files": int(provider_stats.get("files", 0)),
        "chunks": int(provider_stats.get("chunks", 0)),
        "symbols": int(sym_stats.get("symbol_count", 0)),
        "symbol_edges": int(sym_stats.get("edge_count", 0)),
        "languages": list(sym_stats.get("languages", [])),
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
