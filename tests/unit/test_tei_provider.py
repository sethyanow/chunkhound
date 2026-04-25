"""Unit tests for TEIEmbeddingProvider (ch-agj Group D).

Cycle A — class shape:
  - Instantiation requires (dims, base_url, model)
  - name property returns "tei"
  - dims property returns the configured value AND does NOT fall through
    to parent's hardcoded 1536 (regression guard)
  - dims rejects invalid types/values at __init__ entry (fail-fast)
  - supports_reranking() returns False when constructed embedding-only

Cycle B — task → extra_body mapping:
  - task="passage" → extra_body={"task": "retrieval.passage"} on the
    underlying client.embeddings.create() call
  - task="query"   → extra_body={"task": "retrieval.query"}
  - task=None      → extra_body kwarg OMITTED (not passed as None)
  - validate_task fail-fast on unknown values, symmetric with Voyage/OpenAI
  - parent's _embed_batch_with_extras receives the dict (boundary test)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# 5. task → extra_body mapping (Cycle B)
# ---------------------------------------------------------------------------


def _stub_embedding_response(dims: int = 1024, count: int = 1) -> MagicMock:
    """Build a stand-in for client.embeddings.create(...) response."""
    response = MagicMock()
    response.data = [
        MagicMock(index=i, embedding=[0.1] * dims) for i in range(count)
    ]
    response.usage = MagicMock(total_tokens=count * 5)
    return response


class TestTaskToExtraBodyMapping:
    @pytest.mark.parametrize(
        "task,expected_extra_body",
        [
            pytest.param(
                "passage",
                {"task": "retrieval.passage"},
                id="passage",
            ),
            pytest.param(
                "query",
                {"task": "retrieval.query"},
                id="query",
            ),
        ],
    )
    async def test_embed_sends_task_via_extra_body(
        self, task: str, expected_extra_body: dict
    ) -> None:
        # Arrange
        provider = _make_tei()
        create_mock = AsyncMock(return_value=_stub_embedding_response())
        provider._client.embeddings.create = create_mock

        # Act
        await provider.embed(["hello"], task=task)

        # Assert — extra_body present with correct mapping
        create_mock.assert_called_once()
        sent_kwargs = create_mock.call_args.kwargs
        assert "extra_body" in sent_kwargs, (
            f"TEI must forward task via extra_body; got kwargs {set(sent_kwargs)}"
        )
        assert sent_kwargs["extra_body"] == expected_extra_body

    async def test_embed_omits_extra_body_when_task_is_none(self) -> None:
        """task=None must NOT send extra_body=None — symmetric with the
        OpenAI omit-when-None contract. TEI servers should accept the same
        request shape as a vanilla OpenAI request when no task hint is given.
        """
        # Arrange
        provider = _make_tei()
        create_mock = AsyncMock(return_value=_stub_embedding_response())
        provider._client.embeddings.create = create_mock

        # Act
        await provider.embed(["hello"], task=None)

        # Assert — extra_body absent from kwargs
        create_mock.assert_called_once()
        sent_kwargs = create_mock.call_args.kwargs
        assert "extra_body" not in sent_kwargs, (
            f"task=None should omit extra_body kwarg entirely; got {sent_kwargs}"
        )


class TestTeiTaskValidation:
    @pytest.mark.parametrize(
        "bad_task",
        [
            pytest.param("document", id="voyage-legacy-literal"),
            pytest.param("retrieval.passage", id="jina-internal-format"),
            pytest.param("", id="empty-string"),
            pytest.param("PASSAGE", id="uppercase"),
            pytest.param(" passage", id="whitespace-padded"),
            pytest.param(True, id="bool-true"),
            pytest.param(["passage"], id="list-wrap"),
        ],
    )
    async def test_embed_raises_value_error_on_unknown_task(
        self, bad_task
    ) -> None:
        """Symmetric with Voyage and OpenAI — fail-fast on unknown task at
        the deepest internal entry. validate_task error message includes
        repr(task) so list-vs-string-vs-bool is distinguishable.
        """
        # Arrange
        provider = _make_tei()
        create_mock = AsyncMock(return_value=_stub_embedding_response())
        provider._client.embeddings.create = create_mock

        # Act / Assert
        with pytest.raises(ValueError, match="Unknown embedding task"):
            await provider.embed(["hello"], task=bad_task)

        # API must not be called when validation fails
        create_mock.assert_not_called()


class TestParentBoundaryReceivesExtraBody:
    """Boundary regression test: parent's _embed_batch_with_extras must
    receive extra_body — catches a future parent-refactor that strips
    extras silently. Tests the contract at the parent-method seam, not
    just at TEI's _embed_batch_internal output.
    """

    async def test_parent_embed_batch_with_extras_receives_extra_body(self) -> None:
        # Arrange
        provider = _make_tei()
        # Patch the inherited parent method directly so we observe what
        # TEI's override forwards to it.
        captured: dict = {}

        async def capturing_with_extras(
            texts, extra_body=None, *, task=None
        ):
            captured["texts"] = texts
            captured["extra_body"] = extra_body
            captured["task"] = task
            return [[0.0] * 1024]

        provider._embed_batch_with_extras = capturing_with_extras  # type: ignore[method-assign]

        # Act
        await provider._embed_batch_internal(["hello"], task="passage")

        # Assert
        assert captured["extra_body"] == {"task": "retrieval.passage"}, (
            f"parent must receive extra_body dict; got {captured.get('extra_body')!r}"
        )
