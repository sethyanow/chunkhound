"""Unit tests for OpenAIEmbeddingProvider task hint plumbing.

Covers (ch-agj Group C):
  - embed* accept task kwarg without TypeError
  - OpenAI API payload does NOT include task (OpenAI rejects unknown fields)
  - validate_task fires at _embed_batch_internal entry (symmetric fail-fast
    with Voyage — OpenAI doesn't consume task but validates for consistent
    error UX; see ch-agj Key Considerations)
  - Token-limit recursive fallback preserves task via lambda closure
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chunkhound.providers.embeddings.openai_provider import OpenAIEmbeddingProvider

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_provider(**kwargs) -> OpenAIEmbeddingProvider:
    """Build an OpenAIEmbeddingProvider with client_initialized=True.

    Callers replace provider._client with a mock before exercising embed
    paths — the real client would require env vars.
    """
    defaults: dict = {"api_key": "test-key", "model": "text-embedding-3-small"}
    defaults.update(kwargs)
    provider = OpenAIEmbeddingProvider(**defaults)
    provider._client = MagicMock()
    provider._client_initialized = True
    return provider


def _stub_embedding_response(dims: int = 1536, count: int = 1) -> MagicMock:
    """Build a stand-in for client.embeddings.create(...) response."""
    response = MagicMock()
    response.data = [
        MagicMock(index=i, embedding=[0.1] * dims) for i in range(count)
    ]
    response.usage = MagicMock(total_tokens=count * 5)
    return response


# ---------------------------------------------------------------------------
# 1. Accept task kwarg
# ---------------------------------------------------------------------------


class TestAcceptsTaskKwarg:
    async def test_embed_accepts_task_arg_without_error(self) -> None:
        # Arrange
        provider = _make_provider()
        provider._client.embeddings.create = AsyncMock(
            return_value=_stub_embedding_response(count=1)
        )

        # Act — no TypeError expected
        result = await provider.embed(["hello"], task="query")

        # Assert
        assert len(result) == 1
        assert len(result[0]) == 1536


# ---------------------------------------------------------------------------
# 2. task is NOT forwarded to OpenAI API
# ---------------------------------------------------------------------------


class TestTaskNotInApiPayload:
    @pytest.mark.parametrize(
        "task", [None, "passage", "query"], ids=["none", "passage", "query"]
    )
    async def test_embed_does_not_send_task_in_payload(self, task) -> None:
        # Arrange — real OpenAI rejects unknown fields, so we MUST NOT
        # forward task/input_type/prompt_name/extra_body.
        provider = _make_provider()
        create_mock = AsyncMock(return_value=_stub_embedding_response(count=1))
        provider._client.embeddings.create = create_mock

        # Act
        await provider.embed(["hello"], task=task)

        # Assert
        create_mock.assert_called_once()
        forbidden = {"task", "input_type", "prompt_name", "extra_body"}
        sent_kwargs = set(create_mock.call_args.kwargs.keys())
        assert not (forbidden & sent_kwargs), (
            f"OpenAI payload must not include {forbidden & sent_kwargs}; "
            f"got kwargs {sent_kwargs}"
        )


# ---------------------------------------------------------------------------
# 3. Validation — symmetric with Voyage
# ---------------------------------------------------------------------------


class TestTaskValidation:
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
        # Arrange
        provider = _make_provider()
        create_mock = AsyncMock(return_value=_stub_embedding_response(count=1))
        provider._client.embeddings.create = create_mock

        # Act / Assert
        with pytest.raises(ValueError, match="Unknown embedding task"):
            await provider.embed(["hello"], task=bad_task)

        # API must not be called when validation fails
        create_mock.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Token-limit recursion preserves task via lambda closure
# ---------------------------------------------------------------------------


class TestRecursiveTokenLimitFallback:
    async def test_fallback_passes_lambda_not_bare_method(self) -> None:
        """Regression guard: handle_token_limit_error must receive a lambda
        that closes over task, NOT a bare reference to self._embed_batch_internal
        (which would drop kwargs across recursion).
        """
        # Arrange
        provider = _make_provider()

        # Trigger the token-limit error path
        from openai import BadRequestError

        error = BadRequestError(
            message="This model's maximum context length is 8192 tokens",
            response=MagicMock(status_code=400, request=MagicMock()),
            body={"error": {"message": "max tokens", "type": "invalid_request_error"}},
        )
        provider._client.embeddings.create = AsyncMock(side_effect=error)

        captured: dict = {}

        async def capture_handler(**kwargs):
            captured["embed_function"] = kwargs["embed_function"]
            # Short-circuit — return plausible result
            return [[0.0] * 1536]

        # Act
        with patch(
            "chunkhound.providers.embeddings.openai_provider.handle_token_limit_error",
            side_effect=capture_handler,
        ):
            await provider._embed_batch_internal(["long text"], task="passage")

        # Assert — embed_function must be a lambda, not the bare method
        embed_fn = captured.get("embed_function")
        assert embed_fn is not None, "handle_token_limit_error not reached"
        assert embed_fn.__name__ == "<lambda>", (
            "embed_function must be a lambda closing over task. Got bare "
            f"{embed_fn.__name__!r} — would drop task across recursion."
        )

    async def test_fallback_lambda_closure_captures_task(self) -> None:
        """Lambda closure must hold task='passage' so the recursive call
        to _embed_batch_internal gets task threaded through."""
        # Arrange
        provider = _make_provider()

        from openai import BadRequestError

        error = BadRequestError(
            message="This model's maximum context length is 8192 tokens",
            response=MagicMock(status_code=400, request=MagicMock()),
            body={"error": {"message": "max tokens", "type": "invalid_request_error"}},
        )
        provider._client.embeddings.create = AsyncMock(side_effect=error)

        captured: dict = {}

        async def capture_handler(**kwargs):
            captured["embed_function"] = kwargs["embed_function"]
            return [[0.0] * 1536]

        # Act
        with patch(
            "chunkhound.providers.embeddings.openai_provider.handle_token_limit_error",
            side_effect=capture_handler,
        ):
            await provider._embed_batch_internal(["long text"], task="passage")

        # Assert — introspect closure
        embed_fn = captured["embed_function"]
        closure_vars = inspect.getclosurevars(embed_fn)
        # task is captured via the nonlocal scope of _embed_batch_internal's
        # parameter binding
        assert closure_vars.nonlocals.get("task") == "passage", (
            f"Lambda must capture task='passage' via closure; "
            f"nonlocals={closure_vars.nonlocals}"
        )
