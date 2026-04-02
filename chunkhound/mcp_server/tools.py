"""Declarative tool registry for MCP server.

This module defines all MCP tools in a single location, providing a unified
registry that the stdio server uses for tool definitions.

The registry pattern ensures consistent tool metadata and behavior.
"""

import inspect
import json
import types
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, Union, cast, get_args, get_origin

try:
    from typing import NotRequired  # type: ignore[attr-defined]
except (ImportError, AttributeError):
    from typing_extensions import NotRequired

from chunkhound.core.config.config import Config
from chunkhound.database_factory import DatabaseServices
from chunkhound.embeddings import EmbeddingManager
from chunkhound.llm_manager import LLMManager
from chunkhound.services.research.factory import ResearchServiceFactory

# Response size limits (tokens)
MAX_RESPONSE_TOKENS = 20000
MIN_RESPONSE_TOKENS = 1000
MAX_ALLOWED_TOKENS = 25000


# =============================================================================
# Schema Generation Infrastructure
# =============================================================================
# These utilities generate JSON Schema from Python function signatures,
# enabling a single source of truth for tool definitions.


@dataclass
class Tool:
    """Tool definition with metadata and implementation."""

    name: str
    description: str
    parameters: dict[str, Any]
    implementation: Callable
    requires_embeddings: bool = False
    requires_llm: bool = False
    requires_reranker: bool = False


# Tool registry - populated by @register_tool decorator
TOOL_REGISTRY: dict[str, Tool] = {}


def _python_type_to_json_schema_type(type_hint: Any) -> dict[str, Any]:
    """Convert Python type hint to JSON Schema type definition.

    Args:
        type_hint: Python type annotation

    Returns:
        JSON Schema type definition dict
    """
    # Handle None / NoneType
    if type_hint is None or type_hint is type(None):
        return {"type": "null"}

    # Get origin for generic types (list, dict, Union, etc.)
    origin = get_origin(type_hint)
    args = get_args(type_hint)

    # Handle Union types (including Optional which is Union[T, None])
    # Note: Python 3.10+ uses types.UnionType for X | Y syntax
    if origin is Union or isinstance(type_hint, types.UnionType):
        # Filter out NoneType to find the actual type
        non_none_types = [arg for arg in args if arg is not type(None)]
        if len(non_none_types) == 1:
            # Optional[T] case - just return the T's schema
            return _python_type_to_json_schema_type(non_none_types[0])
        else:
            # Multiple non-None types - use anyOf
            return {
                "anyOf": [_python_type_to_json_schema_type(t) for t in non_none_types]
            }

    # Handle Literal types (e.g., Literal["a", "b"])
    if origin is Literal:
        return {"type": "string", "enum": list(args)}

    # Handle basic types
    if type_hint is str:
        return {"type": "string"}
    elif type_hint is int:
        return {"type": "integer"}
    elif type_hint is float:
        return {"type": "number"}
    elif type_hint is bool:
        return {"type": "boolean"}
    elif origin is list:
        item_type = args[0] if args else Any
        return {"type": "array", "items": _python_type_to_json_schema_type(item_type)}
    elif origin is dict:
        return {"type": "object"}
    else:
        # Default to object for complex types
        return {"type": "object"}


def _extract_param_descriptions_from_docstring(func: Callable) -> dict[str, str]:
    """Extract parameter descriptions from function docstring.

    Parses Google-style docstring Args section.

    Args:
        func: Function with docstring

    Returns:
        Dict mapping parameter names to their descriptions
    """
    if not func.__doc__:
        return {}

    descriptions: dict[str, str] = {}
    lines = func.__doc__.split("\n")
    in_args_section = False

    for line in lines:
        stripped = line.strip()

        # Detect Args section
        if stripped == "Args:":
            in_args_section = True
            continue

        # Exit Args section when we hit another section or empty line after args
        if in_args_section and (
            stripped.endswith(":") or (not stripped and descriptions)
        ):
            in_args_section = False

        # Parse parameter descriptions
        if in_args_section and ":" in stripped:
            # Format: "param_name: description"
            parts = stripped.split(":", 1)
            if len(parts) == 2:
                param_name = parts[0].strip()
                description = parts[1].strip()
                descriptions[param_name] = description

    return descriptions


def _generate_json_schema_from_signature(func: Callable) -> dict[str, Any]:
    """Generate JSON Schema from function signature.

    Args:
        func: Function to analyze

    Returns:
        JSON Schema parameters dict compatible with MCP tool schema
    """
    sig = inspect.signature(func)
    properties: dict[str, Any] = {}
    required: list[str] = []

    # Extract parameter descriptions from docstring
    param_descriptions = _extract_param_descriptions_from_docstring(func)

    for param_name, param in sig.parameters.items():
        # Skip service/infrastructure parameters that aren't part of the tool API
        if param_name in (
            "services",
            "embedding_manager",
            "llm_manager",
            "scan_progress",
            "progress",
            "config",
            "lsp_client_pool",
        ):
            continue

        # Get type hint
        type_hint = (
            param.annotation if param.annotation != inspect.Parameter.empty else Any
        )

        # Convert to JSON Schema type
        schema = _python_type_to_json_schema_type(type_hint)

        # Add description if available from docstring
        if param_name in param_descriptions:
            schema["description"] = param_descriptions[param_name]

        # Add default value if present
        if param.default != inspect.Parameter.empty and param.default is not None:
            schema["default"] = param.default

        properties[param_name] = schema

        # Mark as required if no default value
        if param.default == inspect.Parameter.empty:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required if required else [],
    }


