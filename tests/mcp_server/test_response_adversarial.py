"""Adversarial stress tests for response module."""

import pytest

pytestmark = pytest.mark.unit

from chunkhound.mcp_server.tools.response import (
    MAX_RESPONSE_TOKENS,
    estimate_tokens,
    limit_response_size,
)


class TestEstimateTokensAdversarial:
    """Adversarial patterns for estimate_tokens."""

    def test_unicode_multibyte(self):
        """Encoding boundary: multi-byte chars — len() counts codepoints, not bytes."""
        text = "日本語テスト"  # 6 codepoints
        result = estimate_tokens(text)
        assert result == 2  # 6 // 3

    def test_whitespace_only(self):
        """Encoding boundary: all whitespace."""
        assert estimate_tokens("   ") == 1  # 3 // 3

    def test_newlines_and_tabs(self):
        """Encoding boundary: control characters count as chars."""
        assert estimate_tokens("\n\t\r") == 1  # 3 // 3

    def test_very_large_string(self):
        """Resource exhaustion: large string doesn't crash, returns proportional."""
        text = "x" * 1_000_000
        result = estimate_tokens(text)
        assert result == 333_333  # 1M // 3


class TestLimitResponseSizeAdversarial:
    """Adversarial patterns for limit_response_size."""

    def _make_response(self, num_results, content_size=100):
        results = [
            {"file_path": f"f{i}.py", "content": "a" * content_size, "start_line": 1, "end_line": 10}
            for i in range(num_results)
        ]
        return {
            "results": results,
            "pagination": {"offset": 0, "page_size": num_results, "has_more": False, "total": num_results},
        }

    def test_singular_result(self):
        """Singular: one result under limit."""
        resp = self._make_response(1, content_size=50)
        result = limit_response_size(resp)
        assert len(result["results"]) == 1

    def test_max_tokens_zero(self):
        """Type boundary: max_tokens=0 forces empty results."""
        resp = self._make_response(5, content_size=100)
        result = limit_response_size(resp, max_tokens=0)
        assert result["results"] == []
        assert result["pagination"]["has_more"] is True

    def test_second_run_same_result(self):
        """The 'second run' test: same input, same output (no mutation)."""
        resp = self._make_response(3, content_size=50)
        r1 = limit_response_size(resp)
        r2 = limit_response_size(resp)
        assert r1 == r2

    def test_none_results_key(self):
        """Type boundary: results is None — early return."""
        resp = {"results": None, "pagination": {"offset": 0, "page_size": 0, "has_more": False, "total": 0}}
        result = limit_response_size(resp)
        assert result is resp  # early return, same object

    def test_pagination_preserved_on_passthrough(self):
        """State: original pagination fields preserved when no truncation."""
        resp = self._make_response(2, content_size=10)
        resp["pagination"]["has_more"] = True
        resp["pagination"]["total"] = 100
        result = limit_response_size(resp)
        assert result["pagination"]["has_more"] is True
        assert result["pagination"]["total"] == 100
