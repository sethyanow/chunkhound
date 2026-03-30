"""Test utilities shared across test files."""

from chunkhound.core.config import EmbeddingConfig
from chunkhound.core.config.embedding_factory import EmbeddingProviderFactory
from chunkhound.embeddings import EmbeddingManager

import pytest

pytestmark = pytest.mark.unit



# Hardcoded fake config for all tests — never reads real credentials.
# Uses "openai" as provider to pass EmbeddingConfig Pydantic validation
# (Literal["openai", "voyageai"]), but no real API calls are ever made.
_FAKE_EMBEDDING_CONFIG: dict = {
    "api_key": "fake-test-key",
    "provider": "openai",
    "model": "fake-embeddings",
    "base_url": "http://fake.test/v1",
    "rerank_model": "fake-rerank",
    "rerank_url": "http://fake.test/rerank",
    "rerank_format": "auto",
    "rerank_batch_size": 10,
}


def get_api_key_for_tests() -> tuple[str | None, str | None]:
    """Return hardcoded fake API key and provider for testing.

    Returns:
        Tuple of (api_key, provider) — always fake, never real credentials.
    """
    return _FAKE_EMBEDDING_CONFIG["api_key"], _FAKE_EMBEDDING_CONFIG["provider"]


def get_embedding_config_for_tests() -> dict:
    """Return hardcoded fake embedding configuration for testing.

    Returns a complete config dict with all fields any caller might access.
    Never reads environment variables, .chunkhound.json, or any external source.

    Returns:
        Dictionary with fake embedding config fields.
    """
    return dict(_FAKE_EMBEDDING_CONFIG)


def build_embedding_config_from_dict(config_dict: dict | None) -> dict | None:
    """
    Build embedding config dict suitable for Config() from discovered config.

    This helper eliminates code duplication across test files by centralizing
    the logic for propagating optional fields from discovered config to the
    embedding config dict expected by Config().

    Args:
        config_dict: Output from get_embedding_config_for_tests()

    Returns:
        Embedding config dict or None if no config provided

    Example:
        config_dict = get_embedding_config_for_tests()
        embedding_config = build_embedding_config_from_dict(config_dict)
        config = Config(
            database={"path": str(db_path), "provider": "duckdb"},
            embedding=embedding_config
        )
    """
    if not config_dict:
        return None

    embedding_config = {
        "provider": config_dict.get("provider", "openai"),
        "api_key": config_dict["api_key"],
    }

    # Optional fields - propagate only if present
    optional_fields = [
        "model", "base_url",
        "rerank_model", "rerank_url", "rerank_format", "rerank_batch_size"
    ]
    for field in optional_fields:
        if field in config_dict:
            embedding_config[field] = config_dict[field]

    return embedding_config


def create_embedding_manager_for_tests(config_dict: dict | None) -> EmbeddingManager | None:
    """
    Create EmbeddingManager from discovered config using production factory.

    Aligns test provider creation with production path, ensuring all fields
    (rerank settings, performance tuning) flow through correctly.

    Args:
        config_dict: Output from get_embedding_config_for_tests()

    Returns:
        Configured EmbeddingManager or None if no config provided,
        dependencies unavailable, or configuration invalid

    Example:
        config_dict = get_embedding_config_for_tests()
        embedding_manager = create_embedding_manager_for_tests(config_dict)
    """
    if not config_dict:
        return None

    try:
        # Create validated config using production EmbeddingConfig
        config = EmbeddingConfig(**config_dict)

        # Use production factory to create provider (handles all fields)
        provider = EmbeddingProviderFactory.create_provider(config)

        # Register with EmbeddingManager
        # Note: Protocol types from different modules are structurally equivalent
        manager = EmbeddingManager()
        manager.register_provider(provider, set_default=True)  # type: ignore[arg-type]

        return manager
    except (ImportError, ValueError):
        # Dependencies not installed or config invalid - return None so tests skip
        return None
