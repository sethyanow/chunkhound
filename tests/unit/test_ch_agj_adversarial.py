"""Adversarial stress tests for ch-agj Groups A-D (Checkpoints 1+2).

Probes assumptions baked into:
  - validate_task validator (chunkhound/interfaces/embedding_provider.py)
  - VoyageAIEmbeddingProvider task->input_type mapping
  - OpenAIEmbeddingProvider _embed_batch_with_extras (extra_body forwarding,
    lambda closure over task)
  - TEIEmbeddingProvider (dims override, _task_to_extra_body)
  - Pydantic EmbeddingConfig schema widening (provider Literal, dims field,
    validate_tei_required_fields)
  - Factory + registry (validate_provider_dependencies, create_tei_provider,
    PEP 562 __getattr__)

Each test is its own RED-GREEN cycle. GREEN results get the Three-Question
Framework via inline comments. Findings outside Group D scope get logged
on the epic via `bn log`.

Pattern legend:
  EM = Empty, SG = Singular, SR = Self-referential, RD = Redundant,
  EB = Encoding boundaries, TB = Type boundaries, ST = State transitions,
  SH = Semantically hostile, 2R = Second-run.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from chunkhound.core.config.embedding_config import EmbeddingConfig
from chunkhound.core.config.embedding_factory import EmbeddingProviderFactory
from chunkhound.interfaces.embedding_provider import validate_task
from chunkhound.providers.embeddings.openai_provider import OpenAIEmbeddingProvider
from chunkhound.providers.embeddings.tei_provider import TEIEmbeddingProvider

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers (same shape used in cycle-A/B/C tests for consistency)
# ---------------------------------------------------------------------------


def _make_openai(**kwargs) -> OpenAIEmbeddingProvider:
    defaults: dict = {"api_key": "test-key", "model": "text-embedding-3-small"}
    defaults.update(kwargs)
    provider = OpenAIEmbeddingProvider(**defaults)
    provider._client = MagicMock()
    provider._client_initialized = True
    return provider


def _make_tei(**kwargs) -> TEIEmbeddingProvider:
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


def _stub_embedding_response(dims: int = 1536, count: int = 1) -> MagicMock:
    response = MagicMock()
    response.data = [
        MagicMock(index=i, embedding=[0.1] * dims) for i in range(count)
    ]
    response.usage = MagicMock(total_tokens=count * 5)
    return response


# ---------------------------------------------------------------------------
# A1. validate_task — encoding boundary: bytes input
# ---------------------------------------------------------------------------


class TestValidateTaskBytes:
    """[EB] bytes literal looks like a string but is a different type.

    Hypothesis: validate_task should reject b'passage' — bytes objects don't
    match the Literal["passage", "query"] structurally, and the error
    message must show repr(bytes) so debugging differentiates from str.
    """

    def test_rejects_bytes_passage(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="Unknown embedding task"):
            validate_task(b"passage")

    def test_rejects_bytes_query(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding task"):
            validate_task(b"query")

    def test_error_message_distinguishes_bytes_from_str(self) -> None:
        """The error repr must show b'passage' (with the b prefix), not
        'passage' — so a debugger can tell encoding mistakes from typos.
        """
        with pytest.raises(ValueError) as exc_info:
            validate_task(b"passage")
        message = str(exc_info.value)
        assert "b'passage'" in message or 'b"passage"' in message, (
            f"error must repr bytes with b prefix; got: {message}"
        )


# ---------------------------------------------------------------------------
# A2. validate_task — encoding boundary: unicode confusable
# ---------------------------------------------------------------------------


class TestValidateTaskUnicodeConfusable:
    """[EB] Cyrillic 'а' (U+0430) renders identically to Latin 'a' (U+0061)
    but is a different code point. validate_task uses == comparison which
    is byte-level — confusables must be rejected.
    """

    def test_rejects_cyrillic_a_passage(self) -> None:
        # 'pаssage' with cyrillic а
        confusable = "p" + "а" + "ssage"
        with pytest.raises(ValueError, match="Unknown embedding task"):
            validate_task(confusable)


# ---------------------------------------------------------------------------
# B1. OpenAI _embed_batch_with_extras — extra_body={} is NOT None
# ---------------------------------------------------------------------------


class TestOpenAIEmptyExtraBodyForwarded:
    """[TB][EM] The conditional 'if extra_body is not None' must
    distinguish None (omit kwarg) from {} (pass kwarg with empty dict).

    Why this matters: a future caller might pass extra_body={} to mean
    'no extras but explicit acknowledgment.' If our conditional treats
    {} as None and omits the kwarg, the caller's intent is silently
    erased. Validates the contract is is-not-None, not truthiness.
    """

    async def test_empty_dict_extra_body_is_forwarded_not_omitted(
        self,
    ) -> None:
        # Arrange
        provider = _make_openai()
        create_mock = AsyncMock(return_value=_stub_embedding_response(count=1))
        provider._client.embeddings.create = create_mock

        # Act — call _embed_batch_with_extras directly with empty dict
        await provider._embed_batch_with_extras(["hi"], extra_body={})

        # Assert — extra_body={} must be in the SDK call kwargs
        create_mock.assert_called_once()
        sent_kwargs = create_mock.call_args.kwargs
        assert "extra_body" in sent_kwargs, (
            f"extra_body={{}} (empty dict) must be forwarded, not omitted; "
            f"got kwargs {set(sent_kwargs)}"
        )
        assert sent_kwargs["extra_body"] == {}


# ---------------------------------------------------------------------------
# B2. OpenAI _embed_batch_with_extras — extra_body with unknown shape
# ---------------------------------------------------------------------------


class TestOpenAIArbitraryExtraBodyShape:
    """[SH] The method's contract is to forward extra_body verbatim.
    Validates we don't accidentally validate / filter / mutate the dict.
    """

    async def test_arbitrary_keys_forwarded_unchanged(self) -> None:
        provider = _make_openai()
        create_mock = AsyncMock(return_value=_stub_embedding_response(count=1))
        provider._client.embeddings.create = create_mock

        # Hostile shape: keys we don't recognize, mixed value types
        weird_extras = {
            "task": "retrieval.passage",
            "user": "test",
            "nested": {"deep": [1, 2, 3]},
        }
        await provider._embed_batch_with_extras(["hi"], extra_body=weird_extras)

        sent_kwargs = create_mock.call_args.kwargs
        # Identity check (same dict object) — no copy/mutation
        assert sent_kwargs["extra_body"] is weird_extras, (
            "extra_body must be forwarded by reference, not copied — caller "
            "should be able to mutate before next call without surprise"
        )


# ---------------------------------------------------------------------------
# C1. TEI dims — extreme type boundary values
# ---------------------------------------------------------------------------


class TestTeiDimsTypeBoundaries:
    """[TB] Validates that __init__ accepts the legitimate full int range
    (Python has no int overflow) but still rejects non-positive values.
    """

    def test_accepts_minimum_positive_int(self) -> None:
        # Arrange / Act / Assert — no exception
        provider = _make_tei(dims=1)
        assert provider.dims == 1

    def test_accepts_very_large_int(self) -> None:
        """No upper bound on dims — Python int is unbounded. Any future
        artificial cap should be added with explicit rationale.
        """
        big = 2**31 - 1  # max 32-bit signed; Python int handles fine
        provider = _make_tei(dims=big)
        assert provider.dims == big


# ---------------------------------------------------------------------------
# C2. TEI _task_to_extra_body — same input twice returns equal dicts
# ---------------------------------------------------------------------------


class TestTeiTaskToExtraBodyAliasing:
    """[2R][SR] If the static helper returned a module-level constant, two
    calls would return the SAME dict object — caller mutating one would
    poison the other. The helper must return fresh dicts.
    """

    def test_two_calls_return_distinct_dict_objects(self) -> None:
        # Arrange / Act
        a = TEIEmbeddingProvider._task_to_extra_body("passage")
        b = TEIEmbeddingProvider._task_to_extra_body("passage")

        # Assert — equal but not identical (no shared mutable state)
        assert a == b
        assert a is not b, (
            "_task_to_extra_body must return a fresh dict; sharing the "
            "object across calls means caller-mutation poisons future calls"
        )

    def test_caller_mutation_does_not_affect_subsequent_calls(self) -> None:
        # Arrange / Act
        first = TEIEmbeddingProvider._task_to_extra_body("query")
        first["task"] = "retrieval.passage"  # caller mutates
        second = TEIEmbeddingProvider._task_to_extra_body("query")

        # Assert — second call returns the original mapping
        assert second == {"task": "retrieval.query"}, (
            f"caller mutation must not affect subsequent calls; got {second}"
        )


# ---------------------------------------------------------------------------
# D1. Pydantic — provider Literal case sensitivity
# ---------------------------------------------------------------------------


class TestPydanticProviderCaseSensitive:
    """[SH] Pydantic Literal validators are case-sensitive. Validates that
    user typos like 'TEI' (uppercase) or ' tei' (leading space) fail
    loudly, not silently fall through.
    """

    @pytest.mark.parametrize(
        "bad",
        [
            pytest.param("TEI", id="all-caps"),
            pytest.param("Tei", id="title-case"),
            pytest.param(" tei", id="leading-space"),
            pytest.param("tei ", id="trailing-space"),
            pytest.param("tei\n", id="trailing-newline"),
        ],
    )
    def test_rejects_case_or_whitespace_variants(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            EmbeddingConfig(
                provider=bad,  # type: ignore[arg-type]
                model="jinaai/jina-embeddings-v3",
                base_url="http://localhost:8080/v1",
                dims=1024,
            )


# ---------------------------------------------------------------------------
# D2. Pydantic — post-construction mutation
# ---------------------------------------------------------------------------


class TestPydanticPostConstructionMutation:
    """[ST] Pydantic v2 BaseSettings is mutable by default — model_validator
    runs at __init__ but NOT on attribute assignment. Validates whether
    a TEI config can be 'broken' after construction by setting base_url
    or dims to None.

    Expected: this test exposes a latent footgun. The fix (frozen=True)
    is OUT OF SCOPE — it would break other config-mutation paths in
    ChunkHound. We document the exposure and rely on callers not
    mutating EmbeddingConfig.
    """

    def test_mutating_base_url_to_none_after_construction_is_silently_allowed(
        self,
    ) -> None:
        """Documents the expose: post-construction mutation bypasses
        validate_tei_required_fields. is_provider_configured() reflects
        the new state correctly (returns False when base_url is None),
        so downstream code DOES detect the breakage — but a user might
        be surprised that the validator didn't re-fire.

        Three-Question Framework on this GREEN-but-surprising:
          Q1: Pydantic v2 BaseSettings defaults to mutable models. The
              validator is a one-shot at construction.
          Q2: All EmbeddingConfig consumers in the repo (factory, CLI,
              services) consume the config without mutation post-load.
              Search: `grep -rn "config\\.provider = \\|config\\.dims = \\|config\\.base_url = "`
              should show zero hits.
          Q3: The boundary that matters is config-construction (Pydantic
              validation). Once it crosses, downstream code should treat
              the config as immutable in spirit even though Python lets
              it mutate.

        Action: documented; no fix in this task. If we want this hardened,
        a follow-up bones task should add `model_config = SettingsConfigDict(
        ..., frozen=True)` + audit any callers that mutate config.
        """
        # Arrange
        config = EmbeddingConfig(
            provider="tei",
            model="jinaai/jina-embeddings-v3",
            base_url="http://localhost:8080/v1",
            dims=1024,
        )
        assert config.is_provider_configured() is True

        # Act — mutate post-construction
        config.base_url = None  # silently allowed in Pydantic v2 default mode

        # Assert — is_provider_configured catches the breakage
        # (so downstream code DOES detect, even if the validator didn't re-fire)
        assert config.is_provider_configured() is False, (
            "is_provider_configured must reflect post-mutation state, "
            "even though the @model_validator does not re-run on assignment"
        )


# ---------------------------------------------------------------------------
# E1. Factory validate_provider_dependencies — second-run idempotent
# ---------------------------------------------------------------------------


class TestValidateProviderDependenciesIdempotent:
    """[2R] Calling twice must return the same result without side effects.
    Module-level imports inside validate_provider_dependencies are cached
    by sys.modules, so the second call is essentially free.
    """

    def test_two_calls_return_same_result(self) -> None:
        a = EmbeddingProviderFactory.validate_provider_dependencies("tei")
        b = EmbeddingProviderFactory.validate_provider_dependencies("tei")
        assert a == b, (
            f"validate_provider_dependencies must be idempotent; "
            f"first={a!r}, second={b!r}"
        )


# ---------------------------------------------------------------------------
# E2. PEP 562 __getattr__ — repeated resolution stable
# ---------------------------------------------------------------------------


class TestEmbeddingsGetattrRepeated:
    """[2R] Each attribute access invokes __getattr__ unless the result is
    bound back to the module. Validates that repeated access returns the
    same class object — stability is essential because callers may store
    the reference long-term.
    """

    def test_repeated_access_returns_same_class_object(self) -> None:
        import chunkhound.embeddings

        first = chunkhound.embeddings.TEIEmbeddingProvider
        second = chunkhound.embeddings.TEIEmbeddingProvider
        assert first is second, (
            "TEIEmbeddingProvider re-export must return the same class "
            "object across calls (sys.modules cache makes this cheap)"
        )

    def test_attribute_error_for_unknown_name(self) -> None:
        """__getattr__ must raise AttributeError for names it doesn't
        handle, not silently return None or fall through to a wrong import.
        """
        import chunkhound.embeddings

        with pytest.raises(AttributeError, match="not_a_real_export"):
            chunkhound.embeddings.not_a_real_export  # noqa: B018


# ---------------------------------------------------------------------------
# F1. TEI __init__ — empty model name
# ---------------------------------------------------------------------------


class TestTeiEmptyModelName:
    """[EM] Empty string for model — does TEI accept it? OpenAIEmbeddingProvider
    parent doesn't validate model emptiness. Behavior is downstream-dependent
    (TEI server would 404). We check what TEI does at construction time.
    """

    def test_empty_model_name_is_accepted_at_construction(self) -> None:
        """Documents the surface: empty model is constructable. Failure
        would happen at first API call to TEI server.

        Three-Question Framework:
          Q1: TEI inherits parent's permissive model handling.
          Q2: VoyageAIEmbeddingProvider also doesn't validate model
              emptiness at __init__.
          Q3: Boundary that catches this: TEI server returns 404 or
              rejects the embedding call. Failure is loud at runtime
              but not at config-load.

        Action: minor exposure. Pydantic-side validation could be added
        to validate_tei_required_fields ('model must be non-empty') —
        currently relies on Pydantic's `model: str | None = ...` which
        accepts "". Logging on epic.
        """
        # Arrange / Act — no exception expected
        provider = _make_tei(model="")
        # Assert — provider stored the empty model verbatim
        assert provider.model == ""
