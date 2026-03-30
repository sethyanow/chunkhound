"""
Provider configuration for testing.

Returns fake providers only — never loads real credentials or makes network calls.
"""

from typing import Any

from tests.fixtures.fake_providers import FakeEmbeddingProvider


def get_reranking_providers() -> list[tuple[str, type, dict[str, Any]]]:
    """Return fake providers for parametrized testing.

    Returns a FakeEmbeddingProvider that supports reranking,
    using only constructor args FakeEmbeddingProvider accepts.
    """
    return [
        (
            "fake",
            FakeEmbeddingProvider,
            {
                "model": "fake-rerank",
                "dims": 1536,
                "batch_size": 100,
            },
        )
    ]


def get_provider_ids() -> list[str]:
    """Get list of available provider IDs for pytest parametrization."""
    return [provider_name for provider_name, _, _ in get_reranking_providers()]


def should_skip_if_no_providers() -> bool:
    """Check if tests should be skipped due to no available providers."""
    return len(get_reranking_providers()) == 0