def register_tool(
    description: str,
    requires_embeddings: bool = False,
    requires_llm: bool = False,
    requires_reranker: bool = False,
    name: str | None = None,
) -> Callable[[Callable], Callable]:
    """Decorator to register a function as an MCP tool.

    Extracts JSON Schema from function signature and registers in TOOL_REGISTRY.

    Args:
        description: Comprehensive tool description for LLM users
        requires_embeddings: Whether tool requires embedding providers
        requires_llm: Whether tool requires LLM provider
        requires_reranker: Whether tool requires reranking support
        name: Optional tool name (defaults to function name)

    Returns:
        Decorator function

    Example:
        @register_tool(
            description="Search using regex patterns",
            requires_embeddings=False
        )
        async def search_regex(pattern: str, page_size: int = 10) -> dict:
            ...
    """

    def decorator(func: Callable) -> Callable:
        tool_name = name or func.__name__

        # Generate schema from function signature
        parameters = _generate_json_schema_from_signature(func)

        # Register tool in global registry
        TOOL_REGISTRY[tool_name] = Tool(
            name=tool_name,
            description=description,
            parameters=parameters,
            implementation=func,
            requires_embeddings=requires_embeddings,
            requires_llm=requires_llm,
            requires_reranker=requires_reranker,
        )

        return func

    return decorator


# =============================================================================
# Helper Functions
# =============================================================================


