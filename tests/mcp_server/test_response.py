"""Tests for the response module — token estimation and response size limiting."""

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools.response import (
    MAX_RESPONSE_TOKENS,
    SearchResponse,
    estimate_tokens,
    limit_response_size,
)


class TestEstimateTokens:
    """Tests for the estimate_tokens heuristic."""

    def test_empty_string_returns_zero(self):
        assert estimate_tokens("") == 0

    def test_short_string(self):
        # "hello world" is 11 chars, // 3 = 3
        assert estimate_tokens("hello world") == 3

    def test_longer_string(self):
        text = "a" * 300
        assert estimate_tokens(text) == 100

    def test_single_char(self):
        assert estimate_tokens("x") == 0  # 1 // 3 = 0


class TestLimitResponseSize:
    """Tests for limit_response_size truncation logic."""

    def _make_response(self, num_results: int, content_size: int = 100) -> SearchResponse:
        """Helper to build a SearchResponse with controlled size."""
        results = [
            {"file_path": f"file_{i}.py", "content": "x" * content_size, "start_line": 1, "end_line": 10}
            for i in range(num_results)
        ]
        return {
            "results": results,
            "pagination": {
                "offset": 0,
                "page_size": num_results,
                "has_more": False,
                "total": num_results,
            },
        }

    def test_passthrough_when_under_limit(self):
        """Small response passes through unchanged."""
        response = self._make_response(num_results=2, content_size=50)
        result = limit_response_size(response)
        assert len(result["results"]) == 2
        assert result["pagination"]["page_size"] == 2

    def test_truncates_oversized_response(self):
        """Response exceeding MAX_RESPONSE_TOKENS gets reduced."""
        # Each result ~130 chars in JSON. MAX_RESPONSE_TOKENS tokens * 3 chars/token = budget.
        # Create enough results to blow way past the limit.
        chars_per_result = 500
        budget_chars = MAX_RESPONSE_TOKENS * 3
        num_results = (budget_chars // chars_per_result) * 5  # 5x over budget
        response = self._make_response(num_results=num_results, content_size=chars_per_result)

        result = limit_response_size(response)
        assert len(result["results"]) < num_results
        assert result["pagination"]["has_more"] is True

    def test_empty_results_passthrough(self):
        """Response with no results returns unchanged."""
        response: SearchResponse = {
            "results": [],
            "pagination": {"offset": 0, "page_size": 0, "has_more": False, "total": 0},
        }
        result = limit_response_size(response)
        assert result["results"] == []

    def test_pagination_next_offset_set_on_truncation(self):
        """When truncated, next_offset is set for pagination."""
        chars_per_result = 500
        budget_chars = MAX_RESPONSE_TOKENS * 3
        num_results = (budget_chars // chars_per_result) * 5
        response = self._make_response(num_results=num_results, content_size=chars_per_result)

        result = limit_response_size(response)
        if len(result["results"]) < num_results:
            assert result["pagination"]["next_offset"] == len(result["results"])
