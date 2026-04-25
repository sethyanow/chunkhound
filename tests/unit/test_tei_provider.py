"""Unit tests for TEIEmbeddingProvider (ch-agj Group D Cycle A).

Covers Cycle A class shape:
  - Instantiation requires (dims, base_url, model)
  - name property returns "tei"
  - dims property returns the configured value AND does NOT fall through
    to parent's hardcoded 1536 (regression guard)
  - dims rejects invalid types/values at __init__ entry (fail-fast)
  - supports_reranking() returns False when constructed embedding-only

Cycle B will extend with task->extra_body mapping tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from chunkhound.providers.embeddings.tei_provider import TEIEmbeddingProvider

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tei(**kwargs) -> TEIEmbeddingProvider:
    """Build a TEIEmbeddingProvider with a fake client for unit-level tests.

    Defaults are the smallest valid embedding-only config: a positive dims,
    a base_url, and a model. Callers can override any of these to drive
    error-path or cycle-B mapping tests.
    """
    defaults: dict = {
        "dims": 1024,
        "base_url": "http://localhost:8080/v1",
        "model": "jinaai/jina-embeddings-v3",
    }
    defaults.update(kwargs)
    provider = TEIEmbeddingProvider(**defaults)
    provider._client = MagicMock()
    provider._client_initialized = True
    return provider


# ---------------------------------------------------------------------------
# 1. Instantiation
# ---------------------------------------------------------------------------


class TestInstantiation:
    def test_constructs_with_required_args(self) -> None:
        # Act / Assert — no exception
        provider = _make_tei()
        assert provider is not None

    def test_name_is_tei(self) -> None:
        # Arrange / Act
        provider = _make_tei()

        # Assert
        assert provider.name == "tei"

    def test_name_does_not_inherit_openai(self) -> None:
        """Regression guard — if the @property name override is removed,
        parent returns 'openai' or 'azure_openai'. Must stay 'tei'.
        """
        provider = _make_tei()
        assert provider.name not in ("openai", "azure_openai")


# ---------------------------------------------------------------------------
# 2. dims override (the load-bearing one — wrong dims breaks cache key)
# ---------------------------------------------------------------------------


class TestDimsOverride:
    @pytest.mark.parametrize(
        "configured_dims,model_name",
        [
            pytest.param(1024, "jinaai/jina-embeddings-v3", id="jina-v3"),
            pytest.param(
                256, "jinaai/jina-embeddings-v5-text-nano", id="jina-v5-nano"
            ),
            pytest.param(
                512, "jinaai/jina-embeddings-v5-text-small", id="jina-v5-small"
            ),
        ],
    )
    def test_dims_returns_configured_value_and_not_parent_default(
        self, configured_dims: int, model_name: str
    ) -> None:
        """Two assertions in one test (intentional):
          1. positive — dims returns the configured int
          2. negative — dims is NOT 1536 (parent's hardcoded default)

        The negative assertion catches a regression where the @property dims
        override is accidentally removed and parent's fallback leaks through.
        """
        # Arrange / Act
        provider = _make_tei(dims=configured_dims, model=model_name)

        # Assert — positive
        assert provider.dims == configured_dims, (
            f"Expected dims={configured_dims} for {model_name}, got {provider.dims}"
        )

        # Assert — negative (regression guard against parent fallback)
        assert provider.dims != 1536, (
            "Parent OpenAIEmbeddingProvider.dims hardcodes 1536 for non-OpenAI "
            "models; if you see 1536 here, the @property dims override is gone"
        )


# ---------------------------------------------------------------------------
# 3. dims input validation (fail-fast at __init__)
# ---------------------------------------------------------------------------


class TestDimsValidation:
    @pytest.mark.parametrize(
        "bad_dims",
        [
            pytest.param(None, id="none"),
            pytest.param("1024", id="str"),
            pytest.param(1.5, id="float"),
            pytest.param(True, id="bool-true"),
            pytest.param(False, id="bool-false"),
        ],
    )
    def test_rejects_dims_of_wrong_type(self, bad_dims) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="dims"):
            _make_tei(dims=bad_dims)

    @pytest.mark.parametrize(
        "bad_dims",
        [
            pytest.param(0, id="zero"),
            pytest.param(-1, id="negative-one"),
            pytest.param(-100, id="large-negative"),
        ],
    )
    def test_rejects_dims_zero_or_negative(self, bad_dims: int) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="dims"):
            _make_tei(dims=bad_dims)


# ---------------------------------------------------------------------------
# 4. supports_reranking — embedding-only config returns False
# ---------------------------------------------------------------------------


class TestSupportsReranking:
    def test_supports_reranking_false_for_embedding_only_config(self) -> None:
        """A TEI provider constructed without rerank_url / rerank_model must
        report supports_reranking() == False. Inherits from parent, but
        parent's default rerank_url='/rerank' would otherwise leak to True.
        """
        # Arrange / Act
        provider = _make_tei()  # no rerank kwargs

        # Assert
        assert provider.supports_reranking() is False
