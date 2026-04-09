"""Search MCP tool — unified search dispatching to regex, semantic, symbols, structural.

Each search type: validate params → delegate to service or build SQL → execute → format.
Structural search: semantic seed → GraphWalkExpander (overlap → walk → resolution → dedup) → type_filter → paginate.
"""

from pathlib import Path
from typing import Any, Literal, cast

from chunkhound.services.search.graph_walk_expander import GraphWalkExpander

from .queries.search import (
    build_symbol_count_query,
    build_symbol_search_query,
    build_type_filter_query,
)
from .registry import register_tool
from .response import SearchResponse, limit_response_size

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
- **structural**: Semantic search enriched with graph walk expansion. Finds code by meaning, then discovers structurally-related chunks (callers, callees, type references) via the symbol dependency graph. Returns both semantic matches and graph-discovered code.
  Examples: "error handling" (finds handlers + their callers), "database queries" (finds query functions + their call sites)

DECISION GUIDE:
- Known symbol or pattern → regex
- Concept or behavior → semantic
- Symbol by name/type → symbols
- Concept + structural context (callers, dependencies) → structural
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
- **structural**: Semantic search enriched with graph walk expansion. Finds code by meaning, then discovers structurally-related chunks (callers, callees, type references) via the symbol dependency graph. Returns both semantic matches and graph-discovered code.
  Examples: "error handling" (finds handlers + their callers), "database queries" (finds query functions + their call sites)

DECISION GUIDE:
- Known symbol or pattern → regex
- Concept or behavior → semantic
- Symbol by name/type → symbols
- Concept + structural context (callers, dependencies) → structural

OPTIONAL FILTERS:
- **path**: Restrict to a subdirectory (e.g. "src/auth")
- **type_filter**: Filter by type signature substring (e.g. "Result", "int"). Works with all search types.

OUTPUT: {results: [{file_path, content, start_line, end_line}], pagination}"""


# CODE_RESEARCH_DESCRIPTION moved to research.py (owns the code_research tool)


# =============================================================================
# Helper Functions
# =============================================================================


def _convert_paths_to_native(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert file paths in search results to native platform format."""
    for result in results:
        if "file_path" in result and result["file_path"]:
            result["file_path"] = str(Path(result["file_path"]))
    return results


# =============================================================================
# Tool Implementation
# =============================================================================


@register_tool(
    description=SEARCH_DESCRIPTION,
    requires_embeddings=False,
    name="search",
)
async def search_impl(
    services: Any,
    embedding_manager: Any | None,
    type: Literal["regex", "semantic", "symbols", "structural"],
    query: str,
    path: str | None = None,
    page_size: int = 10,
    offset: int = 0,
    fuzzy_path: bool = False,
    type_filter: str | None = None,
) -> SearchResponse:
    """Unified search dispatching to regex, semantic, symbols, or structural.

    Args:
        services: Database services bundle
        embedding_manager: Embedding manager (required for semantic/structural)
        type: Search mode
        query: Search query (pattern, concept, or symbol name)
        path: Optional subdirectory scope
        page_size: Results per page (1-100)
        offset: Pagination offset
        fuzzy_path: Allow fuzzy path matching
        type_filter: Optional type signature substring filter
    """
    if type not in ("semantic", "regex", "symbols", "structural"):
        raise ValueError(f"Invalid search type: '{type}'. Must be 'semantic', 'regex', 'symbols', or 'structural'.")

    page_size = max(1, min(page_size, 100))
    offset = max(0, offset)

    if type == "symbols":
        return await _search_symbols(
            services,
            query,
            path,
            page_size,
            offset,
            type_filter,
        )

    if type == "structural":
        return await _search_structural(
            services,
            embedding_manager,
            query,
            path,
            page_size,
            offset,
            fuzzy_path,
            type_filter,
        )

    if type == "semantic":
        if not embedding_manager or not embedding_manager.list_providers():
            raise ValueError(
                "Semantic search requires embedding provider. "
                "Configure via .chunkhound.json or CHUNKHOUND_EMBEDDING__API_KEY. "
                "Use type='regex' for pattern-based search without embeddings."
            )

        try:
            provider_obj = embedding_manager.get_provider()
            provider_name = provider_obj.name
            model_name = provider_obj.model
        except ValueError:
            raise ValueError("No default embedding provider configured.")

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

    native_results = _convert_paths_to_native(results)

    response = cast(SearchResponse, {"results": native_results, "pagination": pagination})
    return limit_response_size(response)


