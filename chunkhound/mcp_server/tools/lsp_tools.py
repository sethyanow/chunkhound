"""LSP MCP tools — lsp, lsp_status, symbol_context.

Dispatch pattern: LSP_DISPATCH maps operation names to async handler functions.
Each handler takes (client, file_uri, line, character) and returns a result dict.
"""

import asyncio
import os
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any, Literal

from chunkhound.core.types.common import Language
from chunkhound.lsp.types import (
    LSPCapabilityError,
    LSPError,
    LSPTransportError,
    ServerState,
)

from .formatters import (
    _uri_to_path,
    call_item_to_dict,
    diagnostic_to_dict,
    location_to_dict,
)
from .registry import register_tool

# =============================================================================
# Dispatch handlers — each takes (client, file_uri, line, character)
# =============================================================================

# Type alias for dispatch handler signature
DispatchHandler = Callable[..., Coroutine[Any, Any, dict[str, Any]]]


async def _handle_definition(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    locations = await client.go_to_definition(file_uri, line, character)
    return {"results": [location_to_dict(loc) for loc in locations]}


async def _handle_references(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    locations = await client.find_references(file_uri, line, character)
    return {"results": [location_to_dict(loc) for loc in locations]}


async def _handle_implementations(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    locations = await client.go_to_implementation(file_uri, line, character)
    return {"results": [location_to_dict(loc) for loc in locations]}


async def _handle_callers(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    items = await client.incoming_calls(file_uri, line, character)
    return {"results": [call_item_to_dict(item) for item in items]}


async def _handle_callees(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    items = await client.outgoing_calls(file_uri, line, character)
    return {"results": [call_item_to_dict(item) for item in items]}


async def _handle_hover(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    hover_result = await client.hover(file_uri, line, character)
    if hover_result is None:
        return {"contents": None, "range": None}
    return {
        "contents": hover_result.contents,
        "range": {
            "start_line": hover_result.range_start_line,
            "start_character": hover_result.range_start_char,
            "end_line": hover_result.range_end_line,
            "end_character": hover_result.range_end_char,
        }
        if hover_result.range_start_line is not None
        else None,
    }


async def _handle_diagnostics(
    client: Any, file_uri: str, line: int, character: int
) -> dict[str, Any]:
    # diagnostics only uses file_uri; line/character ignored
    diagnostics = await client.get_diagnostics(file_uri)
    return {"results": [diagnostic_to_dict(d) for d in diagnostics]}


# The dispatch dict — maps operation name to handler
LSP_DISPATCH: dict[str, DispatchHandler] = {
    "definition": _handle_definition,
    "references": _handle_references,
    "implementations": _handle_implementations,
    "callers": _handle_callers,
    "callees": _handle_callees,
    "hover": _handle_hover,
    "diagnostics": _handle_diagnostics,
}


# =============================================================================
# Tool: lsp
# =============================================================================

LSP_DESCRIPTION = (
    "Execute LSP (Language Server Protocol) operations on source files. "
    "Provides code intelligence: go-to-definition, find references, "
    "implementations, callers, callees, hover info, and diagnostics. "
    "Line and character are 0-based (LSP convention). "
    "First call for a language may be slow (server startup)."
)


@register_tool(
    description=LSP_DESCRIPTION,
    name="lsp",
)
async def lsp_impl(
    lsp_client_pool: Any,
    services: Any,
    config: Any,
    file: str,
    line: int,
    character: int,
    operation: Literal[
        "definition",
        "references",
        "implementations",
        "callers",
        "callees",
        "hover",
        "diagnostics",
    ],
) -> dict[str, Any]:
    """Unified LSP tool dispatching to 7 operations.

    Args:
        lsp_client_pool: LSPClientPool instance
        services: DatabaseServices instance
        config: Config instance (provides target_dir as workspace_root)
        file: Path to the source file
        line: 0-based line number
        character: 0-based character offset
        operation: LSP operation to perform
    """
    # Guard: pool not ready (server still starting up)
    if lsp_client_pool is None:
        return {
            "error": "lsp_not_ready",
            "message": "LSP client pool not initialized yet. Server is still starting up.",
        }

    # Resolve language from file extension
    lang = Language.from_file_extension(file)
    if lang == Language.UNKNOWN:
        return {
            "error": "unsupported_language",
            "message": f"No language server available for file: {file}",
        }
    language_id = lang.value

    # Determine workspace root
    workspace_root = str(
        config.target_dir
        if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Handle file:// URI input
    resolved_file = file
    if file.startswith("file://"):
        resolved_file = _uri_to_path(file)
    file_uri = Path(resolved_file).resolve().as_uri()

    # Dispatch via dict lookup
    handler = LSP_DISPATCH.get(operation)
    if handler is None:
        return {
            "error": "invalid_operation",
            "message": f"Unknown operation: {operation}",
        }

    try:
        client = await lsp_client_pool.get(language_id, workspace_root)
        return await handler(client, file_uri, line, character)

    except LSPCapabilityError as e:
        return {
            "error": "capability_not_supported",
            "message": str(e),
            "operation": operation,
        }
    except LSPTransportError as e:
        return {
            "error": "server_not_available",
            "message": str(e),
        }
    except LSPError as e:
        return {
            "error": "lsp_error",
            "message": str(e),
        }


# =============================================================================
# Tool: lsp_status
# =============================================================================

LSP_STATUS_DESCRIPTION = (
    "Get health and readiness status of all active LSP language servers. "
    "Returns per-server state, capabilities, and any degradation reasons. "
    "No parameters required."
)


@register_tool(
    description=LSP_STATUS_DESCRIPTION,
    name="lsp_status",
)
async def lsp_status_impl(
    lsp_client_pool: Any,
) -> dict[str, Any]:
    """Query health/readiness of all active LSP servers.

    Args:
        lsp_client_pool: LSPClientPool instance
    """
    if lsp_client_pool is None:
        return {
            "error": "lsp_not_ready",
            "message": "LSP client pool not initialized yet. Server is still starting up.",
        }

    # Snapshot to avoid RuntimeError from concurrent dict mutation
    clients = list(lsp_client_pool._clients.items())

    servers = []
    ready_count = 0
    degraded_count = 0

    for (language_id, workspace_root), client in clients:
        state = client.state
        if state == ServerState.READY:
            ready_count += 1
        elif state == ServerState.DEGRADED:
            degraded_count += 1

        capabilities = [cap.value for cap in client.capabilities]

        servers.append(
            {
                "language_id": language_id,
                "workspace_root": workspace_root,
                "state": state.value,
                "capabilities": capabilities,
                "server_info": client.server_info,
                "degraded_reason": client.degraded_reason,
            }
        )

    return {
        "servers": servers,
        "total": len(servers),
        "ready": ready_count,
        "degraded": degraded_count,
    }


# =============================================================================
# Tool: symbol_context
# =============================================================================

SYMBOL_CONTEXT_DESCRIPTION = (
    "Get a compound profile for a symbol at a given file position. "
    "Returns hover info, definition location, callers, callees, and "
    "graph neighborhood in one call — avoids multiple round-trips. "
    "Line and character are 0-based (LSP convention)."
)


@register_tool(
    description=SYMBOL_CONTEXT_DESCRIPTION,
    name="symbol_context",
)
async def symbol_context_impl(
    lsp_client_pool: Any,
    services: Any,
    config: Any,
    file: str,
    line: int,
    character: int,
) -> dict[str, Any]:
    """Compound symbol profile: hover + definition + callers + callees + graph neighborhood.

    Args:
        lsp_client_pool: LSPClientPool instance
        services: DatabaseServices instance
        config: Config instance (provides target_dir as workspace_root)
        file: Path to the source file
        line: 0-based line number
        character: 0-based character offset
    """
    # Guard: pool not ready
    if lsp_client_pool is None:
        return {
            "error": "lsp_not_ready",
            "message": "LSP client pool not initialized yet. Server is still starting up.",
        }

    # Handle file:// URI input
    resolved_file = file
    if file.startswith("file://"):
        resolved_file = _uri_to_path(file)

    # Resolve language from file extension
    lang = Language.from_file_extension(resolved_file)
    if lang == Language.UNKNOWN:
        return {
            "error": "unsupported_language",
            "message": f"No language server available for file: {file}",
        }
    language_id = lang.value

    # Determine workspace root
    workspace_root = str(
        config.target_dir
        if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Construct file URI for LSP calls
    file_uri = Path(resolved_file).resolve().as_uri()

    # Get LSP client
    client = await lsp_client_pool.get(language_id, workspace_root)

    # 4 async LSP calls with broad exception catching
    async def _safe_hover() -> Any:
        try:
            return await client.hover(file_uri, line, character)
        except Exception:
            return None

    async def _safe_definition() -> list[Any]:
        try:
            result: list[Any] = await client.go_to_definition(file_uri, line, character)
            return result
        except Exception:
            return []

    async def _safe_incoming() -> list[Any]:
        try:
            result: list[Any] = await client.incoming_calls(file_uri, line, character)
            return result
        except Exception:
            return []

    async def _safe_outgoing() -> list[Any]:
        try:
            result: list[Any] = await client.outgoing_calls(file_uri, line, character)
            return result
        except Exception:
            return []

    hover_result, definitions, callers_raw, callees_raw = await asyncio.gather(
        _safe_hover(), _safe_definition(), _safe_incoming(), _safe_outgoing()
    )

    # Format results using helpers
    hover = hover_result.contents if hover_result is not None else None
    definition = [location_to_dict(loc) for loc in definitions]
    callers = [call_item_to_dict(item) for item in callers_raw]
    callees = [call_item_to_dict(item) for item in callees_raw]

    # FQN lookup for graph neighborhood
    relative_path = os.path.relpath(str(Path(resolved_file).resolve()), workspace_root)
    fqn_rows = services.provider.execute_query(
        "SELECT fqn FROM symbols WHERE file_path = ? "
        "AND range_start <= ? AND range_end >= ? "
        "ORDER BY (range_end - range_start) ASC LIMIT 1",
        [relative_path, line, line],
    )

    graph_neighborhood = None
    if fqn_rows:
        try:
            from .graph import _graph_walk

            graph_neighborhood = _graph_walk(
                services, fqn_rows[0]["fqn"], depth=1, edge_kind=None, limit=20
            )
        except Exception:
            graph_neighborhood = None

    return {
        "hover": hover,
        "definition": definition,
        "callers": callers,
        "callees": callees,
        "graph_neighborhood": graph_neighborhood,
    }
