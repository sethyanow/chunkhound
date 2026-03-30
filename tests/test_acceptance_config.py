"""Tests for the acceptance_embedding_config fixture.

Verifies that the fixture provides real API URLs for cassette matching
while allowing env var overrides for recording.
"""

import pytest

pytestmark = pytest.mark.unit


def test_acceptance_config_defaults_to_real_openai_url(acceptance_embedding_config):
    """Without env vars, fixture returns real OpenAI URL for cassette matching."""
    assert acceptance_embedding_config["base_url"] == "https://api.openai.com/v1"
    assert acceptance_embedding_config["model"] == "text-embedding-3-small"
    assert acceptance_embedding_config["provider"] == "openai"


def test_acceptance_config_has_placeholder_key_by_default(acceptance_embedding_config):
    """Without env vars, api_key is a non-empty placeholder (VCR intercepts before auth)."""
    key = acceptance_embedding_config["api_key"]
    assert key is not None
    assert len(key) > 0
    assert "fake" not in key.lower() or "vcr" in key.lower()  # distinguishable from test_utils fake


def test_acceptance_config_env_var_override(monkeypatch, acceptance_embedding_config_factory):
    """Env vars override all config fields."""
    monkeypatch.setenv("CHUNKHOUND_EMBEDDING__API_KEY", "sk-real-key-123")
    monkeypatch.setenv("CHUNKHOUND_EMBEDDING__PROVIDER", "voyageai")
    monkeypatch.setenv("CHUNKHOUND_EMBEDDING__MODEL", "voyage-3")
    monkeypatch.setenv("CHUNKHOUND_EMBEDDING__BASE_URL", "https://api.voyageai.com/v1")

    config = acceptance_embedding_config_factory()
    assert config["api_key"] == "sk-real-key-123"
    assert config["provider"] == "voyageai"
    assert config["model"] == "voyage-3"
    assert config["base_url"] == "https://api.voyageai.com/v1"


def test_acceptance_config_partial_env_override(monkeypatch, acceptance_embedding_config_factory):
    """Setting only API key keeps other defaults."""
    monkeypatch.setenv("CHUNKHOUND_EMBEDDING__API_KEY", "sk-just-key")

    config = acceptance_embedding_config_factory()
    assert config["api_key"] == "sk-just-key"
    assert config["base_url"] == "https://api.openai.com/v1"  # default preserved
    assert config["model"] == "text-embedding-3-small"  # default preserved


def test_acceptance_config_compatible_with_build_embedding_config(acceptance_embedding_config):
    """Config dict is compatible with build_embedding_config_from_dict()."""
    from tests.test_utils import build_embedding_config_from_dict
    result = build_embedding_config_from_dict(acceptance_embedding_config)
    assert result is not None
    assert "provider" in result
    assert "api_key" in result


def test_vcr_config_filters_auth_headers(vcr_config):
    """vcr_config fixture filters sensitive auth headers from cassettes."""
    assert "filter_headers" in vcr_config
    headers = vcr_config["filter_headers"]
    assert "authorization" in headers
    assert "x-api-key" in headers


def test_vcr_config_sets_cassette_directory(vcr_config):
    """vcr_config points cassettes to tests/cassettes/."""
    assert "cassette_library_dir" in vcr_config
    assert vcr_config["cassette_library_dir"].endswith("tests/cassettes")