def _convert_paths_to_native(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert file paths in search results to native platform format."""
    from pathlib import Path

    for result in results:
        if "file_path" in result and result["file_path"]:
            # Use Path for proper native conversion
            result["file_path"] = str(Path(result["file_path"]))
    return results


# Type definitions for return values
class PaginationInfo(TypedDict):
    """Pagination metadata for search results."""

    offset: int
    page_size: int
    has_more: bool
    total: NotRequired[int | None]
    next_offset: NotRequired[int | None]


class SearchResponse(TypedDict):
    """Response structure for search operations."""

    results: list[dict[str, Any]]
    pagination: PaginationInfo


def estimate_tokens(text: str) -> int:
    """Estimate token count using simple heuristic (3 chars ≈ 1 token for safety)."""
    return len(text) // 3


def limit_response_size(
    response_data: SearchResponse, max_tokens: int = MAX_RESPONSE_TOKENS
) -> SearchResponse:
    """Limit response size to fit within token limits by reducing results."""
    if not response_data.get("results"):
        return response_data

    # Start with full response and iteratively reduce until under limit
    limited_results = response_data["results"][:]

    while limited_results:
        # Create test response with current results
        test_response = {
            "results": limited_results,
            "pagination": response_data["pagination"],
        }

        # Estimate token count
        response_text = json.dumps(test_response, default=str)
        token_count = estimate_tokens(response_text)

        if token_count <= max_tokens:
            # Update pagination to reflect actual returned results
            actual_count = len(limited_results)
            updated_pagination = response_data["pagination"].copy()
            updated_pagination["page_size"] = actual_count
            updated_pagination["has_more"] = updated_pagination.get(
                "has_more", False
            ) or actual_count < len(response_data["results"])
            if actual_count < len(response_data["results"]):
                updated_pagination["next_offset"] = (
                    updated_pagination.get("offset", 0) + actual_count
                )

            return {"results": limited_results, "pagination": updated_pagination}

        # Remove results from the end to reduce size
        # Remove in chunks for efficiency
        reduction_size = max(1, len(limited_results) // 4)
        limited_results = limited_results[:-reduction_size]

    # If even empty results exceed token limit, return minimal response
    return {
        "results": [],
        "pagination": {
            "offset": response_data["pagination"].get("offset", 0),
            "page_size": 0,
            "has_more": len(response_data["results"]) > 0,
            "total": response_data["pagination"].get("total", 0),
            "next_offset": None,
        },
    }


# =============================================================================
# Tool Descriptions (optimized for LLM consumption)
# =============================================================================

SEARCH_DESCRIPTION = """Pinpoint specific code locations after building understanding with code_research. Returns structurally-parsed code chunks (functions, classes) — large definitions may span multiple results.

TYPE — choose one:
- **regex**: Match exact patterns against code content. Use for known identifiers, imports, or string literals.
  Examples: "def authenticate", "class.*Handler", "import.*pandas", "TODO:.*refactor"
- **semantic**: Find code by meaning via embedding similarity. Use for concepts or when exact identifiers are unknown.
  Examples: "authentication logic", "retry with exponential backoff", "database connection pooling"
- **symbols**: Search indexed symbol names and FQNs from LSP analysis. Returns symbol metadata (kind, type_signature, location).
  Examples: "parse", "auth::validate", "Handler"

DECISION GUIDE:
- Known symbol or pattern → regex
- Concept or behavior → semantic
- Symbol by name/type → symbols
- Cross-file architecture question → call code_research first

OPTIONAL FILTERS:
- **path**: Restrict to a subdirectory (e.g. "src/auth")
- **type_filter**: Filter by type signature substring (e.g. "Result", "int"). Works with all search types.

OUTPUT: {results: [{file_path, content, start_line, end_line}], pagination}"""

SEARCH_DESCRIPTION_NO_RESEARCH = """Pinpoint specific code locations — find exact symbols, patterns, or concepts in the indexed codebase. Returns structurally-parsed code chunks (functions, classes) — large definitions may span multiple results.

TYPE — choose one:
- **regex**: Match exact patterns against code content. Use for known identifiers, imports, or string literals.
  Examples: "def authenticate", "class.*Handler", "import.*pandas", "TODO:.*refactor"
- **semantic**: Find code by meaning via embedding similarity. Use for concepts or when exact identifiers are unknown.
  Examples: "authentication logic", "retry with exponential backoff", "database connection pooling"
- **symbols**: Search indexed symbol names and FQNs from LSP analysis. Returns symbol metadata (kind, type_signature, location).
  Examples: "parse", "auth::validate", "Handler"

DECISION GUIDE:
- Known symbol or pattern → regex
- Concept or behavior → semantic
- Symbol by name/type → symbols

OPTIONAL FILTERS:
- **path**: Restrict to a subdirectory (e.g. "src/auth")
- **type_filter**: Filter by type signature substring (e.g. "Result", "int"). Works with all search types.

OUTPUT: {results: [{file_path, content, start_line, end_line}], pagination}"""

CODE_RESEARCH_DESCRIPTION = """Start here for any coding task. Call code_research first to understand the relevant code area before writing or modifying code.

WORKFLOW:
1. **Understand** — call code_research to map architecture, components, and data flow
2. **Deepen** — call again with focused queries on specific subsystems discovered in step 1
3. **Pinpoint** — switch to search (regex/semantic) for exact file locations and symbol references
4. **Inspect** — use Explore/grep/read for granular line-level follow-up

WHAT IT RETURNS: Cited markdown report covering architecture overview, key code locations, component relationships, and cross-file data flows.

EXAMPLES:
- "How does authentication work?" — traces the full auth flow across files
- "What happens when a request hits /api/users?" — maps the request lifecycle
- "Explain error handling patterns" — identifies cross-cutting concerns

SCOPE: Use the path parameter to restrict analysis to a subdirectory for faster, focused results.

One call replaces 5-10 manual searches. Call it liberally — understanding first, coding second."""


# =============================================================================
# Tool Implementations
# =============================================================================


@register_tool(
    description=SEARCH_DESCRIPTION,
    requires_embeddings=False,
    name="search",
)
async def search_impl(
    services: DatabaseServices,
    embedding_manager: EmbeddingManager | None,
    type: Literal["regex", "semantic", "symbols"],
    query: str,
    path: str | None = None,
    page_size: int = 10,
    offset: int = 0,
    fuzzy_path: bool = False,
    type_filter: str | None = None,
) -> SearchResponse:
    """Unified search dispatching to regex, semantic, or symbols based on type.

    Args:
        services: Database services bundle
        embedding_manager: Embedding manager (required for semantic type)
        type: Search mode — "regex" for pattern matching, "semantic" for meaning-based, "symbols" for indexed symbol name/FQN search
        query: For regex: a regex pattern. For semantic: a natural language concept. For symbols: a name or FQN substring like "parse"
        path: Optional relative subdirectory to restrict search scope (no leading slash)
        page_size: Number of results per page (1-100)
        offset: Starting offset for pagination
        type_filter: Optional type signature substring filter, e.g. "Result" or "int". Applies to all search types.

    Returns:
        Dict with 'results' and 'pagination' keys

    Raises:
        ValueError: If type is invalid or semantic search lacks embedding provider
    """
    # Validate type parameter
    if type not in ("semantic", "regex", "symbols"):
        raise ValueError(
            f"Invalid search type: '{type}'. Must be 'semantic', 'regex', or 'symbols'."
        )

    # Validate and constrain parameters
    page_size = max(1, min(page_size, 100))
    offset = max(0, offset)

    if type == "symbols":
        return await _search_symbols(
            services, query, path, page_size, offset, type_filter,
        )

    if type == "semantic":
        # Validate embedding manager for semantic search
        if not embedding_manager or not embedding_manager.list_providers():
            raise ValueError(
                "Semantic search requires embedding provider. "
                "Configure via .chunkhound.json or CHUNKHOUND_EMBEDDING__API_KEY. "
                "Use type='regex' for pattern-based search without embeddings."
            )

        # Get default provider/model
        try:
            provider_obj = embedding_manager.get_provider()
            provider_name = provider_obj.name
            model_name = provider_obj.model
        except ValueError:
            raise ValueError("No default embedding provider configured.")

        # Perform semantic search
        results, pagination = await services.search_service.search_semantic(
            query=query,
            page_size=page_size,
            offset=offset,
            provider=provider_name,
            model=model_name,
            path_filter=path,
            fuzzy_path=fuzzy_path,
        )
    else:  # regex
        # Perform regex search
        results, pagination = await services.search_service.search_regex_async(
            pattern=query,
            page_size=page_size,
            offset=offset,
            path_filter=path,
            fuzzy_path=fuzzy_path,
        )

    # Apply type_filter post-filter for regex/semantic results
    if type_filter and results:
        results = _apply_type_filter(services, results, type_filter)

    # Convert file paths to native platform format
    native_results = _convert_paths_to_native(results)

    # Apply response size limiting
    response = cast(
        SearchResponse, {"results": native_results, "pagination": pagination}
    )
    return limit_response_size(response)


def _apply_type_filter(
    services: Any, results: list[dict], type_filter: str,
) -> list[dict]:
    """Post-filter chunk results by matching symbols with type_signature.

    Batches the lookup into a single SQL query to avoid N+1 round-trips.
    """
    if not results:
        return results

    escaped_filter = _escape_like(type_filter)

    # Build batch query — one OR clause per chunk result
    conditions = []
    params: list[Any] = []
    for r in results:
        conditions.append(
            "(s.file_path = ? AND s.range_start <= ? AND s.range_end >= ?)"
        )
        params.extend([r["file_path"], r["end_line"], r["start_line"]])

    where_clause = " OR ".join(conditions)
    params.append(f"%{escaped_filter}%")

    query = (
        "SELECT DISTINCT s.file_path, s.range_start, s.range_end "
        "FROM symbols s "
        f"WHERE ({where_clause}) AND s.type_signature LIKE ?"
    )

    matches = services.provider.execute_query(query, params)

    # Build a set of matching (file_path, start, end) for fast lookup
    match_set = {
        (m["file_path"], m["range_start"], m["range_end"]) for m in matches
    }

    # Keep results where any matching symbol overlaps
    return [
        r for r in results
        if any(
            fp == r["file_path"]
            and rs <= r["end_line"]
            and re >= r["start_line"]
            for fp, rs, re in match_set
        )
    ]


async def _search_symbols(
    services: Any,
    query: str,
    path: str | None,
    page_size: int,
    offset: int,
    type_filter: str | None,
) -> SearchResponse:
    """Search the symbols table directly by name/FQN substring."""
    conditions = []
    params: list[Any] = []

    # Name/FQN filter — empty query matches all
    if query:
        escaped_query = _escape_like(query)
        like_pattern = f"%{escaped_query}%"
        conditions.append("(name LIKE ? OR fqn LIKE ?)")
        params.extend([like_pattern, like_pattern])

    # Path filter
    if path:
        escaped_path = _escape_like(path)
        conditions.append("file_path LIKE ?")
        params.append(f"{escaped_path}%")

    # Type signature filter
    if type_filter:
        escaped_type = _escape_like(type_filter)
        conditions.append("type_signature LIKE ?")
        params.append(f"%{escaped_type}%")

    where_clause = " AND ".join(conditions) if conditions else "1=1"

    # Query symbols
    symbol_query = (
        "SELECT fqn, name, kind, language, file_path, range_start, range_end, type_signature "
        f"FROM symbols WHERE {where_clause} "
        "ORDER BY name LIMIT ? OFFSET ?"
    )
    params.extend([page_size, offset])

    symbol_rows = services.provider.execute_query(symbol_query, params)

    # Count query for pagination
    count_params = params[:-2]  # Exclude LIMIT/OFFSET
    count_query = f"SELECT COUNT(*) as total FROM symbols WHERE {where_clause}"
    count_rows = services.provider.execute_query(count_query, count_params)
    total = count_rows[0]["total"] if count_rows else 0

    # Format results
    results = [
        {
            "fqn": row["fqn"],
            "name": row["name"],
            "kind": row["kind"],
            "language": row.get("language"),
            "file_path": row["file_path"],
            "range_start": row["range_start"],
            "range_end": row["range_end"],
            "type_signature": row.get("type_signature"),
        }
        for row in symbol_rows
    ]

    pagination = {
        "offset": offset,
        "page_size": page_size,
        "has_more": offset + page_size < total,
        "total": total,
    }

    response = cast(
        SearchResponse, {"results": results, "pagination": pagination}
    )
    return limit_response_size(response)


# =============================================================================
# LSP Tools
# =============================================================================

LSP_DESCRIPTION = (
    "Execute LSP (Language Server Protocol) operations on source files. "
    "Provides code intelligence: go-to-definition, find references, "
    "implementations, callers, callees, hover info, and diagnostics. "
    "Line and character are 0-based (LSP convention). "
    "First call for a language may be slow (server startup)."
)


def _uri_to_path(uri: str) -> str:
    """Convert a file:// URI to a filesystem path."""
    if uri.startswith("file://"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        return unquote(parsed.path)
    return uri


def _location_to_dict(loc: Any) -> dict[str, Any]:
    """Convert a Location dataclass to a clean dict."""
    return {
        "file_path": _uri_to_path(loc.uri),
        "line": loc.range_start_line,
        "character": loc.range_start_char,
        "end_line": loc.range_end_line,
        "end_character": loc.range_end_char,
    }


def _call_item_to_dict(item: Any) -> dict[str, Any]:
    """Convert a CallHierarchyItem to a clean dict."""
    return {
        "name": item.name,
        "kind": item.kind,
        "file_path": _uri_to_path(item.uri),
        "line": item.range_start_line,
        "character": item.range_start_char,
        "end_line": item.range_end_line,
        "end_character": item.range_end_char,
        "detail": item.detail,
    }


def _diagnostic_to_dict(diag: Any) -> dict[str, Any]:
    """Convert a Diagnostic to a clean dict."""
    return {
        "line": diag.range_start_line,
        "character": diag.range_start_char,
        "end_line": diag.range_end_line,
        "end_character": diag.range_end_char,
        "severity": diag.severity,
        "message": diag.message,
        "source": diag.source,
        "code": diag.code,
    }


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
    from pathlib import Path

    from chunkhound.core.types.common import Language
    from chunkhound.lsp.types import (
        LSPCapabilityError,
        LSPError,
        LSPTransportError,
    )

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
        config.target_dir if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Handle file:// URI input
    resolved_file = file
    if file.startswith("file://"):
        resolved_file = _uri_to_path(file)
    file_uri = Path(resolved_file).resolve().as_uri()

    try:
        client = await lsp_client_pool.get(language_id, workspace_root)

        if operation == "definition":
            locations = await client.go_to_definition(file_uri, line, character)
            return {"results": [_location_to_dict(loc) for loc in locations]}

        elif operation == "references":
            locations = await client.find_references(file_uri, line, character)
            return {"results": [_location_to_dict(loc) for loc in locations]}

        elif operation == "implementations":
            locations = await client.go_to_implementation(file_uri, line, character)
            return {"results": [_location_to_dict(loc) for loc in locations]}

        elif operation == "callers":
            items = await client.incoming_calls(file_uri, line, character)
            return {"results": [_call_item_to_dict(item) for item in items]}

        elif operation == "callees":
            items = await client.outgoing_calls(file_uri, line, character)
            return {"results": [_call_item_to_dict(item) for item in items]}

        elif operation == "hover":
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

        elif operation == "diagnostics":
            diagnostics = await client.get_diagnostics(file_uri)
            return {"results": [_diagnostic_to_dict(d) for d in diagnostics]}

        else:
            return {"error": "invalid_operation", "message": f"Unknown operation: {operation}"}

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
    from chunkhound.lsp.types import ServerState

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
    import asyncio
    import os
    from pathlib import Path

    from chunkhound.core.types.common import Language

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
        config.target_dir if config and hasattr(config, "target_dir") and config.target_dir
        else Path(".").resolve()
    )

    # Construct file URI for LSP calls
    file_uri = Path(resolved_file).resolve().as_uri()

    # Get LSP client
    client = await lsp_client_pool.get(language_id, workspace_root)

    # 4 async LSP calls with broad exception catching
    async def _safe_hover():
        try:
            return await client.hover(file_uri, line, character)
        except Exception:
            return None

    async def _safe_definition():
        try:
            return await client.go_to_definition(file_uri, line, character)
        except Exception:
            return []

    async def _safe_incoming():
        try:
            return await client.incoming_calls(file_uri, line, character)
        except Exception:
            return []

    async def _safe_outgoing():
        try:
            return await client.outgoing_calls(file_uri, line, character)
        except Exception:
            return []

    hover_result, definitions, callers_raw, callees_raw = await asyncio.gather(
        _safe_hover(), _safe_definition(), _safe_incoming(), _safe_outgoing()
    )

    # Format results using existing helpers
    hover = hover_result.contents if hover_result is not None else None
    definition = [_location_to_dict(loc) for loc in definitions]
    callers = [_call_item_to_dict(item) for item in callers_raw]
    callees = [_call_item_to_dict(item) for item in callees_raw]

    # FQN lookup for graph neighborhood
    relative_path = os.path.relpath(
        str(Path(resolved_file).resolve()), workspace_root
    )
    fqn_rows = services.provider.execute_query(
        "SELECT fqn FROM symbols WHERE file_path = ? "
        "AND range_start <= ? AND range_end >= ? "
        "ORDER BY (range_end - range_start) ASC LIMIT 1",
        [relative_path, line, line],
    )

    graph_neighborhood = None
    if fqn_rows:
        try:
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


def _escape_like(value: str) -> str:
    """Escape LIKE-special characters in a user-provided string."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
    """Unified graph query tool dispatching to walk/reachability/boundary/overview.

    Args:
        services: DatabaseServices instance
        operation: One of 'walk', 'reachability', 'boundary', 'overview'
        symbol: FQN of starting symbol (required for walk)
        depth: Max traversal depth for walk (clamped to 1-20)
        edge_kind: Optional edge kind filter for walk
        scope: File path prefix for reachability/boundary (required for those ops)
        limit: Max results (clamped to 1-100)
    """
    # Validate operation
    valid_ops = {"walk", "reachability", "boundary", "overview"}
    if operation not in valid_ops:
        return {
            "error": "invalid_operation",
            "message": f"Unknown operation '{operation}'. Valid: {', '.join(sorted(valid_ops))}",
        }

    # Clamp bounds
    depth = max(1, min(depth, 20))
    limit = max(1, min(limit, 100))

    if operation == "walk":
        return _graph_walk(services, symbol, depth, edge_kind, limit)
    elif operation == "reachability":
        return _graph_reachability(services, scope, limit)
    elif operation == "boundary":
        return _graph_boundary(services, scope, limit)
    else:  # overview
        return _graph_overview(services, scope, limit)


def _graph_walk(
    services: Any,
    symbol: str | None,
    depth: int,
    edge_kind: str | None,
    limit: int,
) -> dict[str, Any]:
    """Walk connected symbols from a starting FQN."""
    if not symbol:
        return {
            "error": "missing_parameter",
            "message": "walk operation requires 'symbol' parameter (FQN of starting symbol)",
        }

    # Build recursive CTE for node discovery
    edge_filter = ""
    params: list[Any] = [symbol, depth, limit]
    if edge_kind:
        edge_filter = "AND e.edge_kind = ?"
        params = [symbol, depth, edge_kind, limit]

    # Recursive CTE: find all reachable nodes from the seed FQN
    # Uses list_concat for visited tracking to prevent cycles
    nodes_sql = f"""
        WITH RECURSIVE reachable AS (
            SELECT s.fqn, s.name, s.kind, s.file_path, 0 AS depth,
                   [s.fqn] AS visited
            FROM symbols s
            WHERE s.fqn = ?

            UNION ALL

            SELECT s2.fqn, s2.name, s2.kind, s2.file_path,
                   r.depth + 1,
                   list_concat(r.visited, [s2.fqn])
            FROM reachable r
            JOIN symbol_edges e ON e.from_fqn = r.fqn
            JOIN symbols s2 ON s2.fqn = e.to_fqn
            WHERE r.depth < ?
              AND NOT list_contains(r.visited, s2.fqn)
              {edge_filter}
        )
        SELECT DISTINCT fqn, name, kind, file_path, depth
        FROM reachable
        ORDER BY depth, fqn
        LIMIT ?
    """

    nodes = services.provider.execute_query(nodes_sql, params)

    # Collect FQNs for edge query
    fqns = [n["fqn"] for n in nodes]
    if not fqns:
        return {"results": [], "edges": [], "count": 0}

    # Get edges between discovered nodes
    placeholders = ", ".join(["?"] * len(fqns))
    edge_params: list[Any] = fqns + fqns
    edge_filter_sql = ""
    if edge_kind:
        edge_filter_sql = "AND e.edge_kind = ?"
        edge_params.append(edge_kind)

    edges_sql = f"""
        SELECT e.from_fqn, e.to_fqn, e.edge_kind, e.from_file, e.to_file
        FROM symbol_edges e
        WHERE e.from_fqn IN ({placeholders})
          AND e.to_fqn IN ({placeholders})
          {edge_filter_sql}
    """

    raw_edges = services.provider.execute_query(edges_sql, edge_params)

    # Build clean response
    results = [
        {"fqn": n["fqn"], "name": n["name"], "kind": n["kind"],
         "file_path": n["file_path"], "depth": n["depth"]}
        for n in nodes
    ]
    edges = [
        {"from_symbol": e["from_fqn"], "to_symbol": e["to_fqn"],
         "edge_kind": e["edge_kind"], "from_file": e["from_file"],
         "to_file": e["to_file"]}
        for e in raw_edges
    ]

    return {"results": results, "edges": edges, "count": len(results)}


def _graph_reachability(
    services: Any,
    scope: str | None,
    limit: int,
) -> dict[str, Any]:
    """Find symbols unreachable from scope entry points."""
    if not scope:
        return {
            "error": "missing_parameter",
            "message": "reachability operation requires 'scope' parameter (file path prefix)",
        }
    escaped_scope = _escape_like(scope)
    scope_pattern = escaped_scope + "%"

    # Query 1: all symbols in scope
    all_in_scope_sql = """
        SELECT fqn, name, kind, file_path
        FROM symbols
        WHERE file_path LIKE ? ESCAPE '\\'
    """
    all_symbols = services.provider.execute_query(all_in_scope_sql, [scope_pattern])

    # Query 2: reachable FQNs via recursive edge traversal from scope roots
    reachable_sql = """
        WITH RECURSIVE reachable AS (
            SELECT DISTINCT s.fqn
            FROM symbols s
            WHERE s.file_path LIKE ? ESCAPE '\\'

            UNION

            SELECT DISTINCT s2.fqn
            FROM reachable r
            JOIN symbol_edges e ON e.from_fqn = r.fqn
            JOIN symbols s2 ON s2.fqn = e.to_fqn
            WHERE s2.file_path LIKE ? ESCAPE '\\'
        )
        SELECT fqn FROM reachable
    """
    reachable_rows = services.provider.execute_query(
        reachable_sql, [scope_pattern, scope_pattern]
    )
    reachable_fqns = {r["fqn"] for r in reachable_rows}

    # Unreachable = all in scope minus reachable
    unreachable = [
        {"fqn": s["fqn"], "name": s["name"], "kind": s["kind"], "file_path": s["file_path"]}
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
    if not scope:
        return {
            "error": "missing_parameter",
            "message": "boundary operation requires 'scope' parameter (file path prefix)",
        }

    escaped_scope = _escape_like(scope)
    scope_pattern = escaped_scope + "%"

    # Edges where one side is in scope and the other is not
    boundary_sql = """
        SELECT
            e.from_fqn, s1.name AS from_name, s1.kind AS from_kind, e.from_file,
            e.to_fqn, s2.name AS to_name, s2.kind AS to_kind, e.to_file,
            e.edge_kind
        FROM symbol_edges e
        JOIN symbols s1 ON e.from_fqn = s1.fqn
        JOIN symbols s2 ON e.to_fqn = s2.fqn
        WHERE (
            (s1.file_path LIKE ? ESCAPE '\\' AND s2.file_path NOT LIKE ? ESCAPE '\\')
            OR
            (s1.file_path NOT LIKE ? ESCAPE '\\' AND s2.file_path LIKE ? ESCAPE '\\')
        )
        LIMIT ?
    """
    raw_edges = services.provider.execute_query(
        boundary_sql,
        [scope_pattern, scope_pattern, scope_pattern, scope_pattern, limit],
    )

    edges = [
        {
            "from_symbol": e["from_fqn"], "from_name": e["from_name"],
            "from_kind": e["from_kind"], "from_file": e["from_file"],
            "to_symbol": e["to_fqn"], "to_name": e["to_name"],
            "to_kind": e["to_kind"], "to_file": e["to_file"],
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
    # Split into outgoing + incoming counts to avoid OR-join performance issues
    scope_filter = ""
    params: list[Any] = []
    if scope:
        escaped_scope = _escape_like(scope)
        scope_pattern = escaped_scope + "%"
        scope_filter = "WHERE s.file_path LIKE ? ESCAPE '\\'"
        params.append(scope_pattern)

    # Query 1: top symbols by total edge count (UNION ALL avoids OR-join)
    top_sql = f"""
        WITH edge_counts AS (
            SELECT s.fqn, s.name, s.kind, s.file_path,
                   COUNT(*) AS total_edges
            FROM symbols s
            JOIN (
                SELECT from_fqn AS fqn FROM symbol_edges
                UNION ALL
                SELECT to_fqn AS fqn FROM symbol_edges
            ) AS all_refs ON all_refs.fqn = s.fqn
            {scope_filter}
            GROUP BY s.fqn, s.name, s.kind, s.file_path
        )
        SELECT fqn, name, kind, file_path, total_edges
        FROM edge_counts
        ORDER BY total_edges DESC
        LIMIT ?
    """
    params.append(limit)
    top_symbols = services.provider.execute_query(top_sql, params)

    if not top_symbols:
        return {"symbols": [], "count": 0}

    # Query 2: per-symbol edge_kind breakdown for the top symbols
    fqns = [s["fqn"] for s in top_symbols]
    placeholders = ", ".join(["?"] * len(fqns))
    breakdown_sql = f"""
        SELECT fqn, edge_kind, edge_count FROM (
            SELECT s.fqn, e.edge_kind, COUNT(*) AS edge_count
            FROM symbols s
            JOIN symbol_edges e ON e.from_fqn = s.fqn OR e.to_fqn = s.fqn
            WHERE s.fqn IN ({placeholders})
            GROUP BY s.fqn, e.edge_kind
        )
    """
    breakdown_rows = services.provider.execute_query(breakdown_sql, fqns)

    # Build breakdown lookup: fqn → {edge_kind: count}
    breakdown_map: dict[str, dict[str, int]] = {}
    for row in breakdown_rows:
        fqn = row["fqn"]
        if fqn not in breakdown_map:
            breakdown_map[fqn] = {}
        breakdown_map[fqn][row["edge_kind"]] = row["edge_count"]

    symbols = [
        {
            "fqn": s["fqn"], "name": s["name"], "kind": s["kind"],
            "file_path": s["file_path"], "total_edges": s["total_edges"],
            "breakdown": breakdown_map.get(s["fqn"], {}),
        }
        for s in top_symbols
    ]

    return {"symbols": symbols, "count": len(symbols)}


@register_tool(
    description=CODE_RESEARCH_DESCRIPTION,
    requires_embeddings=True,
    requires_llm=True,
    requires_reranker=True,
    name="code_research",
)
async def deep_research_impl(
    services: DatabaseServices,
    embedding_manager: EmbeddingManager,
    llm_manager: LLMManager | None,
    query: str,
    progress: Any = None,
    path: str | None = None,
    config: Config | None = None,
) -> dict[str, Any]:
    """Core deep research implementation.

    Args:
        services: Database services bundle
        embedding_manager: Embedding manager instance
        llm_manager: LLM manager instance
        query: Natural language question about codebase architecture or behavior, e.g. "how does authentication work end-to-end?" or "explain the request lifecycle"
        progress: Optional Rich Progress instance for terminal UI (None for MCP)
        path: Optional relative subdirectory to restrict analysis scope, e.g. "src/auth" or "lib/payments" (no leading slash)
        config: Application configuration (optional, defaults to environment config)

    Returns:
        Dict with answer and metadata

    Raises:
        Exception: If LLM or reranker not configured
    """
    # Validate LLM is configured
    if not llm_manager:
        raise Exception(
            "No LLM provider configured. Code research requires an LLM. "
            "Configure an llm section in your chunkhound configuration."
        )

    # Validate reranker is configured
    if not embedding_manager or not embedding_manager.list_providers():
        raise Exception(
            "No embedding providers available. Code research requires reranking "
            "support."
        )

    embedding_provider = embedding_manager.get_provider()
    if not (
        hasattr(embedding_provider, "supports_reranking")
        and embedding_provider.supports_reranking()
    ):
        raise Exception(
            "Code research requires a provider with reranking support. "
            "Configure a rerank_model in your embedding configuration."
        )

    # Create default config from environment if not provided
    if config is None:
        config = Config.from_environment()

    # Create code research service using factory (v1 or v2 based on config)
    # This ensures followup suggestions automatically update if tool is renamed
    research_service = ResearchServiceFactory.create(
        config=config,
        db_services=services,
        embedding_manager=embedding_manager,
        llm_manager=llm_manager,
        tool_name="code_research",
        progress=progress,
        path_filter=path,
    )

    return await research_service.deep_research(query)


# =============================================================================
# Stats Tool
# =============================================================================

GET_STATS_DESCRIPTION = """Get database and index statistics — file, chunk, symbol, and edge counts with per-language breakdown and optional LSP server status. No parameters required."""


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
    # Query counts from each table
    file_rows = services.provider.execute_query(
        "SELECT COUNT(*) as count FROM files", []
    )
    chunk_rows = services.provider.execute_query(
        "SELECT COUNT(*) as count FROM chunks", []
    )
    symbol_rows = services.provider.execute_query(
        "SELECT COUNT(*) as count FROM symbols", []
    )
    edge_rows = services.provider.execute_query(
        "SELECT COUNT(*) as count FROM symbol_edges", []
    )

    # Per-language breakdown
    lang_rows = services.provider.execute_query(
        "SELECT language, COUNT(*) as count FROM symbols "
        "GROUP BY language ORDER BY count DESC",
        [],
    )

    result: dict[str, Any] = {
        "files": file_rows[0]["count"] if file_rows else 0,
        "chunks": chunk_rows[0]["count"] if chunk_rows else 0,
        "symbols": symbol_rows[0]["count"] if symbol_rows else 0,
        "symbol_edges": edge_rows[0]["count"] if edge_rows else 0,
        "languages": [
            {"language": row["language"], "count": row["count"]}
            for row in lang_rows
        ],
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


# =============================================================================
# Tool Execution
# =============================================================================


async def execute_tool(
    tool_name: str,
    services: Any,
    embedding_manager: Any,
    arguments: dict[str, Any],
    scan_progress: dict | None = None,
    llm_manager: Any = None,
    config: Config | None = None,
    lsp_client_pool: Any = None,
) -> dict[str, Any] | str:
    """Execute a tool from the registry with proper argument handling.

    Args:
        tool_name: Name of the tool to execute
        services: DatabaseServices instance
        embedding_manager: EmbeddingManager instance
        arguments: Tool arguments from the request
        scan_progress: Optional scan progress from MCPServerBase
        llm_manager: Optional LLMManager instance for code_research
        config: Optional Config instance for research service factory

    Returns:
        Tool execution result

    Raises:
        ValueError: If tool not found in registry
        Exception: If tool execution fails
    """
    if tool_name not in TOOL_REGISTRY:
        raise ValueError(f"Unknown tool: {tool_name}")

    tool = TOOL_REGISTRY[tool_name]

    # Build kwargs by inspecting function signature and mapping available arguments
    sig = inspect.signature(tool.implementation)
    kwargs: dict[str, Any] = {}

    for param_name in sig.parameters.keys():
        # Map infrastructure parameters
        if param_name == "services":
            kwargs["services"] = services
        elif param_name == "embedding_manager":
            kwargs["embedding_manager"] = embedding_manager
        elif param_name == "llm_manager":
            kwargs["llm_manager"] = llm_manager
        elif param_name == "scan_progress":
            kwargs["scan_progress"] = scan_progress
        elif param_name == "config":
            kwargs["config"] = config
        elif param_name == "lsp_client_pool":
            kwargs["lsp_client_pool"] = lsp_client_pool
        elif param_name == "progress":
            # Progress parameter for terminal UI (None for MCP mode)
            kwargs["progress"] = None
        elif param_name in arguments:
            # Tool-specific parameter from request
            kwargs[param_name] = arguments[param_name]
        # If parameter not found and has default, it will use the default

    # Execute the tool
    result = await tool.implementation(**kwargs)

    # Handle special return types
    if tool_name == "code_research":
        # Code research returns dict with 'answer' key - return raw markdown string
        if isinstance(result, dict):
            query_arg = arguments.get("query", "unknown")
            fallback = (
                "Research incomplete: Unable to analyze "
                f"'{query_arg}'. "
                "Try a more specific query or check that relevant code exists."
            )
            answer = result.get("answer", fallback)
            return str(answer)

    # Convert result to dict if it's not already
    if hasattr(result, "__dict__"):
        return dict(result)
    elif isinstance(result, dict):
        return result
    else:
        return {"result": result}
