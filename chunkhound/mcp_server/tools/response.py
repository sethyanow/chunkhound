"""Response types and size limiting for MCP tool responses.

Provides token estimation, response truncation, and TypedDict definitions
for structured search results with pagination.
"""

import json
from typing import Any, TypedDict

try:
    from typing import NotRequired  # type: ignore[attr-defined]
except (ImportError, AttributeError):
    from typing_extensions import NotRequired

# =============================================================================
# Constants
# =============================================================================

MAX_RESPONSE_TOKENS = 8000
MIN_RESPONSE_TOKENS = 1000
MAX_ALLOWED_TOKENS = 50000


# =============================================================================
# Type Definitions
# =============================================================================


class PaginationInfo(TypedDict):
    """Pagination metadata for search results."""

    offset: int
    page_size: int
    has_more: bool
    total: int
    next_offset: NotRequired[int | None]


class SearchResponse(TypedDict):
    """Structured search response with results and pagination."""

    results: list[dict[str, Any]]
    pagination: PaginationInfo


# =============================================================================
# Token Estimation & Response Limiting
# =============================================================================


def estimate_tokens(text: str) -> int:
    """Estimate token count using simple heuristic (3 chars ~ 1 token for safety)."""
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