# =============================================================================
# Internal Search Implementations
# =============================================================================


async def _search_symbols(
    services: Any,
    query: str,
    path: str | None,
    page_size: int,
    offset: int,
    type_filter: str | None,
) -> SearchResponse:
    """Search the symbols table directly by name/FQN substring."""
    # Build and execute search query
    search_sql, search_params = build_symbol_search_query(
        query=query,
        path=path,
        type_filter=type_filter,
        limit=page_size,
        offset=offset,
    )
    symbol_rows = services.provider.execute_query(search_sql, search_params)

    # Build and execute count query for pagination
    count_sql, count_params = build_symbol_count_query(
        query=query,
        path=path,
        type_filter=type_filter,
    )
    count_rows = services.provider.execute_query(count_sql, count_params)
    total = count_rows[0]["total"] if count_rows else 0

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

    response = cast(SearchResponse, {"results": results, "pagination": pagination})
    return limit_response_size(response)


async def _search_structural(
    services: Any,
    embedding_manager: Any,
    query: str,
    path: str | None,
    page_size: int,
    offset: int,
    fuzzy_path: bool,
    type_filter: str | None,
) -> SearchResponse:
    """Structural search: semantic search + graph walk expansion.

    Pipeline:
    1. Semantic search (broader seed pool)
    2-5. GraphWalkExpander (overlap → walk → resolution → dedup)
    6. Combine (semantic first, then graph-discovered)
    7. Apply type_filter if present
    8. Paginate combined pool
    """
    # Validate embedding manager (structural requires semantic as first stage)
    if not embedding_manager or not embedding_manager.list_providers():
        raise ValueError(
            "Structural search requires embedding provider. "
            "Configure via .chunkhound.json or CHUNKHOUND_EMBEDDING__API_KEY. "
            "Use type='regex' for pattern-based search without embeddings."
        )

    try:
        provider_obj = embedding_manager.get_provider()
        provider_name = provider_obj.name
        model_name = provider_obj.model
    except ValueError:
        raise ValueError("No default embedding provider configured.")

    # Stage 1: Semantic search with broader seed pool
    results, pagination = await services.search_service.search_semantic(
        query=query,
        page_size=page_size * 2,
        offset=0,
        provider=provider_name,
        model=model_name,
        path_filter=path,
        fuzzy_path=fuzzy_path,
    )

    if not results:
        response = cast(SearchResponse, {"results": [], "pagination": pagination})
        return limit_response_size(response)

    # Stages 2-5: Graph expansion via GraphWalkExpander
    # Overlap → walk → chunk resolution → dedup handled internally
    expander = GraphWalkExpander(services.provider)
    graph_chunks = await expander.expand(results, depth=2)

    # Stage 6: Combine — semantic first, then graph-discovered
    combined = results + graph_chunks

    # Stage 7: Apply type_filter if present
    if type_filter and combined:
        combined = _apply_type_filter(services, combined, type_filter)

    # Stage 8: Paginate combined pool
    total = len(combined)
    paginated = combined[offset : offset + page_size]
    native_results = _convert_paths_to_native(paginated)

    response = cast(
        SearchResponse,
        {
            "results": native_results,
            "pagination": {
                "offset": offset,
                "page_size": page_size,
                "has_more": total > offset + page_size,
                "total": total,
                "next_offset": offset + page_size if total > offset + page_size else None,
            },
        },
    )
    return limit_response_size(response)


def _apply_type_filter(
    services: Any,
    results: list[dict],
    type_filter: str,
) -> list[dict]:
    """Post-filter chunk results by matching symbols with type_signature."""
    if not results:
        return results

    filter_sql, filter_params = build_type_filter_query(
        results=results,
        type_filter=type_filter,
    )
    matches = services.provider.execute_query(filter_sql, filter_params)

    match_set = {(m["file_path"], m["range_start"], m["range_end"]) for m in matches}

    return [
        r
        for r in results
        if any(fp == r["file_path"] and rs <= r["end_line"] and re >= r["start_line"] for fp, rs, re in match_set)
    ]
