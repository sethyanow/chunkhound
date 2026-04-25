"""Unit tests for embedding configuration schema and provider factory
covering the TEI/jina additions (ch-agj Group D Cycle C).

Cycle C RED-1 — Pydantic schema:
  - EmbeddingConfig accepts provider="tei" with dims+base_url+model
  - rejects tei without base_url, with a clear message
  - rejects tei without dims, with a clear message
  - reports BOTH missing fields in a single ValidationError when neither
    is supplied
  - cross-product over [openai, voyageai, tei] for is_provider_configured /
    get_missing_config / get_default_model / get_provider_config — forces
    each if/elif chain to be exercised explicitly

Cycle C RED-2 (extended below in this file) — Factory + registry.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from chunkhound.core.config.embedding_config import EmbeddingConfig

pytestmark = pytest.mark.unit


# Reusable construction kwargs per provider — drives cross-product tests.
_TEI_KWARGS = {
    "provider": "tei",
    "model": "jinaai/jina-embeddings-v3",
    "base_url": "http://localhost:8080/v1",
    "dims": 1024,
}


# ---------------------------------------------------------------------------
# 1. Schema accepts provider="tei"
# ---------------------------------------------------------------------------


class TestSchemaAcceptsTei:
    def test_accepts_tei_with_required_fields(self) -> None:
        # Act / Assert — no ValidationError
        config = EmbeddingConfig(**_TEI_KWARGS)

        # Assert
        assert config.provider == "tei"
        assert config.dims == 1024
        assert config.base_url == "http://localhost:8080/v1"
        assert config.model == "jinaai/jina-embeddings-v3"

    def test_dims_field_exists(self) -> None:
        """dims must be a real Pydantic field, not just an extra ignored kwarg.
        If extra='ignore' silently swallows it, this test catches the regression.
        """
        config = EmbeddingConfig(**_TEI_KWARGS)
        assert hasattr(config, "dims")
        assert config.dims == 1024


# ---------------------------------------------------------------------------
# 2. Schema rejects tei missing required fields
# ---------------------------------------------------------------------------


class TestSchemaRequiresFieldsForTei:
    def test_rejects_tei_without_base_url(self) -> None:
        kwargs = {**_TEI_KWARGS}
        kwargs.pop("base_url")

        with pytest.raises(ValidationError) as exc_info:
            EmbeddingConfig(**kwargs)

        # The error message must mention base_url so the user knows what to fix.
        assert "base_url" in str(exc_info.value).lower()

    def test_rejects_tei_without_dims(self) -> None:
        kwargs = {**_TEI_KWARGS}
        kwargs.pop("dims")

        with pytest.raises(ValidationError) as exc_info:
            EmbeddingConfig(**kwargs)

        assert "dims" in str(exc_info.value).lower()

    def test_single_validation_error_names_both_missing_fields(self) -> None:
        """When BOTH base_url and dims are missing, the single ValidationError
        must mention both — agents debugging config shouldn't have to
        round-trip twice. List-of-errors pattern in the validator.
        """
        kwargs = {**_TEI_KWARGS}
        kwargs.pop("base_url")
        kwargs.pop("dims")

        with pytest.raises(ValidationError) as exc_info:
            EmbeddingConfig(**kwargs)

        message = str(exc_info.value).lower()
        assert "base_url" in message, (
            f"validator must name base_url; got: {message}"
        )
        assert "dims" in message, (
            f"validator must name dims (in same error); got: {message}"
        )


# ---------------------------------------------------------------------------
# 3. Cross-product: each provider exercises every if/elif chain
# ---------------------------------------------------------------------------


def _valid_kwargs_for(provider: str) -> dict:
    """Return minimal valid construction kwargs for the given provider."""
    if provider == "openai":
        return {"provider": "openai", "api_key": "sk-test"}
    if provider == "voyageai":
        return {"provider": "voyageai", "api_key": "vk-test"}
    if provider == "tei":
        return dict(_TEI_KWARGS)
    raise AssertionError(f"unhandled provider: {provider}")


class TestProviderCrossProduct:
    @pytest.mark.parametrize(
        "provider", ["openai", "voyageai", "tei"]
    )
    def test_is_provider_configured_returns_true_for_valid_config(
        self, provider: str
    ) -> None:
        config = EmbeddingConfig(**_valid_kwargs_for(provider))
        assert config.is_provider_configured() is True, (
            f"valid {provider} config should report configured; got "
            f"missing={config.get_missing_config()}"
        )

    @pytest.mark.parametrize(
        "provider", ["openai", "voyageai", "tei"]
    )
    def test_get_missing_config_empty_for_valid_config(
        self, provider: str
    ) -> None:
        config = EmbeddingConfig(**_valid_kwargs_for(provider))
        assert config.get_missing_config() == [], (
            f"valid {provider} config should have no missing fields"
        )

    def test_tei_get_provider_config_includes_dims(self) -> None:
        config = EmbeddingConfig(**_TEI_KWARGS)
        provider_config = config.get_provider_config()
        assert provider_config.get("dims") == 1024, (
            f"TEI provider config must include dims; got {provider_config}"
        )

    @pytest.mark.parametrize(
        "provider,expected_default_present",
        [
            pytest.param("openai", True, id="openai-has-default"),
            pytest.param("voyageai", True, id="voyageai-has-default"),
        ],
    )
    def test_get_default_model_returns_string_for_built_in_providers(
        self, provider: str, expected_default_present: bool
    ) -> None:
        # Construct without explicit model — provider default must populate.
        kwargs = _valid_kwargs_for(provider)
        kwargs.pop("model", None)  # ensure no explicit model
        config = EmbeddingConfig(**kwargs)
        default = config.get_default_model()
        assert isinstance(default, str) and default, (
            f"{provider} should have a default model; got {default!r}"
        )

    def test_tei_get_default_model_raises_when_model_unset(self) -> None:
        """TEI has no canonical default model — user must set one. Calling
        get_default_model() with model=None must raise so misconfiguration
        fails loudly, not silently fall through to an OpenAI default.
        """
        kwargs = dict(_TEI_KWARGS)
        kwargs.pop("model")
        # Pydantic accepts model=None at construction; the raise happens at
        # get_default_model(). If TEI is missing other fields, build with
        # them present so the only thing being tested is the model fallback.
        config = EmbeddingConfig(**kwargs)
        with pytest.raises(ValueError, match="(?i)tei"):
            config.get_default_model()


# ===========================================================================
# 4. Factory dispatch + dependency validation (Cycle C RED-2)
# ===========================================================================


from chunkhound.core.config.embedding_factory import EmbeddingProviderFactory
from chunkhound.providers.embeddings.tei_provider import TEIEmbeddingProvider


class TestFactoryCreatesTei:
    def test_factory_returns_tei_embedding_provider_for_tei_config(self) -> None:
        # Arrange
        config = EmbeddingConfig(**_TEI_KWARGS)

        # Act
        provider = EmbeddingProviderFactory.create_provider(config)

        # Assert
        assert isinstance(provider, TEIEmbeddingProvider), (
            f"factory must dispatch tei → TEIEmbeddingProvider; "
            f"got {type(provider).__name__}"
        )
        assert provider.dims == 1024
        assert provider.name == "tei"

    def test_get_supported_providers_includes_tei(self) -> None:
        supported = EmbeddingProviderFactory.get_supported_providers()
        assert "tei" in supported, (
            f"get_supported_providers must list tei; got {supported}"
        )


class TestValidateProviderDependencies:
    def test_returns_true_for_tei_when_openai_available(self) -> None:
        # Real environment — openai is installed (test will skip in env without it,
        # but we're not in such env).
        available, error = EmbeddingProviderFactory.validate_provider_dependencies(
            "tei"
        )
        assert available is True, (
            f"tei should be available when openai is importable; got error={error!r}"
        )
        assert error is None

    def test_returns_false_for_tei_when_openai_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Simulate openai SDK not being installed by poisoning sys.modules.
        TEI subclasses OpenAIEmbeddingProvider, so it transitively requires
        the openai package — validate_provider_dependencies must reflect
        this so misconfigured environments fail loudly at config-load time
        rather than at provider-construction time.
        """
        import sys

        # Remove cached chunkhound modules so the dep-check re-imports fresh.
        # Setting openai to None makes Python's import machinery treat it as failed.
        monkeypatch.setitem(sys.modules, "openai", None)
        # Also evict the TEI provider's import cache so it tries openai again.
        monkeypatch.delitem(
            sys.modules,
            "chunkhound.providers.embeddings.tei_provider",
            raising=False,
        )
        monkeypatch.delitem(
            sys.modules,
            "chunkhound.providers.embeddings.openai_provider",
            raising=False,
        )

        available, error = EmbeddingProviderFactory.validate_provider_dependencies(
            "tei"
        )

        assert available is False, (
            "tei dep-check must fail when openai is missing"
        )
        assert error is not None and "tei" in error.lower(), (
            f"error message must reference tei; got {error!r}"
        )


