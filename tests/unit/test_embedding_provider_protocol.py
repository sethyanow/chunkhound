"""Unit tests for the EmbeddingProvider protocol surface.

Covers:
  - validate_task: accepts None/"passage"/"query", rejects everything else
  - EmbeddingProvider protocol methods accept optional task kwarg
"""

from __future__ import annotations

import inspect

import pytest

from chunkhound.interfaces.embedding_provider import (
    EmbeddingProvider,
    validate_task,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# validate_task — happy path
# ---------------------------------------------------------------------------


class TestValidateTaskAccepts:
    @pytest.mark.parametrize(
        "value,expected",
        [
            pytest.param(None, None, id="none"),
            pytest.param("passage", "passage", id="passage"),
            pytest.param("query", "query", id="query"),
        ],
    )
    def test_validate_task_accepts_none_passage_query(
        self, value: str | None, expected: str | None
    ) -> None:
        # Act
        result = validate_task(value)
        # Assert
        assert result == expected


# ---------------------------------------------------------------------------
# validate_task — rejection
# ---------------------------------------------------------------------------


class TestValidateTaskRejects:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("document", id="voyage-legacy-literal"),
            pytest.param("retrieval.passage", id="jina-internal-format"),
            pytest.param("", id="empty-string"),
            pytest.param("Q", id="short-stub"),
            pytest.param("PASSAGE", id="uppercase"),
            pytest.param(" passage", id="whitespace-padded"),
            pytest.param(True, id="bool-true"),
            pytest.param(0, id="int-zero"),
            pytest.param(["passage"], id="list-wrap"),
        ],
    )
    def test_validate_task_rejects_unknown_value(self, value: object) -> None:
        # Act / Assert
        with pytest.raises(ValueError, match="Unknown embedding task"):
            validate_task(value)

    def test_validate_task_error_message_includes_repr(self) -> None:
        # Arrange
        bad_value = ["passage"]

        # Act / Assert
        with pytest.raises(ValueError) as exc_info:
            validate_task(bad_value)
        # repr distinguishes list-wrap from the literal "passage"
        assert repr(bad_value) in str(exc_info.value)


# ---------------------------------------------------------------------------
# EmbeddingProvider protocol — signature shape
# ---------------------------------------------------------------------------


class TestProtocolSignatures:
    @pytest.mark.parametrize(
        "method_name",
        ["embed", "embed_single", "embed_batch", "embed_streaming"],
    )
    def test_embed_methods_accept_optional_task(self, method_name: str) -> None:
        # Arrange
        method = getattr(EmbeddingProvider, method_name)
        sig = inspect.signature(method)

        # Assert
        assert "task" in sig.parameters, (
            f"{method_name} signature missing 'task' parameter: {sig}"
        )
        task_param = sig.parameters["task"]
        assert task_param.default is None, (
            f"{method_name} 'task' parameter must default to None, got {task_param.default!r}"
        )

    @pytest.mark.parametrize(
        "method_name",
        ["embed", "embed_single", "embed_batch", "embed_streaming"],
    )
    def test_task_is_trailing_parameter(self, method_name: str) -> None:
        # Arrange — task MUST be appended at the end of every signature so
        # positional calls don't shift. Convention locked in ch-agj Key
        # Considerations ("Signature ordering convention").
        method = getattr(EmbeddingProvider, method_name)
        sig = inspect.signature(method)
        params = [name for name in sig.parameters if name != "self"]

        # Assert
        assert params[-1] == "task", (
            f"{method_name}: 'task' must be the last parameter, got order {params}"
        )