# ===========================================================================
# 5. Registry function in chunkhound.embeddings
# ===========================================================================


class TestCreateTeiProviderRegistryFunction:
    def test_returns_tei_provider_for_valid_args(self) -> None:
        # Arrange / Act — import lazily so the test surfaces ImportError on
        # the re-export, not at module load time.
        from chunkhound.embeddings import create_tei_provider

        provider = create_tei_provider(
            base_url="http://localhost:8080/v1",
            model="jinaai/jina-embeddings-v3",
            dims=1024,
        )

        # Assert
        assert isinstance(provider, TEIEmbeddingProvider)
        assert provider.dims == 1024

    def test_rejects_none_base_url(self) -> None:
        from chunkhound.embeddings import create_tei_provider

        with pytest.raises(ValueError, match="(?i)base_url"):
            create_tei_provider(
                base_url=None,  # type: ignore[arg-type]
                model="jinaai/jina-embeddings-v3",
                dims=1024,
            )

    def test_rejects_invalid_base_url_scheme(self) -> None:
        from chunkhound.embeddings import create_tei_provider

        with pytest.raises(ValueError, match="(?i)base_url|http"):
            create_tei_provider(
                base_url="not-a-url",
                model="jinaai/jina-embeddings-v3",
                dims=1024,
            )

    @pytest.mark.parametrize(
        "bad_dims",
        [
            pytest.param(0, id="zero"),
            pytest.param(-1, id="negative"),
            pytest.param(None, id="none"),
        ],
    )
    def test_rejects_zero_or_negative_or_none_dims(self, bad_dims) -> None:
        from chunkhound.embeddings import create_tei_provider

        with pytest.raises(ValueError, match="dims"):
            create_tei_provider(
                base_url="http://localhost:8080/v1",
                model="jinaai/jina-embeddings-v3",
                dims=bad_dims,
            )

    def test_chunkhound_embeddings_re_exports_tei_class(self) -> None:
        """Smoke test: TEIEmbeddingProvider is reachable from chunkhound.embeddings.

        Limitation: by the time this unit test runs, sibling chunkhound
        modules are already loaded, so a circular-import bug between
        chunkhound.embeddings and chunkhound.providers.* would NOT trip
        here. The cold-import case is exercised by the integration tests
        that spawn `chunkhound index` via subprocess. Both layers needed.
        """
        import chunkhound.embeddings

        assert hasattr(chunkhound.embeddings, "TEIEmbeddingProvider"), (
            "TEIEmbeddingProvider must be re-exported from chunkhound.embeddings"
        )
        # Identity check — the re-export must be the same class
        assert chunkhound.embeddings.TEIEmbeddingProvider is TEIEmbeddingProvider

    def test_chunkhound_embeddings_re_exports_create_tei_provider_factory(
        self,
    ) -> None:
        import chunkhound.embeddings

        assert hasattr(chunkhound.embeddings, "create_tei_provider")
        assert callable(chunkhound.embeddings.create_tei_provider)
